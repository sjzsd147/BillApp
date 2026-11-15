"""Synthetic Generic Bank Statement Generator for YAML Profile Testing (signed/split/flag modes)."""

from __future__ import annotations

import io
from typing import List, Optional
import openpyxl


def generate_synthetic_profile_csv(
    mode: str = "signed",  # "signed" | "split" | "flag"
    bank_name: str = "招商银行",
    card_tail: str = "1234",
    output_path: Optional[str] = None,
) -> bytes:
    """Generate generic bank CSV conforming to signed, split, or flag YAML profile specifications."""
    lines: List[str] = [
        f"{bank_name}个人账户交易明细清单",
        f"账号: 621400000000{card_tail}",
        "币别: 人民币",
        "--------------------------------------------------------------------------------",
    ]

    if mode == "signed":
        lines.append("交易日期,交易时间,交易摘要,交易金额,账户余额,对方户名")
        lines.append("2026-11-09,10:30:00,网上快捷支付,-35.50,1500.00,美团外卖")
        lines.append("2026-11-08,12:00:00,工资入账,5000.00,6500.00,科技有限公司")
        lines.append("2026-11-07,09:15:00,退款,35.50,1535.50,美团外卖")
        lines.append("合计: 支出 35.50 收入 5035.50")
    elif mode == "split":
        lines.append("交易日期,交易时间,交易摘要,支出金额,收入金额,账户余额,对方户名")
        lines.append("2026-11-09,10:30:00,网上快捷支付,35.50,,1500.00,美团外卖")
        lines.append("2026-11-08,12:00:00,工资入账,,5000.00,6500.00,科技有限公司")
        lines.append("2026-11-07,09:15:00,退款,,35.50,1535.50,美团外卖")
        lines.append("小计,35.50,5035.50")
    elif mode == "flag":
        lines.append("交易日期,交易时间,交易摘要,交易金额,借贷,账户余额,对方户名")
        lines.append("2026-11-09,10:30:00,网上快捷支付,35.50,借,1500.00,美团外卖")
        lines.append("2026-11-08,12:00:00,工资入账,5000.00,贷,6500.00,科技有限公司")
        lines.append("2026-11-07,09:15:00,退款,35.50,贷,1535.50,美团外卖")
        lines.append("总计,35.50,5035.50")
    else:
        raise ValueError(f"Unknown mode: {mode}")

    content = "\r\n".join(lines) + "\r\n"
    encoded = content.encode("utf-8-sig")
    if output_path:
        with open(output_path, "wb") as f:
            f.write(encoded)
    return encoded


def generate_synthetic_profile_xlsx(
    mode: str = "signed",
    bank_name: str = "建设银行",
    card_tail: str = "5678",
    output_path: Optional[str] = None,
) -> bytes:
    """Generate generic bank XLSX conforming to profile specifications."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "明细"

    ws.cell(1, 1, f"{bank_name}账户流水")
    ws.cell(2, 1, f"卡号：621700020000{card_tail}")

    if mode == "signed":
        headers = ["交易日期", "交易时间", "摘要", "金额", "余额", "对方账户"]
        rows = [
            ["2026-11-08", "14:29:13", "提现到账", 100.00, 1100.00, "财付通"],
            ["2026-11-07", "11:00:00", "POS消费", -55.00, 1000.00, "联华超市"],
            ["2026-11-06", "18:20:00", "退款", 55.00, 1055.00, "联华超市"],
        ]
    elif mode == "split":
        headers = ["交易日期", "交易时间", "摘要", "借方金额", "贷方金额", "余额", "对方账户"]
        rows = [
            ["2026-11-08", "14:29:13", "提现到账", None, 100.00, 1100.00, "财付通"],
            ["2026-11-07", "11:00:00", "POS消费", 55.00, None, 1000.00, "联华超市"],
            ["2026-11-06", "18:20:00", "退款", None, 55.00, 1055.00, "联华超市"],
        ]
    else:  # flag
        headers = ["交易日期", "交易时间", "摘要", "金额", "借贷标志", "余额", "对方账户"]
        rows = [
            ["2026-11-08", "14:29:13", "提现到账", 100.00, "贷", 1100.00, "财付通"],
            ["2026-11-07", "11:00:00", "POS消费", 55.00, "借", 1000.00, "联华超市"],
            ["2026-11-06", "18:20:00", "退款", 55.00, "贷", 1055.00, "联华超市"],
        ]

    for c, h in enumerate(headers, 1):
        ws.cell(4, c, h)

    for r_idx, row in enumerate(rows, 5):
        for c_idx, val in enumerate(row, 1):
            ws.cell(r_idx, c_idx, val)

    bio = io.BytesIO()
    wb.save(bio)
    xlsx_bytes = bio.getvalue()
    if output_path:
        with open(output_path, "wb") as f:
            f.write(xlsx_bytes)
    return xlsx_bytes
