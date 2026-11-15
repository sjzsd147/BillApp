"""Synthetic Alipay CSV Generator (GBK encoded, matching official export schema)."""

from __future__ import annotations

import io
from dataclasses import dataclass
from typing import List, Optional


@dataclass
class AlipayRow:
    txn_time: str
    category: str
    counterparty: str
    account: str
    description: str
    direction: str  # 支出 / 收入 / 不计收支
    amount: float
    pay_method: str
    status: str     # 交易成功 / 交易关闭 / 退款成功
    order_id: str
    merchant_order_id: str
    note: str = ""


def get_default_alipay_rows() -> List[AlipayRow]:
    """Default realistic synthetic rows matching sample characteristics."""
    return [
        AlipayRow(
            txn_time="2026-11-09 11:44:08",
            category="餐饮美食",
            counterparty="麦当劳",
            account="mcd***@example.com",
            description="上海金拱门食品有限公司",
            direction="支出",
            amount=27.47,
            pay_method="工商银行信用卡(8801)&工商银行天天减",
            status="交易成功",
            order_id="2026110900000000000000000101",
            merchant_order_id="MCH_SYN_0000000000101",
        ),
        AlipayRow(
            txn_time="2026-11-09 09:49:38",
            category="日用百货",
            counterparty="罗森(中心广场店)",
            account="ser***@example.com",
            description="支付宝支付中心广场店REDEMPTION",
            direction="支出",
            amount=10.90,
            pay_method="工商银行信用卡(8801)",
            status="交易成功",
            order_id="2026110900000000000000000102",
            merchant_order_id="MCH_SYN_0000000000102",
        ),
        AlipayRow(
            txn_time="2026-11-08 13:25:09",
            category="交通出行",
            counterparty="滴滴出行",
            account="dri***@example.com",
            description="滴滴快车-张师傅-11月08日行程",
            direction="支出",
            amount=14.80,
            pay_method="中国银行储蓄卡(6666)",
            status="交易成功",
            order_id="2026110800000000000000000103",
            merchant_order_id="MCH_SYN_0000000000103",
        ),
        AlipayRow(
            txn_time="2026-10-16 13:25:48",
            category="文化休闲",
            counterparty="shop**a",
            account="shop***@example.com",
            description="无线遥控手柄 电子元件开发板传感器配件",
            direction="支出",
            amount=29.80,
            pay_method="中国银行储蓄卡(6666)&红包",
            status="交易关闭",
            order_id="2026101600000000000000000104",
            merchant_order_id="T200P9000000000000000104",
        ),
        AlipayRow(
            txn_time="2026-10-16 13:26:21",
            category="退款",
            counterparty="shop**a",
            account="shop***@example.com",
            description="退款-无线遥控手柄 电子元件开发板传感器配件",
            direction="不计收支",
            amount=29.80,
            pay_method="中国银行储蓄卡(6666)&红包",
            status="退款成功",
            order_id="2026101600000000000000000104_9000000000000000104",
            merchant_order_id="T200P9000000000000000104",
        ),
    ]


def generate_synthetic_alipay_csv(
    rows: Optional[List[AlipayRow]] = None,
    output_path: Optional[str] = None,
    holder_name: str = "张三",
    account_no: str = "13800000001",
    corrupt_headers: bool = False,
    empty: bool = False,
    override_sums: Optional[dict] = None,
) -> bytes:
    """Generate Alipay CSV with authentic GBK encoding, preamble, and self-check summary."""
    if empty:
        data_rows = []
    elif rows is not None:
        data_rows = rows
    else:
        data_rows = get_default_alipay_rows()

    # Calculate sums for self-check
    total_count = len(data_rows)
    income_count = sum(1 for r in data_rows if r.direction == "收入")
    income_sum = sum(r.amount for r in data_rows if r.direction == "收入")
    expense_count = sum(1 for r in data_rows if r.direction == "支出")
    expense_sum = sum(r.amount for r in data_rows if r.direction == "支出")
    neutral_count = sum(1 for r in data_rows if r.direction == "不计收支")
    neutral_sum = sum(r.amount for r in data_rows if r.direction == "不计收支")

    if override_sums:
        total_count = override_sums.get("total_count", total_count)
        income_count = override_sums.get("income_count", income_count)
        income_sum = override_sums.get("income_sum", income_sum)
        expense_count = override_sums.get("expense_count", expense_count)
        expense_sum = override_sums.get("expense_sum", expense_sum)
        neutral_count = override_sums.get("neutral_count", neutral_count)
        neutral_sum = override_sums.get("neutral_sum", neutral_sum)

    lines: List[str] = []
    if not corrupt_headers:
        lines.append("------------------------------------------------------------------------------------")
        lines.append("导出信息：")
        lines.append(f"姓名：{holder_name}")
        lines.append(f"支付宝账户：{account_no}")
        lines.append("起始时间：[2026-10-09 00:00:00]    终止时间：[2026-11-09 23:59:59]")
        lines.append("导出交易类型：[全部]")
        lines.append("导出时间：[2026-11-09 20:09:35]")
        lines.append(f"共{total_count}笔记录")
        lines.append(f"收入：{income_count}笔 {income_sum:.2f}元")
        lines.append(f"支出：{expense_count}笔 {expense_sum:.2f}元")
        lines.append(f"不计收支：{neutral_count}笔 {neutral_sum:.2f}元")
        lines.append("")
        lines.append("特别提示：")
        lines.append("1.本回单内容可表明支付宝受理了相应支付交易申请，以实际交易情况为准；")
        lines.append("2.请勿使用本回单进行重复记账；")
        lines.append("")
        lines.append("------------------------支付宝支付科技有限公司  电子客户回单------------------------")
        lines.append("交易时间,交易分类,交易对方,对方账号,商品说明,收/支,金额,收/付款方式,交易状态,交易订单号,商家订单号,备注,")
    else:
        # Malformed header for Tier 2 boundary testing
        lines.append("CORRUPTED_HEADER_DATA,INVALID_ROW")

    for r in data_rows:
        row_str = (
            f"{r.txn_time},{r.category},{r.counterparty},{r.account},"
            f"{r.description},{r.direction},{r.amount:.2f},{r.pay_method},"
            f"{r.status},{r.order_id}\t,{r.merchant_order_id}\t,{r.note},"
        )
        lines.append(row_str)

    content = "\r\n".join(lines) + "\r\n"
    encoded_bytes = content.encode("gbk", errors="replace")

    if output_path:
        with open(output_path, "wb") as f:
            f.write(encoded_bytes)

    return encoded_bytes
