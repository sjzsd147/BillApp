"""Bank of China (中国银行) Debit Card PDF Importer."""

from __future__ import annotations

import re
from decimal import Decimal
from typing import List, Optional, Tuple

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
    parse_datetime,
)
from billapp.importers.pdf import (
    PDFPasswordRequiredError,
    extract_tables_strategy_a,
    open_pdf_document,
)


class BOCDebitImporter(Importer):
    """Importer for Bank of China Personal Debit Card Statement PDFs."""

    source_id = "boc_debit"
    source_name = "中国银行借记卡"

    def detect(self, probe: FileProbe) -> float:
        """Detect if the file is a BOC Debit Card PDF."""
        if not probe.is_pdf():
            return 0.0

        fname = probe.file_name.lower()
        has_boc_hint = "中国银行" in probe.file_name or "boc" in fname

        try:
            with open_pdf_document(probe) as doc:
                first_page_text = doc[0].get_text() if len(doc) > 0 else ""
                if "中国银行交易流水明细清单" in first_page_text:
                    return 1.0
                if "中国银行" in first_page_text and "借记卡号" in first_page_text:
                    return 0.95
                if "中国银行" in first_page_text:
                    return 0.8
                return 0.5 if has_boc_hint else 0.0
        except PDFPasswordRequiredError:
            return 0.85 if has_boc_hint else 0.2
        except Exception:
            return 0.0

    def parse(self, probe: FileProbe) -> ParseResult:
        """Parse BOC Debit Card PDF into transactions, account, and self-checks."""
        transactions: List[TransactionCreate] = []
        holder_name: Optional[str] = None
        card_tail: Optional[str] = None
        period_start: Optional[str] = None
        period_end: Optional[str] = None

        expected_row_count: Optional[int] = None
        expected_debit_sum: Optional[Decimal] = None
        expected_credit_sum: Optional[Decimal] = None

        actual_debit_sum = Decimal(0)
        actual_credit_sum = Decimal(0)

        with open_pdf_document(probe) as doc:
            for page in doc:
                page_text = page.get_text()

                # 1. Preamble Header Metadata Extraction
                if not holder_name:
                    m_holder = re.search(r"客户姓名[：:]\s*([^\s\n]+)", page_text)
                    if m_holder:
                        holder_name = m_holder.group(1).strip()

                if not card_tail:
                    m_card = re.search(r"借记卡号[：:]\s*(\d+)", page_text)
                    if m_card:
                        full_card = m_card.group(1).strip()
                        card_tail = full_card[-4:]

                if not period_start:
                    m_period = re.search(r"交易区间[：:]\s*(\d{4}-\d{2}-\d{2})\s*至\s*(\d{4}-\d{2}-\d{2})", page_text)
                    if m_period:
                        period_start, period_end = m_period.group(1), m_period.group(2)

                if expected_row_count is None:
                    m_rows = re.search(r"行数[：:]\s*(\d+)", page_text)
                    if m_rows:
                        expected_row_count = int(m_rows.group(1))

                if expected_debit_sum is None:
                    m_deb = re.search(r"借方发生数[：:]\s*([\d,]+\.\d{2})", page_text)
                    if m_deb:
                        expected_debit_sum = Decimal(m_deb.group(1).replace(",", ""))

                if expected_credit_sum is None:
                    m_cred = re.search(r"贷方发生数[：:]\s*([\d,]+\.\d{2})", page_text)
                    if m_cred:
                        expected_credit_sum = Decimal(m_cred.group(1).replace(",", ""))

                # 2. Table Extraction via Strategy A
                tables = extract_tables_strategy_a(page)
                for table in tables:
                    if len(table) < 2:
                        continue

                    # Check headers
                    header = [c.replace("\n", "").strip() for c in table[0]]
                    col_map = {name: idx for idx, name in enumerate(header)}

                    for row in table[1:]:
                        if len(row) < 10:
                            continue

                        # Helper to get cell value
                        def get_cell(col_name: str, fallback_idx: int) -> str:
                            idx = col_map.get(col_name, fallback_idx)
                            if 0 <= idx < len(row):
                                return row[idx].replace("\n", "").strip()
                            return ""

                        post_date = get_cell("记账日期", 0)
                        post_time = get_cell("记账时间", 1)
                        curr_str = get_cell("币别", 2)
                        amt_str = get_cell("金额", 3)
                        bal_str = get_cell("余额", 4)
                        txn_name = get_cell("交易名称", 5)
                        channel_s = get_cell("渠道", 6)
                        branch = get_cell("网点名称", 7)
                        note = get_cell("附言", 8)
                        cp_name = get_cell("对方账户名", 9)
                        cp_acc = get_cell("对方卡号/账号", 10)
                        cp_bank = get_cell("对方开户行", 11)

                        if not post_date or not amt_str:
                            continue

                        try:
                            amt_dec = Decimal(amt_str.replace(",", ""))
                        except Exception:
                            continue

                        if amt_dec < 0:
                            actual_debit_sum += abs(amt_dec)
                            if any(kw in txn_name or kw in note for kw in ("转账", "转帐")):
                                direction = "transfer"
                            else:
                                direction = "expense"
                        else:
                            actual_credit_sum += amt_dec
                            if "退款" in txn_name or "退款" in note:
                                direction = "refund"
                            elif any(kw in txn_name or kw in note for kw in ("转账", "转帐")):
                                direction = "transfer"
                            else:
                                direction = "income"

                        amount_cents = to_cents(abs(amt_dec))
                        dt_input = f"{post_date} {post_time}".strip() if post_time else post_date
                        iso_time, precision = parse_datetime(dt_input)
                        currency = "CNY" if ("人民币" in curr_str or not curr_str) else curr_str

                        raw_counterparty = cp_name or note or cp_bank or "中国银行"
                        detected_ch, merchant_norm = extract_channel_and_normalize(raw_counterparty)

                        account_ref = AccountRef(
                            kind="bank_debit",
                            bank="中国银行",
                            tail=card_tail,
                            holder=holder_name,
                        )

                        txn = TransactionCreate(
                            source="boc_debit",
                            txn_time=iso_time,
                            time_precision=precision,
                            post_date=post_date,
                            amount_cents=amount_cents,
                            currency=currency,
                            direction=direction,
                            counterparty=raw_counterparty,
                            merchant_norm=merchant_norm,
                            description=txn_name,
                            pay_method=f"中国银行借记卡({card_tail})" if card_tail else "中国银行借记卡",
                            channel=detected_ch,
                            status="交易成功",
                            account_ref=account_ref,
                        )
                        transactions.append(txn)

        # 3. Create Account
        accounts: List[AccountCreate] = []
        if card_tail:
            accounts.append(
                AccountCreate(
                    kind="bank_debit",
                    bank="中国银行",
                    tail=card_tail,
                    alias=f"中行借记卡{card_tail}",
                    holder=holder_name,
                    currency="CNY",
                )
            )

        # 4. Fingerprint Assignment
        assign_fingerprints(transactions)

        # 5. Build Self-Check Results
        self_checks: List[SelfCheckResult] = []
        if expected_row_count is not None:
            passed = (len(transactions) == expected_row_count)
            self_checks.append(
                SelfCheckResult(
                    name="交易笔数自检",
                    expected=expected_row_count,
                    actual=len(transactions),
                    passed=passed,
                    description=f"账单行数: {expected_row_count}, 实际解析: {len(transactions)}",
                )
            )

        if expected_debit_sum is not None:
            dev = abs(actual_debit_sum - expected_debit_sum)
            passed = (dev < Decimal("0.01"))
            self_checks.append(
                SelfCheckResult(
                    name="借方发生额汇总自检",
                    expected=float(expected_debit_sum),
                    actual=float(actual_debit_sum),
                    passed=passed,
                    deviation=float(dev),
                    description="借方支出累计金额吻合",
                )
            )

        if expected_credit_sum is not None:
            dev = abs(actual_credit_sum - expected_credit_sum)
            passed = (dev < Decimal("0.01"))
            self_checks.append(
                SelfCheckResult(
                    name="贷方发生额汇总自检",
                    expected=float(expected_credit_sum),
                    actual=float(actual_credit_sum),
                    passed=passed,
                    deviation=float(dev),
                    description="贷方收入/退款累计金额吻合",
                )
            )

        period = (period_start, period_end) if period_start and period_end else None

        return ParseResult(
            transactions=transactions,
            accounts=accounts,
            holder_name=holder_name,
            period=period,
            self_checks=self_checks,
            warnings=[],
        )
