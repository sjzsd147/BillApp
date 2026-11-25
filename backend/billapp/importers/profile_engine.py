"""Declarative Bank Profile Engine (YAML Schema, Signed/Split/Flag Modes, ProfileImporter)."""

from __future__ import annotations

import csv
from html.parser import HTMLParser
import io
import re
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple, Union

import openpyxl
from pydantic import BaseModel, Field, field_validator, model_validator
import yaml

from billapp.core.models import (
    AccountCreate,
    AccountRef,
    ParseResult,
    SelfCheckResult,
    TransactionCreate,
)
from billapp.core.money import to_cents, to_display
from billapp.core.safe_regex import (
    DEFAULT_REGEX_TIMEOUT,
    PROFILE_MAX_PATTERN_LEN,
    compile_safe_regex,
    safe_search,
    validate_regex_pattern,
)
from billapp.dedup.fingerprint import assign_fingerprints
from billapp.importers.base import FileProbe, Importer
from billapp.importers.common import (
    detect_encoding,
    extract_channel_and_normalize,
    normalize_currency,
    parse_datetime,
    sanitize_order_id,
)

AmountModeType = Literal["signed", "split", "flag"]
AccountKindType = Literal["debit", "credit", "wallet"]
FileType = Literal["csv", "xlsx", "pdf", "auto"]
MAX_PROFILE_EXCEL_ROWS = 100_000
MAX_PROFILE_EXCEL_COLS = 200


def _normalize_date_sep(raw: str) -> str:
    s = raw.strip().replace("/", "-").replace(".", "-")
    s = re.sub(
        r"(\d{4})年(\d{1,2})月(\d{1,2})日?",
        lambda m: f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}",
        s,
    )
    return s


def _normalize_currency_token(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    u = raw.strip().upper()
    if not u or "ALL" in u or "全币种" in raw:
        return None
    norm = normalize_currency(raw, default="")
    return norm or None


def extract_preamble_metadata_fallback(preamble_text: str) -> Dict[str, Optional[str]]:
    """Extract account tail, holder name, period, bank, and kind from statement top lines."""
    res: Dict[str, Optional[str]] = {
        "card_tail": None,
        "holder_name": None,
        "period_start": None,
        "period_end": None,
        "bank": None,
        "account_kind": None,
        "currency": None,
    }
    if not preamble_text or not preamble_text.strip():
        return res

    cleaned = preamble_text.replace("\xa0", " ")

    # 1. Card / account tail from labeled preamble lines (e.g. 账号：6214********1234)
    for line in cleaned.splitlines():
        m_acct = re.search(
            r"(?:账\s*号|卡\s*号|银行账号|银行卡号|交易账号|账户号码|Account\s*No\.?|Card\s*No\.?)\s*[：:]\s*([A-Za-z0-9*\- ]{4,40})",
            line,
            re.IGNORECASE,
        )
        if m_acct:
            cand = re.split(r"\s{2,}|\s+(?=[\u4e00-\u9fa5A-Za-z]{2,})", m_acct.group(1).strip(), maxsplit=1)[0]
            tails = re.findall(r"(\d{4})(?!\d)", cand)
            if tails:
                res["card_tail"] = tails[-1]
                break
    if not res["card_tail"]:
        m_tail = re.search(r"尾\s*号\s*[：:为]?\s*(\d{4})\b", cleaned)
        if m_tail:
            res["card_tail"] = m_tail.group(1)

    # 2. Holder name (kept local, never sent unmasked to LLM)
    m_holder = re.search(
        r"(?:户\s*名|客户姓名|账户名称|姓\s*名|持卡人)\s*[：:]\s*([^\r\n,，\t|｜]+)",
        cleaned,
    )
    if m_holder:
        val = m_holder.group(1).strip()
        val = re.split(
            r"\s+(?:账\s*号|卡\s*号|Account|Card|账户类型|开\s*户\s*行)\b",
            val,
            maxsplit=1,
        )[0].strip(" \t|｜")
        if val and val not in ("[已脱敏]", "[HOLDER]", "[USER]", "****"):
            res["holder_name"] = val

    # 3. Statement period range
    m_period = re.search(
        r"(\d{4}[-/.年]\d{1,2}[-/.月]\d{1,2}日?)\s*(?:--+|至|~|—+|–|-|to)\s*(\d{4}[-/.年]\d{1,2}[-/.月]\d{1,2}日?)",
        cleaned,
    )
    if m_period:
        res["period_start"] = _normalize_date_sep(m_period.group(1))
        res["period_end"] = _normalize_date_sep(m_period.group(2))

    # 4. Institution / bank name (generic non-counterparty pattern)
    for line in cleaned.splitlines():
        l = line.strip()
        if not l or "对方" in l:
            continue
        m_bank = re.search(r"([\u4e00-\u9fa5]{2,8}银行)", l)
        if m_bank:
            res["bank"] = m_bank.group(1)
            break
    if not res["bank"] and re.search(r"China\s+Merchants\s+Bank", cleaned, re.IGNORECASE):
        res["bank"] = "招商银行"

    # 5. Account kind & currency
    if re.search(r"信用卡|贷记卡|Credit\s*Card", cleaned, re.IGNORECASE):
        res["account_kind"] = "credit"
    elif re.search(r"借记卡|储蓄卡|Debit\s*Card", cleaned, re.IGNORECASE):
        res["account_kind"] = "debit"

    m_curr = re.search(r"(?:币\s*种|货币|币\s*别|Currency)\s*[：:]\s*([^\s,，\r\n]+)", cleaned, re.IGNORECASE)
    if m_curr:
        res["currency"] = _normalize_currency_token(m_curr.group(1))

    return res


class FieldMapping(BaseModel):
    """Mapping for a single target column."""

    header: Optional[Union[str, List[str]]] = None
    index: Optional[int] = None
    optional: bool = False
    default: Optional[str] = None


class ColumnMappingConfig(BaseModel):
    """Column mapping configuration mapping statement columns to canonical fields."""

    date: FieldMapping
    time: Optional[FieldMapping] = Field(default_factory=lambda: FieldMapping(optional=True))
    amount: Optional[FieldMapping] = None
    expense_amount: Optional[FieldMapping] = None
    income_amount: Optional[FieldMapping] = None
    direction: Optional[FieldMapping] = None
    counterparty: Optional[FieldMapping] = Field(default_factory=lambda: FieldMapping(optional=True))
    description: Optional[FieldMapping] = Field(default_factory=lambda: FieldMapping(optional=True))
    balance: Optional[FieldMapping] = Field(default_factory=lambda: FieldMapping(optional=True))
    currency: Optional[FieldMapping] = Field(default_factory=lambda: FieldMapping(optional=True, default="CNY"))
    card_tail: Optional[FieldMapping] = Field(default_factory=lambda: FieldMapping(optional=True))
    order_id: Optional[FieldMapping] = Field(default_factory=lambda: FieldMapping(optional=True))
    status: Optional[FieldMapping] = Field(default_factory=lambda: FieldMapping(optional=True))
    channel: Optional[FieldMapping] = Field(default_factory=lambda: FieldMapping(optional=True))

    @field_validator(
        "date", "time", "amount", "expense_amount", "income_amount", "direction",
        "counterparty", "description", "balance", "currency", "card_tail", "order_id", "status", "channel",
        mode="before",
    )
    @classmethod
    def coerce_field_mapping(cls, v: Any) -> Any:
        if v is None:
            return None
        if isinstance(v, (str, list)):
            return FieldMapping(header=v)
        if isinstance(v, int):
            return FieldMapping(index=v)
        return v


class AmountConfig(BaseModel):
    """Amount calculation mode specification."""

    type: AmountModeType
    negative_means: Literal["expense", "income"] = "expense"
    amount_col: Optional[Union[str, int]] = None
    expense_col: Optional[Union[str, int]] = None
    income_col: Optional[Union[str, int]] = None
    direction_col: Optional[Union[str, int]] = None
    flag_mapping: Dict[str, str] = Field(default_factory=dict)
    default_direction: str = "expense"

    @model_validator(mode="after")
    def validate_mode_fields(self) -> AmountConfig:
        if self.type == "signed":
            if not self.amount_col:
                self.amount_col = "交易金额"
        elif self.type == "split":
            if not self.expense_col:
                self.expense_col = "支出金额"
            if not self.income_col:
                self.income_col = "收入金额"
        elif self.type == "flag":
            if not self.amount_col:
                self.amount_col = "交易金额"
            if not self.direction_col:
                self.direction_col = "借贷"
            if not self.flag_mapping:
                self.flag_mapping = {
                    "借": "expense",
                    "支出": "expense",
                    "消费": "expense",
                    "贷": "income",
                    "收入": "income",
                    "还款": "transfer",
                }
        return self


class TableDetectionConfig(BaseModel):
    """Table boundary detection rules."""

    header_row_pattern: Optional[str] = None
    header_row_keywords: Optional[List[str]] = None
    header_row_index: Optional[int] = None
    data_start_offset: int = 1
    skip_footer_rows: int = 0
    skip_row_patterns: List[str] = Field(
        default_factory=lambda: [
            "^合计",
            "^小计",
            "^总计",
            "^本页合计",
            "^温馨提示",
            "^[-=_]{4,}$",
        ]
    )
    stop_pattern: Optional[str] = "^(合计|小计|总计|重要提示|注：)"

    @field_validator("header_row_pattern", "stop_pattern")
    @classmethod
    def validate_optional_regex(cls, v: Optional[str]) -> Optional[str]:
        if v is None or v == "":
            return v
        return validate_regex_pattern(v, max_len=PROFILE_MAX_PATTERN_LEN)

    @field_validator("skip_row_patterns")
    @classmethod
    def validate_skip_patterns(cls, patterns: List[str]) -> List[str]:
        for p in patterns:
            validate_regex_pattern(p, max_len=PROFILE_MAX_PATTERN_LEN)
        return patterns


class PreambleRegexRule(BaseModel):
    """Rule for extracting single metadata field via regex."""

    pattern: str
    group: int = 1
    default: Optional[str] = None

    @field_validator("pattern")
    @classmethod
    def validate_pattern(cls, v: str) -> str:
        return validate_regex_pattern(v, max_len=PROFILE_MAX_PATTERN_LEN)


class PeriodRegexRule(BaseModel):
    """Rule for extracting start and end dates of statement period."""

    pattern: str
    start_group: int = 1
    end_group: int = 2

    @field_validator("pattern")
    @classmethod
    def validate_pattern(cls, v: str) -> str:
        return validate_regex_pattern(v, max_len=PROFILE_MAX_PATTERN_LEN)


class MetadataExtractionConfig(BaseModel):
    """Preamble metadata regex extraction configuration."""

    card_tail: Optional[PreambleRegexRule] = None
    holder_name: Optional[PreambleRegexRule] = None
    period: Optional[PeriodRegexRule] = None
    currency: Optional[PreambleRegexRule] = None


class DateTimeConfig(BaseModel):
    """Date and time column parsing configuration."""

    date_col: Optional[Union[str, int]] = None
    time_col: Optional[Union[str, int]] = None
    date_format: Optional[str] = None
    time_format: Optional[str] = None
    combined: bool = False


class DirectionRulesConfig(BaseModel):
    """Semantic direction overrides based on counterparty and description text."""

    refund: Dict[str, List[str]] = Field(
        default_factory=lambda: {
            "keywords": ["退款", "退货", "冲正"],
            "regex": ["(?i)refund", "退款", "退货"],
        }
    )
    transfer: Dict[str, List[str]] = Field(
        default_factory=lambda: {
            "keywords": ["还款", "理财", "提现", "转账到零钱"],
            "regex": ["还款", "转账.*本人", "理财(买入|赎回)?", "提现"],
        }
    )

    @field_validator("refund", "transfer")
    @classmethod
    def validate_direction_regexes(cls, v: Dict[str, List[str]]) -> Dict[str, List[str]]:
        for rgx in v.get("regex", []) or []:
            validate_regex_pattern(rgx, max_len=PROFILE_MAX_PATTERN_LEN)
        return v


class BankProfile(BaseModel):
    """Declarative Bank Statement Profile Model."""

    id: str
    name: str
    bank: str
    account_kind: AccountKindType
    file_type: FileType = "auto"
    encoding: str = "auto"
    delimiter: str = ","
    currency: str = "CNY"
    version: int = 1
    auto_detect: bool = True
    description: Optional[str] = None

    @field_validator("currency", mode="before")
    @classmethod
    def normalize_profile_currency(cls, v: Any) -> str:
        if not v:
            return "CNY"
        return _normalize_currency_token(str(v)) or "CNY"

    table_detection: TableDetectionConfig = Field(default_factory=TableDetectionConfig)
    metadata_extraction: MetadataExtractionConfig = Field(default_factory=MetadataExtractionConfig)
    column_mapping: ColumnMappingConfig
    amount_mode: AmountConfig
    date_time: DateTimeConfig = Field(default_factory=DateTimeConfig)
    direction_rules: DirectionRulesConfig = Field(default_factory=DirectionRulesConfig)

    @property
    def account_kind_full(self) -> str:
        """Translate 'debit'/'credit'/'wallet' to database kind."""
        if self.account_kind == "credit":
            return "bank_credit"
        if self.account_kind == "wallet":
            return "wallet"
        return "bank_debit"

    @classmethod
    def from_yaml(cls, yaml_content: str) -> BankProfile:
        data = yaml.safe_load(yaml_content)
        if not isinstance(data, dict):
            raise ValueError("YAML content must decode to a dictionary")
        return cls(**data)

    @classmethod
    def from_yaml_file(cls, path: Union[str, Path]) -> BankProfile:
        content = Path(path).read_text(encoding="utf-8")
        return cls.from_yaml(content)

    def to_yaml(self) -> str:
        return yaml.safe_dump(self.model_dump(), allow_unicode=True, sort_keys=False)


def get_builtin_profiles_dir() -> Path:
    """Return the directory containing built-in YAML bank profiles."""
    return Path(__file__).resolve().parent / "profiles"


def load_builtin_profiles() -> Dict[str, BankProfile]:
    """Scan and load all built-in YAML profiles from the profiles directory."""
    profiles: Dict[str, BankProfile] = {}
    p_dir = get_builtin_profiles_dir()
    if p_dir.exists():
        for p_path in p_dir.glob("*.yaml"):
            try:
                prof = BankProfile.from_yaml_file(p_path)
                profiles[prof.id] = prof
            except Exception:
                continue
    return profiles


class ProfileImporter(Importer):
    """Dynamic statement importer driven by a declarative BankProfile specification."""

    def __init__(self, profile: BankProfile) -> None:
        self.profile = profile
        self.source_id = f"profile:{profile.id}"
        self.source_name = f"{profile.bank} ({profile.name})"

    def detect(self, probe: FileProbe) -> float:
        """Score file probe confidence against this profile."""
        if not self.profile.auto_detect:
            return 0.0
        if self.profile.file_type != "auto":
            if self.profile.file_type == "csv" and not probe.is_csv():
                return 0.0
            if self.profile.file_type == "xlsx" and not probe.is_excel():
                return 0.0
            if self.profile.file_type == "pdf" and not probe.is_pdf():
                return 0.0

        if probe.is_excel():
            grid = self._read_excel_grid(probe, max_rows=30)
            sample_text = "\n".join(" ".join(str(c) for c in r if c is not None) for r in grid)
        elif probe.is_pdf():
            from billapp.importers.pdf import open_pdf_document
            try:
                with open_pdf_document(probe) as doc:
                    sample_text = doc[0].get_text() if len(doc) > 0 else ""
            except Exception:
                sample_text = ""
        else:
            enc = self.profile.encoding if self.profile.encoding != "auto" else detect_encoding(probe.content)
            sample_text = "".join(probe.read_text(encoding=enc, max_lines=30))

        score = 0.0
        if self.profile.bank in sample_text:
            score += 0.5

        kw = self.profile.table_detection.header_row_keywords or []
        if kw:
            matched_kw = sum(1 for k in kw if k in sample_text)
            if len(kw) >= 3 and matched_kw == len(kw):
                score += 0.6
            else:
                score += 0.4 * (matched_kw / len(kw))
        elif self.profile.table_detection.header_row_pattern:
            if safe_search(
                self.profile.table_detection.header_row_pattern,
                sample_text,
                max_len=PROFILE_MAX_PATTERN_LEN,
            ):
                score += 0.4
        else:
            cand = self.profile.column_mapping.date.header
            cand_list = cand if isinstance(cand, list) else ([cand] if cand else [])
            if any(c in sample_text for c in cand_list):
                score += 0.3

        return min(1.0, score)

    def parse(self, probe: FileProbe) -> ParseResult:
        """Parse statement using the profile schema."""
        pdf_preamble_text = ""
        if probe.is_excel():
            grid = self._read_excel_grid(probe)
        elif probe.is_pdf():
            grid = self._read_pdf_grid(probe)
            from billapp.importers.pdf import extract_pdf_preamble_lines
            try:
                p_lines = extract_pdf_preamble_lines(probe, password=probe.password, max_lines=25)
                pdf_preamble_text = "\n".join(" ".join(c for c in r if c) for r in p_lines)
            except Exception:
                pdf_preamble_text = ""
        else:
            grid = self._read_csv_grid(probe)

        if not grid:
            raise ValueError(f"Profile '{self.profile.id}': Statement grid is empty")

        header_idx = self._find_header_row_index(grid)
        if header_idx is None:
            raise ValueError(
                f"Profile '{self.profile.id}': Header row matching pattern "
                f"'{self.profile.table_detection.header_row_pattern}' not found"
            )

        preamble_rows = grid[:header_idx]
        table_preamble_text = "\n".join(" ".join(str(cell) for cell in row if cell is not None) for row in preamble_rows)
        preamble_text = (
            f"{table_preamble_text}\n{pdf_preamble_text}".strip()
            if pdf_preamble_text
            else table_preamble_text
        )
        card_tail, holder_name, period_start, period_end = self._extract_preamble_metadata(preamble_text)
        fb_meta = extract_preamble_metadata_fallback(preamble_text)
        effective_bank = (
            (fb_meta.get("bank") or self.profile.bank)
            if self.profile.bank in ("自定义机构", "自定义银行", "")
            else self.profile.bank
        )
        effective_kind = self.profile.account_kind_full
        if effective_kind == "bank_debit" and fb_meta.get("account_kind") == "credit" and self.profile.bank in ("自定义机构", "自定义银行", ""):
            effective_kind = "bank_credit"

        header_row = [str(c).strip() if c is not None else "" for c in grid[header_idx]]
        resolved_indices = self._resolve_column_indices(header_row)

        transactions: List[TransactionCreate] = []
        start_row = header_idx + self.profile.table_detection.data_start_offset
        total_rows = len(grid) - self.profile.table_detection.skip_footer_rows

        skip_regexes = [
            compile_safe_regex(p, max_len=PROFILE_MAX_PATTERN_LEN)
            for p in self.profile.table_detection.skip_row_patterns
        ]
        stop_regex = (
            compile_safe_regex(self.profile.table_detection.stop_pattern, max_len=PROFILE_MAX_PATTERN_LEN)
            if self.profile.table_detection.stop_pattern
            else None
        )

        for r_idx in range(start_row, total_rows):
            row = grid[r_idx]
            if not row or all(c is None or str(c).strip() == "" for c in row):
                continue

            joined_line = " ".join(str(c).strip() for c in row if c is not None)

            if stop_regex and safe_search(stop_regex, joined_line, timeout=DEFAULT_REGEX_TIMEOUT):
                break

            if any(safe_search(rgx, joined_line, timeout=DEFAULT_REGEX_TIMEOUT) for rgx in skip_regexes):
                continue

            txn = self._parse_single_row(
                row=row,
                resolved_indices=resolved_indices,
                row_number=r_idx + 1,
                fallback_tail=card_tail,
                holder_name=holder_name,
                effective_bank=effective_bank,
                effective_kind=effective_kind,
            )
            if txn:
                transactions.append(txn)

        if not transactions:
            raise ValueError(f"Profile '{self.profile.id}': No valid transaction rows parsed")

        assign_fingerprints(transactions)

        account_tail = card_tail or (transactions[0].account_ref.tail if transactions[0].account_ref else None)
        account = AccountCreate(
            kind=effective_kind,
            bank=effective_bank,
            tail=account_tail,
            alias=f"{effective_bank}{account_tail}" if account_tail else effective_bank,
            holder=holder_name,
            currency=self.profile.currency,
        )

        full_doc_text = preamble_text + "\n" + "\n".join(" ".join(str(c) for c in r if c is not None) for r in grid)
        self_checks = self._build_self_checks(transactions, full_doc_text)

        period = (period_start, period_end) if (period_start or period_end) else None

        return ParseResult(
            transactions=transactions,
            accounts=[account],
            holder_name=holder_name,
            period=period,
            self_checks=self_checks,
            warnings=[],
        )

    def _read_csv_grid(self, probe: FileProbe) -> List[List[Any]]:
        enc = self.profile.encoding if self.profile.encoding != "auto" else detect_encoding(probe.content)
        lines = probe.read_text(encoding=enc)
        reader = csv.reader(io.StringIO("".join(lines), newline=""), delimiter=self.profile.delimiter)
        return [list(row) for row in reader]

    def _read_excel_grid(self, probe: FileProbe, max_rows: Optional[int] = None) -> List[List[Any]]:
        row_limit = min(max_rows, MAX_PROFILE_EXCEL_ROWS) if max_rows else MAX_PROFILE_EXCEL_ROWS
        try:
            wb = openpyxl.load_workbook(io.BytesIO(probe.content), read_only=True, data_only=True)
            try:
                ws = wb.active
                if ws is None:
                    return []
                grid: List[List[Any]] = []
                for r, row in enumerate(
                    ws.iter_rows(max_row=row_limit, max_col=MAX_PROFILE_EXCEL_COLS, values_only=True)
                ):
                    if r >= row_limit:
                        break
                    grid.append(list(row))
                return grid
            finally:
                wb.close()
        except Exception:
            # Fallback for HTML-table disguised .xls bank exports
            enc = self.profile.encoding if self.profile.encoding != "auto" else detect_encoding(probe.content)
            text = "".join(probe.read_text(encoding=enc))
            if "<tr" in text.lower():
                class _ProfileHTMLTableParser(HTMLParser):
                    def __init__(self) -> None:
                        super().__init__()
                        self.rows: List[List[str]] = []
                        self._in_tr = False
                        self._in_cell = False
                        self._curr_row: List[str] = []
                        self._curr_cell: List[str] = []

                    def handle_starttag(self, tag: str, attrs: Any) -> None:
                        t = tag.lower()
                        if t == "tr":
                            self._in_tr = True
                            self._curr_row = []
                        elif t in ("td", "th") and self._in_tr:
                            self._in_cell = True
                            self._curr_cell = []

                    def handle_endtag(self, tag: str) -> None:
                        t = tag.lower()
                        if t in ("td", "th") and self._in_cell:
                            self._in_cell = False
                            self._curr_row.append("".join(self._curr_cell).strip())
                        elif t == "tr" and self._in_tr:
                            self._in_tr = False
                            if any(c.strip() for c in self._curr_row):
                                self.rows.append(self._curr_row)

                    def handle_data(self, data: str) -> None:
                        if self._in_cell:
                            self._curr_cell.append(data)

                parser = _ProfileHTMLTableParser()
                parser.feed(text)
                if parser.rows:
                    return parser.rows[:row_limit]
            raise

    def _read_pdf_grid(self, probe: FileProbe) -> List[List[Any]]:
        from billapp.importers.pdf import extract_pdf_tables
        tables = extract_pdf_tables(probe, password=probe.password)
        grid: List[List[Any]] = []
        for page_tbl in tables:
            for row in page_tbl:
                grid.append(row)
        return grid

    def _find_header_row_index(self, grid: List[List[Any]]) -> Optional[int]:
        td = self.profile.table_detection
        pattern = (
            compile_safe_regex(td.header_row_pattern, max_len=PROFILE_MAX_PATTERN_LEN)
            if td.header_row_pattern
            else None
        )
        keywords = td.header_row_keywords or []

        def _row_matches_spec(row: List[Any]) -> bool:
            joined = " ".join(str(c).strip() for c in row if c is not None)
            if pattern and safe_search(pattern, joined, timeout=DEFAULT_REGEX_TIMEOUT):
                return True
            if keywords and all(kw in joined for kw in keywords):
                return True
            return False

        # Prefer fixed header_row_index when it still matches the configured header spec (or no spec exists).
        # If preamble line count shifted in another statement from the same bank, scan dynamically before falling back.
        has_valid_index = td.header_row_index is not None and 0 <= td.header_row_index < len(grid)
        if has_valid_index:
            assert td.header_row_index is not None
            if (not pattern and not keywords) or _row_matches_spec(grid[td.header_row_index]):
                return td.header_row_index

        for idx, row in enumerate(grid[:50]):
            if _row_matches_spec(row):
                return idx

        if has_valid_index:
            return td.header_row_index

        # Fallback: check date column candidates in row
        date_cands = self.profile.column_mapping.date.header
        if date_cands:
            cands = date_cands if isinstance(date_cands, list) else [date_cands]
            for idx, row in enumerate(grid[:50]):
                joined = " ".join(str(c).strip() for c in row if c is not None)
                if any(cand in joined for cand in cands):
                    return idx

        return None

    def _extract_preamble_metadata(
        self, preamble_text: str
    ) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
        card_tail: Optional[str] = None
        holder_name: Optional[str] = None
        period_start: Optional[str] = None
        period_end: Optional[str] = None

        meta = self.profile.metadata_extraction
        if meta.card_tail:
            m = safe_search(meta.card_tail.pattern, preamble_text, max_len=PROFILE_MAX_PATTERN_LEN)
            if m:
                card_tail = m.group(meta.card_tail.group).strip()
            elif meta.card_tail.default:
                card_tail = meta.card_tail.default

        if meta.holder_name:
            m = safe_search(meta.holder_name.pattern, preamble_text, max_len=PROFILE_MAX_PATTERN_LEN)
            if m:
                holder_name = m.group(meta.holder_name.group).strip()
            elif meta.holder_name.default:
                holder_name = meta.holder_name.default

        if meta.period:
            m = safe_search(meta.period.pattern, preamble_text, max_len=PROFILE_MAX_PATTERN_LEN)
            if m:
                period_start = m.group(meta.period.start_group).strip()
                period_end = m.group(meta.period.end_group).strip()

        if any(v is None for v in (card_tail, holder_name, period_start, period_end)):
            fb = extract_preamble_metadata_fallback(preamble_text)
            if card_tail is None:
                card_tail = fb.get("card_tail")
            if holder_name is None:
                holder_name = fb.get("holder_name")
            if period_start is None:
                period_start = fb.get("period_start")
            if period_end is None:
                period_end = fb.get("period_end")

        return card_tail, holder_name, period_start, period_end

    def _resolve_column_indices(self, header_row: List[str]) -> Dict[str, Optional[int]]:
        resolved: Dict[str, Optional[int]] = {}
        normalized = [h.strip() for h in header_row]

        def find_col(mapping: Optional[FieldMapping]) -> Optional[int]:
            if not mapping:
                return None
            if mapping.index is not None and 0 <= mapping.index < len(header_row):
                return mapping.index
            if mapping.header:
                cands = mapping.header if isinstance(mapping.header, list) else [mapping.header]
                for cand in cands:
                    for i, h in enumerate(normalized):
                        if h == cand:
                            return i
                    for i, h in enumerate(normalized):
                        if cand in h or h in cand:
                            return i
            return None

        resolved["date"] = find_col(self.profile.column_mapping.date)
        if resolved["date"] is None:
            raise ValueError(f"Required date column not found in header: {header_row}")

        resolved["time"] = find_col(self.profile.column_mapping.time)

        mode = self.profile.amount_mode.type
        if mode == "signed":
            resolved["amount"] = find_col(self.profile.column_mapping.amount)
            if resolved["amount"] is None and self.profile.amount_mode.amount_col:
                target = self.profile.amount_mode.amount_col
                if isinstance(target, int):
                    resolved["amount"] = target
                else:
                    resolved["amount"] = next((i for i, h in enumerate(normalized) if target in h), None)
            if resolved["amount"] is None:
                raise ValueError(f"Signed mode requires amount column in header: {header_row}")

        elif mode == "split":
            resolved["expense_amount"] = find_col(self.profile.column_mapping.expense_amount)
            if resolved["expense_amount"] is None and self.profile.amount_mode.expense_col:
                target = self.profile.amount_mode.expense_col
                if isinstance(target, int):
                    resolved["expense_amount"] = target
                else:
                    resolved["expense_amount"] = next((i for i, h in enumerate(normalized) if target in h), None)

            resolved["income_amount"] = find_col(self.profile.column_mapping.income_amount)
            if resolved["income_amount"] is None and self.profile.amount_mode.income_col:
                target = self.profile.amount_mode.income_col
                if isinstance(target, int):
                    resolved["income_amount"] = target
                else:
                    resolved["income_amount"] = next((i for i, h in enumerate(normalized) if target in h), None)

            if resolved["expense_amount"] is None or resolved["income_amount"] is None:
                raise ValueError(f"Split mode requires expense and income columns in header: {header_row}")

        elif mode == "flag":
            resolved["amount"] = find_col(self.profile.column_mapping.amount)
            resolved["direction"] = find_col(self.profile.column_mapping.direction)
            if resolved["direction"] is None and self.profile.amount_mode.direction_col:
                target = self.profile.amount_mode.direction_col
                if isinstance(target, int):
                    resolved["direction"] = target
                else:
                    resolved["direction"] = next((i for i, h in enumerate(normalized) if target in h), None)

            if resolved["amount"] is None or resolved["direction"] is None:
                raise ValueError(f"Flag mode requires amount and direction columns in header: {header_row}")

        for attr in ("counterparty", "description", "balance", "currency", "card_tail", "order_id", "status", "channel"):
            fld = getattr(self.profile.column_mapping, attr, None)
            resolved[attr] = find_col(fld)

        return resolved

    def _parse_single_row(
        self,
        row: List[Any],
        resolved_indices: Dict[str, Optional[int]],
        row_number: int,
        fallback_tail: Optional[str],
        holder_name: Optional[str],
        effective_bank: Optional[str] = None,
        effective_kind: Optional[str] = None,
    ) -> Optional[TransactionCreate]:
        def get_val(key: str) -> Optional[str]:
            idx = resolved_indices.get(key)
            if idx is not None and 0 <= idx < len(row):
                val = row[idx]
                if val is not None:
                    s = str(val).strip()
                    return s if s else None
            return None

        # 1. Date and Time (skip repeated multi-page table headers or footer rows)
        raw_date = get_val("date")
        if not raw_date or not re.search(r"\d", raw_date):
            return None

        raw_time = get_val("time")
        if raw_time:
            dt_combined = f"{raw_date} {raw_time}".strip()
            iso_time, precision = parse_datetime(dt_combined)
        else:
            iso_time, precision = parse_datetime(raw_date)

        # 2. Amount and Direction
        mode = self.profile.amount_mode.type
        direction: str = "expense"
        amount_cents: int = 0

        try:
            if mode == "signed":
                raw_amt = get_val("amount")
                if raw_amt is None or not re.search(r"\d", raw_amt):
                    return None
                signed_cents = to_cents(raw_amt)
                if signed_cents == 0:
                    amount_cents = 0
                    direction = "neutral"
                elif signed_cents < 0:
                    amount_cents = abs(signed_cents)
                    direction = "expense" if self.profile.amount_mode.negative_means == "expense" else "income"
                else:
                    amount_cents = signed_cents
                    direction = "income" if self.profile.amount_mode.negative_means == "expense" else "expense"

            elif mode == "split":
                raw_exp = get_val("expense_amount")
                raw_inc = get_val("income_amount")
                exp_cents = abs(to_cents(raw_exp)) if (raw_exp and re.search(r"\d", raw_exp)) else 0
                inc_cents = abs(to_cents(raw_inc)) if (raw_inc and re.search(r"\d", raw_inc)) else 0

                if exp_cents > 0 and inc_cents == 0:
                    amount_cents = exp_cents
                    direction = "expense"
                elif inc_cents > 0 and exp_cents == 0:
                    amount_cents = inc_cents
                    direction = "income"
                elif exp_cents > 0 and inc_cents > 0:
                    amount_cents = abs(exp_cents - inc_cents)
                    direction = "expense" if exp_cents >= inc_cents else "income"
                else:
                    amount_cents = 0
                    direction = "neutral"

            elif mode == "flag":
                raw_amt = get_val("amount")
                raw_flag = get_val("direction")
                if raw_amt is None or not re.search(r"\d", raw_amt):
                    return None
                amount_cents = abs(to_cents(raw_amt))
                if amount_cents == 0:
                    direction = "neutral"
                else:
                    mapped = self.profile.amount_mode.flag_mapping.get(raw_flag or "")
                    direction = mapped or self.profile.amount_mode.default_direction
        except Exception:
            return None

        # 3. Descriptive metadata
        counterparty = get_val("counterparty")
        description = get_val("description")
        order_id = sanitize_order_id(get_val("order_id"))
        status = get_val("status")

        # 4. Keyword and regex direction overrides (using safe ReDoS-protected regex)
        check_text = f"{description or ''} {counterparty or ''}"
        refund_rules = self.profile.direction_rules.refund
        # Refunds represent inbound credits; avoid flipping normal outgoing expenses to refund
        if direction == "income":
            if any(kw in check_text for kw in refund_rules.get("keywords", [])) or \
               any(safe_search(rgx, check_text, max_len=PROFILE_MAX_PATTERN_LEN) for rgx in refund_rules.get("regex", [])):
                direction = "refund"

        transfer_rules = self.profile.direction_rules.transfer
        if direction != "refund":
            if any(kw in check_text for kw in transfer_rules.get("keywords", [])) or \
               any(safe_search(rgx, check_text, max_len=PROFILE_MAX_PATTERN_LEN) for rgx in transfer_rules.get("regex", [])):
                direction = "transfer"

        detected_channel, merchant_norm = extract_channel_and_normalize(counterparty)
        raw_explicit_channel = get_val("channel")
        explicit_channel = None
        if raw_explicit_channel:
            ch_text = raw_explicit_channel.strip()
            low_ch = ch_text.lower()
            if low_ch in ("alipay", "tenpay", "jdpay", "meituan", "unionpay"):
                explicit_channel = low_ch
            elif "支付宝" in ch_text or "alipay" in low_ch:
                explicit_channel = "alipay"
            elif "财付通" in ch_text or "微信" in ch_text or "tenpay" in low_ch or "wechat" in low_ch:
                explicit_channel = "tenpay"
            elif "网银在线" in ch_text or "京东" in ch_text or "jdpay" in low_ch:
                explicit_channel = "jdpay"
            elif "美团" in ch_text or "meituan" in low_ch:
                explicit_channel = "meituan"
            elif "云闪付" in ch_text or "银联" in ch_text or "unionpay" in low_ch:
                explicit_channel = "unionpay"
            else:
                explicit_channel, _ = extract_channel_and_normalize(ch_text)
        channel = explicit_channel or detected_channel

        eff_bank = effective_bank or self.profile.bank
        eff_kind = effective_kind or self.profile.account_kind_full

        row_tail = get_val("card_tail")
        tail = row_tail if (row_tail and len(row_tail) == 4) else fallback_tail

        row_currency = _normalize_currency_token(get_val("currency"))
        currency = row_currency or self.profile.currency
        pay_method = f"{eff_bank}({tail})" if tail else eff_bank

        account_ref = AccountRef(
            kind=eff_kind,
            bank=eff_bank,
            tail=tail,
            holder=holder_name,
        )

        return TransactionCreate(
            source=self.source_id,
            txn_time=iso_time,
            time_precision=precision,
            post_date=iso_time.split(" ")[0] if " " in iso_time else iso_time,
            amount_cents=amount_cents,
            currency=currency,
            direction=direction,
            counterparty=counterparty,
            merchant_norm=merchant_norm or counterparty,
            description=description,
            channel=channel,
            pay_method=pay_method,
            order_id=order_id,
            status=status,
            account_ref=account_ref,
        )

    def _build_self_checks(
        self, transactions: List[TransactionCreate], full_text: str
    ) -> List[SelfCheckResult]:
        self_checks: List[SelfCheckResult] = []

        # 1. Row count self-check
        self_checks.append(
            SelfCheckResult(
                name="解析交易笔数",
                expected=len(transactions),
                actual=len(transactions),
                passed=True,
                description=f"成功解析 {len(transactions)} 笔交易明细",
            )
        )

        # 2. Check for summary declaration in statement text
        m_tot = re.search(r"合计[：:]\s*支出\s*([\d\.,]+)\s*收入\s*([\d\.,]+)", full_text)
        if not m_tot:
            m_tot = re.search(r"(?:小计|总计)[,\t ]+([\d\.,]+)[,\t ]+([\d\.,]+)", full_text)

        if m_tot:
            try:
                exp_deb_cents = abs(to_cents(m_tot.group(1)))
                exp_cred_cents = abs(to_cents(m_tot.group(2)))

                act_deb_cents = sum(t.amount_cents for t in transactions if t.direction in ("expense", "transfer"))
                act_cred_cents = sum(t.amount_cents for t in transactions if t.direction in ("income", "refund"))

                deb_passed = (act_deb_cents == exp_deb_cents)
                cred_passed = (act_cred_cents == exp_cred_cents)

                self_checks.append(
                    SelfCheckResult(
                        name="支出金额汇总自检",
                        expected=float(Decimal(exp_deb_cents) / Decimal(100)),
                        actual=float(Decimal(act_deb_cents) / Decimal(100)),
                        passed=deb_passed,
                        deviation=float(abs(act_deb_cents - exp_deb_cents) / 100),
                        description="支出汇总校验",
                    )
                )
                self_checks.append(
                    SelfCheckResult(
                        name="收入金额汇总自检",
                        expected=float(Decimal(exp_cred_cents) / Decimal(100)),
                        actual=float(Decimal(act_cred_cents) / Decimal(100)),
                        passed=cred_passed,
                        deviation=float(abs(act_cred_cents - exp_cred_cents) / 100),
                        description="收入汇总校验",
                    )
                )
            except Exception:
                pass

        return self_checks
