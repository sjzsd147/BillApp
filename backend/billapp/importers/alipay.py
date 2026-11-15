"""Alipay GBK CSV Parser and Self-Check Reconciliation Engine."""

from __future__ import annotations

import csv
import io
import re
from typing import Dict, List, Optional, Tuple

from billapp.core.models import (
    AccountCreate,
    AccountRef,
    ParseResult,
    SelfCheckResult,
    TransactionCreate,
)
from billapp.core.money import to_cents, to_display
from billapp.dedup.fingerprint import assign_fingerprints
from billapp.dedup.refund import extract_alipay_parent_order_id, link_in_memory_refunds
from billapp.importers.base import FileProbe, Importer
from billapp.importers.common import (
    detect_encoding,
    extract_channel_and_normalize,
    parse_datetime,
    parse_funding_channel,
    sanitize_order_id,
)


class AlipayImporter(Importer):
    """Importer for Alipay electronic customer receipt statements (GBK CSV)."""

    source_id = "alipay"
    source_name = "支付宝"

    def detect(self, probe: FileProbe) -> float:
        """Evaluate if the file is an Alipay CSV statement."""
        if probe.is_excel() or probe.is_pdf():
            return 0.0

        enc = probe.encoding or detect_encoding(probe.content)
        sample_lines = probe.read_text(encoding=enc, max_lines=30)
        sample_text = "".join(sample_lines)

        score = 0.0
        if "支付宝" in sample_text or "alipay" in sample_text.lower():
            score += 0.4
        if "电子客户回单" in sample_text or "支付宝支付科技有限公司" in sample_text:
            score += 0.3
        if "交易订单号" in sample_text and "收/支" in sample_text and "交易时间" in sample_text:
            score += 0.3

        return min(1.0, score)

    def parse(self, probe: FileProbe) -> ParseResult:
        """Parse Alipay CSV statement with dynamic encoding detection."""
        enc = probe.encoding or detect_encoding(probe.content)
        raw_lines = probe.read_text(encoding=enc)
        if not raw_lines:
            raise ValueError("Alipay CSV statement is empty")

        # 1. Parse Metadata Header
        holder_name: Optional[str] = None
        account_no: Optional[str] = None
        period_start: Optional[str] = None
        period_end: Optional[str] = None
        exp_declared_count: Optional[int] = None
        exp_declared_cents: Optional[int] = None
        inc_declared_count: Optional[int] = None
        inc_declared_cents: Optional[int] = None
        neu_declared_count: Optional[int] = None
        neu_declared_cents: Optional[int] = None
        total_declared_count: Optional[int] = None

        header_row_idx = -1

        for i, line in enumerate(raw_lines):
            line_str = line.strip()
            if "姓名：" in line_str:
                m = re.search(r"姓名：\s*([^,\t\r\n]+)", line_str)
                if m:
                    holder_name = m.group(1).strip()
            elif "支付宝账户：" in line_str:
                m = re.search(r"支付宝账户：\s*([^,\t\r\n]+)", line_str)
                if m:
                    account_no = m.group(1).strip()
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
            elif "不计收支：" in line_str:
                m = re.search(r"不计收支：\s*(\d+)\s*笔\s*([\d\.,]+)\s*元", line_str)
                if m:
                    neu_declared_count = int(m.group(1))
                    neu_declared_cents = to_cents(m.group(2))
            elif "交易时间" in line_str and "交易分类" in line_str and "交易对方" in line_str:
                header_row_idx = i
                break

        if header_row_idx == -1:
            raise ValueError("Failed to locate Alipay table header row")

        # 2. Parse Table Columns
        csv_reader = csv.reader(raw_lines[header_row_idx:])
        header_cols = [c.strip() for c in next(csv_reader)]
        # Map column indices
        col_map: Dict[str, int] = {}
        for idx, col in enumerate(header_cols):
            if col:
                col_map[col] = idx

        required_cols = ["交易时间", "交易分类", "交易对方", "收/支", "金额", "交易状态", "交易订单号"]
        for req in required_cols:
            if req not in col_map:
                raise ValueError(f"Missing required Alipay column: {req}")

        transactions: List[TransactionCreate] = []
        accounts_map: Dict[str, AccountCreate] = {}
        warnings: List[str] = []

        # Platform main account
        acc_tail = account_no[-4:] if account_no and len(account_no) >= 4 else None
        main_platform_account = AccountCreate(
            kind="platform",
            bank="支付宝",
            tail=acc_tail,
            alias=f"支付宝({acc_tail})" if acc_tail else "支付宝",
            holder=holder_name,
            currency="CNY",
        )
        accounts_map["platform_alipay"] = main_platform_account

        # 3. Iterate Data Rows
        raw_rows_data: List[dict] = []

        for row in csv_reader:
            if not row or not row[0].strip() or row[0].strip().startswith("---"):
                continue

            def get_col(name: str) -> str:
                idx = col_map.get(name)
                if idx is not None and idx < len(row):
                    return row[idx].strip()
                return ""

            raw_time = get_col("交易时间")
            if not raw_time:
                continue

            iso_time, precision = parse_datetime(raw_time)
            category_raw = get_col("交易分类")
            counterparty_raw = get_col("交易对方")
            desc_raw = get_col("商品说明")
            direction_raw = get_col("收/支")
            amount_raw = get_col("金额")
            pay_method = get_col("收/付款方式")
            status = get_col("交易状态")
            # Sanitize order ids
            order_id = sanitize_order_id(get_col("交易订单号"))
            merchant_order_id = sanitize_order_id(get_col("商家订单号"))
            note = get_col("备注")

            amount_cents = to_cents(amount_raw)
            channel, merchant_norm = extract_channel_and_normalize(counterparty_raw)

            # Determine direction according to Alipay status matrix
            if direction_raw == "支出":
                if status == "交易关闭":
                    direction = "neutral"
                else:
                    direction = "expense"
            elif direction_raw == "不计收支":
                if "退款" in status or "退款" in category_raw or "退款" in desc_raw:
                    direction = "refund"
                elif any(k in category_raw or k in desc_raw for k in ("转账", "资金转移", "充值", "提现")):
                    direction = "transfer"
                else:
                    direction = "neutral"
            elif direction_raw == "收入":
                if "退款" in status or "退款" in category_raw:
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
                source="alipay",
                txn_time=iso_time,
                time_precision=precision,
                amount_cents=amount_cents,
                currency="CNY",
                direction=direction,
                counterparty=counterparty_raw or None,
                description=desc_raw or None,
                pay_method=pay_method or None,
                channel=channel,
                merchant_norm=merchant_norm or counterparty_raw,
                order_id=order_id or None,
                merchant_order_id=merchant_order_id or None,
                status=status or None,
                platform_category=category_raw or None,
                note=note or None,
                account_ref=AccountRef(kind="platform", bank="支付宝", tail=acc_tail),
                fund_account_ref=fund_ref,
            )
            transactions.append(txn)
            raw_rows_data.append({
                "direction_raw": direction_raw,
                "status": status,
                "amount_cents": amount_cents,
                "order_id": order_id,
                "category_raw": category_raw,
            })

        # 4. Fingerprint & In-memory Refund Linking
        assign_fingerprints(transactions)
        link_in_memory_refunds(transactions)

        # 5. Header Self-Check Reconciliation
        self_checks: List[SelfCheckResult] = []

        # Total rows check
        actual_total = len(transactions)
        if total_declared_count is not None:
            self_checks.append(
                SelfCheckResult(
                    name="总笔数校验",
                    expected=total_declared_count,
                    actual=actual_total,
                    passed=(total_declared_count == actual_total),
                    description="校验数据总行数与表头声明是否一致",
                )
            )

        # Net Expense Check:
        # Sum(successful expenses) - Sum(effective refunds where parent order was not closed in this file)
        closed_orders = {
            r["order_id"]
            for r in raw_rows_data
            if r["status"] == "交易关闭" and r["order_id"]
        }

        succ_exp_cents = sum(
            r["amount_cents"]
            for r in raw_rows_data
            if r["direction_raw"] == "支出" and r["status"] != "交易关闭"
        )

        eff_refund_cents = sum(
            r["amount_cents"]
            for r in raw_rows_data
            if r["direction_raw"] == "不计收支"
            and ("退款" in r["status"] or "退款" in r["category_raw"])
            and extract_alipay_parent_order_id(r["order_id"]) not in closed_orders
        )

        actual_net_exp_cents = succ_exp_cents - eff_refund_cents
        raw_exp_count = sum(1 for r in raw_rows_data if r["direction_raw"] == "支出")

        if exp_declared_cents is not None:
            passed = (actual_net_exp_cents == exp_declared_cents)
            self_checks.append(
                SelfCheckResult(
                    name="支出金额自检",
                    expected=exp_declared_cents,
                    actual=actual_net_exp_cents,
                    passed=passed,
                    deviation=(actual_net_exp_cents - exp_declared_cents) / 100.0,
                    description=f"支付宝表头支出自检 (扣除关闭订单与有效退款后净支出: {to_display(actual_net_exp_cents)}元)",
                )
            )

        if exp_declared_count is not None:
            self_checks.append(
                SelfCheckResult(
                    name="支出笔数自检",
                    expected=exp_declared_count,
                    actual=raw_exp_count,
                    passed=(exp_declared_count == raw_exp_count),
                    description="支付宝原始支出行数校验",
                )
            )

        # Neutral transactions check
        raw_neu_cents = sum(
            r["amount_cents"]
            for r in raw_rows_data
            if r["direction_raw"] == "不计收支"
        )
        raw_neu_count = sum(1 for r in raw_rows_data if r["direction_raw"] == "不计收支")

        if neu_declared_cents is not None:
            self_checks.append(
                SelfCheckResult(
                    name="不计收支金额自检",
                    expected=neu_declared_cents,
                    actual=raw_neu_cents,
                    passed=(raw_neu_cents == neu_declared_cents),
                    description="支付宝不计收支累计金额校验",
                )
            )

        if neu_declared_count is not None:
            self_checks.append(
                SelfCheckResult(
                    name="不计收支笔数自检",
                    expected=neu_declared_count,
                    actual=raw_neu_count,
                    passed=(raw_neu_count == neu_declared_count),
                    description="支付宝不计收支笔数校验",
                )
            )

        # Income transactions check
        raw_inc_cents = sum(
            r["amount_cents"] for r in raw_rows_data if r["direction_raw"] == "收入"
        )
        raw_inc_count = sum(1 for r in raw_rows_data if r["direction_raw"] == "收入")

        if inc_declared_cents is not None:
            self_checks.append(
                SelfCheckResult(
                    name="收入金额自检",
                    expected=inc_declared_cents,
                    actual=raw_inc_cents,
                    passed=(raw_inc_cents == inc_declared_cents),
                    description="支付宝收入累计金额校验",
                )
            )

        if inc_declared_count is not None:
            self_checks.append(
                SelfCheckResult(
                    name="收入笔数自检",
                    expected=inc_declared_count,
                    actual=raw_inc_count,
                    passed=(raw_inc_count == inc_declared_count),
                    description="支付宝收入笔数校验",
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
