"""Synthetic WeChat Pay XLSX & CSV Generator (matching official WeChat Pay bill schema)."""

from __future__ import annotations

import io
from dataclasses import dataclass
from typing import List, Optional
import openpyxl


@dataclass
class WeChatRow:
    txn_time: str
    txn_type: str        # 商户消费 / 扫二维码付款 / 经营账户提现 / 零钱提现 / 转账 / 退款
    counterparty: str
    product: str
    direction: str       # 支出 / 收入 / /
    amount: float
    pay_method: str      # 工商银行信用卡(8801) / 零钱通 / 零钱 / 经营账户
    status: str          # 支付成功 / 到账成功 / 已全额退款
    order_id: str
    merchant_order_id: str
    note: str = "/"


def get_default_wechat_rows() -> List[WeChatRow]:
    """Default realistic synthetic rows matching sample characteristics."""
    return [
        WeChatRow(
            txn_time="2026-11-09 19:52:36",
            txn_type="商户消费",
            counterparty="校园物业中心",
            product="SN-SYN-000101",
            direction="支出",
            amount=4.00,
            pay_method="工商银行信用卡(8801)",
            status="支付成功",
            order_id="4500000000202611090000000101",
            merchant_order_id="MCH_WX_SYN_0000000101",
        ),
        WeChatRow(
            txn_time="2026-11-09 17:47:09",
            txn_type="商户消费",
            counterparty="星辰大学",
            product="第二食堂便利店",
            direction="支出",
            amount=9.90,
            pay_method="工商银行信用卡(8801)",
            status="支付成功",
            order_id="4200000000202611090000000102",
            merchant_order_id="MCH_WX_SYN_0000000102",
        ),
        WeChatRow(
            txn_time="2026-11-09 14:34:18",
            txn_type="商户消费",
            counterparty="中国人寿保险股份有限公司上海市分公司",
            product="2026年度综合意外险",
            direction="支出",
            amount=105.00,
            pay_method="零钱通",
            status="支付成功",
            order_id="4500000000202611090000000103",
            merchant_order_id="MCH_WX_SYN_0000000103",
        ),
        WeChatRow(
            txn_time="2026-11-08 14:29:13",
            txn_type="经营账户提现",
            counterparty="建设银行(5678)",
            product="/",
            direction="/",
            amount=100.00,
            pay_method="经营账户",
            status="到账成功",
            order_id="20910000000000000000000000000104",
            merchant_order_id="/",
        ),
        WeChatRow(
            txn_time="2026-11-04 12:44:05",
            txn_type="商户消费",
            counterparty="美宜佳",
            product="便利店日用品",
            direction="支出",
            amount=4.00,
            pay_method="工商银行信用卡(8801)",
            status="已全额退款",
            order_id="4200000000202611040000000105",
            merchant_order_id="MYJ_SYN_000105",
        ),
    ]


def generate_synthetic_wechat_xlsx(
    rows: Optional[List[WeChatRow]] = None,
    output_path: Optional[str] = None,
    user_nickname: str = "demo_user",
    empty: bool = False,
    corrupt_headers: bool = False,
    override_sums: Optional[dict] = None,
) -> bytes:
    """Generate WeChat Pay XLSX with openpyxl matching the 18-row header format."""
    if empty:
        data_rows = []
    elif rows is not None:
        data_rows = rows
    else:
        data_rows = get_default_wechat_rows()

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "微信支付账单明细"

    total_count = len(data_rows)
    income_count = sum(1 for r in data_rows if r.direction == "收入")
    income_sum = sum(r.amount for r in data_rows if r.direction == "收入")
    expense_count = sum(1 for r in data_rows if r.direction == "支出")
    expense_sum = sum(r.amount for r in data_rows if r.direction == "支出")
    neutral_count = sum(1 for r in data_rows if r.direction in ("/", "中性", "其他") or "提现" in r.txn_type or r.status == "已全额退款")
    neutral_sum = sum(r.amount for r in data_rows if r.direction in ("/", "中性", "其他") or "提现" in r.txn_type or r.status == "已全额退款")

    if override_sums:
        total_count = override_sums.get("total_count", total_count)
        income_count = override_sums.get("income_count", income_count)
        income_sum = override_sums.get("income_sum", income_sum)
        expense_count = override_sums.get("expense_count", expense_count)
        expense_sum = override_sums.get("expense_sum", expense_sum)
        neutral_count = override_sums.get("neutral_count", neutral_count)
        neutral_sum = override_sums.get("neutral_sum", neutral_sum)

    if not corrupt_headers:
        ws.cell(1, 1, "微信支付账单明细")
        ws.cell(2, 1, f"微信昵称：[{user_nickname}]")
        ws.cell(3, 1, "起始时间：[2026-10-09 00:00:00] 终止时间：[2026-11-09 20:08:20]")
        ws.cell(4, 1, "导出类型：[全部账单]")
        ws.cell(5, 1, "导出时间：[2026-11-09 20:08:20]")
        ws.cell(6, 1, "")
        ws.cell(7, 1, f"共{total_count}笔记录")
        ws.cell(8, 1, f"收入：{income_count}笔 {income_sum:.2f}元")
        ws.cell(9, 1, f"支出：{expense_count}笔 {expense_sum:.2f}元")
        ws.cell(10, 1, f"中性交易：{neutral_count}笔 {neutral_sum:.2f}元")
        ws.cell(11, 1, "注：")
        ws.cell(12, 1, "1. 充值/提现/理财通购买/零钱通存取/信用卡还款等交易，将计入中性交易")
        ws.cell(13, 1, "2. 若交易记录明细无有效内容，则代表该时间段内此微信号无交易")
        ws.cell(14, 1, "3. 本明细仅供个人对账使用")
        ws.cell(15, 1, "4. 本账单中所有时间均为UTC+08:00时间")
        ws.cell(16, 1, "")
        ws.cell(17, 1, "----------------------微信支付账单明细列表--------------------")

        headers = [
            "交易时间", "交易类型", "交易对方", "商品", "收/支",
            "金额(元)", "支付方式", "当前状态", "交易单号", "商户单号", "备注"
        ]
        for col_idx, h in enumerate(headers, 1):
            ws.cell(18, col_idx, h)

        start_row = 19
    else:
        ws.cell(1, 1, "CORRUPT_HEADER")
        start_row = 2

    for i, r in enumerate(data_rows):
        current_row = start_row + i
        ws.cell(current_row, 1, r.txn_time)
        ws.cell(current_row, 2, r.txn_type)
        ws.cell(current_row, 3, r.counterparty)
        ws.cell(current_row, 4, r.product)
        ws.cell(current_row, 5, r.direction)
        ws.cell(current_row, 6, r.amount)  # numeric
        ws.cell(current_row, 7, r.pay_method)
        ws.cell(current_row, 8, r.status)
        ws.cell(current_row, 9, r.order_id)
        ws.cell(current_row, 10, r.merchant_order_id)
        ws.cell(current_row, 11, r.note)

    bio = io.BytesIO()
    wb.save(bio)
    xlsx_bytes = bio.getvalue()

    if output_path:
        with open(output_path, "wb") as f:
            f.write(xlsx_bytes)

    return xlsx_bytes


def generate_synthetic_wechat_csv(
    rows: Optional[List[WeChatRow]] = None,
    output_path: Optional[str] = None,
    user_nickname: str = "demo_user",
    empty: bool = False,
) -> bytes:
    """Generate WeChat Pay CSV format (legacy export compatibility)."""
    if empty:
        data_rows = []
    elif rows is not None:
        data_rows = rows
    else:
        data_rows = get_default_wechat_rows()

    lines = [
        "微信支付账单明细",
        f"微信昵称：[{user_nickname}]",
        "起始时间：[2026-10-09 00:00:00] 终止时间：[2026-11-09 20:08:20]",
        "导出类型：[全部账单]",
        "导出时间：[2026-11-09 20:08:20]",
        "",
        f"共{len(data_rows)}笔记录",
        "----------------------微信支付账单明细列表--------------------",
        "交易时间,交易类型,交易对方,商品,收/支,金额(元),支付方式,当前状态,交易单号,商户单号,备注"
    ]
    for r in data_rows:
        lines.append(
            f"{r.txn_time},{r.txn_type},{r.counterparty},{r.product},"
            f"{r.direction},¥{r.amount:.2f},{r.pay_method},{r.status},"
            f"{r.order_id}\t,{r.merchant_order_id}\t,{r.note}"
        )
    content = "\r\n".join(lines) + "\r\n"
    encoded = content.encode("utf-8-sig")
    if output_path:
        with open(output_path, "wb") as f:
            f.write(encoded)
    return encoded
