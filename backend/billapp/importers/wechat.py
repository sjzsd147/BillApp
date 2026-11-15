"""WeChat Pay XLSX and CSV Parser with 80-Row Self-Check Reconciliation."""

from __future__ import annotations

import csv
import io
import re
from typing import Any, Dict, List, Optional, Tuple

import openpyxl

from billapp.core.models import (
    AccountCreate,
    AccountRef,
    ParseResult,
    SelfCheckResult,
    TransactionCreate,
)
from billapp.core.money import to_cents, to_display
from billapp.dedup.fingerprint import assign_fingerprints
from billapp.dedup.refund import link_in_memory_refunds
from billapp.importers.base import FileProbe, Importer
from billapp.importers.common import (
    detect_encoding,
    extract_channel_and_normalize,
    parse_datetime,
    parse_funding_channel,
    sanitize_order_id,
)


class WeChatImporter(Importer):
    """Importer for WeChat Pay transaction statements (XLSX or CSV)."""

    source_id = "wechat"
    source_name = "微信支付"

    def detect(self, probe: FileProbe) -> float:
        """Evaluate if the file is a WeChat Pay statement."""
        if probe.is_pdf():
            return 0.0

        if probe.is_excel():
            try:
                wb = openpyxl.load_workbook(io.BytesIO(probe.content), read_only=True)
                found = False
                for ws in wb.worksheets:
                    sample_text = ""
                    for r in range(1, 25):
                        val = ws.cell(r, 1).value
                        if val:
                            sample_text += str(val) + " "
                    if "微信支付" in sample_text or "微信昵称" in sample_text:
                        found = True
                        break
                wb.close()
                if found:
                    return 1.0
            except Exception:
                pass
            return 0.0

        # Check CSV
        enc = detect_encoding(probe.content)
        sample_lines = probe.read_text(encoding=enc, max_lines=30)
        sample_text = "".join(sample_lines)

        score = 0.0
        if "微信支付" in sample_text or "wechat" in sample_text.lower():
            score += 0.5
        if "微信昵称" in sample_text or "微信支付账单明细" in sample_text:
            score += 0.3
        if "交易单号" in sample_text and "收/支" in sample_text:
            score += 0.2

        return min(1.0, score)

    def parse(self, probe: FileProbe) -> ParseResult:
        """Parse WeChat Pay statement from XLSX or CSV."""
        if probe.is_excel():
            return self._parse_xlsx(probe)
        return self._parse_csv(probe)

    def _parse_xlsx(self, probe: FileProbe) -> ParseResult:
        wb = openpyxl.load_workbook(io.BytesIO(probe.content), data_only=True)

        target_ws = None
        header_row_idx = -1
        holder_name: Optional[str] = None
        period_start: Optional[str] = None
        period_end: Optional[str] = None
        total_declared_count: Optional[int] = None
        inc_declared_count: Optional[int] = None
        inc_declared_cents: Optional[int] = None
        exp_declared_count: Optional[int] = None
        exp_declared_cents: Optional[int] = None
        neu_declared_count: Optional[int] = None
        neu_declared_cents: Optional[int] = None

        for ws_candidate in wb.worksheets:
            h_idx = -1
            cand_holder = None
            cand_start = None
            cand_end = None
            cand_tot = None
            cand_inc_cnt = None
            cand_inc_cents = None
            cand_exp_cnt = None
            cand_exp_cents = None
            cand_neu_cnt = None
            cand_neu_cents = None

            for r in range(1, 25):
                val = str(ws_candidate.cell(r, 1).value or "").strip()
                if not val:
                    continue

                if "微信昵称：" in val:
                    m = re.search(r"微信昵称：\s*\[(.*?)\]", val)
                    if m:
                        cand_holder = m.group(1).strip()
                elif "起始时间：" in val and "终止时间：" in val:
                    m = re.search(r"起始时间：\s*\[(.*?)\]\s*终止时间：\s*\[(.*?)\]", val)
                    if m:
                        cand_start, _ = parse_datetime(m.group(1))
                        cand_end, _ = parse_datetime(m.group(2))
                elif re.search(r"共\s*(\d+)\s*笔记录", val):
                    m = re.search(r"共\s*(\d+)\s*笔记录", val)
                    if m:
                        cand_tot = int(m.group(1))
                elif "收入：" in val:
                    m = re.search(r"收入：\s*(\d+)\s*笔\s*([\d\.,]+)\s*元", val)
                    if m:
                        cand_inc_cnt = int(m.group(1))
                        cand_inc_cents = to_cents(m.group(2))
                elif "支出：" in val:
                    m = re.search(r"支出：\s*(\d+)\s*笔\s*([\d\.,]+)\s*元", val)
                    if m:
                        cand_exp_cnt = int(m.group(1))
                        cand_exp_cents = to_cents(m.group(2))
                elif "中性交易：" in val:
                    m = re.search(r"中性交易：\s*(\d+)\s*笔\s*([\d\.,]+)\s*元", val)
                    if m:
                        cand_neu_cnt = int(m.group(1))
                        cand_neu_cents = to_cents(m.group(2))

                cell_vals = [str(ws_candidate.cell(r, c).value or "").strip() for c in range(1, 12)]
                if "交易时间" in cell_vals and "交易类型" in cell_vals and "收/支" in cell_vals:
                    h_idx = r
                    break

            if h_idx != -1:
                target_ws = ws_candidate
                header_row_idx = h_idx
                holder_name = cand_holder
                period_start = cand_start
                period_end = cand_end
                total_declared_count = cand_tot
                inc_declared_count = cand_inc_cnt
                inc_declared_cents = cand_inc_cents
                exp_declared_count = cand_exp_cnt
                exp_declared_cents = cand_exp_cents
                neu_declared_count = cand_neu_cnt
                neu_declared_cents = cand_neu_cents
                break

        if target_ws is None or header_row_idx == -1:
            wb.close()
            raise ValueError("Failed to locate WeChat table header row in XLSX")

        ws = target_ws

        # Map header columns
        col_names = [str(ws.cell(header_row_idx, c).value or "").strip() for c in range(1, 12)]
        col_map: Dict[str, int] = {}
        for c_idx, name in enumerate(col_names, start=1):
            if name:
                col_map[name] = c_idx

        # Extract rows
        matrix_rows: List[Dict[str, str]] = []
        for r in range(header_row_idx + 1, ws.max_row + 1):
            time_val = str(ws.cell(r, col_map.get("交易时间", 1)).value or "").strip()
            if not time_val:
                continue

            row_dict: Dict[str, str] = {}
            for col_name, c_idx in col_map.items():
                val_cell = ws.cell(r, c_idx).value
                row_dict[col_name] = str(val_cell or "").strip()
            matrix_rows.append(row_dict)

        wb.close()

        return self._build_result(
            matrix_rows=matrix_rows,
            holder_name=holder_name,
            period_start=period_start,
            period_end=period_end,
            total_declared=total_declared_count,
            exp_declared_cnt=exp_declared_count,
            exp_declared_cents=exp_declared_cents,
            inc_declared_cnt=inc_declared_count,
            inc_declared_cents=inc_declared_cents,
            neu_declared_cnt=neu_declared_count,
            neu_declared_cents=neu_declared_cents,
        )

    def _parse_csv(self, probe: FileProbe) -> ParseResult:
        enc = detect_encoding(probe.content)
        raw_lines = probe.read_text(encoding=enc)

        holder_name: Optional[str] = None
        period_start: Optional[str] = None
        period_end: Optional[str] = None
        total_declared_count: Optional[int] = None
        inc_declared_count: Optional[int] = None
        inc_declared_cents: Optional[int] = None
        exp_declared_count: Optional[int] = None
        exp_declared_cents: Optional[int] = None
        neu_declared_count: Optional[int] = None
        neu_declared_cents: Optional[int] = None

        header_row_idx = -1

        for i, line in enumerate(raw_lines):
            line_str = line.strip()
            if "微信昵称：" in line_str:
                m = re.search(r"微信昵称：\s*\[(.*?)\]", line_str)
                if m:
                    holder_name = m.group(1).strip()
            elif "起始时间：" in line_str and "终止时间：" in line_str:
                m = re.search(r"起始时间：\s*\[(.*?)\]\s*终止时间：\s*\[(.*?)\]", line_str)
                if m:
                    period_start, _ = parse_datetime(m.group(1))
                    period_end, _ = parse_datetime(m.group(2))
            elif re.search(r"共\s*(\d+)\s*笔记录", line_str):
                m = re.search(r"共\s*(\d+)\s*笔记录", line_str)
                if m:
                    total_declared_count = int(m.group(1))
            elif "收入：" in line_str:
                m = re.search(r"收入：\s*(\d+)\s*笔\s*([\d\.,]+)\s*元", line_str)
                if m:
                    inc_declared_count = int(m.group(1))
                    inc_declared_cents = to_cents(m.group(2))
            elif "支出：" in line_str:
                m = re.search(r"支出：\s*(\d+)\s*笔\s*([\d\.,]+)\s*元", line_str)
                if m:
                    exp_declared_count = int(m.group(1))
                    exp_declared_cents = to_cents(m.group(2))
            elif "中性交易：" in line_str:
                m = re.search(r"中性交易：\s*(\d+)\s*笔\s*([\d\.,]+)\s*元", line_str)
                if m:
                    neu_declared_count = int(m.group(1))
                    neu_declared_cents = to_cents(m.group(2))
            elif "交易时间" in line_str and "交易类型" in line_str and "收/支" in line_str:
                header_row_idx = i
                break

        if header_row_idx == -1:
            raise ValueError("Failed to locate WeChat table header row in CSV")

        csv_reader = csv.reader(raw_lines[header_row_idx:])
        header_cols = [c.strip() for c in next(csv_reader)]
        col_map: Dict[str, int] = {name: idx for idx, name in enumerate(header_cols) if name}

        matrix_rows: List[Dict[str, str]] = []
        for row in csv_reader:
            if not row or not row[0].strip() or row[0].strip().startswith("---"):
                continue
            row_dict = {}
            for col_name, idx in col_map.items():
                if idx < len(row):
                    row_dict[col_name] = row[idx].strip()
                else:
                    row_dict[col_name] = ""
            if row_dict.get("交易时间"):
                matrix_rows.append(row_dict)

        return self._build_result(
            matrix_rows=matrix_rows,
            holder_name=holder_name,
            period_start=period_start,
            period_end=period_end,
            total_declared=total_declared_count,
            exp_declared_cnt=exp_declared_count,
            exp_declared_cents=exp_declared_cents,
            inc_declared_cnt=inc_declared_count,
            inc_declared_cents=inc_declared_cents,
            neu_declared_cnt=neu_declared_count,
            neu_declared_cents=neu_declared_cents,
        )

    def _build_result(
        self,
        matrix_rows: List[Dict[str, str]],
        holder_name: Optional[str],
        period_start: Optional[str],
        period_end: Optional[str],
        total_declared: Optional[int],
        exp_declared_cnt: Optional[int],
        exp_declared_cents: Optional[int],
        inc_declared_cnt: Optional[int],
        inc_declared_cents: Optional[int],
        neu_declared_cnt: Optional[int],
        neu_declared_cents: Optional[int],
    ) -> ParseResult:
        transactions: List[TransactionCreate] = []
        accounts_map: Dict[str, AccountCreate] = {}
        warnings: List[str] = []

        # WeChat Wallet Account
        main_wallet = AccountCreate(
            kind="wallet",
            bank="微信支付",
            tail=None,
            alias="微信零钱/零钱通",
            holder=holder_name,
            currency="CNY",
        )
        accounts_map["wallet_wechat"] = main_wallet

        raw_exp_cents = 0
        raw_exp_count = 0
        raw_inc_cents = 0
        raw_inc_count = 0
        raw_neu_cents = 0
        raw_neu_count = 0

        for r in matrix_rows:
            raw_time = r.get("交易时间", "")
            iso_time, precision = parse_datetime(raw_time)
            txn_type = r.get("交易类型", "")
            counterparty = r.get("交易对方", "")
            goods = r.get("商品", "")
            dir_raw = r.get("收/支", "")
            amt_raw = r.get("金额(元)", "")
            pay_method = r.get("支付方式", "")
            status = r.get("当前状态", "")
            order_id = sanitize_order_id(r.get("交易单号", ""))
            merchant_order_id = sanitize_order_id(r.get("商户单号", ""))
            note = r.get("备注", "")

            amount_cents = to_cents(amt_raw)
            channel, merchant_norm = extract_channel_and_normalize(counterparty)

            # Determine direction
            if dir_raw == "支出":
                raw_exp_count += 1
                raw_exp_cents += amount_cents
                if status in ("已全额退款", "已退款"):
                    direction = "neutral"
                else:
                    direction = "expense"
            elif dir_raw == "/":
                raw_neu_count += 1
                raw_neu_cents += amount_cents
                direction = "transfer"
            elif dir_raw == "收入":
                raw_inc_count += 1
                raw_inc_cents += amount_cents
                if "退款" in txn_type or "退款" in status:
                    direction = "refund"
                else:
                    direction = "income"
            else:
                direction = "neutral"

            # Parse funding channel
            fund_ref: Optional[AccountRef] = None
            fund_info = parse_funding_channel(pay_method)
            if fund_info:
                f_bank, f_tail, f_kind = fund_info
                fund_ref = AccountRef(kind=f_kind, bank=f_bank, tail=f_tail)
                key = f"{f_kind}_{f_bank}_{f_tail}"
                if key not in accounts_map:
                    accounts_map[key] = AccountCreate(
                        kind=f_kind,
                        bank=f_bank,
                        tail=f_tail,
                        alias=f"{f_bank}{f_tail}",
                        holder=holder_name,
                        currency="CNY",
                    )

            txn = TransactionCreate(
                source="wechat",
                txn_time=iso_time,
                time_precision=precision,
                amount_cents=amount_cents,
                currency="CNY",
                direction=direction,
                counterparty=counterparty or None,
                description=goods or txn_type or None,
                pay_method=pay_method or None,
                channel=channel,
                merchant_norm=merchant_norm or counterparty,
                order_id=order_id or None,
                merchant_order_id=merchant_order_id or None,
                status=status or None,
                platform_category=txn_type or None,
                note=note or None,
                account_ref=AccountRef(kind="wallet", bank="微信支付"),
                fund_account_ref=fund_ref,
            )
            transactions.append(txn)

        # L1 Fingerprint & L2 In-Memory Refund Linking
        assign_fingerprints(transactions)
        link_in_memory_refunds(transactions)

        # Self-Check validation
        self_checks: List[SelfCheckResult] = []

        total_rows = len(transactions)
        if total_declared is not None:
            self_checks.append(
                SelfCheckResult(
                    name="总笔数自检",
                    expected=total_declared,
                    actual=total_rows,
                    passed=(total_declared == total_rows),
                    description="微信支付账单总行数校验",
                )
            )

        if exp_declared_cents is not None:
            self_checks.append(
                SelfCheckResult(
                    name="支出金额自检",
                    expected=exp_declared_cents,
                    actual=raw_exp_cents,
                    passed=(exp_declared_cents == raw_exp_cents),
                    deviation=(raw_exp_cents - exp_declared_cents) / 100.0,
                    description=f"微信表头支出金额自检 ({to_display(raw_exp_cents)}元)",
                )
            )

        if exp_declared_cnt is not None:
            self_checks.append(
                SelfCheckResult(
                    name="支出笔数自检",
                    expected=exp_declared_cnt,
                    actual=raw_exp_count,
                    passed=(exp_declared_cnt == raw_exp_count),
                    description="微信表头支出笔数校验",
                )
            )

        if inc_declared_cents is not None:
            self_checks.append(
                SelfCheckResult(
                    name="收入金额自检",
                    expected=inc_declared_cents,
                    actual=raw_inc_cents,
                    passed=(inc_declared_cents == raw_inc_cents),
                    deviation=(raw_inc_cents - inc_declared_cents) / 100.0,
                    description=f"微信表头收入金额自检 ({to_display(raw_inc_cents)}元)",
                )
            )

        if inc_declared_cnt is not None:
            self_checks.append(
                SelfCheckResult(
                    name="收入笔数自检",
                    expected=inc_declared_cnt,
                    actual=raw_inc_count,
                    passed=(inc_declared_cnt == raw_inc_count),
                    description="微信表头收入笔数校验",
                )
            )

        if neu_declared_cents is not None:
            self_checks.append(
                SelfCheckResult(
                    name="中性交易金额自检",
                    expected=neu_declared_cents,
                    actual=raw_neu_cents,
                    passed=(neu_declared_cents == raw_neu_cents),
                    deviation=(raw_neu_cents - neu_declared_cents) / 100.0,
                    description=f"微信表中性交易金额自检 ({to_display(raw_neu_cents)}元)",
                )
            )

        if neu_declared_cnt is not None:
            self_checks.append(
                SelfCheckResult(
                    name="中性交易笔数自检",
                    expected=neu_declared_cnt,
                    actual=raw_neu_count,
                    passed=(neu_declared_cnt == raw_neu_count),
                    description="微信表中性交易笔数校验",
                )
            )

        return ParseResult(
            transactions=transactions,
            accounts=list(accounts_map.values()),
            holder_name=holder_name,
            period=(period_start, period_end) if period_start and period_end else None,
            self_checks=self_checks,
            warnings=warnings,
        )
