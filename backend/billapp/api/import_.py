"""Statement ingestion, preview session, commit pipeline, and rollback API endpoints (/api/import/*)."""

from __future__ import annotations

import csv
from datetime import datetime, timedelta
import hashlib
from html.parser import HTMLParser
import io
import json
import logging
from pathlib import Path
import re
import sqlite3
import time
from typing import Any, Dict, List, Optional, Tuple
import uuid
import zipfile

import anyio
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from pydantic import BaseModel, Field

from billapp.api._upload import MAX_STATEMENT_UPLOAD_BYTES, read_upload_limited
from billapp.app import AppException, NotFoundError, ValidationError, get_db_conn
from billapp.classify.pipeline import ClassificationPipeline
from billapp.core.fx import convert_transaction
from billapp.core.models import AccountCreate, AccountKind, AccountRef, ParseResult, TransactionCreate
from billapp.dedup.engine import rollback_import_batch, run_dedup_pipeline
from billapp.dedup.fingerprint import assign_fingerprints
from billapp.importers.base import FileProbe
from billapp.importers.common import detect_encoding, normalize_currency
from billapp.importers.pdf import (
    PDFInvalidPasswordError,
    PDFPasswordRequiredError,
    extract_pdf_preamble_lines,
    extract_pdf_statement_lines,
    extract_pdf_tables,
    open_pdf_document,
)
from billapp.importers.profile_engine import BankProfile, ProfileImporter
from billapp.importers.registry import get_registry
from billapp.llm.client import LLMClient, LLMConfig
from billapp.llm.prompts import collect_known_holder_names
from billapp.llm.tasks import (
    _normalize_account_kind,
    parse_statement_with_llm_sync,
    recognize_statement_account_sync,
)

logger = logging.getLogger("billapp.api.import")
router = APIRouter(prefix="/api/import", tags=["import"])


def _safe_file_ext(file_name: Optional[str]) -> str:
    """Return lowercase extension only for safe, PII-free diagnostic logging."""
    suffix = Path(str(file_name or "")).suffix.lower()
    return suffix if suffix and re.fullmatch(r"\.[a-z0-9]{1,12}", suffix) else "(none)"

# Volatile RAM-only preview store (15-min TTL, bounded capacity, zero disk persistence)
_PREVIEW_STORE: Dict[str, Dict[str, Any]] = {}
_MAX_PREVIEW_ENTRIES = 20
_MAX_ZIP_COMPRESSION_RATIO = 100.0
_ZIP_RATIO_CHECK_MIN_BYTES = 1 * 1024 * 1024


def _prune_preview_store() -> None:
    """Evict expired preview entries and cap total in-memory sessions to prevent memory leaks (S-07)."""
    now = datetime.now()
    expired = [k for k, v in _PREVIEW_STORE.items() if now > v["expires_at"]]
    for k in expired:
        _PREVIEW_STORE.pop(k, None)

    while len(_PREVIEW_STORE) >= _MAX_PREVIEW_ENTRIES:
        oldest_key = min(_PREVIEW_STORE.keys(), key=lambda k: _PREVIEW_STORE[k]["expires_at"])
        _PREVIEW_STORE.pop(oldest_key, None)


class _HTMLTableExtractor(HTMLParser):
    """Lightweight HTML <table> row extractor for bank .xls HTML exports."""

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


def _decode_zip_entry_name(info: zipfile.ZipInfo) -> str:
    """Fix CP437-decoded GBK filenames commonly produced by Windows/bank ZIP tools."""
    name = info.filename
    if not (info.flag_bits & 0x800):
        try:
            name = name.encode("cp437").decode("gb18030")
        except Exception:
            pass
    return Path(name).name or name


def _unwrap_zip_probe_with_warnings(probe: FileProbe) -> Tuple[FileProbe, bool, List[str]]:
    """Unpack a ZIP statement archive into the primary inner FileProbe, returning any skip warnings."""
    if not probe.is_zip():
        return probe, False, []

    try:
        zf = zipfile.ZipFile(io.BytesIO(probe.content))
    except zipfile.BadZipFile as exc:
        raise AppException(status_code=400, code="IMPORT_PARSE_FAILED", message=f"无法读取 ZIP 压缩包: {exc}")

    ext_priority = {".csv": 0, ".xlsx": 1, ".xls": 2, ".pdf": 3, ".txt": 4}
    ignored_exts = {".sig", ".p7s", ".asc", ".sha1", ".sha256", ".md5"}

    candidates: List[Tuple[int, int, zipfile.ZipInfo, str]] = []
    zip_warnings: List[str] = []
    with zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            raw_path = info.filename
            if "__MACOSX" in raw_path or Path(raw_path).name.startswith("._"):
                continue
            decoded_name = _decode_zip_entry_name(info)
            suffix = Path(decoded_name).suffix.lower()
            if suffix in ignored_exts:
                continue
            rank = ext_priority.get(suffix, 99)
            candidates.append((rank, -info.file_size, info, decoded_name))

        if not candidates:
            raise AppException(status_code=400, code="IMPORT_PARSE_FAILED", message="ZIP 压缩包中未找到可导入的账单文件")

        candidates.sort(key=lambda item: (item[0], item[1]))
        _, _, target_info, target_name = candidates[0]
        if len(candidates) > 1:
            skipped_names = [item[3] for item in candidates[1:4]]
            more_suffix = f" 等共 {len(candidates) - 1} 个文件" if len(candidates) > 4 else ""
            zip_warnings.append(
                f"ZIP 压缩包包含 {len(candidates)} 个账单候选文件，本次仅解析 '{target_name}'，"
                f"已跳过 {', '.join(skipped_names)}{more_suffix}（如需全部导入请解压后分别上传）"
            )

        # ZIP bomb defenses: enforce uncompressed size ceiling and compression ratio ceiling (S-07)
        if target_info.file_size > MAX_STATEMENT_UPLOAD_BYTES:
            raise AppException(
                status_code=413,
                code="FILE_TOO_LARGE",
                message="ZIP 内部账单文件解压后超过 50 MB 安全限制，已拒绝解压",
            )
        if (
            target_info.file_size >= _ZIP_RATIO_CHECK_MIN_BYTES
            and (target_info.file_size / max(1, target_info.compress_size)) > _MAX_ZIP_COMPRESSION_RATIO
        ):
            raise AppException(
                status_code=400,
                code="IMPORT_ZIP_BOMB_DETECTED",
                message="检测到异常压缩比（疑似 ZIP 炸弹），已拒绝解压",
            )

        is_encrypted = bool(target_info.flag_bits & 0x1)
        if is_encrypted and not probe.password:
            return probe, True, zip_warnings

        pwd_attempts: List[Optional[bytes]]
        if is_encrypted and probe.password is not None:
            pwd_attempts = []
            for enc in ("utf-8", "gb18030", "latin-1"):
                try:
                    b = probe.password.encode(enc)
                    if b not in pwd_attempts:
                        pwd_attempts.append(b)
                except Exception:
                    continue
        else:
            pwd_attempts = [None]

        inner_bytes: Optional[bytes] = None
        last_exc: Optional[Exception] = None
        for pwd_bytes in pwd_attempts:
            try:
                with zf.open(target_info, pwd=pwd_bytes) as member:
                    inner_bytes = member.read(MAX_STATEMENT_UPLOAD_BYTES + 1)
                if len(inner_bytes) > MAX_STATEMENT_UPLOAD_BYTES:
                    raise AppException(
                        status_code=413,
                        code="FILE_TOO_LARGE",
                        message="ZIP 内部账单文件解压后超过 50 MB 安全限制，已拒绝解压",
                    )
                last_exc = None
                break
            except NotImplementedError as exc:
                raise AppException(
                    status_code=400,
                    code="IMPORT_ZIP_UNSUPPORTED_ENCRYPTION",
                    message="该 ZIP 压缩包使用了高级加密压缩 (如 AES-256)，请先在本地解压出其中的账单文件后再导入",
                ) from exc
            except RuntimeError as exc:
                last_exc = exc
                msg = str(exc).lower()
                if "password" in msg or "bad crc" in msg or "encrypted" in msg:
                    continue
                if "compression method" in msg:
                    raise AppException(
                        status_code=400,
                        code="IMPORT_ZIP_UNSUPPORTED_ENCRYPTION",
                        message="该 ZIP 压缩包使用了不支持的加密压缩算法，请先在本地解压出账单文件后再导入",
                    ) from exc
                raise AppException(status_code=400, code="IMPORT_PARSE_FAILED", message=f"解压 ZIP 账单失败: {exc}") from exc

        if last_exc is not None or inner_bytes is None:
            raise AppException(
                status_code=400,
                code="IMPORT_PASSWORD_WRONG",
                message="解压密码不正确，请检查后重试",
            )

    return FileProbe(inner_bytes, file_name=target_name, password=probe.password), False, zip_warnings


def _unwrap_zip_probe(probe: FileProbe) -> Tuple[FileProbe, bool]:
    """Unpack a ZIP statement archive into the primary inner FileProbe.

    Returns:
        (unwrapped_probe, need_password)
    """
    unwrapped, need_pwd, _ = _unwrap_zip_probe_with_warnings(probe)
    return unwrapped, need_pwd


def extract_raw_rows_from_probe(probe: FileProbe, max_rows: int = 30) -> Tuple[str, List[List[str]]]:
    """Extract raw string grid rows from CSV/TSV/TXT (UTF-8/GBK), Excel, or PDF for mapping."""
    rows: List[List[str]] = []

    if probe.is_zip():
        unwrapped, need_pwd = _unwrap_zip_probe(probe)
        if need_pwd:
            return "zip", []
        probe = unwrapped

    if probe.is_pdf():
        file_type = "pdf"
        try:
            tables = extract_pdf_tables(probe, password=probe.password)
            for page_tbl in tables:
                for r in page_tbl:
                    str_row = [str(c).strip() if c is not None else "" for c in r]
                    if any(str_row):
                        rows.append(str_row)
                    if len(rows) >= max_rows:
                        break
                if len(rows) >= max_rows:
                    break
        except Exception:
            rows = []

        if not rows:
            try:
                rows = extract_pdf_preamble_lines(probe, password=probe.password, max_lines=max_rows)
            except Exception:
                rows = []
            if not rows:
                try:
                    with open_pdf_document(probe, password=probe.password) as doc:
                        for page in doc:
                            for line in page.get_text("text").splitlines():
                                parts = [p.strip() for p in line.split() if p.strip()]
                                if parts:
                                    rows.append(parts)
                                if len(rows) >= max_rows:
                                    break
                            if len(rows) >= max_rows:
                                break
                except Exception:
                    pass
        return file_type, rows[:max_rows]

    if probe.is_excel():
        file_type = "xlsx"
        try:
            import openpyxl
            wb = openpyxl.load_workbook(io.BytesIO(probe.content), read_only=True, data_only=True)
            try:
                ws = wb.active
                if ws is not None:
                    for r in ws.iter_rows(values_only=True):
                        str_row = [str(c).strip() if c is not None else "" for c in r]
                        if any(str_row):
                            rows.append(str_row)
                        if len(rows) >= max_rows:
                            break
            finally:
                wb.close()
            if rows:
                return file_type, rows[:max_rows]
        except Exception:
            pass

        # Fallback for HTML/text disguised .xls bank exports
        enc = detect_encoding(probe.content)
        text = "".join(probe.read_text(encoding=enc))
        if "<tr" in text.lower():
            parser = _HTMLTableExtractor()
            parser.feed(text)
            if parser.rows:
                return "auto", parser.rows[:max_rows]

    # Text / CSV / TSV fallback
    file_type = "csv"
    enc = detect_encoding(probe.content)
    text_lines = [line for line in probe.read_text(encoding=enc, max_lines=max_rows * 2) if line.strip()]
    if text_lines:
        sample = "\n".join(text_lines[:10])
        delimiter = ","
        if "\t" in sample and sample.count("\t") >= sample.count(","):
            delimiter = "\t"
        elif ";" in sample and "," not in sample:
            delimiter = ";"
        try:
            reader = csv.reader(io.StringIO("".join(text_lines), newline=""), delimiter=delimiter)
            for r in reader:
                str_row = [str(c).strip() for c in r]
                if any(str_row):
                    rows.append(str_row)
                if len(rows) >= max_rows:
                    break
        except csv.Error:
            for line in text_lines[:max_rows]:
                parts = [c.strip() for c in line.split(delimiter)]
                if any(parts):
                    rows.append(parts)

    return file_type, rows[:max_rows]


def _extract_preamble_rows_for_mapping(probe: FileProbe) -> List[List[str]]:
    """Extract PDF page-1 top lines (outside table borders) without shifting table row indices."""
    if not probe.is_pdf():
        return []
    try:
        return extract_pdf_preamble_lines(probe, password=probe.password, max_lines=20)
    except Exception:
        return []


def _extract_raw_statement_lines(probe: FileProbe) -> Tuple[str, List[str]]:
    """Extract full-file reading-order lines for LLM full-statement takeover."""
    if probe.is_zip():
        unwrapped, need_pwd = _unwrap_zip_probe(probe)
        if need_pwd:
            return "zip", []
        probe = unwrapped

    if probe.is_pdf():
        return "pdf", extract_pdf_statement_lines(probe, password=probe.password)

    if probe.is_excel():
        lines: List[str] = []
        try:
            import openpyxl

            wb = openpyxl.load_workbook(
                io.BytesIO(probe.content), read_only=True, data_only=True
            )
            try:
                ws = wb.active
                if ws is not None:
                    for r in ws.iter_rows(values_only=True):
                        parts = [str(c).strip() for c in r if c is not None and str(c).strip()]
                        if parts:
                            lines.append(" | ".join(parts))
            finally:
                wb.close()
            if lines:
                return "xlsx", lines
        except Exception:
            pass

        enc = detect_encoding(probe.content)
        text = "".join(probe.read_text(encoding=enc))
        if "<tr" in text.lower():
            parser = _HTMLTableExtractor()
            parser.feed(text)
            if parser.rows:
                return "auto", [" | ".join(c for c in r if c.strip()) for r in parser.rows if any(c.strip() for c in r)]

    enc = detect_encoding(probe.content)
    text_lines = [line.strip() for line in probe.read_text(encoding=enc) if line.strip()]
    return "csv", text_lines


def _has_mappable_table_grid(raw_rows: List[List[str]]) -> bool:
    """Return True when extracted preview rows contain a multi-column grid suitable for column mapping."""
    wide_rows = sum(1 for r in raw_rows if sum(1 for c in r if str(c).strip()) >= 3)
    return wide_rows >= 2


def _build_llm_client(db: sqlite3.Connection, timeout_min: float = 90.0) -> Tuple[LLMConfig, LLMClient]:
    cfg = LLMConfig.load_from_db(db)
    if float(cfg.timeout_sec or 0) < timeout_min:
        cfg.timeout_sec = timeout_min
    cfg.max_retries = min(cfg.max_retries, 1)
    return cfg, LLMClient(cfg, conn=db)


_GENERIC_BANK_NAMES = {"", "未知银行/机构", "自定义机构", "未知机构", "未知", "unknown"}


def _apply_account_to_parse_result(
    parse_result: ParseResult,
    *,
    bank: Optional[str] = None,
    kind: Optional[str] = None,
    tail: Optional[str] = None,
    alias: Optional[str] = None,
    holder: Optional[str] = None,
    currency: Optional[str] = None,
    override_explicit: bool = False,
) -> None:
    """Apply recognized or user-overridden top-of-statement account fields to ParseResult & txns."""
    clean_bank = bank.strip() if isinstance(bank, str) and bank.strip() else None
    norm_kind: Optional[AccountKind] = _normalize_account_kind(kind) if kind else None
    clean_tail: Optional[str] = None
    if tail is not None:
        m = re.search(r"(\d{4})(?!\d)", str(tail).strip())
        clean_tail = m.group(1) if m else (str(tail).strip() or None)
    clean_holder = holder.strip() if isinstance(holder, str) and holder.strip() else None
    clean_alias = alias.strip() if isinstance(alias, str) and alias.strip() else None
    clean_curr = currency.strip().upper() if isinstance(currency, str) and currency.strip() else None

    if clean_holder and (override_explicit or not parse_result.holder_name):
        parse_result.holder_name = clean_holder

    old_bank: Optional[str] = None
    old_tail: Optional[str] = None
    is_multi_account = len(parse_result.accounts) > 1

    if not parse_result.accounts:
        if not (clean_bank or clean_tail or norm_kind):
            return
        eff_bank = clean_bank or "未知银行/机构"
        eff_kind: AccountKind = norm_kind or "bank_debit"
        parse_result.accounts.append(
            AccountCreate(
                kind=eff_kind,
                bank=eff_bank,
                tail=clean_tail,
                alias=clean_alias or (f"{eff_bank} ({clean_tail})" if clean_tail else eff_bank),
                holder=clean_holder,
                currency=clean_curr or "CNY",
            )
        )
    else:
        acc = parse_result.accounts[0]
        old_bank = acc.bank
        old_tail = acc.tail
        if clean_bank and (override_explicit or acc.bank.strip() in _GENERIC_BANK_NAMES):
            acc.bank = clean_bank
        if norm_kind and (override_explicit or not acc.kind):
            acc.kind = norm_kind
        if tail is not None and (override_explicit or not acc.tail):
            acc.tail = clean_tail
        if clean_holder and (override_explicit or not acc.holder):
            acc.holder = clean_holder
        if clean_curr and (override_explicit or not acc.currency):
            acc.currency = clean_curr
        if clean_alias:
            acc.alias = clean_alias
        elif acc.bank != old_bank or acc.tail != old_tail or not acc.alias:
            acc.alias = f"{acc.bank} ({acc.tail})" if acc.tail else acc.bank

    primary = parse_result.accounts[0]
    is_platform_or_wallet = primary.kind in ("platform", "wallet")

    for t in parse_result.transactions:
        if t.account_ref is None:
            t.account_ref = AccountRef(
                kind=primary.kind,
                bank=primary.bank,
                tail=primary.tail,
                alias=primary.alias,
                holder=primary.holder,
            )
            belongs_to_primary = True
        else:
            if not is_multi_account:
                belongs_to_primary = True
            else:
                ref_bank = (t.account_ref.bank or "").strip()
                ref_tail = t.account_ref.tail
                belongs_to_primary = (
                    (ref_tail == old_tail or not ref_tail)
                    and (not ref_bank or ref_bank == (old_bank or "").strip() or ref_bank in _GENERIC_BANK_NAMES)
                )

            if belongs_to_primary:
                if override_explicit or (t.account_ref.bank or "").strip() in _GENERIC_BANK_NAMES:
                    t.account_ref.bank = primary.bank
                if override_explicit or not t.account_ref.kind:
                    t.account_ref.kind = primary.kind
                if override_explicit or not t.account_ref.tail:
                    t.account_ref.tail = primary.tail
                if override_explicit or not t.account_ref.holder:
                    t.account_ref.holder = primary.holder
                if override_explicit or not t.account_ref.alias:
                    t.account_ref.alias = primary.alias
            else:
                # Preserve distinct card tails on multi-card statements (e.g. ICBC multi-card PDF)
                if clean_holder and not t.account_ref.holder:
                    t.account_ref.holder = clean_holder

        if belongs_to_primary:
            eff_b = t.account_ref.bank or primary.bank
            eff_t = t.account_ref.tail or primary.tail
            # Never clobber real funding pay_method on platform/wallet bills (Alipay/WeChat)
            # or existing pay_method during passive top-account enrichment
            preserve_pay_method = is_platform_or_wallet or t.source in ("alipay", "wechat")
            if not preserve_pay_method and (override_explicit or not t.pay_method):
                t.pay_method = f"{eff_b}({eff_t})" if eff_t else eff_b
            if clean_curr and override_explicit:
                t.currency = clean_curr

    assign_fingerprints(parse_result.transactions)


def _enrich_parse_result_top_account(
    parse_result: ParseResult,
    probe: FileProbe,
    file_name: str,
    llm_client: LLMClient,
) -> None:
    """Fill missing top-of-statement account info (bank / 4-digit tail / holder) when absent."""
    primary = parse_result.accounts[0] if parse_result.accounts else None
    # Platform and wallet accounts (e.g. WeChat Pay, Alipay) legitimately omit a 4-digit bank card tail
    if primary is not None and primary.kind in ("platform", "wallet") and primary.bank.strip() not in _GENERIC_BANK_NAMES:
        return
    needs_enrich = (
        primary is None
        or not primary.tail
        or primary.bank.strip() in _GENERIC_BANK_NAMES
        or not parse_result.holder_name
    )
    if not needs_enrich:
        return
    try:
        _, top_rows = extract_raw_rows_from_probe(probe, max_rows=20)
    except Exception:
        top_rows = []
    if not top_rows and not file_name:
        return
    if llm_client.is_available() and llm_client.config.task_suggest_profile_enabled:
        rec = recognize_statement_account_sync(top_rows, llm_client, file_name=file_name)
    else:
        disabled_cfg = LLMConfig(enabled=False)
        rec = recognize_statement_account_sync(top_rows, LLMClient(disabled_cfg), file_name=file_name)
    _apply_account_to_parse_result(
        parse_result,
        bank=rec.bank,
        kind=rec.account_kind,
        tail=rec.card_tail,
        holder=rec.holder_name,
        currency=rec.currency,
        override_explicit=False,
    )
    if not parse_result.period and (rec.period_start or rec.period_end):
        parse_result.period = (rec.period_start, rec.period_end)


# =========================================================================
# Ingestion DTO Schemas
# =========================================================================

class ImportPreviewItem(BaseModel):
    token: str
    file_name: str
    source: str
    profile_id: Optional[str] = None
    existing_batch_id: Optional[int] = None
    total_rows: int
    need_password: bool = False
    need_mapping: bool = False
    accounts: List[Dict[str, Any]] = Field(default_factory=list)
    self_checks: List[Dict[str, Any]] = Field(default_factory=list)
    rows: List[Dict[str, Any]] = Field(default_factory=list)
    raw_rows: List[List[str]] = Field(default_factory=list)
    preamble_rows: List[List[str]] = Field(default_factory=list)
    detected_file_type: Optional[str] = None
    warnings: List[str] = Field(default_factory=list)
    expires_at: str


class ExtractRawResponse(BaseModel):
    file_name: str
    file_type: str
    raw_rows: List[List[str]] = Field(default_factory=list)
    preamble_rows: List[List[str]] = Field(default_factory=list)
    total_extracted: int = 0


class CommitRequest(BaseModel):
    token: str
    account_overrides: Optional[Dict[str, Any]] = None


class ImportCommitResponse(BaseModel):
    batch_id: int
    replaced_batch_id: Optional[int] = None
    imported_count: int
    duplicate_count: int
    refund_count: int
    transfer_count: int
    rule_hits: int
    unclassified_count: int
    summary: Dict[str, Any]


class BatchItem(BaseModel):
    id: int
    file_name: str
    file_sha1: str
    file_size: int
    source: str
    profile_id: Optional[str] = None
    imported_at: str
    row_count: int
    period_start: Optional[str] = None
    period_end: Optional[str] = None
    self_check_json: Optional[str] = None
    can_rollback: bool = True


# =========================================================================
# Endpoints
# =========================================================================

@router.post("/extract-raw", response_model=ExtractRawResponse)
async def extract_raw_statement(
    file: UploadFile = File(...),
    password: Optional[str] = Form(None),
):
    """Extract raw grid rows from ZIP/CSV/Excel/PDF for custom mapping and AI suggestion."""
    content = await read_upload_limited(file, MAX_STATEMENT_UPLOAD_BYTES)
    file_name = file.filename or "statement"
    ext = _safe_file_ext(file_name)
    logger.info("Extract-raw received: ext=%s size_bytes=%d has_password=%s", ext, len(content), bool(password))

    def _work() -> ExtractRawResponse:
        probe = FileProbe(content, file_name=file_name, password=password)
        probe, need_pwd = _unwrap_zip_probe(probe)
        if need_pwd:
            raise AppException(status_code=400, code="IMPORT_PASSWORD_REQUIRED", message="ZIP 压缩包已加密，请先输入解压密码")
        file_type, raw_rows = extract_raw_rows_from_probe(probe, max_rows=30)
        preamble_rows = _extract_preamble_rows_for_mapping(probe)
        logger.info("Extract-raw completed: ext=%s file_type=%s rows=%d", ext, file_type, len(raw_rows))
        return ExtractRawResponse(
            file_name=probe.file_name,
            file_type=file_type,
            raw_rows=raw_rows,
            preamble_rows=preamble_rows,
            total_extracted=len(raw_rows),
        )

    return await anyio.to_thread.run_sync(_work)


@router.post("/preview", response_model=ImportPreviewItem)
async def import_preview(
    file: UploadFile = File(...),
    password: Optional[str] = Form(None),
    profile_id: Optional[str] = Form(None),
    use_llm: Optional[str] = Form(None),
    db: sqlite3.Connection = Depends(get_db_conn),
):
    """Parse bill statement file into an in-memory preview session token.
    
    Zero disk persistence: PDF passwords reside purely in volatile RAM.
    """
    started_at = time.perf_counter()
    content = await read_upload_limited(file, MAX_STATEMENT_UPLOAD_BYTES)
    file_name = file.filename or "statement"
    ext = _safe_file_ext(file_name)
    logger.info(
        "Import preview received: ext=%s size_bytes=%d has_password=%s profile_id=%s use_llm=%s",
        ext,
        len(content),
        bool(password),
        profile_id or "-",
        bool(str(use_llm or "").strip().lower() in ("true", "1", "yes", "force")),
    )
    res = await anyio.to_thread.run_sync(
        _import_preview_sync,
        content,
        file_name,
        password,
        profile_id,
        use_llm,
        db,
    )
    elapsed_ms = int((time.perf_counter() - started_at) * 1000)
    logger.info(
        "Import preview completed: ext=%s source=%s rows=%d need_password=%s need_mapping=%s existing_batch_id=%s warnings=%d (%dms)",
        ext,
        res.source,
        res.total_rows,
        res.need_password,
        res.need_mapping,
        res.existing_batch_id if res.existing_batch_id is not None else "-",
        len(res.warnings),
        elapsed_ms,
    )
    return res


def _import_preview_sync(
    content: bytes,
    file_name: str,
    password: Optional[str],
    profile_id: Optional[str],
    use_llm: Optional[str],
    db: sqlite3.Connection,
) -> ImportPreviewItem:
    sha1 = hashlib.sha1(content).hexdigest()

    # 1. SHA1 Duplicate File Detection (allow re-parse and replace existing batch on commit)
    existing = db.execute("SELECT id FROM import_batches WHERE file_sha1 = ?", (sha1,)).fetchone()
    existing_batch_id: Optional[int] = int(existing["id"]) if existing else None
    pre_warnings: List[str] = []
    if existing_batch_id is not None:
        pre_warnings.append(
            f"账单文件 '{file_name}' 此前已导入（原批次 #{existing_batch_id}），确认导入后将重新解析并替换原批次入库"
        )

    # 2. Ephemeral Probe, ZIP Unpack, & PDF Password Detection
    probe = FileProbe(content, file_name=file_name, password=password)
    if probe.is_zip():
        probe, zip_needs_pwd, zip_warnings = _unwrap_zip_probe_with_warnings(probe)
        pre_warnings.extend(zip_warnings)
        logger.info(
            "ZIP archive unpacked: inner_ext=%s inner_size=%d needs_password=%s warnings=%d",
            _safe_file_ext(probe.file_name),
            probe.file_size,
            zip_needs_pwd,
            len(zip_warnings),
        )
        if zip_needs_pwd:
            return ImportPreviewItem(
                token="",
                file_name=file_name,
                source="zip",
                existing_batch_id=existing_batch_id,
                total_rows=0,
                need_password=True,
                need_mapping=False,
                accounts=[],
                self_checks=[],
                rows=[],
                warnings=pre_warnings,
                expires_at="",
            )

    if probe.is_pdf():
        import pymupdf
        try:
            doc = pymupdf.open(stream=probe.content, filetype="pdf")
            if doc.needs_pass:
                if not password:
                    doc.close()
                    return ImportPreviewItem(
                        token="",
                        file_name=file_name,
                        source="pdf",
                        existing_batch_id=existing_batch_id,
                        total_rows=0,
                        need_password=True,
                        need_mapping=False,
                        accounts=[],
                        self_checks=[],
                        rows=[],
                        warnings=pre_warnings,
                        expires_at="",
                    )
                auth = doc.authenticate(password)
                if auth == 0:
                    doc.close()
                    raise AppException(
                        status_code=400,
                        code="IMPORT_PASSWORD_WRONG",
                        message="密码不正确，请检查后重试",
                    )
            doc.close()
        except AppException:
            raise
        except Exception as exc:
            raise AppException(status_code=400, code="IMPORT_PARSE_FAILED", message=f"无法读取PDF文件: {exc}")

    # 3. Importer Resolution & Optional Full-Statement LLM Takeover
    force_llm = str(use_llm or "").strip().lower() in ("true", "1", "yes", "force")
    llm_config, llm_client = _build_llm_client(db, timeout_min=90.0)

    def _run_llm_full_takeover() -> ParseResult:
        _, raw_lines = _extract_raw_statement_lines(probe)
        known_holders = collect_known_holder_names(db)
        parsed, _ = parse_statement_with_llm_sync(
            raw_lines=raw_lines,
            client=llm_client,
            file_name=file_name,
            extra_holders=known_holders or None,
        )
        return parsed

    importer = None
    parse_result: Optional[ParseResult] = None
    resolved_source = "unknown"

    if force_llm:
        try:
            parse_result = _run_llm_full_takeover()
            resolved_source = "llm_statement"
            profile_id = None
        except Exception as exc:
            raise AppException(status_code=400, code="IMPORT_PARSE_FAILED", message=f"AI 整单识别失败: {exc}")
    else:
        if profile_id:
            row = db.execute("SELECT yaml_content FROM bank_profiles WHERE id = ?", (profile_id,)).fetchone()
            if not row:
                raise NotFoundError(f"未找到指定的银行配置: {profile_id}")
            profile = BankProfile.from_yaml(row["yaml_content"])
            importer = ProfileImporter(profile)
        else:
            registry = get_registry()
            importer, score = registry.detect_importer(probe)
            if importer is None:
                # Also match saved bank profiles from DB (custom Mapping Wizard templates + builtin YAML)
                best_profile_imp: Optional[ProfileImporter] = None
                best_profile_score = 0.0
                rows = db.execute(
                    "SELECT id, yaml_content FROM bank_profiles ORDER BY is_builtin ASC, updated_at DESC"
                ).fetchall()
                for r in rows:
                    try:
                        p = BankProfile.from_yaml(r["yaml_content"])
                        cand_imp = ProfileImporter(p)
                        cand_score = cand_imp.detect(probe)
                        if cand_score >= 0.5 and cand_score > best_profile_score:
                            best_profile_score = cand_score
                            best_profile_imp = cand_imp
                            profile_id = r["id"]
                    except Exception:
                        continue
                if best_profile_imp is not None:
                    importer = best_profile_imp
                    score = best_profile_score

        if importer is None:
            detected_type, raw_rows = extract_raw_rows_from_probe(probe, max_rows=30)
            if (
                not _has_mappable_table_grid(raw_rows)
                and llm_client.is_available()
                and llm_config.task_full_parse_enabled
            ):
                try:
                    parse_result = _run_llm_full_takeover()
                    resolved_source = "llm_statement"
                    profile_id = None
                except Exception:
                    parse_result = None

            if parse_result is None:
                preamble_rows = _extract_preamble_rows_for_mapping(probe)
                return ImportPreviewItem(
                    token="",
                    file_name=file_name,
                    source="unknown",
                    existing_batch_id=existing_batch_id,
                    total_rows=0,
                    need_password=False,
                    need_mapping=True,
                    accounts=[],
                    self_checks=[],
                    rows=[],
                    raw_rows=raw_rows,
                    preamble_rows=preamble_rows,
                    detected_file_type=detected_type,
                    warnings=pre_warnings,
                    expires_at="",
                )
        else:
            # 4. Dry-run Parse (with automatic LLM takeover fallback on unparseable statements)
            resolved_source = importer.source_id
            parse_exc: Optional[Exception] = None
            try:
                parse_result = importer.parse(probe)
            except PDFInvalidPasswordError:
                raise AppException(status_code=400, code="IMPORT_PASSWORD_WRONG", message="密码不正确，请检查后重试")
            except PDFPasswordRequiredError:
                return ImportPreviewItem(
                    token="",
                    file_name=file_name,
                    source=importer.source_id,
                    existing_batch_id=existing_batch_id,
                    total_rows=0,
                    need_password=True,
                    need_mapping=False,
                    accounts=[],
                    self_checks=[],
                    rows=[],
                    warnings=pre_warnings,
                    expires_at="",
                )
            except Exception as exc:
                parse_exc = exc
                parse_result = None

            if parse_result is None or not parse_result.transactions:
                if llm_client.is_available() and llm_config.task_full_parse_enabled:
                    try:
                        parse_result = _run_llm_full_takeover()
                        resolved_source = "llm_statement"
                        profile_id = None
                    except Exception:
                        pass
                if parse_result is None:
                    raise AppException(status_code=400, code="IMPORT_PARSE_FAILED", message=f"解析账单失败: {parse_exc}")
                if not parse_result.transactions:
                    raise AppException(status_code=400, code="IMPORT_EMPTY", message="未解析到有效交易记录")
            else:
                _enrich_parse_result_top_account(parse_result, probe, file_name, llm_client)

    assert parse_result is not None
    if not parse_result.transactions:
        raise AppException(status_code=400, code="IMPORT_EMPTY", message="未解析到有效交易记录")

    # 5. In-Memory Preview Token Store (15-min TTL, bounded capacity)
    combined_warnings = pre_warnings + list(parse_result.warnings)
    _prune_preview_store()
    token = uuid.uuid4().hex
    expires_at = datetime.now() + timedelta(minutes=15)
    _PREVIEW_STORE[token] = {
        "parse_result": parse_result,
        "file_name": file_name,
        "file_sha1": sha1,
        "file_size": len(content),
        "source": resolved_source,
        "profile_id": profile_id,
        "existing_batch_id": existing_batch_id,
        "expires_at": expires_at,
    }

    sample_rows = [
        {
            "txn_time": t.txn_time,
            "direction": t.direction,
            "amount_cents": t.amount_cents,
            "amount_display": f"{t.amount_cents / 100:.2f}",
            "counterparty": t.counterparty,
            "description": t.description,
            "channel": t.channel,
        }
        for t in parse_result.transactions[:50]
    ]

    return ImportPreviewItem(
        token=token,
        file_name=file_name,
        source=resolved_source,
        profile_id=profile_id,
        existing_batch_id=existing_batch_id,
        total_rows=len(parse_result.transactions),
        need_password=False,
        need_mapping=False,
        accounts=[a.model_dump() for a in parse_result.accounts],
        self_checks=[sc.model_dump() for sc in parse_result.self_checks],
        rows=sample_rows,
        warnings=combined_warnings,
        expires_at=expires_at.isoformat(),
    )


@router.post("/commit", response_model=ImportCommitResponse)
def import_commit(
    payload: CommitRequest,
    db: sqlite3.Connection = Depends(get_db_conn),
):
    """Commit verified statement transactions into database, triggering dedup and classification pipelines.

    When re-importing an already imported file (matching file_sha1), atomically rolls back the
    previous batch (preserving any manual category/excluded markings by fingerprint) and commits
    the freshly parsed batch in its place.
    """
    item = _PREVIEW_STORE.pop(payload.token, None)
    if not item or datetime.now() > item["expires_at"]:
        raise AppException(status_code=410, code="IMPORT_TOKEN_EXPIRED", message="预览已过期，请重新上传解析")

    parse_result: ParseResult = item["parse_result"]
    source = item["source"]
    file_name = item["file_name"]
    file_sha1 = item["file_sha1"]
    file_size = item["file_size"]
    profile_id = item["profile_id"]

    if payload.account_overrides:
        ov = payload.account_overrides
        _apply_account_to_parse_result(
            parse_result,
            bank=ov.get("bank"),
            kind=ov.get("kind") or ov.get("account_kind"),
            tail=ov.get("tail") if "tail" in ov else ov.get("card_tail"),
            alias=ov.get("alias"),
            holder=ov.get("holder") or ov.get("holder_name"),
            currency=ov.get("currency"),
            override_explicit=True,
        )

    # 0. Duplicate Batch Replacement (re-parse & re-import atomically inside current DB transaction)
    replaced_batch_id: Optional[int] = None
    preserved_manual_state: Dict[str, Dict[str, Any]] = {}
    existing_batch = db.execute(
        "SELECT id FROM import_batches WHERE file_sha1 = ?",
        (file_sha1,),
    ).fetchone()
    if existing_batch:
        replaced_batch_id = int(existing_batch["id"])
        manual_rows = db.execute(
            """
            SELECT fingerprint, category_id, category_source, category_confidence, excluded, note
            FROM transactions
            WHERE batch_id = ?
              AND (
                COALESCE(category_source, '') = 'manual'
                OR excluded = 1
                OR COALESCE(TRIM(note), '') != ''
              )
            """,
            (replaced_batch_id,),
        ).fetchall()
        for mr in manual_rows:
            preserved_manual_state[mr["fingerprint"]] = {
                "category_id": mr["category_id"],
                "category_source": mr["category_source"],
                "category_confidence": mr["category_confidence"],
                "excluded": mr["excluded"],
                "note": mr["note"],
            }
        rollback_import_batch(db, replaced_batch_id, manage_transaction=False)

    # 1. Resolve & Upsert Accounts
    account_id_map: Dict[str, int] = {}
    for acc in parse_result.accounts:
        row = db.execute(
            "SELECT id FROM accounts WHERE kind = ? AND bank = ? AND tail IS ?",
            (acc.kind, acc.bank, acc.tail),
        ).fetchone()
        if row:
            acc_id = row["id"]
        else:
            cur = db.execute(
                "INSERT INTO accounts (kind, bank, tail, alias, holder, currency) VALUES (?, ?, ?, ?, ?, ?)",
                (acc.kind, acc.bank, acc.tail, acc.alias, acc.holder, acc.currency),
            )
            acc_id = cur.lastrowid
        key = f"{acc.kind}:{acc.bank}:{acc.tail or ''}"
        account_id_map[key] = acc_id

    # 2. Insert import_batches row
    start_d, end_d = parse_result.period if parse_result.period else (None, None)
    self_check_json = json.dumps([sc.model_dump() for sc in parse_result.self_checks], ensure_ascii=False)
    cur = db.execute(
        """
        INSERT INTO import_batches (file_name, file_sha1, file_size, source, profile_id, row_count, period_start, period_end, self_check_json, imported_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now', 'localtime'))
        """,
        (file_name, file_sha1, file_size, source, profile_id, len(parse_result.transactions), start_d, end_d, self_check_json),
    )
    batch_id = cur.lastrowid

    # 3. Assign fingerprints & insert transactions
    assign_fingerprints(parse_result.transactions)
    inserted_count = 0
    duplicate_count = 0
    fx_online_cache: Dict[Tuple[str, str], Optional[float]] = {}

    for t in parse_result.transactions:
        # Normalize transaction currency to canonical ISO code (R-BM4)
        curr = normalize_currency(t.currency, default="CNY") if t.currency else "CNY"

        # Resolve main account_id
        main_acc_id = t.account_id
        if not main_acc_id and t.account_ref:
            ref = t.account_ref
            key = f"{ref.kind}:{ref.bank or ''}:{ref.tail or ''}"
            main_acc_id = account_id_map.get(key)
            if not main_acc_id:
                row = db.execute("SELECT id FROM accounts WHERE bank = ? AND tail IS ?", (ref.bank, ref.tail)).fetchone()
                main_acc_id = row["id"] if row else None

        if not main_acc_id and parse_result.accounts:
            first_acc = parse_result.accounts[0]
            main_acc_id = account_id_map.get(f"{first_acc.kind}:{first_acc.bank}:{first_acc.tail or ''}")

        if not main_acc_id:
            fb_bank = (t.account_ref.bank if t.account_ref and t.account_ref.bank else None) or "未知银行/机构"
            fb_kind = (t.account_ref.kind if t.account_ref and t.account_ref.kind else None) or "bank_debit"
            fb_tail = t.account_ref.tail if t.account_ref else None
            row = db.execute(
                "SELECT id FROM accounts WHERE kind = ? AND bank = ? AND tail IS ?",
                (fb_kind, fb_bank, fb_tail),
            ).fetchone()
            if row:
                main_acc_id = row["id"]
            else:
                acc_cur = db.execute(
                    "INSERT INTO accounts (kind, bank, tail, alias, holder, currency) VALUES (?, ?, ?, ?, ?, ?)",
                    (fb_kind, fb_bank, fb_tail, f"{fb_bank} ({fb_tail})" if fb_tail else fb_bank, parse_result.holder_name, curr),
                )
                main_acc_id = acc_cur.lastrowid

        # Resolve funding account_id
        fund_acc_id = t.fund_account_id
        if not fund_acc_id and t.fund_account_ref:
            fref = t.fund_account_ref
            row = db.execute("SELECT id FROM accounts WHERE bank = ? AND tail IS ?", (fref.bank, fref.tail)).fetchone()
            fund_acc_id = row["id"] if row else None

        # Convert FX if foreign currency (M6 integration, memoized per batch)
        if curr != "CNY":
            amt_cny, fx_rate, fx_rate_at, fx_rate_source = convert_transaction(
                db, curr, t.amount_cents, t.txn_time, online_cache=fx_online_cache
            )
        else:
            amt_cny, fx_rate, fx_rate_at, fx_rate_source = t.amount_cents, None, None, None

        # Restore preserved manual classification/excluded/note when replacing a duplicate batch
        preserved = preserved_manual_state.get(t.fingerprint or "")
        cat_id = preserved["category_id"] if preserved and preserved.get("category_source") == "manual" else None
        cat_src = "manual" if preserved and preserved.get("category_source") == "manual" else None
        cat_conf = preserved["category_confidence"] if preserved and preserved.get("category_source") == "manual" else 1.0
        excluded_val = preserved["excluded"] if preserved else 0
        note_val = preserved["note"] if (preserved and preserved.get("note")) else t.note

        try:
            db.execute(
                """
                INSERT INTO transactions (
                    batch_id, source, account_id, fund_account_id, txn_time, time_precision, post_date,
                    amount_cents, currency, direction, counterparty, description, pay_method, channel,
                    merchant_norm, order_id, merchant_order_id, status, platform_category, raw_json,
                    fingerprint, category_id, category_source, category_confidence,
                    refund_of, refunded_cents, note, excluded,
                    amount_cny, fx_rate, fx_rate_at, fx_rate_source
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    batch_id, source, main_acc_id, fund_acc_id, t.txn_time, t.time_precision, t.post_date,
                    t.amount_cents, curr, t.direction, t.counterparty, t.description, t.pay_method,
                    t.channel, t.merchant_norm, t.order_id, t.merchant_order_id, t.status, t.platform_category,
                    t.raw_json, t.fingerprint, cat_id, cat_src, cat_conf,
                    None, 0, note_val, excluded_val,
                    amt_cny, fx_rate, fx_rate_at, fx_rate_source
                ),
            )
            inserted_count += 1
        except sqlite3.IntegrityError as exc:
            err_msg = str(exc).lower()
            if "unique constraint failed: transactions.fingerprint" in err_msg:
                duplicate_count += 1
            else:
                raise

    # Update batch row_count to reflect actual persisted transactions after L1 fingerprint dedup
    db.execute("UPDATE import_batches SET row_count = ? WHERE id = ?", (inserted_count, batch_id))

    # 4. Deduplication Pipeline (L2 refunds, L3 cross-source, L4 internal transfers)
    dedup_summary = run_dedup_pipeline(db, batch_id=batch_id, auto_commit=False)

    # 5. Classification Pipeline (with optional LLM fallback when enabled; keep outer transaction atomic)
    llm_cfg = LLMConfig.load_from_db(db)
    llm_client = (
        LLMClient(llm_cfg, conn=db, auto_commit=False)
        if (llm_cfg.enabled and llm_cfg.task_classify_enabled and llm_cfg.base_url.strip())
        else None
    )
    pipeline = ClassificationPipeline(db, llm_client=llm_client, auto_init_builtins=False)
    batch_txns = db.execute("SELECT * FROM transactions WHERE batch_id = ?", (batch_id,)).fetchall()
    _, class_summary = pipeline.classify_batch(batch_txns, update_db=True, auto_commit=False)

    logger.info(
        "Import commit completed: batch_id=%d replaced_batch_id=%s source=%s imported=%d duplicates=%d refunds=%d transfers=%d rule_hits=%d unclassified=%d",
        batch_id,
        replaced_batch_id if replaced_batch_id is not None else "-",
        source,
        inserted_count,
        duplicate_count + dedup_summary.duplicates_auto,
        dedup_summary.refunds_linked,
        dedup_summary.transfers_paired,
        class_summary.user_rule_matched + class_summary.builtin_rule_matched,
        class_summary.unclassified,
    )

    return ImportCommitResponse(
        batch_id=batch_id,
        replaced_batch_id=replaced_batch_id,
        imported_count=inserted_count,
        duplicate_count=duplicate_count + dedup_summary.duplicates_auto,
        refund_count=dedup_summary.refunds_linked,
        transfer_count=dedup_summary.transfers_paired,
        rule_hits=class_summary.user_rule_matched + class_summary.builtin_rule_matched,
        unclassified_count=class_summary.unclassified,
        summary=dedup_summary.model_dump(),
    )


@router.get("/batches", response_model=List[BatchItem])
def list_batches(
    db: sqlite3.Connection = Depends(get_db_conn),
):
    """List all historical import batches in descending order."""
    rows = db.execute("SELECT * FROM import_batches ORDER BY id DESC").fetchall()
    return [BatchItem(**dict(r)) for r in rows]


@router.delete("/batches/{batch_id}")
def rollback_batch(
    batch_id: int,
    db: sqlite3.Connection = Depends(get_db_conn),
):
    """Safely rollback an import batch, reverting deduplication and refund states."""
    existing = db.execute("SELECT id FROM import_batches WHERE id = ?", (batch_id,)).fetchone()
    if not existing:
        raise NotFoundError(f"导入批次 #{batch_id} 不存在")

    res = rollback_import_batch(db, batch_id)
    logger.info("Rolled back import batch: batch_id=%d", batch_id)
    return res
