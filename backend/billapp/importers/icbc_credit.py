"""Industrial and Commercial Bank of China (中国工商银行) Multi-Card Credit Card PDF Importer."""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Dict, List, Optional, Tuple

from billapp.core.models import (
    AccountCreate,
    AccountRef,
    ParseResult,
    SelfCheckResult,
    TransactionCreate,
)
from billapp.core.money import to_cents
from billapp.dedup.fingerprint import assign_fingerprints
from billapp.importers.base import FileProbe, Importer
from billapp.importers.common import (
    extract_channel_and_normalize,
    normalize_currency,
    parse_datetime,
)
from billapp.importers.pdf import (
    PDFPasswordRequiredError,
    extract_clean_text_spans,
    extract_tables_hybrid,
    open_pdf_document,
)


class ICBCCreditImporter(Importer):
    """Importer for ICBC Multi-Card Credit Card Electronic History Statements."""

    source_id = "icbc_credit"
    source_name = "工商银行信用卡"

    def detect(self, probe: FileProbe) -> float:
        """Detect if the file is an ICBC Credit Card PDF."""
        if not probe.is_pdf():
            return 0.0

        fname = probe.file_name.lower()
        has_icbc_hint = "工商银行" in probe.file_name or "icbc" in fname

        try:
            with open_pdf_document(probe) as doc:
                first_page_text = doc[0].get_text() if len(doc) > 0 else ""
                if "中国工商银行信用卡历史明细" in first_page_text or "活期历史明细清单" in first_page_text:
                    return 1.0
                if "中国工商银行" in first_page_text and ("卡号:" in first_page_text or "卡号：" in first_page_text or "交易卡号" in first_page_text):
                    return 0.95
                if "中国工商银行" in first_page_text:
                    return 0.8
                return 0.5 if has_icbc_hint else 0.0
        except PDFPasswordRequiredError:
            return 0.85 if has_icbc_hint else 0.2
        except Exception:
            return 0.0

    def parse(self, probe: FileProbe) -> ParseResult:
        """Parse ICBC Multi-Card Statement with Watermark Stripping and Per-Page Footer Self-Checks."""
        transactions: List[TransactionCreate] = []
        accounts_map: Dict[str, AccountCreate] = {}
        self_checks: List[SelfCheckResult] = []

        holder_name: Optional[str] = None
        period_start: Optional[str] = None
        period_end: Optional[str] = None

        total_parsed_debits = Decimal(0)
        total_parsed_credits = Decimal(0)
        total_expected_debits = Decimal(0)
        total_expected_credits = Decimal(0)
        total_expected_tx = 0

        with open_pdf_document(probe) as doc:
            for page_idx, page in enumerate(doc):
                pno = page_idx + 1

                # Clean spans for text extraction
                clean_spans = extract_clean_text_spans(page, strip_watermark=True)
                full_text = " ".join(s.get("text", "") for s in clean_spans)
                # Fallback to page.get_text() for unstripped metadata if needed
                raw_page_text = page.get_text()
                combined_text = full_text + "\n" + raw_page_text

                # 1. Preamble Metadata
                if not holder_name:
                    m_h = re.search(r"户名[：:]\s*([^\s\n]+)", combined_text) or re.search(r"客户姓名[：:]\s*([^\s\n]+)", combined_text)
                    if m_h:
                        holder_name = m_h.group(1).strip()

                if not period_start:
                    m_p = re.search(r"起止日期[：:]\s*(\d{4}-\d{2}-\d{2})\s*[—至~-]\s*(\d{4}-\d{2}-\d{2})", combined_text)
                    if m_p:
                        period_start, period_end = m_p.group(1), m_p.group(2)

                # 2. Per-Page Footer Self-Check Expected Metrics
                m_tx = re.search(r"本页交易笔数[：:]\s*(\d+)", combined_text)
                m_exp = re.search(r"本页支出算术合计[：:]\s*([\d\.,]+)", combined_text)
                m_inc = re.search(r"本页收入算术合计[：:]\s*([\d\.,]+)", combined_text)

                page_expected_tx = int(m_tx.group(1)) if m_tx else None
                page_expected_exp = Decimal(m_exp.group(1).replace(",", "")) if m_exp else None
                page_expected_inc = Decimal(m_inc.group(1).replace(",", "")) if m_inc else None

                if page_expected_tx is not None:
                    total_expected_tx += page_expected_tx
                if page_expected_exp is not None:
                    total_expected_debits += page_expected_exp
                if page_expected_inc is not None:
                    total_expected_credits += page_expected_inc

                # 3. Table Extraction via Hybrid Strategy (Watermarks Discarded)
                tables = extract_tables_hybrid(page, strip_watermark=True)
                if not tables:
                    continue

                table = tables[0]
                if len(table) < 2:
                    continue

                # Header index resolution
                header_row = [c.replace("\n", "").strip() for c in table[0]]
                col_map = {name: idx for idx, name in enumerate(header_row)}

                def get_val(row_cells: List[str], col_name: str, fallback_idx: int) -> str:
                    # Look up by exact name or substring
                    for k, idx in col_map.items():
                        if col_name in k or k in col_name:
                            if 0 <= idx < len(row_cells):
                                return row_cells[idx].strip()
                    if 0 <= fallback_idx < len(row_cells):
                        return row_cells[fallback_idx].strip()
                    return ""

                page_tx_count = 0
                page_actual_debit = Decimal(0)
                page_actual_credit = Decimal(0)

                for r in table[1:]:
                    if len(r) < 10:
                        continue

                    dt_raw = get_val(r, "入账日期", 0)
                    card_raw = get_val(r, "交易卡号", 1)
                    dir_raw = get_val(r, "收支", 2)
                    tx_curr = get_val(r, "交易币种", 3)
                    tx_amt_s = get_val(r, "交易金额", 4)
                    post_curr = get_val(r, "入账币种", 5)
                    post_amt_s = get_val(r, "入账金额", 6)
                    bal_s = get_val(r, "账户余额", 7)
                    cp_name = get_val(r, "对方户名", 8)
                    cp_acc = get_val(r, "对方账号", 9)
                    summary = get_val(r, "摘要", 10)
                    merchant = get_val(r, "交易场所", 11)

                    # Validate transaction row
                    dt_clean = dt_raw.replace("\n", " ").strip()
                    if not dt_clean or not re.search(r"\d{4}-\d{2}-\d{2}", dt_clean):
                        continue
                    if dir_raw not in ("借", "贷"):
                        continue

                    # Card tail extraction
                    m_tail = re.search(r"(\d{4})$", card_raw.strip()) or re.search(r"(\d{4})", card_raw.strip())
                    card_tail = m_tail.group(1) if m_tail else "0000"

                    # Currency handling (supports CNY, USD, EUR, HKD, JPY, GBP, etc.)
                    raw_curr = post_curr or tx_curr
                    currency = normalize_currency(raw_curr, default="CNY")

                    # Amount parsing
                    amt_clean = post_amt_s or tx_amt_s
                    if not amt_clean:
                        continue
                    try:
                        amt_dec = Decimal(amt_clean.replace(",", ""))
                    except Exception:
                        continue

                    if not summary and merchant:
                        for _pfx in ("自动还款", "跨行还款", "转账还款", "转账", "转帐", "还款", "消费", "退货", "退款"):
                            if merchant.startswith(_pfx):
                                summary = _pfx
                                merchant = merchant[len(_pfx):].strip()
                                break

                    # Direction mapping
                    is_transfer = any(kw in summary for kw in ("转账", "转帐", "还款", "自动还款", "跨行还款"))
                    is_refund = any(kw in summary for kw in ("退货", "退款"))

                    if dir_raw == "借":
                        page_actual_debit += amt_dec
                        if is_transfer:
                            direction = "transfer"
                        else:
                            direction = "expense"
                    else:  # 贷
                        page_actual_credit += amt_dec
                        if is_refund:
                            direction = "refund"
                        elif is_transfer:
                            direction = "transfer"
                        else:
                            direction = "income"

                    amount_cents = to_cents(abs(amt_dec))
                    iso_time, precision = parse_datetime(dt_clean)

                    raw_counterparty = cp_name or merchant or "工商银行"
                    # Clean newlines from counterparty and merchant
                    raw_counterparty = raw_counterparty.replace("\n", "").strip()
                    detected_ch, merchant_norm = extract_channel_and_normalize(raw_counterparty)

                    # Account mapping for multi-card credit card
                    if card_tail not in accounts_map:
                        if currency == "USD":
                            alias = f"工行Visa{card_tail}"
                        else:
                            alias = f"工行信用卡{card_tail}"

                        accounts_map[card_tail] = AccountCreate(
                            kind="bank_credit",
                            bank="工商银行",
                            tail=card_tail,
                            alias=alias,
                            holder=holder_name,
                            currency=currency,
                        )

                    account_ref = AccountRef(
                        kind="bank_credit",
                        bank="工商银行",
                        tail=card_tail,
                        holder=holder_name,
                    )

                    txn = TransactionCreate(
                        source="icbc_credit",
                        txn_time=iso_time,
                        time_precision=precision,
                        post_date=dt_clean.split(" ")[0],
                        amount_cents=amount_cents,
                        currency=currency,
                        direction=direction,
                        counterparty=raw_counterparty,
                        merchant_norm=merchant_norm,
                        description=summary,
                        pay_method=f"工商银行信用卡({card_tail})",
                        channel=detected_ch,
                        status="交易成功",
                        account_ref=account_ref,
                    )
                    transactions.append(txn)
                    page_tx_count += 1

                # Page metrics accumulation
                total_parsed_debits += page_actual_debit
                total_parsed_credits += page_actual_credit

                # Per-page Self-Checks
                if page_expected_tx is not None:
                    passed = (page_tx_count == page_expected_tx)
                    self_checks.append(
                        SelfCheckResult(
                            name=f"第{pno}页交易笔数自检",
                            expected=page_expected_tx,
                            actual=page_tx_count,
                            passed=passed,
                            description=f"第{pno}页交易笔数吻合",
                        )
                    )

                if page_expected_exp is not None:
                    dev = abs(page_actual_debit - page_expected_exp)
                    passed = (dev < Decimal("0.01"))
                    self_checks.append(
                        SelfCheckResult(
                            name=f"第{pno}页支出合计自检",
                            expected=float(page_expected_exp),
                            actual=float(page_actual_debit),
                            passed=passed,
                            deviation=float(dev),
                            description=f"第{pno}页支出金额吻合",
                        )
                    )

                if page_expected_inc is not None:
                    dev = abs(page_actual_credit - page_expected_inc)
                    passed = (dev < Decimal("0.01"))
                    self_checks.append(
                        SelfCheckResult(
                            name=f"第{pno}页收入合计自检",
                            expected=float(page_expected_inc),
                            actual=float(page_actual_credit),
                            passed=passed,
                            deviation=float(dev),
                            description=f"第{pno}页收入金额吻合",
                        )
                    )

        # 4. Document-Level Cumulative Self-Checks
        if total_expected_tx > 0:
            self_checks.append(
                SelfCheckResult(
                    name="全文件交易总笔数自检",
                    expected=total_expected_tx,
                    actual=len(transactions),
                    passed=(len(transactions) == total_expected_tx),
                    description="全文件交易总笔数吻合",
                )
            )

        if total_expected_debits > 0:
            dev = abs(total_parsed_debits - total_expected_debits)
            self_checks.append(
                SelfCheckResult(
                    name="全文件支出合计自检",
                    expected=float(total_expected_debits),
                    actual=float(total_parsed_debits),
                    passed=(dev < Decimal("0.01")),
                    deviation=float(dev),
                    description="全文件支出总金额吻合",
                )
            )

        if total_expected_credits > 0:
            dev = abs(total_parsed_credits - total_expected_credits)
            self_checks.append(
                SelfCheckResult(
                    name="全文件收入合计自检",
                    expected=float(total_expected_credits),
                    actual=float(total_parsed_credits),
                    passed=(dev < Decimal("0.01")),
                    deviation=float(dev),
                    description="全文件收入总金额吻合",
                )
            )

        # 5. Fingerprint Assignment
        assign_fingerprints(transactions)

        period = (period_start, period_end) if period_start and period_end else None

        return ParseResult(
            transactions=transactions,
            accounts=list(accounts_map.values()),
            holder_name=holder_name,
            period=period,
            self_checks=self_checks,
            warnings=[],
        )
