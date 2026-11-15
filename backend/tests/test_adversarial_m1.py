"""Adversarial stress-test suite for Milestone 1 parsers and deduplication engines."""

from __future__ import annotations

import csv
import io
import sqlite3
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import openpyxl
import pytest

from billapp.core.db import init_db
from billapp.core.models import (
    AccountCreate,
    AccountRef,
    ImportBatchCreate,
    TransactionCreate,
)
from billapp.core.money import format_money, parse_signed_amount, to_cents, to_display
from billapp.dedup.fingerprint import (
    assign_fingerprints,
    compute_fallback_fingerprint,
    compute_order_fingerprint,
)
from billapp.dedup.refund import (
    extract_alipay_parent_order_id,
    link_in_memory_refunds,
    link_in_source_refunds,
)
from billapp.importers.alipay import AlipayImporter
from billapp.importers.base import FileProbe
from billapp.importers.common import (
    detect_encoding,
    extract_channel_and_normalize,
    parse_datetime,
    parse_funding_channel,
)
from billapp.importers.wechat import WeChatImporter


# Helper to build mock Alipay CSV
def build_alipay_csv(
    rows: list[dict],
    holder_name: str = "张三",
    account_no: str = "13800000001",
    exp_cents: int = 0,
    exp_count: int = 0,
    inc_cents: int = 0,
    inc_count: int = 0,
    neu_cents: int = 0,
    neu_count: int = 0,
    encoding: str = "gbk",
) -> bytes:
    lines = [
        "------------------------------------------------------------------------------------",
        "导出信息：",
        f"姓名：{holder_name}",
        f"支付宝账户：{account_no}",
        "起始时间：[2026-10-09 00:00:00]    终止时间：[2026-11-09 23:59:59]",
        "导出交易类型：[全部]",
        "导出时间：[2026-11-09 20:09:35]",
        f"共{len(rows)}笔记录",
        f"收入：{inc_count}笔 {to_display(inc_cents)}元",
        f"支出：{exp_count}笔 {to_display(exp_cents)}元",
        f"不计收支：{neu_count}笔 {to_display(neu_cents)}元",
        "",
        "------------------------支付宝支付科技有限公司  电子客户回单------------------------",
        "交易时间,交易分类,交易对方,对方账号,商品说明,收/支,金额,收/付款方式,交易状态,交易订单号,商家订单号,备注,",
    ]
    for r in rows:
        row_str = (
            f"{r.get('time', '2026-11-01 12:00:00')},"
            f"{r.get('cat', '餐饮美食')},"
            f"{r.get('cparty', '商户A')},"
            f"{r.get('account', 'test@test.com')},"
            f"{r.get('desc', '商品A')},"
            f"{r.get('dir', '支出')},"
            f"{r.get('amt', '10.00')},"
            f"{r.get('pay_method', '工商银行信用卡(8801)')},"
            f"{r.get('status', '交易成功')},"
            f"{r.get('order_id', 'ORD12345')}\t,"
            f"{r.get('merchant_order_id', 'M12345')}\t,"
            f"{r.get('note', '')},"
        )
        lines.append(row_str)
    content = "\n".join(lines).encode(encoding, errors="replace")
    return content


# Helper to build mock WeChat XLSX
def build_wechat_xlsx(
    rows: list[dict],
    sheet_name: str = "Sheet1",
    holder_name: str = "demo_user",
    exp_cents: int = 0,
    exp_count: int = 0,
    inc_cents: int = 0,
    inc_count: int = 0,
    neu_cents: int = 0,
    neu_count: int = 0,
) -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = sheet_name

    meta = [
        ["微信支付账单明细"],
        [f"微信昵称：[{holder_name}]"],
        ["起始时间：[2026-10-09 00:00:00] 终止时间：[2026-11-09 20:08:20]"],
        ["导出类型：[全部账单]"],
        ["导出时间：[2026-11-09 20:08:20]"],
        [""],
        [f"共{len(rows)}笔记录"],
        [f"收入：{inc_count}笔 {to_display(inc_cents)}元"],
        [f"支出：{exp_count}笔 {to_display(exp_cents)}元"],
        [f"中性交易：{neu_count}笔 {to_display(neu_cents)}元"],
        ["注："],
        ["1. 充值/提现/理财通购买/零钱通存取/信用卡还款等交易，将计入中性交易"],
        ["2. 若交易记录明细无有效内容，则代表该时间段内此微信号无交易"],
        ["3. 本明细仅供个人对账使用"],
        ["4. 本账单中所有时间均为UTC+08:00时间"],
        [""],
        ["----------------------微信支付账单明细列表--------------------"],
        ["交易时间", "交易类型", "交易对方", "商品", "收/支", "金额(元)", "支付方式", "当前状态", "交易单号", "商户单号", "备注"],
    ]
    for row_idx, r in enumerate(meta, start=1):
        for col_idx, val in enumerate(r, start=1):
            ws.cell(row_idx, col_idx, val)

    for r_idx, r in enumerate(rows, start=19):
        ws.cell(r_idx, 1, r.get("time", "2026-11-01 12:00:00"))
        ws.cell(r_idx, 2, r.get("type", "商户消费"))
        ws.cell(r_idx, 3, r.get("cparty", "商户A"))
        ws.cell(r_idx, 4, r.get("goods", "商品A"))
        ws.cell(r_idx, 5, r.get("dir", "支出"))
        ws.cell(r_idx, 6, r.get("amt", "10.00"))
        ws.cell(r_idx, 7, r.get("pay_method", "工商银行信用卡(8801)"))
        ws.cell(r_idx, 8, r.get("status", "支付成功"))
        ws.cell(r_idx, 9, r.get("order_id", "45000001") + "\t")
        ws.cell(r_idx, 10, r.get("m_order_id", "03122") + "\t")
        ws.cell(r_idx, 11, r.get("note", "/"))

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ============================================================================
# Section 1: Alipay Adversarial Tests
# ============================================================================

def test_alipay_boundary_amounts():
    """Challenge Alipay parser with boundary amounts: 0.00, huge numbers, fractional cents."""
    rows = [
        {"amt": "0.00", "dir": "支出", "order_id": "ORD001"},
        {"amt": "0.01", "dir": "支出", "order_id": "ORD002"},
        {"amt": "99999999.99", "dir": "支出", "order_id": "ORD003"},
        {"amt": "15.005", "dir": "支出", "order_id": "ORD004"},  # rounding half up -> 15.01
    ]
    content = build_alipay_csv(rows, exp_cents=10000001502, exp_count=4)
    probe = FileProbe(content, file_name="alipay_boundary.csv")
    importer = AlipayImporter()
    assert importer.detect(probe) > 0.8
    res = importer.parse(probe)
    assert len(res.transactions) == 4
    assert res.transactions[0].amount_cents == 0
    assert res.transactions[1].amount_cents == 1
    assert res.transactions[2].amount_cents == 9999999999
    assert res.transactions[3].amount_cents == 1501


def test_alipay_unusual_characters():
    """Challenge Alipay parser with quotes, commas, emojis, and XML tags."""
    rows = [
        {
            "cparty": "McDonald's, \"Special\" Branch",
            "desc": "Combo A & B, <Special Edition>",
            "amt": "25.00",
            "dir": "支出",
            "order_id": "ORD101",
        },
        {
            "cparty": "🍎 Apple Store 📱",
            "desc": "iPhone 16 Pro Max",
            "amt": "9999.00",
            "dir": "支出",
            "order_id": "ORD102",
        },
    ]
    # Note: GBK encoding of emojis will replace or raise unless encoded safely; probe uses errors='replace'
    content = build_alipay_csv(rows, exp_cents=1002400, exp_count=2, encoding="gbk")
    probe = FileProbe(content, file_name="alipay_unusual.csv")
    importer = AlipayImporter()
    res = importer.parse(probe)
    assert len(res.transactions) == 2
    assert "Apple Store" in (res.transactions[1].counterparty or "")


def test_alipay_multi_part_funding_channels():
    """Challenge parse_funding_channel with composite strings."""
    # 1. Normal format
    res1 = parse_funding_channel("工商银行信用卡(8801)&工商银行天天减")
    assert res1 == ("工商银行", "8801", "bank_credit")

    # 2. ABC bank format
    res2 = parse_funding_channel("ABC(1234)&Bonus&Discount")
    assert res2 == ("ABC", "1234", "bank_debit")

    # 3. Full-width parentheses (common in Chinese input)
    res3 = parse_funding_channel("工商银行信用卡（8801）")
    assert res3 == ("工商银行", "8801", "bank_credit")

    # 4. Composite where bank card is second part: '余额宝&工商银行储蓄卡(8801)'
    res4 = parse_funding_channel("余额宝&工商银行储蓄卡(8801)")
    assert res4 == ("工商银行", "8801", "bank_debit")


def test_alipay_corrupt_headers():
    """Challenge Alipay parser with missing headers or corrupt structures."""
    # Missing required column
    bad_csv = (
        "姓名：张三\n支付宝账户：1234\n"
        "------------------------支付宝支付科技有限公司  电子客户回单------------------------\n"
        "交易时间,交易分类,交易对方,对方账号,商品说明,收/支,交易状态,交易订单号\n"  # Missing 金额
        "2026-11-01 12:00:00,餐饮,商户A,a@a.com,商品,支出,交易成功,ORD001\n"
    ).encode("gbk")
    probe = FileProbe(bad_csv, file_name="bad_alipay.csv")
    importer = AlipayImporter()
    with pytest.raises(ValueError, match="Missing required Alipay column"):
        importer.parse(probe)

    # Empty file
    empty_probe = FileProbe(b"", file_name="empty.csv")
    with pytest.raises(ValueError, match="empty"):
        importer.parse(empty_probe)


def test_alipay_utf8_encoding_behavior():
    """Challenge Alipay parser when user saves Alipay CSV as UTF-8 or UTF-8-BOM."""
    rows = [{"amt": "10.00", "dir": "支出", "order_id": "ORD_UTF8"}]
    # Encode as UTF-8 with BOM
    content_utf8 = build_alipay_csv(rows, exp_cents=1000, exp_count=1, encoding="utf-8-sig")
    probe = FileProbe(content_utf8, file_name="alipay_utf8.csv")
    importer = AlipayImporter()
    # Test detection
    score = importer.detect(probe)
    assert score > 0.8
    # Test parsing
    res = importer.parse(probe)
    assert len(res.transactions) == 1
    assert res.transactions[0].amount_cents == 1000



# ============================================================================
# Section 2: WeChat Adversarial Tests
# ============================================================================

def test_wechat_varied_sheet_names():
    """Challenge WeChat parser with non-default sheet names (e.g. '账单明细', 'MySheet')."""
    rows = [{"amt": "20.00", "dir": "支出", "order_id": "WX001"}]
    content = build_wechat_xlsx(rows, sheet_name="微信账单流水_2026", exp_cents=2000, exp_count=1)
    probe = FileProbe(content, file_name="wechat_sheet.xlsx")
    importer = WeChatImporter()
    assert importer.detect(probe) == 1.0
    res = importer.parse(probe)
    assert len(res.transactions) == 1
    assert res.transactions[0].amount_cents == 2000


def test_wechat_multi_sheet_active_non_statement():
    """Challenge WeChat parser when an Excel file has multiple sheets and active sheet is NOT the statement."""
    rows = [{"amt": "50.00", "dir": "支出", "order_id": "WX999"}]
    # Create workbook with 2 sheets: Sheet 1 is Cover (active), Sheet 2 is WeChat statement
    wb = openpyxl.Workbook()
    ws_cover = wb.active
    ws_cover.title = "Cover"
    ws_cover.cell(1, 1, "My Personal Finances")

    ws_data = wb.create_sheet(title="微信支付账单")
    # Build data on ws_data
    meta = [
        ["微信支付账单明细"],
        ["微信昵称：[用户A]"],
        ["起始时间：[2026-10-09 00:00:00] 终止时间：[2026-11-09 20:08:20]"],
        ["导出类型：[全部账单]"],
        ["导出时间：[2026-11-09 20:08:20]"],
        [""],
        ["共1笔记录"],
        ["收入：0笔 0.00元"],
        ["支出：1笔 50.00元"],
        ["中性交易：0笔 0.00元"],
        ["注："],
        [""],
        ["----------------------微信支付账单明细列表--------------------"],
        ["交易时间", "交易类型", "交易对方", "商品", "收/支", "金额(元)", "支付方式", "当前状态", "交易单号", "商户单号", "备注"],
        ["2026-11-01 12:00:00", "商户消费", "商户A", "商品A", "支出", "50.00", "零钱", "支付成功", "45000001", "M01", "/"],
    ]
    for row_idx, r in enumerate(meta, start=1):
        for col_idx, val in enumerate(r, start=1):
            ws_data.cell(row_idx, col_idx, val)

    wb.active = ws_cover  # Keep Cover active!
    buf = io.BytesIO()
    wb.save(buf)

    probe = FileProbe(buf.getvalue(), file_name="wechat_multisheet.xlsx")
    importer = WeChatImporter()
    det_score = importer.detect(probe)
    assert det_score == 1.0
    res = importer.parse(probe)
    assert len(res.transactions) == 1
    assert res.transactions[0].amount_cents == 5000



def test_wechat_amount_formats():
    """Challenge WeChat parser with amounts with/without currency symbol ¥ and thousand separators."""
    rows = [
        {"amt": "¥15.00", "dir": "支出", "order_id": "WX01"},
        {"amt": "¥ 25.50", "dir": "支出", "order_id": "WX02"},
        {"amt": "1,024.00", "dir": "支出", "order_id": "WX03"},
        {"amt": "0.00", "dir": "支出", "order_id": "WX04"},
    ]
    content = build_wechat_xlsx(rows, exp_cents=106450, exp_count=4)
    probe = FileProbe(content, file_name="wechat_amt.xlsx")
    importer = WeChatImporter()
    res = importer.parse(probe)
    assert len(res.transactions) == 4
    assert res.transactions[0].amount_cents == 1500
    assert res.transactions[1].amount_cents == 2550
    assert res.transactions[2].amount_cents == 102400
    assert res.transactions[3].amount_cents == 0


def test_wechat_millisecond_and_slash_timestamps():
    """Challenge timestamp parsing with milliseconds, slashes, and dates."""
    t1, p1 = parse_datetime("2026/11/09 19:52:36")
    assert t1 == "2026-11-09 19:52:36"
    assert p1 == "second"

    t2, p2 = parse_datetime("2026-11-09")
    assert t2 == "2026-11-09"
    assert p2 == "day"

    t3, p3 = parse_datetime("2026-11-09 19:52")
    assert t3 == "2026-11-09 19:52:00"
    assert p3 == "minute"

    t4, p4 = parse_datetime("2026-11-09 19:52:36.123")
    print(f"Millisecond timestamp parsed: t4='{t4}', p4='{p4}'")
    # Observation: formats list does not include '%Y-%m-%d %H:%M:%S.%f', so precision falls back to 'day'



def test_wechat_expense_status_fully_refunded_without_in_file_refund_row():
    """When a WeChat purchase row states '已全额退款' but no refund row exists in the file."""
    rows = [{"amt": "20.00", "dir": "支出", "status": "已全额退款", "order_id": "WX_REF"}]
    content = build_wechat_xlsx(rows, exp_cents=2000, exp_count=1)
    probe = FileProbe(content, file_name="wechat_orphan_refund.xlsx")
    importer = WeChatImporter()
    res = importer.parse(probe)
    assert res.transactions[0].direction == "neutral"



# ============================================================================
# Section 3: L1 Fingerprinting Adversarial Tests
# ============================================================================

def test_fingerprint_delimiter_collision_vulnerability():
    """Stress test fallback fingerprint for delimiter injection vulnerability."""
    # Field injection: counterparty containing '|'
    fp1 = compute_fallback_fingerprint(
        source="bank",
        card_tail="8801",
        txn_time="2026-11-09 12:00:00",
        amount_cents=1000,
        currency="CNY",
        direction="expense",
        counterparty="Starbucks|Special",
        description="Coffee",
        seq_n=0,
    )
    fp2 = compute_fallback_fingerprint(
        source="bank",
        card_tail="8801",
        txn_time="2026-11-09 12:00:00",
        amount_cents=1000,
        currency="CNY",
        direction="expense",
        counterparty="Starbucks",
        description="Special|Coffee",
        seq_n=0,
    )
    # Check if a collision occurred due to naive '|' joining
    assert fp1 != fp2


def test_fingerprint_empty_or_whitespace_order_id():
    """Ensure order_id with only whitespace correctly falls back to fallback hashing."""
    txns = [
        TransactionCreate(
            source="alipay",
            txn_time="2026-11-09 12:00:00",
            amount_cents=500,
            direction="expense",
            order_id="   ",  # whitespace only
            counterparty="Shop A",
        ),
        TransactionCreate(
            source="alipay",
            txn_time="2026-11-09 12:00:00",
            amount_cents=500,
            direction="expense",
            order_id="",     # empty
            counterparty="Shop B",
        ),
    ]
    assign_fingerprints(txns)
    assert txns[0].fingerprint != txns[1].fingerprint
    assert len(txns[0].fingerprint) == 40
    assert len(txns[1].fingerprint) == 40


def test_order_id_placeholder_collision():
    """When order_id contains placeholder like '/' or '--', compute_order_fingerprint causes collision."""
    txns = [
        TransactionCreate(
            source="alipay",
            txn_time="2026-11-09 12:00:00",
            amount_cents=500,
            direction="expense",
            order_id="/",
            counterparty="Shop A",
        ),
        TransactionCreate(
            source="alipay",
            txn_time="2026-11-09 13:00:00",
            amount_cents=1500,
            direction="expense",
            order_id="/",
            counterparty="Shop B",
        ),
    ]
    assign_fingerprints(txns)
    assert txns[0].fingerprint != txns[1].fingerprint



# ============================================================================
# Section 4: L2 Refund Matching Adversarial Tests
# ============================================================================

def test_refund_multiple_partial_refunds_against_single_parent():
    """Challenge L2 refund engine with multiple partial refunds against the same parent."""
    # Parent: 100.00 Yuan (10000 cents)
    # Refund 1: 30.00 Yuan (3000 cents)
    # Refund 2: 20.00 Yuan (2000 cents)
    txns = [
        TransactionCreate(
            source="alipay",
            txn_time="2026-10-10 10:00:00",
            amount_cents=10000,
            currency="CNY",
            direction="expense",
            order_id="202610100001",
            status="交易成功",
        ),
        TransactionCreate(
            source="alipay",
            txn_time="2026-10-11 11:00:00",
            amount_cents=3000,
            currency="CNY",
            direction="refund",
            order_id="202610100001*F-refund1",
            status="退款成功",
        ),
        TransactionCreate(
            source="alipay",
            txn_time="2026-10-12 12:00:00",
            amount_cents=2000,
            currency="CNY",
            direction="refund",
            order_id="202610100001*F-refund2",
            status="退款成功",
        ),
    ]
    linked = link_in_memory_refunds(txns)
    assert linked == 2
    # In Alipay, refunded_cents should sum both partial refunds: 3000 + 2000 = 5000 cents
    assert txns[0].refunded_cents == 5000
    assert txns[0].direction == "expense"


def test_wechat_multiple_refunds_and_over_refund():
    """Challenge WeChat refund matching with identical amounts or repeated refunds."""
    # Parent: 4.00 Yuan (400 cents)
    # Refund 1: 4.00 Yuan (400 cents)
    # Refund 2: 4.00 Yuan (400 cents) (e.g. duplicate refund row or separate refund)
    txns = [
        TransactionCreate(
            source="wechat",
            txn_time="2026-11-05 21:19:00",
            amount_cents=400,
            currency="CNY",
            direction="expense",
            counterparty="商户B",
            status="支付成功",
        ),
        TransactionCreate(
            source="wechat",
            txn_time="2026-11-05 21:20:00",
            amount_cents=400,
            currency="CNY",
            direction="refund",
            counterparty="商户B",
            status="退款成功",
        ),
        TransactionCreate(
            source="wechat",
            txn_time="2026-11-05 21:21:00",
            amount_cents=400,
            currency="CNY",
            direction="refund",
            counterparty="商户B",
            status="退款成功",
        ),
    ]
    linked = link_in_memory_refunds(txns)
    assert linked == 1
    assert txns[0].refunded_cents == 400


def test_wechat_refund_out_of_window():
    """Challenge WeChat refund matching when refund time exceeds the 10-minute window."""
    txns = [
        TransactionCreate(
            source="wechat",
            txn_time="2026-11-05 10:00:00",
            amount_cents=5000,
            currency="CNY",
            direction="expense",
            counterparty="京东商城",
            status="支付成功",
        ),
        TransactionCreate(
            source="wechat",
            txn_time="2026-11-05 10:15:00",  # 15 minutes later (> 10 min window)
            amount_cents=5000,
            currency="CNY",
            direction="refund",
            counterparty="京东商城",
            status="退款成功",
        ),
    ]
    linked = link_in_memory_refunds(txns)
    # Expected: should NOT match if 10 min window is strictly enforced
    assert linked == 0
    assert txns[0].refunded_cents == 0


def test_db_refund_linking_over_refund_behavior():
    """Test what SQLite database does when multiple refunds link to one parent."""
    conn = init_db(":memory:")
    cur = conn.execute(
        "INSERT INTO accounts (kind, bank, tail, currency) VALUES ('platform', '支付宝', '6040', 'CNY')"
    )
    acc_id = cur.lastrowid
    cur = conn.execute(
        "INSERT INTO import_batches (file_name, file_sha1, file_size, source) VALUES ('b.csv', 's1', 10, 'alipay')"
    )
    batch_id = cur.lastrowid

    # 1. Insert parent expense 50.00 Yuan (5000 cents)
    conn.execute(
        """
        INSERT INTO transactions (
            batch_id, source, account_id, txn_time, time_precision,
            amount_cents, direction, order_id, status, fingerprint
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (batch_id, "alipay", acc_id, "2026-10-10 10:00:00", "second", 5000, "expense", "202610100001", "交易成功", "FP_P"),
    )
    # 2. Insert refund 1: 30.00 Yuan
    conn.execute(
        """
        INSERT INTO transactions (
            batch_id, source, account_id, txn_time, time_precision,
            amount_cents, direction, order_id, status, fingerprint
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (batch_id, "alipay", acc_id, "2026-10-11 10:00:00", "second", 3000, "refund", "202610100001*F-1", "退款成功", "FP_R1"),
    )
    # 3. Insert refund 2: 30.00 Yuan (Total refund 6000 cents > 5000 cents!)
    conn.execute(
        """
        INSERT INTO transactions (
            batch_id, source, account_id, txn_time, time_precision,
            amount_cents, direction, order_id, status, fingerprint
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (batch_id, "alipay", acc_id, "2026-10-12 10:00:00", "second", 3000, "refund", "202610100001*F-2", "退款成功", "FP_R2"),
    )

    linked = link_in_source_refunds(conn, batch_id)
    row = conn.execute("SELECT amount_cents, refunded_cents FROM transactions WHERE order_id = '202610100001'").fetchone()
    assert row["refunded_cents"] == 5000

