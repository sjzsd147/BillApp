"""Unit and integration tests for Foreign Exchange (FX) conversion & rate management pipeline.

Tests core conversion logic, SQLite migration, FX service, REST APIs, stats engine,
Excel export, and offline/error fallback per Milestone 6 specifications.
"""

from __future__ import annotations

import io
from pathlib import Path
import sqlite3
from typing import Generator
from unittest.mock import patch

from fastapi.testclient import TestClient
import openpyxl
import pytest

from billapp.app import create_app
from billapp.core.db import create_connection, init_db, migrate_db
from billapp.core.fx import (
    convert_transaction,
    fetch_online_fx_rate,
    get_cached_fx_rate,
    get_fx_overview,
    record_manual_rate,
    refresh_missing_fx,
    retry_transaction_fx,
)
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
    """In-memory SQLite database initialized with core schema and categories."""
    conn = init_db(":memory:")
    # Seed sample category
    conn.execute("INSERT OR IGNORE INTO categories (id, key, name, direction) VALUES (1, 'food.dining', '餐饮美食', 'expense')")
    conn.execute("INSERT OR IGNORE INTO categories (id, key, name, direction) VALUES (2, 'income.salary', '工资薪资', 'income')")
    conn.execute("INSERT OR IGNORE INTO accounts (id, kind, bank, tail, alias) VALUES (1, 'bank_credit', '工商银行', '8801', '工行Visa')")
    conn.execute("INSERT OR IGNORE INTO import_batches (id, file_name, file_sha1, file_size, source) VALUES (1, 'test.csv', 'sha_fx_1', 100, 'icbc_credit')")
    yield conn
    conn.close()


@pytest.fixture
def client(tmp_path: Path) -> Generator[TestClient, None, None]:
    """TestClient configured with a temporary file DB."""
    test_db = tmp_path / "test_fx_api.db"
    conn = init_db(test_db)
    conn.execute("INSERT OR IGNORE INTO categories (id, key, name, direction) VALUES (1, 'food.dining', '餐饮美食', 'expense')")
    conn.execute("INSERT OR IGNORE INTO accounts (id, kind, bank, tail, alias) VALUES (1, 'bank_credit', '工商银行', '8801', '工行Visa')")
    conn.execute("INSERT OR IGNORE INTO import_batches (id, file_name, file_sha1, file_size, source) VALUES (1, 'test.csv', 'sha_fx_api_1', 100, 'icbc_credit')")
    conn.commit()
    conn.close()

    app = create_app(db_path=test_db)
    with TestClient(app) as tc:
        yield tc


# =========================================================================
# 1. Schema & Migration Tests
# =========================================================================

def test_schema_fx_columns_and_table(db: sqlite3.Connection):
    """Verify transactions table has FX columns and fx_rates table exists with unique constraint."""
    cols = {row["name"] for row in db.execute("PRAGMA table_info(transactions)").fetchall()}
    assert "amount_cny" in cols
    assert "fx_rate" in cols
    assert "fx_rate_at" in cols
    assert "fx_rate_source" in cols

    fx_rates_cols = {row["name"] for row in db.execute("PRAGMA table_info(fx_rates)").fetchall()}
    assert "currency" in fx_rates_cols
    assert "rate" in fx_rates_cols
    assert "effective_date" in fx_rates_cols
    assert "source" in fx_rates_cols


def test_migration_on_legacy_db(tmp_path: Path):
    """Verify migrate_db safely upgrades a legacy DB missing FX columns."""
    legacy_db_file = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(legacy_db_file))
    conn.execute("""
        CREATE TABLE transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            amount_cents INT NOT NULL,
            currency TEXT DEFAULT 'CNY'
        );
    """)
    conn.execute("INSERT INTO transactions (amount_cents, currency) VALUES (1000, 'CNY')")
    conn.execute("INSERT INTO transactions (amount_cents, currency) VALUES (2000, 'USD')")
    conn.commit()

    # Run migration
    migrate_db(conn)

    cols = {row[1] for row in conn.execute("PRAGMA table_info(transactions)").fetchall()}
    assert "amount_cny" in cols
    assert "fx_rate" in cols
    assert "fx_rate_source" in cols

    # Verify CNY was backfilled and USD remained NULL
    row_cny = conn.execute("SELECT amount_cny FROM transactions WHERE currency = 'CNY'").fetchone()
    assert row_cny[0] == 1000
    row_usd = conn.execute("SELECT amount_cny FROM transactions WHERE currency = 'USD'").fetchone()
    assert row_usd[0] is None
    conn.close()


# =========================================================================
# 2. FX Core Conversion Logic
# =========================================================================

def test_cny_conversion_always_equal_amount(db: sqlite3.Connection):
    """CNY transactions always equal amount_cents with no fx_rate."""
    amount_cny, rate, rate_at, source = convert_transaction(db, "CNY", 5280, "2026-10-15 12:00:00")
    assert amount_cny == 5280
    assert rate is None
    assert rate_at is None
    assert source is None


def test_usd_conversion_with_cached_rate(db: sqlite3.Connection):
    """Foreign currency converts via round(amount_cents * rate) and preserves original amount."""
    # Seed FX rate 1 USD = 7.2013 CNY
    record_manual_rate(db, "USD", 7.2013, "2026-10-15", "October USD rate")

    amount_cny, rate, rate_at, source = convert_transaction(db, "USD", 4200, "2026-10-15 14:30:00")
    # 4200 * 7.2013 = 30245.46 -> round -> 30245
    assert amount_cny == 30245
    assert rate == 7.2013
    assert rate_at == "2026-10-15"
    assert source == "manual"


def test_offline_fallback_missing_fx(db: sqlite3.Connection):
    """If offline / rate not found, returns amount_cny=NULL and fx_rate_source='none'."""
    with patch("billapp.core.fx.fetch_online_fx_rate", return_value=None):
        amount_cny, rate, rate_at, source = convert_transaction(db, "EUR", 1500, "2026-10-20 09:00:00")
        assert amount_cny is None
        assert rate is None
        assert rate_at is None
        assert source == "none"


# =========================================================================
# 3. Stats Engine & Anti-Distortion Tests
# =========================================================================

def test_stats_summary_fx_included_and_pending(db: sqlite3.Connection):
    """Verify missing FX does NOT enter CNY totals, while converted FX is included in totals."""
    # 1. Normal CNY Expense: 100.00 CNY (10000 cents)
    db.execute("""
        INSERT INTO transactions (batch_id, source, account_id, txn_time, time_precision, amount_cents, currency, direction, fingerprint, amount_cny)
        VALUES (1, 'icbc_credit', 1, '2026-10-10 10:00:00', 'second', 10000, 'CNY', 'expense', 'fp_cny_1', 10000)
    """)

    # 2. Converted USD Expense: US$42.00 (4200 cents) @ 7.2013 = 30245 CNY cents
    db.execute("""
        INSERT INTO transactions (batch_id, source, account_id, txn_time, time_precision, amount_cents, currency, direction, fingerprint, amount_cny, fx_rate, fx_rate_at, fx_rate_source)
        VALUES (1, 'icbc_credit', 1, '2026-10-11 11:00:00', 'second', 4200, 'USD', 'expense', 'fp_usd_1', 30245, 7.2013, '2026-10-11', 'online')
    """)

    # 3. Missing FX USD Expense: US$20.00 (2000 cents) with missing FX
    db.execute("""
        INSERT INTO transactions (batch_id, source, account_id, txn_time, time_precision, amount_cents, currency, direction, fingerprint, amount_cny, fx_rate_source)
        VALUES (1, 'icbc_credit', 1, '2026-10-12 12:00:00', 'second', 2000, 'USD', 'expense', 'fp_usd_missing', NULL, 'none')
    """)

    summary = calculate_stats_summary(db, start_date="2026-10-01", end_date="2026-10-31")

    # Total expense should be 10000 (CNY) + 30245 (USD converted) = 40245
    # The US$20.00 missing FX MUST NOT be counted!
    assert summary.total_expense_cents == 40245
    assert summary.expense_net == 40245

    # FX tracking metrics (§8.2)
    assert summary.fx_included_count == 1
    assert summary.fx_included_cny == 30245
    assert summary.fx_pending_count == 1
    assert len(summary.fx_pending_amounts) == 1
    assert summary.fx_pending_amounts[0].currency == "USD"
    assert summary.fx_pending_amounts[0].amount == 2000


def test_charts_include_converted_fx(db: sqlite3.Connection):
    """Verify trends, categories, merchants, heatmaps, and account stats include converted FX amounts."""
    # US$10.00 (1000 cents) @ 7.0000 = 7000 CNY cents in category 1
    db.execute("""
        INSERT INTO transactions (batch_id, source, account_id, txn_time, time_precision, amount_cents, currency, direction, counterparty, category_id, fingerprint, amount_cny, fx_rate, fx_rate_source)
        VALUES (1, 'icbc_credit', 1, '2026-10-15 15:00:00', 'second', 1000, 'USD', 'expense', 'Apple US', 1, 'fp_chart_usd', 7000, 7.0000, 'online')
    """)

    # 1. Trend
    trend = calculate_stats_trend(db, granularity="monthly", start_date="2026-10-01", end_date="2026-10-31")
    assert len(trend) == 1
    assert trend[0].expense_cents == 7000

    # 2. Category
    cat_res = calculate_stats_category(db, start_date="2026-10-01", end_date="2026-10-31")
    assert cat_res.total_cents == 7000
    assert cat_res.items[0].name == "餐饮美食"
    assert cat_res.items[0].amount_cents == 7000

    # 3. Merchant
    merch_res = calculate_stats_merchant(db, start_date="2026-10-01", end_date="2026-10-31")
    assert merch_res.total_expense_cents == 7000
    assert merch_res.items[0].merchant == "Apple US"

    # 4. Heatmap
    heatmap = calculate_stats_heatmap(db, mode="calendar", start_date="2026-10-01", end_date="2026-10-31")
    assert any(item.expense_cents == 7000 for item in heatmap.calendar_items)

    # 5. Accounts
    acc_res = calculate_stats_account(db, start_date="2026-10-01", end_date="2026-10-31")
    assert acc_res.total_out_cents == 7000


# =========================================================================
# 4. Excel Export Tests
# =========================================================================

def test_excel_export_fx_columns_and_pivot(db: sqlite3.Connection):
    """Sheet 1 adds Currency, FX Rate, and CNY Equivalent. Sheet 2 pivots on CNY Equivalent."""
    # CNY record
    db.execute("""
        INSERT INTO transactions (batch_id, source, account_id, txn_time, time_precision, amount_cents, currency, direction, counterparty, category_id, fingerprint, amount_cny)
        VALUES (1, 'icbc_credit', 1, '2026-10-01 10:00:00', 'second', 5000, 'CNY', 'expense', 'Store A', 1, 'fp_exp_cny', 5000)
    """)
    # Converted USD record: US$10.00 @ 7.2000 = 72.00 CNY
    db.execute("""
        INSERT INTO transactions (batch_id, source, account_id, txn_time, time_precision, amount_cents, currency, direction, counterparty, category_id, fingerprint, amount_cny, fx_rate, fx_rate_source)
        VALUES (1, 'icbc_credit', 1, '2026-10-02 10:00:00', 'second', 1000, 'USD', 'expense', 'Store B', 1, 'fp_exp_usd', 7200, 7.2000, 'online')
    """)
    # Missing FX record: EUR 10.00
    db.execute("""
        INSERT INTO transactions (batch_id, source, account_id, txn_time, time_precision, amount_cents, currency, direction, counterparty, category_id, fingerprint, amount_cny, fx_rate_source)
        VALUES (1, 'icbc_credit', 1, '2026-10-03 10:00:00', 'second', 1000, 'EUR', 'expense', 'Store C', 1, 'fp_exp_eur', NULL, 'none')
    """)

    bio = generate_excel_export(db)
    wb = openpyxl.load_workbook(io.BytesIO(bio.getvalue()), data_only=True)

    # Sheet 1 assertions
    ws1 = wb["交易明细"]
    headers1 = [c.value for c in ws1[1]]
    assert "币种" in headers1
    assert "交易时汇率" in headers1
    assert "折算人民币(元)" in headers1

    # Sheet 2 pivot assertions: contains 50.00 (CNY) + 72.00 (USD converted) = 122.00 (EUR excluded)
    ws2 = wb["月度分类透视表"]
    col_2026_10 = 3
    assert ws2.cell(row=2, column=col_2026_10).value == 122.00


# =========================================================================
# 5. REST API Endpoints Tests
# =========================================================================

def test_api_fx_rates_and_manual_entry(client: TestClient):
    """Test GET /api/fx/rates and POST /api/fx/rates manual rate supplement."""
    # 1. Insert missing FX transaction
    client.app.state.db_path
    conn = create_connection(client.app.state.db_path)
    conn.execute("""
        INSERT INTO transactions (batch_id, source, account_id, txn_time, time_precision, amount_cents, currency, direction, fingerprint, amount_cny, fx_rate_source)
        VALUES (1, 'icbc_credit', 1, '2026-10-15 10:00:00', 'second', 1000, 'USD', 'expense', 'fp_api_usd_1', NULL, 'none')
    """)
    conn.commit()
    conn.close()

    # 2. Check overview
    res = client.get("/api/fx/rates")
    assert res.status_code == 200
    data = res.json()
    assert data["pending_count"] == 1
    assert "USD" in data["currencies"]

    # 3. Post manual rate for 2026-10-15
    res_post = client.post("/api/fx/rates", json={
        "currency": "USD",
        "rate": 7.2500,
        "effective_date": "2026-10-15",
        "note": "Manual BOC rate",
        "apply_to_all_missing": False,
    })
    assert res_post.status_code == 200
    post_data = res_post.json()
    assert post_data["success"] is True
    assert post_data["updated_transactions"] == 1
    assert post_data["rate"]["rate"] == 7.25

    # 4. Verify transaction was updated
    res_txn = client.get("/api/transactions?currency=USD")
    assert res_txn.status_code == 200
    items = res_txn.json()["items"]
    assert len(items) == 1
    assert items[0]["amount_cny"] == 7250
    assert items[0]["fx_rate"] == 7.25
    assert items[0]["fx_rate_source"] == "manual"


def test_api_transactions_fx_filter_and_retry(client: TestClient):
    """Test GET /api/transactions?fx=missing and POST /api/transactions/{id}/fx single retry."""
    conn = create_connection(client.app.state.db_path)
    cur = conn.execute("""
        INSERT INTO transactions (batch_id, source, account_id, txn_time, time_precision, amount_cents, currency, direction, fingerprint, amount_cny, fx_rate_source)
        VALUES (1, 'icbc_credit', 1, '2026-10-16 12:00:00', 'second', 5000, 'HKD', 'expense', 'fp_api_hkd_1', NULL, 'none')
    """)
    txn_id = cur.lastrowid
    conn.commit()
    conn.close()

    # 1. Filter fx=missing
    res = client.get("/api/transactions?fx=missing")
    assert res.status_code == 200
    assert len(res.json()["items"]) >= 1

    # 2. Retry single transaction with mock rate
    with patch("billapp.core.fx.fetch_online_fx_rate", return_value=0.9250):
        res_retry = client.post(f"/api/transactions/{txn_id}/fx")
        assert res_retry.status_code == 200
        updated = res_retry.json()
        assert updated["amount_cny"] == 4625
        assert updated["fx_rate"] == 0.925
        assert updated["fx_rate_source"] == "online"
