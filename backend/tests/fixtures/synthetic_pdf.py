"""Synthetic Bank PDF Statement Generator using PyMuPDF (BOC Debit & ICBC Credit)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import List, Optional, Tuple
import pymupdf


def _get_cjk_font_file() -> Optional[str]:
    """Find a CJK TTF/TTC font on the system for authentic rendering."""
    candidates = [
        # Windows
        "C:/Windows/Fonts/simsun.ttc",
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/simhei.ttf",
        # Linux
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/usr/share/fonts/truetype/arphic/uming.ttc",
        # macOS
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/STHeiti Light.ttc",
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    return None


def prepare_cjk_font(page: pymupdf.Page) -> str:
    """Register a CJK font on *page* and return the fontname for text insertion.

    Uses a system CJK TTF/TTC font when available and falls back to PyMuPDF's
    built-in ``china-ss`` CID font (never ``helv``, which turns CJK glyphs into
    dots) so synthetic PDF tests behave identically in headless Linux CI.
    """
    font_path = _get_cjk_font_file()
    if font_path:
        page.insert_font(fontname="cjk", fontfile=font_path)
        return "cjk"
    return "china-ss"


@dataclass
class BOCRow:
    post_date: str          # 2026-11-09
    post_time: str          # 19:56:44
    currency: str           # 人民币
    amount: float           # -12.25
    balance: float          # 760.75
    txn_name: str           # 网上快捷支付 / 网上快捷退款 / 跨行转账
    channel: str            # 银企对接
    branch: str             # -------------------
    note: str               # 网银在线-沙县小吃测试店 / 财付通-快餐连锁测试 / 支付宝-遥控手柄配件
    counterparty_name: str  # 网银在线-沙县小吃测试店
    counterparty_account: str # Z0000000000001N
    counterparty_bank: str  # -------------------


def get_default_boc_rows() -> List[BOCRow]:
    return [
        BOCRow(
            post_date="2026-11-09",
            post_time="19:56:44",
            currency="人民币",
            amount=-12.25,
            balance=760.75,
            txn_name="网上快捷支付",
            channel="银企对接",
            branch="-------------------",
            note="网银在线-沙县小吃测试店",
            counterparty_name="网银在线-沙县小吃测试店",
            counterparty_account="Z0000000000001N",
            counterparty_bank="-------------------",
        ),
        BOCRow(
            post_date="2026-11-01",
            post_time="13:12:11",
            currency="人民币",
            amount=-120.00,
            balance=773.00,
            txn_name="网上快捷支付",
            channel="银企对接",
            branch="-------------------",
            note="财付通-快餐连锁测试",
            counterparty_name="财付通-快餐连锁测试",
            counterparty_account="Z0000000000002N",
            counterparty_bank="-------------------",
        ),
        BOCRow(
            post_date="2026-10-16",
            post_time="13:26:05",
            currency="人民币",
            amount=-29.80,
            balance=893.00,
            txn_name="网上快捷支付",
            channel="银企对接",
            branch="-------------------",
            note="支付宝-遥控手柄配件",
            counterparty_name="支付宝-遥控手柄配件",
            counterparty_account="Z0000000000002N",
            counterparty_bank="-------------------",
        ),
        BOCRow(
            post_date="2026-10-16",
            post_time="13:26:22",
            currency="人民币",
            amount=29.80,
            balance=922.80,
            txn_name="网上快捷退款",
            channel="银企对接",
            branch="-------------------",
            note="支付宝-退款",
            counterparty_name="支付宝-退款",
            counterparty_account="Z0000000000002N",
            counterparty_bank="-------------------",
        ),
        BOCRow(
            post_date="2026-10-15",
            post_time="18:30:00",
            currency="人民币",
            amount=-107.00,
            balance=893.00,
            txn_name="网上快捷支付",
            channel="银企对接",
            branch="-------------------",
            note="财付通-美宜佳",
            counterparty_name="财付通-美宜佳",
            counterparty_account="Z0000000000002N",
            counterparty_bank="-------------------",
        ),
        BOCRow(
            post_date="2026-10-10",
            post_time="09:15:00",
            currency="人民币",
            amount=1000.00,
            balance=1000.00,
            txn_name="跨行转账",
            channel="网银",
            branch="-------------------",
            note="张三 招商银行 1234",
            counterparty_name="张三",
            counterparty_account="6214000000001234",
            counterparty_bank="招商银行",
        ),
    ]


def generate_synthetic_boc_pdf(
    rows: Optional[List[BOCRow]] = None,
    output_path: Optional[str] = None,
    password: Optional[str] = "boc123",
    holder_name: str = "张三",
    card_tail: str = "6666",
    override_sums: Optional[dict] = None,
) -> bytes:
    """Generate BOC Debit Card PDF with 12-column table and self-check headers."""
    if rows is not None:
        data_rows = rows
    else:
        data_rows = get_default_boc_rows()

    debits = sum(abs(r.amount) for r in data_rows if r.amount < 0)
    credits = sum(r.amount for r in data_rows if r.amount > 0)
    row_count = len(data_rows)

    if override_sums:
        debits = override_sums.get("debits", debits)
        credits = override_sums.get("credits", credits)
        row_count = override_sums.get("row_count", row_count)

    doc = pymupdf.open()
    page = doc.new_page(width=842, height=595)  # landscape A4

    fontname = prepare_cjk_font(page)

    # Title & Metadata Cards
    page.insert_text(pymupdf.Point(40, 40), "中国银行交易流水明细清单", fontname=fontname, fontsize=14)
    page.insert_text(pymupdf.Point(40, 65), f"借记卡号：621700000000{card_tail}", fontname=fontname, fontsize=10)
    page.insert_text(pymupdf.Point(240, 65), f"客户姓名：{holder_name}", fontname=fontname, fontsize=10)
    page.insert_text(pymupdf.Point(400, 65), f"借方发生数：{debits:.2f}", fontname=fontname, fontsize=10)
    page.insert_text(pymupdf.Point(550, 65), f"贷方发生数：{credits:.2f}", fontname=fontname, fontsize=10)
    page.insert_text(pymupdf.Point(700, 65), f"行数：{row_count}", fontname=fontname, fontsize=10)

    # 12-column Table drawing for Strategy A find_tables()
    x0, y0, total_w = 40, 90, 760
    headers = [
        "记账日期", "记账时间", "币别", "金额", "余额", "交易名称",
        "渠道", "网点名称", "附言", "对方账户名", "对方卡号/账号", "对方开户行"
    ]
    col_widths = [60, 50, 40, 50, 50, 65, 50, 60, 110, 110, 65, 50]
    col_xs = [x0]
    for w in col_widths:
        col_xs.append(col_xs[-1] + w)

    table_rows = [headers]
    for r in data_rows:
        table_rows.append([
            r.post_date, r.post_time, r.currency, f"{r.amount:+.2f}", f"{r.balance:.2f}",
            r.txn_name, r.channel, r.branch, r.note, r.counterparty_name,
            r.counterparty_account, r.counterparty_bank
        ])

    row_h = 24
    num_rows = len(table_rows)

    # Draw grid lines
    for r_idx in range(num_rows + 1):
        y = y0 + r_idx * row_h
        page.draw_line(pymupdf.Point(x0, y), pymupdf.Point(col_xs[-1], y), color=(0.4, 0.4, 0.4), width=0.5)

    for c_idx in range(len(col_xs)):
        x = col_xs[c_idx]
        page.draw_line(pymupdf.Point(x, y0), pymupdf.Point(x, y0 + num_rows * row_h), color=(0.4, 0.4, 0.4), width=0.5)

    # Insert cell texts
    for r_idx, row_data in enumerate(table_rows):
        y = y0 + r_idx * row_h + 16
        is_hdr = (r_idx == 0)
        fs = 8 if not is_hdr else 8.5
        for c_idx, cell_text in enumerate(row_data):
            x = col_xs[c_idx] + 3
            display_txt = cell_text[:12] if len(cell_text) > 12 else cell_text
            page.insert_text(pymupdf.Point(x, y), display_txt, fontname=fontname, fontsize=fs)

    save_kwargs = {}
    if password:
        save_kwargs = {
            "encryption": pymupdf.PDF_ENCRYPT_AES_128,
            "user_pw": password,
            "owner_pw": password,
        }

    pdf_bytes = doc.tobytes(**save_kwargs)
    if output_path:
        with open(output_path, "wb") as f:
            f.write(pdf_bytes)
    return pdf_bytes


@dataclass
class ICBCRow:
    post_date: str          # 2026-11-09 11:44:08
    card_tail: str          # 8801 / 8802 / 8803
    direction: str          # 借 / 贷
    txn_currency: str       # 人民币 / 美元
    txn_amount: float       # 27.47
    post_currency: str      # 人民币 / 美元
    post_amount: float      # 27.47
    balance: float          # 1520.30
    counterparty: str       # 支付宝-麦当劳 / ICBCVisaMerchantRebate
    account_no: str         # 6222000000008801
    summary: str            # 消费 / 退货 / 还款
    place: str              # 支付宝-上海金拱门食品有限公司


def get_default_icbc_rows() -> List[ICBCRow]:
    return [
        ICBCRow(
            post_date="2026-11-09 11:44:08",
            card_tail="8801",
            direction="借",
            txn_currency="人民币",
            txn_amount=27.47,
            post_currency="人民币",
            post_amount=27.47,
            balance=1520.30,
            counterparty="支付宝-麦当劳",
            account_no="6222000000008801",
            summary="消费",
            place="支付宝-上海金拱门食品有限公司",
        ),
        ICBCRow(
            post_date="2026-11-09 09:49:38",
            card_tail="8801",
            direction="借",
            txn_currency="人民币",
            txn_amount=10.90,
            post_currency="人民币",
            post_amount=10.90,
            balance=1492.83,
            counterparty="支付宝-罗森(中心广场店)",
            account_no="6222000000008801",
            summary="消费",
            place="支付宝-罗森中心广场店",
        ),
        ICBCRow(
            post_date="2026-11-04 12:44:06",
            card_tail="8801",
            direction="贷",
            txn_currency="人民币",
            txn_amount=4.00,
            post_currency="人民币",
            post_amount=4.00,
            balance=1481.93,
            counterparty="财付通-美宜佳",
            account_no="6222000000008801",
            summary="退货",
            place="财付通-美宜佳退款",
        ),
        ICBCRow(
            post_date="2026-11-02 08:30:00",
            card_tail="8802",
            direction="贷",
            txn_currency="美元",
            txn_amount=12.50,
            post_currency="美元",
            post_amount=12.50,
            balance=12.50,
            counterparty="ICBCVisaMerchantRebate",
            account_no="4000000000008802",
            summary="退货",
            place="Visa Glocal Rebate",
        ),
        ICBCRow(
            post_date="2026-10-25 15:00:00",
            card_tail="8803",
            direction="借",
            txn_currency="人民币",
            txn_amount=35.00,
            post_currency="人民币",
            post_amount=35.00,
            balance=35.00,
            counterparty="滴滴出行（云闪付）",
            account_no="6214000000008803",
            summary="消费",
            place="银联云闪付滴滴",
        ),
    ]


def generate_synthetic_icbc_pdf(
    rows: Optional[List[ICBCRow]] = None,
    output_path: Optional[str] = None,
    password: Optional[str] = "icbc123",
    watermark_angle: float = -60.0,
    with_watermarks: bool = True,
    holder_name: str = "张三",
) -> bytes:
    """Generate ICBC Credit Card PDF with rotated diagonal watermarks and multi-card rows."""
    if rows is not None:
        data_rows = rows
    else:
        data_rows = get_default_icbc_rows()

    doc = pymupdf.open()
    # ICBC sample is 4 pages or multi-page landscape (842 x 595 pt)
    page = doc.new_page(width=842, height=595)

    fontname = prepare_cjk_font(page)

    # 1. Inject diagonal watermarks with morph angle
    if with_watermarks:
        wm_mat = pymupdf.Matrix(watermark_angle)
        for wy in [150, 300, 450]:
            for wx in [100, 350, 600]:
                pt = pymupdf.Point(wx, wy)
                page.insert_text(
                    pt,
                    f"中国工商银行 00001111 2026-11-09 20:11:51 DEMO00000001",
                    fontname=fontname,
                    fontsize=20,
                    color=(0.85, 0.85, 0.85),
                    morph=(pt, wm_mat),
                )

    # 2. Main title & Cardholder (Horizontal text: dir == (1, 0))
    page.insert_text(pymupdf.Point(40, 40), "中国工商银行信用卡历史明细（电子版）", fontname=fontname, fontsize=14)
    page.insert_text(pymupdf.Point(40, 65), f"客户姓名：{holder_name}", fontname=fontname, fontsize=10)

    # 3. Table Headers
    headers = [
        "入账日期", "交易卡号", "收支", "交易币种", "交易金额",
        "入账币种", "入账金额", "账户余额", "对方户名", "对方账号", "摘要", "交易场所"
    ]
    col_widths = [105, 55, 30, 45, 55, 45, 55, 55, 100, 95, 35, 85]
    col_xs = [40]
    for w in col_widths:
        col_xs.append(col_xs[-1] + w)

    table_rows = [headers]
    page_debits = 0.0
    page_credits = 0.0
    for r in data_rows:
        if r.direction == "借":
            page_debits += r.post_amount
        else:
            page_credits += r.post_amount
        table_rows.append([
            r.post_date, f"...{r.card_tail}", r.direction, r.txn_currency,
            f"{r.txn_amount:.2f}", r.post_currency, f"{r.post_amount:.2f}",
            f"{r.balance:.2f}", r.counterparty[:12], r.account_no[-8:],
            r.summary, r.place[:12]
        ])

    row_h = 24
    y0 = 85
    num_rows = len(table_rows)

    for r_idx in range(num_rows + 1):
        y = y0 + r_idx * row_h
        page.draw_line(pymupdf.Point(col_xs[0], y), pymupdf.Point(col_xs[-1], y), color=(0.5, 0.5, 0.5), width=0.5)

    for c_idx in range(len(col_xs)):
        x = col_xs[c_idx]
        page.draw_line(pymupdf.Point(x, y0), pymupdf.Point(x, y0 + num_rows * row_h), color=(0.5, 0.5, 0.5), width=0.5)

    for r_idx, row_data in enumerate(table_rows):
        y = y0 + r_idx * row_h + 16
        is_hdr = (r_idx == 0)
        fs = 7.5 if not is_hdr else 8.0
        for c_idx, cell_text in enumerate(row_data):
            x = col_xs[c_idx] + 2
            page.insert_text(pymupdf.Point(x, y), cell_text, fontname=fontname, fontsize=fs)

    # 4. Footer summary for page check
    footer_y = y0 + num_rows * row_h + 25
    footer_text = (
        f"本页支出算术合计: {page_debits:.2f}  "
        f"本页收入算术合计: {page_credits:.2f}  "
        f"本页交易笔数: {len(data_rows)}"
    )
    page.insert_text(pymupdf.Point(40, footer_y), footer_text, fontname=fontname, fontsize=9)

    save_kwargs = {}
    if password:
        save_kwargs = {
            "encryption": pymupdf.PDF_ENCRYPT_AES_128,
            "user_pw": password,
            "owner_pw": password,
        }

    pdf_bytes = doc.tobytes(**save_kwargs)
    if output_path:
        with open(output_path, "wb") as f:
            f.write(pdf_bytes)
    return pdf_bytes
