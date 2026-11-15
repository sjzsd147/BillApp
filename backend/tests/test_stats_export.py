"""Unit & Integration tests for Stats Engine and OpenPyXL Excel Export."""

from __future__ import annotations

import io
import json
import sqlite3
from typing import Any, Generator, Optional
import pytest
import openpyxl

from billapp.core.db import init_db
from billapp.classify.categories import CategoryService
from billapp.export import generate_excel_export
from billapp.stats import (
    calculate_stats_account,
    calculate_stats_category,
    calculate_stats_heatmap,
    calculate_stats_merchant,
    calculate_stats_summary,
    calculate_stats_trend,
)


@pytest.fixture
def db() -> Generator[sqlite3.Connection, None, None]:
    conn = init_db(":memory:")
    CategoryService.init_builtin_categories(conn)
    # Default account and batch for FK integrity
    conn.execute("INSERT INTO accounts (id, kind, bank, tail, alias) VALUES (1, 'bank_debit', 'BOC', '6666', '中行借记卡')")
    conn.execute("INSERT INTO accounts (id, kind, bank, tail, alias) VALUES (2, 'bank_credit', 'ICBC', '8801', '工行信用卡')")
    conn.execute("INSERT INTO import_batches (id, file_name, file_sha1, file_size, source) VALUES (1, 'test.csv', 'sha1_test', 1024, 'alipay')")
    conn.commit()
    yield conn
    conn.close()


def insert_test_txn(
    db: sqlite3.Connection,
    txn_id: Optional[int] = None,
    source: str = "alipay",
    account_id: int = 1,
    fund_account_id: Optional[int] = None,
    txn_time: str = "2026-10-10 12:00:00",
    time_precision: str = "second",
    amount_cents: int = 1000,
    refunded_cents: int = 0,
    direction: str = "expense",
    counterparty: Optional[str] = None,
    description: Optional[str] = None,
    merchant_norm: Optional[str] = None,
    category_id: Optional[int] = None,
    refund_of: Optional[int] = None,
    duplicate_of: Optional[int] = None,
    transfer_peer: Optional[int] = None,
    excluded: int = 0,
    fingerprint: Optional[str] = None,
    currency: str = "CNY",
    note: Optional[str] = None,
):
    fp = fingerprint or f"fp_{txn_id or txn_time}_{amount_cents}"
    if txn_id is not None:
        db.execute(
            """
            INSERT INTO transactions (
                id, batch_id, source, account_id, fund_account_id, txn_time, time_precision,
                amount_cents, refunded_cents, direction, counterparty, description, merchant_norm,
                category_id, refund_of, duplicate_of, transfer_peer, excluded, fingerprint,
                currency, note
            ) VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                txn_id, source, account_id, fund_account_id, txn_time, time_precision,
                amount_cents, refunded_cents, direction, counterparty, description, merchant_norm,
                category_id, refund_of, duplicate_of, transfer_peer, excluded, fp,
                currency, note
            ),
        )
    else:
        db.execute(
            """
            INSERT INTO transactions (
                batch_id, source, account_id, fund_account_id, txn_time, time_precision,
                amount_cents, refunded_cents, direction, counterparty, description, merchant_norm,
                category_id, refund_of, duplicate_of, transfer_peer, excluded, fingerprint,
                currency, note
            ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source, account_id, fund_account_id, txn_time, time_precision,
                amount_cents, refunded_cents, direction, counterparty, description, merchant_norm,
                category_id, refund_of, duplicate_of, transfer_peer, excluded, fp,
                currency, note
            ),
        )


def test_effective_expense_calculation_comprehensive(db: sqlite3.Connection):
    """Test effective expense calculation strictly excludes duplicates, internal transfers,
    and correctly offsets refunds without treating refunds as income.
    """
    cat_food = db.execute("SELECT id FROM categories WHERE key = 'food.dining'").fetchone()[0]
    cat_transfer = db.execute("SELECT id FROM categories WHERE key = 'financial.internal_transfer'").fetchone()[0]

    # 1. Normal expense: 50.00 (5000 cents)
    insert_test_txn(db, txn_id=1, txn_time="2026-10-10 12:00:00", amount_cents=5000, direction="expense", category_id=cat_food)
    # 2. Expense with linked refund: 100.00 with 30.00 refunded -> net 70.00 (7000 cents)
    insert_test_txn(db, txn_id=2, txn_time="2026-10-12 12:00:00", amount_cents=10000, refunded_cents=3000, direction="expense", category_id=cat_food)
    # 3. Linked refund record itself (direction='refund', refund_of=2) -> should NOT be added to income
    insert_test_txn(db, txn_id=3, txn_time="2026-10-13 12:00:00", amount_cents=3000, direction="refund", refund_of=2)
    # 4. Duplicate transaction: 50.00 (duplicate_of=1) -> must be EXCLUDED
    insert_test_txn(db, txn_id=4, txn_time="2026-10-10 12:00:00", amount_cents=5000, direction="expense", category_id=cat_food, duplicate_of=1)
    # 5. User-excluded transaction: 40.00 (excluded=1) -> must be EXCLUDED
    insert_test_txn(db, txn_id=5, txn_time="2026-10-14 12:00:00", amount_cents=4000, direction="expense", category_id=cat_food, excluded=1)
    # 6. Direction = transfer -> must be EXCLUDED
    insert_test_txn(db, txn_id=6, txn_time="2026-10-15 12:00:00", amount_cents=80000, direction="transfer")
    # 7. Category = financial.internal_transfer -> must be EXCLUDED
    insert_test_txn(db, txn_id=7, txn_time="2026-10-16 12:00:00", amount_cents=50000, direction="expense", category_id=cat_transfer)
    # 8. Normal income: 200.00 (20000 cents)
    insert_test_txn(db, txn_id=9, txn_time="2026-10-18 12:00:00", amount_cents=20000, direction="income")
    # 9. Transfer peer leg (transfer_peer=9) -> must be EXCLUDED
    insert_test_txn(db, txn_id=8, txn_time="2026-10-17 12:00:00", amount_cents=10000, direction="expense", transfer_peer=9)
    db.commit()

    # Calculate summary
    summary = calculate_stats_summary(db, start_date="2026-10-01", end_date="2026-10-31")

    # Expected net expense: 50.00 (txn 1) + 70.00 (txn 2 net) = 120.00 (12000 cents)
    assert summary.total_expense_cents == 12000
    assert summary.expense_net == 12000
    # Expected income: 200.00 (20000 cents) - refunds must NOT be included in income
    assert summary.total_income_cents == 20000
    assert summary.income == 20000
    # Expected balance: 20000 - 12000 = 8000 cents
    assert summary.net_savings_cents == 8000
    assert summary.balance == 8000
    # Savings rate: (8000 / 20000) * 100 = 40.0%
    assert summary.savings_rate == 40.0
    # Days: 31 days in October
    assert summary.days == 31
    assert summary.daily_average_cents == round(12000 / 31)


def test_unlinked_refund_deduction(db: sqlite3.Connection):
    """Test unlinked refund (refund without parent order) deducts from period net expense."""
    cat_food = db.execute("SELECT id FROM categories WHERE key = 'food.dining'").fetchone()[0]

    # Expense: 100.00
    insert_test_txn(db, txn_id=1, txn_time="2026-10-10 12:00:00", amount_cents=10000, direction="expense", category_id=cat_food)
    # Unlinked refund: 20.00 (refund_of IS NULL)
    insert_test_txn(db, txn_id=2, txn_time="2026-10-15 12:00:00", amount_cents=2000, direction="refund", refund_of=None)
    db.commit()

    summary = calculate_stats_summary(db, start_date="2026-10-01", end_date="2026-10-31")
    # 100.00 expense - 20.00 unlinked refund = 80.00 (8000 cents)
    assert summary.total_expense_cents == 8000
    assert summary.total_income_cents == 0


def test_stats_trend_monthly_and_daily(db: sqlite3.Connection):
    """Test monthly and daily trend series aggregation."""
    cat_food = db.execute("SELECT id FROM categories WHERE key = 'food.dining'").fetchone()[0]

    insert_test_txn(db, txn_time="2026-09-15 10:00:00", amount_cents=5000, direction="expense", category_id=cat_food)
    insert_test_txn(db, txn_time="2026-09-20 10:00:00", amount_cents=15000, direction="income")
    insert_test_txn(db, txn_time="2026-10-05 10:00:00", amount_cents=8000, direction="expense", category_id=cat_food)
    insert_test_txn(db, txn_time="2026-10-10 10:00:00", amount_cents=20000, direction="income")
    db.commit()

    monthly_trend = calculate_stats_trend(db, granularity="monthly")
    assert len(monthly_trend) == 2
    assert monthly_trend[0].period == "2026-09"
    assert monthly_trend[0].expense_cents == 5000
    assert monthly_trend[0].income_cents == 15000
    assert monthly_trend[0].net_cents == 10000

    assert monthly_trend[1].period == "2026-10"
    assert monthly_trend[1].expense_cents == 8000
    assert monthly_trend[1].income_cents == 20000

    daily_trend = calculate_stats_trend(db, granularity="daily", start_date="2026-09-01", end_date="2026-09-30")
    assert len(daily_trend) == 2
    assert daily_trend[0].period == "2026-09-15"
    assert daily_trend[0].expense_cents == 5000


def test_stats_category_hierarchy(db: sqlite3.Connection):
    """Test 2-tier tree category calculation for ECharts Sunburst/Donut."""
    cat_dining = db.execute("SELECT id FROM categories WHERE key = 'food.dining'").fetchone()[0]
    cat_takeout = db.execute("SELECT id FROM categories WHERE key = 'food.takeout'").fetchone()[0]
    cat_transport = db.execute("SELECT id FROM categories WHERE key = 'transport.taxi'").fetchone()[0]

    insert_test_txn(db, txn_time="2026-10-01 10:00:00", amount_cents=3000, direction="expense", category_id=cat_dining)
    insert_test_txn(db, txn_time="2026-10-02 10:00:00", amount_cents=2000, direction="expense", category_id=cat_takeout)
    insert_test_txn(db, txn_time="2026-10-03 10:00:00", amount_cents=5000, direction="expense", category_id=cat_transport)
    insert_test_txn(db, txn_time="2026-10-04 10:00:00", amount_cents=1000, direction="expense", category_id=None)
    db.commit()

    res = calculate_stats_category(db, direction="expense")
    # Total expense: 3000 + 2000 + 5000 + 1000 = 11000 cents
    assert res.total_cents == 11000

    # Food root should have dining + takeout = 5000 cents
    food_node = next(n for n in res.items if n.key == "food")
    assert food_node.amount_cents == 5000
    assert len(food_node.children) == 2

    # Unclassified node should exist with 1000 cents
    unclass_node = next(n for n in res.items if n.key == "unclassified")
    assert unclass_node.amount_cents == 1000


def test_stats_merchant_ranking(db: sqlite3.Connection):
    """Test top N merchant ranking and tail aggregation."""
    cat_food = db.execute("SELECT id FROM categories WHERE key = 'food.dining'").fetchone()[0]

    insert_test_txn(db, txn_time="2026-10-01 10:00:00", amount_cents=5000, direction="expense", counterparty="麦当劳中国", merchant_norm="麦当劳", category_id=cat_food)
    insert_test_txn(db, txn_time="2026-10-02 10:00:00", amount_cents=3000, direction="expense", counterparty="麦当劳中国", merchant_norm="麦当劳", category_id=cat_food)
    insert_test_txn(db, txn_time="2026-10-03 10:00:00", amount_cents=4000, direction="expense", counterparty="罗森便利店", merchant_norm="罗森", category_id=cat_food)
    insert_test_txn(db, txn_time="2026-10-04 10:00:00", amount_cents=1000, direction="expense", counterparty="滴滴出行", merchant_norm="滴滴", category_id=cat_food)
    db.commit()

    ranking = calculate_stats_merchant(db, limit=2)
    # Total: 8000 (麦当劳) + 4000 (罗森) + 1000 (滴滴) = 13000 cents
    assert ranking.total_expense_cents == 13000
    assert len(ranking.items) == 2
    assert ranking.items[0].merchant == "麦当劳"
    assert ranking.items[0].expense_cents == 8000
    assert ranking.items[1].merchant == "罗森"
    assert ranking.items[1].expense_cents == 4000
    # Tail item: 滴滴 1000
    assert ranking.other_expense_cents == 1000
    assert ranking.other_count == 1


def test_stats_heatmap_dual_mode(db: sqlite3.Connection):
    """Test calendar heatmap and 7x24 weekday-hour heatmap."""
    cat_food = db.execute("SELECT id FROM categories WHERE key = 'food.dining'").fetchone()[0]

    # Insert a Sunday 14:00 record (2026-10-18 is Sunday)
    insert_test_txn(db, txn_time="2026-10-18 14:30:00", time_precision="second", amount_cents=5000, direction="expense", category_id=cat_food)
    insert_test_txn(db, txn_time="2026-10-18 00:00:00", time_precision="day", amount_cents=3000, direction="expense", category_id=cat_food)
    db.commit()

    # Calendar mode includes both
    cal_res = calculate_stats_heatmap(db, mode="calendar", start_date="2026-10-01", end_date="2026-10-31")
    assert len(cal_res.calendar_items) == 1
    assert cal_res.calendar_items[0].date == "2026-10-18"
    assert cal_res.calendar_items[0].expense_cents == 8000

    # Weekday-hour mode excludes day precision, keeping only second precision
    wh_res = calculate_stats_heatmap(db, mode="weekday_hour", start_date="2026-10-01", end_date="2026-10-31")
    assert len(wh_res.weekday_hour_items) == 1
    # Sunday maps to weekday 7
    assert wh_res.weekday_hour_items[0].weekday == 7
    assert wh_res.weekday_hour_items[0].hour == 14
    assert wh_res.weekday_hour_items[0].expense_cents == 5000


def test_stats_account_turnover(db: sqlite3.Connection):
    """Test account inflow/outflow turnover computation."""
    insert_test_txn(db, account_id=1, txn_time="2026-10-01 10:00:00", amount_cents=5000, direction="expense")
    insert_test_txn(db, account_id=1, txn_time="2026-10-02 10:00:00", amount_cents=20000, direction="income")
    db.commit()

    res = calculate_stats_account(db, by="account")
    assert len(res.items) >= 1
    item = next(i for i in res.items if i.account_id == 1)
    assert item.name == "中行借记卡"
    assert item.total_out_cents == 5000
    assert item.total_in_cents == 20000
    assert item.net_cents == 15000


def test_openpyxl_dual_sheet_excel_export(db: sqlite3.Connection):
    """Test professional OpenPyXL dual-sheet Excel generation with dynamic formulas."""
    cat_dining = db.execute("SELECT id FROM categories WHERE key = 'food.dining'").fetchone()[0]
    cat_takeout = db.execute("SELECT id FROM categories WHERE key = 'food.takeout'").fetchone()[0]

    insert_test_txn(db, txn_id=101, source="alipay", account_id=2, txn_time="2026-10-10 12:00:00", amount_cents=2747, refunded_cents=0, direction="expense", counterparty="麦当劳", description="午餐", category_id=cat_dining)
    insert_test_txn(db, txn_id=102, source="icbc_credit", account_id=2, txn_time="2026-10-10 12:00:00", amount_cents=2747, refunded_cents=0, direction="expense", counterparty="支付宝-麦当劳", description="午餐", category_id=cat_dining, duplicate_of=101)
    insert_test_txn(db, txn_id=103, source="alipay", account_id=2, txn_time="2026-11-05 18:00:00", amount_cents=3500, refunded_cents=500, direction="expense", counterparty="美团外卖", description="晚餐", category_id=cat_takeout)
    db.commit()

    bio = generate_excel_export(db)
    val = bio.getvalue()

    # XLSX ZIP container verification
    assert val.startswith(b"PK")

    wb = openpyxl.load_workbook(io.BytesIO(val))
    assert wb.sheetnames == ["交易明细", "月度分类透视表"]

    # Sheet 1 verification
    ws1 = wb["交易明细"]
    headers = [cell.value for cell in ws1[1]]
    assert "净支出额(元)" in headers
    assert "冲抵金额(元)" in headers
    assert "状态标签" in headers
    assert ws1.freeze_panes == "A2"

    # Status tag check
    assert ws1.cell(row=2, column=14).value in ("主记录", "判定重复")

    # Numeric formatting check
    assert ws1.cell(row=2, column=7).number_format == "#,##0.00"

    # Sheet 2 verification
    ws2 = wb["月度分类透视表"]
    assert ws2.freeze_panes == "C2"
    ws2_headers = [cell.value for cell in ws2[1]]
    assert ws2_headers[0] == "一级分类"
    assert ws2_headers[1] == "二级分类"
    assert "2026-10" in ws2_headers
    assert "2026-11" in ws2_headers
    assert ws2_headers[-1] == "合计"

    # Verify dynamic Excel formula on row total
    row2_formula = ws2.cell(row=2, column=len(ws2_headers)).value
    assert isinstance(row2_formula, str)
    assert row2_formula.startswith("=SUM(")

    # Verify bottom summary row ("月度总支出")
    last_row = ws2.max_row
    assert ws2.cell(row=last_row, column=1).value == "月度总支出"
    bottom_formula = ws2.cell(row=last_row, column=3).value
    assert isinstance(bottom_formula, str)
    assert bottom_formula.startswith("=SUM(")


def test_refund_mode_refund_month_vs_original_month(db: sqlite3.Connection):
    """Verify refund_settings mode='refund_month' aggregates refunds in refund's month vs original_month."""
    cat_dining = db.execute("SELECT id FROM categories WHERE key = 'food.dining'").fetchone()[0]

    # September purchase: 10,000 cents
    insert_test_txn(db, txn_id=501, account_id=1, txn_time="2026-09-15 12:00:00", amount_cents=10000, refunded_cents=4000, direction="expense", category_id=cat_dining)
    # November refund: 4,000 cents linked to 501
    insert_test_txn(db, txn_id=502, account_id=1, txn_time="2026-11-05 10:00:00", amount_cents=4000, direction="refund", refund_of=501)
    # November independent expense: 6,000 cents
    insert_test_txn(db, txn_id=503, account_id=1, txn_time="2026-11-12 18:00:00", amount_cents=6000, refunded_cents=0, direction="expense", category_id=cat_dining)
    db.commit()

    # 1. Mode: refund_month
    db.execute(
        "INSERT INTO settings (key, value) VALUES ('refund_settings', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (json.dumps({"mode": "refund_month"}),),
    )
    db.commit()

    sep_sum = calculate_stats_summary(db, start_date="2026-09-01", end_date="2026-09-30")
    assert sep_sum.total_expense_cents == 10000, "In refund_month mode, September retains full expense"

    nov_sum = calculate_stats_summary(db, start_date="2026-11-01", end_date="2026-11-30")
    assert nov_sum.total_expense_cents == 2000, "In refund_month mode, November expense is 6000 - 4000 = 2000"

    trend_rm = calculate_stats_trend(db, granularity="monthly")
    trend_map_rm = {t.period: t.expense_cents for t in trend_rm}
    assert trend_map_rm["2026-09"] == 10000
    assert trend_map_rm["2026-11"] == 2000

    # 2. Mode: original_month (default)
    db.execute(
        "INSERT INTO settings (key, value) VALUES ('refund_settings', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (json.dumps({"mode": "original_month"}),),
    )
    db.commit()

    sep_sum_orig = calculate_stats_summary(db, start_date="2026-09-01", end_date="2026-09-30")
    assert sep_sum_orig.total_expense_cents == 6000, "In original_month mode, September expense is offset by linked refund (10000 - 4000 = 6000)"

    nov_sum_orig = calculate_stats_summary(db, start_date="2026-11-01", end_date="2026-11-30")
    assert nov_sum_orig.total_expense_cents == 6000, "In original_month mode, November independent expense is 6000"


def test_currency_isolation_and_fx_pending(db: sqlite3.Connection):
    """Verify non-CNY records are isolated from CNY KPI metrics and reported under fx_pending."""
    cat_dining = db.execute("SELECT id FROM categories WHERE key = 'food.dining'").fetchone()[0]

    # CNY transaction
    insert_test_txn(db, txn_id=601, account_id=1, txn_time="2026-10-01 10:00:00", amount_cents=5000, currency="CNY", direction="expense", category_id=cat_dining)
    # USD foreign currency transaction
    insert_test_txn(db, txn_id=602, account_id=1, txn_time="2026-10-02 11:00:00", amount_cents=2500, currency="USD", direction="expense", category_id=cat_dining)
    # EUR foreign currency transaction
    insert_test_txn(db, txn_id=603, account_id=1, txn_time="2026-10-03 12:00:00", amount_cents=1500, currency="EUR", direction="expense", category_id=cat_dining)
    db.commit()

    summary = calculate_stats_summary(db, start_date="2026-10-01", end_date="2026-10-31")
    # Only CNY 5000 is included in expense
    assert summary.total_expense_cents == 5000
    assert summary.expense_net == 5000

    # FX pending counts and amounts per §8.2 contract
    assert summary.fx_pending_count == 2
    currencies_pending = {p.currency: p.amount for p in summary.fx_pending_amounts}
    assert currencies_pending["USD"] == 2500
    assert currencies_pending["EUR"] == 1500

    # Sheet 2 pivot table only aggregates CNY expense
    bio = generate_excel_export(db)
    wb = openpyxl.load_workbook(io.BytesIO(bio.getvalue()), data_only=True)
    ws2 = wb["月度分类透视表"]
    # Row 2, Column 3 is 2026-10 total
    assert ws2.cell(row=2, column=3).value == 50.00  # 5000 cents == 50.00 yuan (USD and EUR excluded)


def test_excel_formula_injection_sanitization(db: sqlite3.Connection):
    """Verify formula injection characters (=, +, -, @) are sanitized with leading single quote."""
    db.execute("INSERT OR IGNORE INTO accounts (id, kind, bank, tail, alias) VALUES (99, 'wallet', '支付宝', NULL, '=SumAccount')")
    cat_id = db.execute("SELECT id FROM categories WHERE key = 'food.dining'").fetchone()[0]

    insert_test_txn(
        db,
        txn_id=701,
        account_id=99,
        txn_time="2026-10-05 10:00:00",
        amount_cents=1000,
        direction="expense",
        counterparty="=cmd|'/C calc'!A0",
        description="+cmd|'/C calc'!A0",
        note="-SUM(1,2)",
        category_id=cat_id,
    )
    db.commit()

    bio = generate_excel_export(db)
    wb = openpyxl.load_workbook(io.BytesIO(bio.getvalue()), data_only=False)
    ws1 = wb["交易明细"]

    # Target row is row 2
    cp_val = ws1.cell(row=2, column=4).value
    desc_val = ws1.cell(row=2, column=5).value
    acc_val = ws1.cell(row=2, column=12).value
    note_val = ws1.cell(row=2, column=16).value

    assert cp_val == "'=cmd|'/C calc'!A0"
    assert desc_val == "'+cmd|'/C calc'!A0"
    assert acc_val == "'=SumAccount"
    assert note_val == "'-SUM(1,2)"
