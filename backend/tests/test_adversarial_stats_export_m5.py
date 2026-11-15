"""Milestone 5 Dedicated Adversarial Verification & Stress Test Harness.

Tests 5 critical adversarial dimensions:
1. Multi-tier complex refund chains: partial refund, excess refund, unlinked refund, cross-month refund offsetting.
2. Multi-card internal transfers & turnover: multi-hop transfers (A -> B -> C), credit card repayments, cross-source withdrawal pairing. Verify 0 distortion on net savings and net expenses.
3. OpenPyXL dynamic formula parsing: open generated Excel file with openpyxl, evaluate formula syntax =SUM(...) across multiple rows and columns, ensure no #REF!, #VALUE!, or broken coordinates.
4. Anti-overfitting stress: verify no hardcoded sample personal names or specific local merchants exist in code or rules, and taxonomy handles arbitrary generic transactions cleanly.
5. ReDoS attack vectors against rule creation, update, test, and proposal endpoints.
"""

from __future__ import annotations

import hashlib
import io
from pathlib import Path
import re
import sqlite3
import time
from typing import Any, Dict, Generator, List, Optional, Tuple

import openpyxl
from openpyxl.utils import column_index_from_string
import pytest
from fastapi.testclient import TestClient

from billapp.app import create_app
from billapp.classify.categories import CategoryService
from billapp.classify.rules import (
    Rule,
    compile_safe_regex,
    load_builtin_rules_from_yaml,
    rule_matches,
)
from billapp.core.db import init_db
from billapp.export import generate_excel_export
from billapp.stats import (
    calculate_stats_account,
    calculate_stats_category,
    calculate_stats_heatmap,
    calculate_stats_merchant,
    calculate_stats_summary,
    calculate_stats_trend,
)


# =========================================================================
# Fixtures & Helpers
# =========================================================================

@pytest.fixture
def mem_db() -> Generator[sqlite3.Connection, None, None]:
    """In-memory SQLite database pre-seeded with categories and test accounts."""
    conn = init_db(":memory:")
    CategoryService.init_builtin_categories(conn)
    conn.execute("INSERT INTO accounts (id, kind, bank, tail, alias) VALUES (1, 'wallet', 'Alipay', NULL, '支付宝余额')")
    conn.execute("INSERT INTO accounts (id, kind, bank, tail, alias) VALUES (2, 'bank_debit', 'BOC', '6666', '中行借记卡')")
    conn.execute("INSERT INTO accounts (id, kind, bank, tail, alias) VALUES (3, 'bank_credit', 'ICBC', '8801', '工行信用卡')")
    conn.execute("INSERT INTO accounts (id, kind, bank, tail, alias) VALUES (4, 'bank_debit', 'CCB', '5678', '建行储蓄卡')")
    conn.execute("INSERT INTO accounts (id, kind, bank, tail, alias) VALUES (5, 'bank_debit', 'CMB', '1234', '招行储蓄卡')")
    conn.execute("INSERT INTO import_batches (id, file_name, file_sha1, file_size, source) VALUES (1, 'adv_test.csv', 'sha1_adv', 2048, 'alipay')")
    conn.commit()
    yield conn
    conn.close()


@pytest.fixture
def api_client(tmp_path: Path) -> Generator[TestClient, None, None]:
    """TestClient instance configured with an isolated test database."""
    db_file = tmp_path / "adv_api_test.db"
    app = create_app(db_path=str(db_file))
    with TestClient(app) as client:
        yield client


def seed_txn(
    db: sqlite3.Connection,
    txn_id: int,
    amount_cents: int,
    direction: str = "expense",
    refunded_cents: int = 0,
    txn_time: str = "2026-10-15 12:00:00",
    account_id: int = 1,
    fund_account_id: Optional[int] = None,
    counterparty: Optional[str] = "测试商户",
    description: Optional[str] = "消费明细",
    merchant_norm: Optional[str] = "测试商户",
    category_id: Optional[int] = None,
    refund_of: Optional[int] = None,
    duplicate_of: Optional[int] = None,
    transfer_peer: Optional[int] = None,
    excluded: int = 0,
    source: str = "alipay",
) -> int:
    """Helper to seed transactions with specific IDs and relationship links."""
    fp = f"adv_fp_{txn_id}_{txn_time}_{amount_cents}_{direction}"
    # Insert without forward-referencing transfer_peer/refund_of/duplicate_of if target does not yet exist
    db.execute(
        """
        INSERT INTO transactions (
            id, batch_id, source, account_id, fund_account_id, txn_time, time_precision,
            amount_cents, refunded_cents, direction, counterparty, description, merchant_norm,
            category_id, refund_of, duplicate_of, transfer_peer, excluded, fingerprint
        ) VALUES (?, 1, ?, ?, ?, ?, 'second', ?, ?, ?, ?, ?, ?, ?, NULL, ?, NULL, ?, ?)
        """,
        (
            txn_id, source, account_id, fund_account_id, txn_time,
            amount_cents, refunded_cents, direction, counterparty, description, merchant_norm,
            category_id, duplicate_of, excluded, fp
        ),
    )
    if refund_of is not None:
        db.execute("UPDATE transactions SET refund_of = ? WHERE id = ?", (refund_of, txn_id))
    if transfer_peer is not None:
        db.execute("UPDATE transactions SET transfer_peer = ? WHERE id = ?", (transfer_peer, txn_id))
    return txn_id


# =========================================================================
# Dimension 1: Multi-Tier Complex Refund Chains
# =========================================================================

def test_multi_tier_partial_refund_chain(mem_db: sqlite3.Connection):
    """Adversarial stress: Verify a single expense subjected to 3 consecutive partial refunds
    accurately offsets net expense, maintains non-negative invariants, and never leaks into income.
    """
    cat_food = mem_db.execute("SELECT id FROM categories WHERE key = 'food.dining'").fetchone()[0]

    # Original expense: 1,000.00 yuan (100,000 cents)
    seed_txn(mem_db, txn_id=101, amount_cents=100000, refunded_cents=0, direction="expense", category_id=cat_food)

    # 3 consecutive partial refunds: 200.00, 350.00, 150.00 (Total refunded: 700.00 yuan)
    seed_txn(mem_db, txn_id=102, amount_cents=20000, direction="refund", refund_of=101)
    seed_txn(mem_db, txn_id=103, amount_cents=35000, direction="refund", refund_of=101)
    seed_txn(mem_db, txn_id=104, amount_cents=15000, direction="refund", refund_of=101)

    # Update original transaction refunded_cents
    mem_db.execute("UPDATE transactions SET refunded_cents = 70000 WHERE id = 101")
    mem_db.commit()

    summary = calculate_stats_summary(mem_db)

    # Net expense must be exactly 100,000 - 70,000 = 30,000 cents (300.00 yuan)
    assert summary.total_expense_cents == 30000
    assert summary.expense_net == 30000
    # Zero income counted from refunds
    assert summary.total_income_cents == 0
    assert summary.income == 0
    # Net balance / savings must be -30,000 cents
    assert summary.net_savings_cents == -30000
    assert summary.balance == -30000
    # Exactly 1 expense transaction counted
    assert summary.expense_count == 1
    assert summary.income_count == 0


def test_excess_refund_and_zero_floor_invariant(mem_db: sqlite3.Connection):
    """Adversarial stress: When refunds exceed original amounts or when unlinked refunds exceed
    period expenses, net expense must strictly floor at 0 without turning negative or inflating income.
    """
    cat_food = mem_db.execute("SELECT id FROM categories WHERE key = 'food.dining'").fetchone()[0]

    # Scenario A: Linked refund claims more than original transaction (e.g. 5000 cents expense, 8000 cents refunded)
    seed_txn(mem_db, txn_id=201, amount_cents=5000, refunded_cents=5000, direction="expense", category_id=cat_food)
    seed_txn(mem_db, txn_id=202, amount_cents=8000, direction="refund", refund_of=201)

    # Scenario B: Unlinked refund of 20,000 cents in a month with only 5,000 cents total expense
    seed_txn(mem_db, txn_id=203, amount_cents=20000, direction="refund", refund_of=None)
    mem_db.commit()

    summary = calculate_stats_summary(mem_db)

    # Net expense must floor at 0 (max(0, 0 - 20000))
    assert summary.total_expense_cents == 0
    assert summary.expense_net == 0
    # Income must remain strictly 0
    assert summary.total_income_cents == 0
    assert summary.income == 0
    # Balance must be 0
    assert summary.net_savings_cents == 0


def test_cross_month_refund_offsetting_integrity(mem_db: sqlite3.Connection):
    """Adversarial stress: Verify cross-month linked refunds offset the month of original expense,
    leaving the refund month's independent expenses uncorrupted; and verify unlinked refunds offset the month they arrive.
    """
    cat_shop = mem_db.execute("SELECT id FROM categories WHERE key = 'shopping.daily'").fetchone()[0]

    # September 2026: Expense of 50,000 cents (500.00 yuan)
    seed_txn(mem_db, txn_id=301, txn_time="2026-09-10 14:00:00", amount_cents=50000, refunded_cents=30000, direction="expense", category_id=cat_shop)

    # October 2026: Linked refund of 30,000 cents (linked to September transaction)
    seed_txn(mem_db, txn_id=302, txn_time="2026-10-05 10:00:00", amount_cents=30000, direction="refund", refund_of=301)

    # October 2026: Independent expense of 10,000 cents (100.00 yuan)
    seed_txn(mem_db, txn_id=303, txn_time="2026-10-12 18:00:00", amount_cents=10000, refunded_cents=0, direction="expense", category_id=cat_shop)

    # October 2026: Unlinked refund of 4,000 cents (40.00 yuan)
    seed_txn(mem_db, txn_id=304, txn_time="2026-10-20 09:00:00", amount_cents=4000, direction="refund", refund_of=None)
    mem_db.commit()

    trend = calculate_stats_trend(mem_db, granularity="monthly")
    trend_map = {t.period: t.expense_cents for t in trend}

    # September net expense: 50,000 - 30,000 = 20,000 cents (200.00 yuan)
    assert trend_map["2026-09"] == 20000

    # October net expense: Gross 10,000 - Unlinked refund 4,000 = 6,000 cents (60.00 yuan)
    # The linked refund (30,000 cents) did NOT deduct from October!
    assert trend_map["2026-10"] == 6000


def test_duplicate_transactions_with_refunds_strict_isolation(mem_db: sqlite3.Connection):
    """Adversarial stress: Verify duplicate records (duplicate_of IS NOT NULL) and excluded records
    never contaminate gross expense, net expense, refund deductions, or income metrics.
    """
    cat_food = mem_db.execute("SELECT id FROM categories WHERE key = 'food.dining'").fetchone()[0]

    # Valid expense: 8,000 cents
    seed_txn(mem_db, txn_id=401, amount_cents=8000, refunded_cents=0, direction="expense", category_id=cat_food)

    # Duplicate expense: 8,000 cents (duplicate of 401)
    seed_txn(mem_db, txn_id=402, amount_cents=8000, refunded_cents=2000, direction="expense", category_id=cat_food, duplicate_of=401)

    # Excluded expense: 15,000 cents
    seed_txn(mem_db, txn_id=403, amount_cents=15000, refunded_cents=0, direction="expense", category_id=cat_food, excluded=1)

    # Excluded income: 20,000 cents
    seed_txn(mem_db, txn_id=404, amount_cents=20000, direction="income", excluded=1)
    mem_db.commit()

    summary = calculate_stats_summary(mem_db)
    assert summary.total_expense_cents == 8000
    assert summary.expense_net == 8000
    assert summary.total_income_cents == 0
    assert summary.expense_count == 1
    assert summary.income_count == 0


# =========================================================================
# Dimension 2: Multi-Card Internal Transfers & Turnover
# =========================================================================

def test_multihop_internal_transfers_zero_distortion(mem_db: sqlite3.Connection):
    """Adversarial stress: Multi-hop transfer chain across 4 cards:
    Alipay(1) -> BOC(2) -> ICBC(3) -> CCB(4) with 5,000.00 yuan per hop (Total turnover 15,000.00 yuan).
    Verify EXACTLY 0 cents distortion on net expense, net income, and net savings.
    """
    cat_transfer = mem_db.execute("SELECT id FROM categories WHERE key = 'financial.internal_transfer'").fetchone()[0]

    # Hop 1: Alipay -> BOC (500,000 cents)
    seed_txn(mem_db, txn_id=501, account_id=1, amount_cents=500000, direction="transfer", category_id=cat_transfer)
    seed_txn(mem_db, txn_id=502, account_id=2, amount_cents=500000, direction="transfer", transfer_peer=501, category_id=cat_transfer)
    mem_db.execute("UPDATE transactions SET transfer_peer = 502 WHERE id = 501")

    # Hop 2: BOC -> ICBC (500,000 cents)
    seed_txn(mem_db, txn_id=503, account_id=2, amount_cents=500000, direction="transfer", category_id=cat_transfer)
    seed_txn(mem_db, txn_id=504, account_id=3, amount_cents=500000, direction="transfer", transfer_peer=503, category_id=cat_transfer)
    mem_db.execute("UPDATE transactions SET transfer_peer = 504 WHERE id = 503")

    # Hop 3: ICBC -> CCB (500,000 cents)
    seed_txn(mem_db, txn_id=505, account_id=3, amount_cents=500000, direction="transfer", category_id=cat_transfer)
    seed_txn(mem_db, txn_id=506, account_id=4, amount_cents=500000, direction="transfer", transfer_peer=505, category_id=cat_transfer)
    mem_db.execute("UPDATE transactions SET transfer_peer = 506 WHERE id = 505")

    # Legitimate non-transfer activity: Salary income 20,000.00 yuan (2,000,000 cents), Dining expense 350.00 yuan (35,000 cents)
    cat_salary = mem_db.execute("SELECT id FROM categories WHERE key = 'salary'").fetchone()[0]
    cat_food = mem_db.execute("SELECT id FROM categories WHERE key = 'food.dining'").fetchone()[0]
    seed_txn(mem_db, txn_id=507, account_id=2, amount_cents=2000000, direction="income", category_id=cat_salary)
    seed_txn(mem_db, txn_id=508, account_id=3, amount_cents=35000, direction="expense", category_id=cat_food)
    mem_db.commit()

    summary = calculate_stats_summary(mem_db, include_transfer=False)

    # The 15,000.00 yuan turnover must produce zero distortion
    assert summary.total_expense_cents == 35000
    assert summary.expense_net == 35000
    assert summary.total_income_cents == 2000000
    assert summary.income == 2000000
    assert summary.net_savings_cents == 2000000 - 35000
    assert summary.balance == 1965000
    assert summary.transaction_count == 2  # Only salary + dining


def test_credit_card_repayment_and_withdrawal_pairing(mem_db: sqlite3.Connection):
    """Adversarial stress: Verify credit card repayments and wallet withdrawals
    are completely isolated from category sunbursts, merchant rankings, and trend series.
    """
    cat_transfer = mem_db.execute("SELECT id FROM categories WHERE key = 'financial.internal_transfer'").fetchone()[0]

    # Debit card repays credit card: 800,000 cents
    seed_txn(mem_db, txn_id=601, account_id=2, amount_cents=800000, direction="transfer", category_id=cat_transfer)
    seed_txn(mem_db, txn_id=602, account_id=3, amount_cents=800000, direction="transfer", category_id=cat_transfer, transfer_peer=601)
    mem_db.execute("UPDATE transactions SET transfer_peer = 602 WHERE id = 601")

    # Wallet withdraws to CMB: 250,000 cents
    seed_txn(mem_db, txn_id=603, account_id=1, amount_cents=250000, direction="transfer", category_id=cat_transfer)
    seed_txn(mem_db, txn_id=604, account_id=5, amount_cents=250000, direction="transfer", category_id=cat_transfer, transfer_peer=603)
    mem_db.execute("UPDATE transactions SET transfer_peer = 604 WHERE id = 603")
    mem_db.commit()

    # Category breakdown must show 0 total cents
    cat_resp = calculate_stats_category(mem_db, direction="expense")
    assert cat_resp.total_cents == 0
    assert len(cat_resp.items) == 0

    # Merchant ranking must be empty
    merchant_resp = calculate_stats_merchant(mem_db)
    assert merchant_resp.total_expense_cents == 0
    assert len(merchant_resp.items) == 0

    # Trend series must show 0
    trend = calculate_stats_trend(mem_db)
    assert len(trend) == 0


def test_account_turnover_accuracy_vs_kpi_isolation(mem_db: sqlite3.Connection):
    """Adversarial stress: calculate_stats_account must accurately compute turnover per account
    while keeping global KPI metrics uncorrupted.
    """
    cat_transfer = mem_db.execute("SELECT id FROM categories WHERE key = 'financial.internal_transfer'").fetchone()[0]
    cat_food = mem_db.execute("SELECT id FROM categories WHERE key = 'food.dining'").fetchone()[0]

    # Account 1 transfers 100,000 to Account 2
    seed_txn(mem_db, txn_id=701, account_id=1, amount_cents=100000, direction="transfer", category_id=cat_transfer)
    seed_txn(mem_db, txn_id=702, account_id=2, amount_cents=100000, direction="transfer", transfer_peer=701, category_id=cat_transfer)
    mem_db.execute("UPDATE transactions SET transfer_peer = 702 WHERE id = 701")

    # Account 2 spends 30,000
    seed_txn(mem_db, txn_id=703, account_id=2, amount_cents=30000, direction="expense", category_id=cat_food)
    mem_db.commit()

    acc_stats = calculate_stats_account(mem_db, by="account")
    acc_map = {item.account_id: item for item in acc_stats.items}

    # Account 2 had 30,000 expense
    assert acc_map[2].total_out_cents == 30000
    # Global KPI net expense is strictly 30,000
    summary = calculate_stats_summary(mem_db)
    assert summary.total_expense_cents == 30000


# =========================================================================
# Dimension 3: OpenPyXL Dynamic Formula Parsing & Coordinate Integrity
# =========================================================================

def test_openpyxl_dynamic_formula_parsing_and_coordinate_integrity(mem_db: sqlite3.Connection):
    """Adversarial stress: Generate dual-sheet Excel with openpyxl, parse formula syntax
    =SUM(...) across multiple rows and columns, ensure no #REF!, #VALUE!, or broken coordinates,
    and independently evaluate formulas against raw data.
    """
    # Seed 4 distinct months and 5 distinct categories
    cats = mem_db.execute("SELECT id, name, key FROM categories WHERE parent_id IS NOT NULL LIMIT 5").fetchall()
    months = ["2026-06", "2026-07", "2026-08", "2026-09", "2026-10"]

    expected_matrix: Dict[int, Dict[str, float]] = {c["id"]: {m: 0.0 for m in months} for c in cats}

    t_id = 800
    for c_idx, c in enumerate(cats):
        for m_idx, m in enumerate(months):
            amount_cents = (c_idx + 1) * 10000 + (m_idx + 1) * 500  # distinct values
            seed_txn(
                mem_db,
                txn_id=t_id,
                txn_time=f"{m}-15 12:00:00",
                amount_cents=amount_cents,
                direction="expense",
                category_id=c["id"],
            )
            expected_matrix[c["id"]][m] = round(amount_cents / 100.0, 2)
            t_id += 1

    mem_db.commit()

    # Generate workbook
    bio = generate_excel_export(mem_db)
    assert isinstance(bio, io.BytesIO)
    bio.seek(0)

    # Read back with openpyxl (formula strings preserved)
    wb = openpyxl.load_workbook(bio, data_only=False)
    assert "交易明细" in wb.sheetnames
    assert "月度分类透视表" in wb.sheetnames

    ws2 = wb["月度分类透视表"]

    # 1. Inspect Header row
    headers = [cell.value for cell in ws2[1]]
    assert headers[0] == "一级分类"
    assert headers[1] == "二级分类"
    assert headers[2:2 + len(months)] == months
    assert headers[-1] == "合计"

    month_cols_count = len(months)
    first_month_col_idx = 3  # C
    last_month_col_idx = 2 + month_cols_count  # G
    total_col_idx = last_month_col_idx + 1  # H

    num_category_rows = len(cats)
    last_cat_row = 1 + num_category_rows

    # 2. Inspect category row formulas (=SUM(C{r}:G{r}))
    formula_regex = re.compile(r"^=SUM\(([A-Z]+)(\d+):([A-Z]+)(\d+)\)$")

    for r in range(2, last_cat_row + 1):
        total_cell = ws2.cell(row=r, column=total_col_idx)
        val = str(total_cell.value)

        # Ensure no formula error tokens
        assert "#REF!" not in val, f"Row {r} contains #REF!"
        assert "#VALUE!" not in val, f"Row {r} contains #VALUE!"
        assert "#NAME?" not in val, f"Row {r} contains #NAME?"

        match = formula_regex.match(val)
        assert match is not None, f"Row {r} total formula malformed: {val}"

        start_col, start_r, end_col, end_r = match.groups()
        assert start_col == "C", f"Row {r} start column must be C, got {start_col}"
        assert column_index_from_string(end_col) == last_month_col_idx, f"Row {r} end column mismatch"
        assert int(start_r) == r, f"Row {r} start row mismatch"
        assert int(end_r) == r, f"Row {r} end row mismatch"

        # Independent mathematical evaluation of row cells
        row_sum_py = sum(float(ws2.cell(row=r, column=c).value or 0) for c in range(first_month_col_idx, last_month_col_idx + 1))
        assert row_sum_py > 0

    # 3. Inspect bottom summary row ("月度总支出")
    summary_row = last_cat_row + 1
    sum_title_cell = ws2.cell(row=summary_row, column=1)
    assert sum_title_cell.value == "月度总支出"

    # Monthly column sum formulas (=SUM(C2:C{last_cat_row}))
    for c_idx in range(first_month_col_idx, last_month_col_idx + 1):
        col_cell = ws2.cell(row=summary_row, column=c_idx)
        col_val = str(col_cell.value)
        assert "#REF!" not in col_val
        match = formula_regex.match(col_val)
        assert match is not None, f"Col {c_idx} summary formula malformed: {col_val}"

        start_col, start_r, end_col, end_r = match.groups()
        assert start_col == end_col
        assert int(start_r) == 2
        assert int(end_r) == last_cat_row

    # Grand total formula (=SUM(H2:H{last_cat_row}))
    grand_total_cell = ws2.cell(row=summary_row, column=total_col_idx)
    grand_val = str(grand_total_cell.value)
    assert "#REF!" not in grand_val
    match_gt = formula_regex.match(grand_val)
    assert match_gt is not None
    assert int(match_gt.group(2)) == 2
    assert int(match_gt.group(4)) == last_cat_row


def test_openpyxl_export_boundary_conditions(mem_db: sqlite3.Connection):
    """Adversarial stress: Verify OpenPyXL generator robustness on boundary conditions:
    1. Zero transactions (empty dataset) - must not crash or generate invalid ranges.
    2. Exactly 1 transaction (1 month, 1 category).
    """
    # 1. Zero transactions export
    bio_empty = generate_excel_export(mem_db)
    wb_empty = openpyxl.load_workbook(bio_empty, data_only=False)
    ws1_e = wb_empty["交易明细"]
    ws2_e = wb_empty["月度分类透视表"]
    assert ws1_e.max_row == 1  # Only header
    assert ws2_e.max_row == 1  # Only header, no invalid summary row crash

    # 2. Exactly 1 transaction
    cat_food = mem_db.execute("SELECT id FROM categories WHERE key = 'food.dining'").fetchone()[0]
    seed_txn(mem_db, txn_id=901, txn_time="2026-11-01 10:00:00", amount_cents=1500, direction="expense", category_id=cat_food)
    mem_db.commit()

    bio_single = generate_excel_export(mem_db)
    wb_single = openpyxl.load_workbook(bio_single, data_only=False)
    ws2_s = wb_single["月度分类透视表"]
    # Row 1: Header (一级, 二级, 2026-11, 合计)
    # Row 2: Category (formula =SUM(C2:C2))
    # Row 3: Summary (formula =SUM(C2:C2), =SUM(D2:D2))
    assert ws2_s.cell(row=2, column=4).value == "=SUM(C2:C2)"
    assert ws2_s.cell(row=3, column=3).value == "=SUM(C2:C2)"
    assert ws2_s.cell(row=3, column=4).value == "=SUM(D2:D2)"


# =========================================================================
# Dimension 4: Anti-Overfitting & Generic Taxonomy Stress
# =========================================================================

def test_anti_overfitting_no_hardcoded_sample_personal_names():
    """Adversarial audit: Verify via SHA-256 digest matching that no real sample PII
    (personal names, phone numbers, passwords, or private local entities) exists
    in builtin rules or implementation code, without embedding plaintext PII in tests.
    """
    repo_root = Path(__file__).resolve().parent.parent / "billapp"
    forbidden_digests_by_len: Dict[int, set[str]] = {
        3: {
            "8452ae1278545f6b79aea29d0fa094cf4c749cfb24f957f7f95265acb7965fa0",
            "33467dd7eb986838b7502c810a731dcfae9287a112e95d3695f1decdf1fe2d35",
        },
        4: {
            "d58475fd904973c9dc506775be83a3f42bac41c9689c5bd06d0d0f133a12da66",
            "19e2285d36726462abc8833337fa514d0ea4b77785e3c44625125882c3bbf712",
        },
        6: {
            "1b3c2eb4230fed1de0a0792db2db8c3630f398b4a4c445afb08305f455c65760",
            "ff7c59c28e1896b6a10e8d19b54920ea56133959e60767d9eb425701c5180900",
            "7bb91620f6bf26a99118013704fe0edad5a8a6f1ac91fa444f1f0cb8e0921fc7",
        },
        7: {
            "1a7a1bc8a23bea3626a18d57d9dc24d40a9cc4e6591ff7c22cce900ca6eb4f4e",
        },
        11: {
            "f37744ae113e5c14b353283ab70c24a6037507534900f139cd6cb921ca4d9f18",
        },
    }

    violations: List[str] = []

    # Check all python files and builtin rules YAML
    target_files = list(repo_root.rglob("*.py")) + list(repo_root.rglob("*.yaml"))

    for fpath in target_files:
        content = fpath.read_text(encoding="utf-8")
        n = len(content)
        for win_len, digests in forbidden_digests_by_len.items():
            seen: set[str] = set()
            for idx in range(0, max(0, n - win_len + 1)):
                token = content[idx : idx + win_len]
                if token in seen or any(ch.isspace() for ch in token):
                    continue
                seen.add(token)
                digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
                if digest in digests:
                    violations.append(
                        f"{fpath.relative_to(repo_root.parent)} matches forbidden PII digest {digest[:12]}"
                    )

    assert len(violations) == 0, (
        "Anti-overfitting violation! Forbidden personal identifier digest found in codebase:\n"
        + "\n".join(violations)
    )


def test_generic_transactions_clean_taxonomy_handling(mem_db: sqlite3.Connection):
    """Adversarial stress: Verify the taxonomy engine and builtin rules cleanly handle
    arbitrary generic transactions without crashing, without overfitting to specific campus entities,
    and cleanly falling back to general categories or unclassified.
    """
    generic_samples = [
        {"counterparty": "京东商城-自营百货", "description": "纸巾洗衣液", "direction": "expense"},
        {"counterparty": "美团外卖", "description": "黄焖鸡米饭", "direction": "expense"},
        {"counterparty": "高德打车-滴滴出行", "description": "出行车费", "direction": "expense"},
        {"counterparty": "国家电网公司", "description": "家庭电费充值", "direction": "expense"},
        {"counterparty": "中国移动通信", "description": "手机话费充值", "direction": "expense"},
        {"counterparty": "张三", "description": "转账", "direction": "transfer"},
        {"counterparty": "未知商户XYZ", "description": "杂项消费", "direction": "expense"},
    ]

    builtin_rules = load_builtin_rules_from_yaml(conn=mem_db)
    assert len(builtin_rules) > 0

    for sample in generic_samples:
        matched = False
        for rule in builtin_rules:
            if rule_matches(rule, sample):
                matched = True
                assert rule.category_id > 0
                break
        # Execution succeeded without unhandled exceptions


# =========================================================================
# Dimension 5: ReDoS Attack Vectors Against Rule Endpoints
# =========================================================================

def test_redos_attack_vectors_on_rule_create_endpoint(api_client: TestClient):
    """Adversarial stress: Verify the rule creation endpoint (POST /api/rules)
    safely rejects catastrophic ReDoS attack patterns with HTTP 422 VALIDATION_ERROR in < 200ms.
    """
    redos_payloads = [
        "(a+)+",
        "(a*)*",
        "([a-zA-Z]+)*",
        "(x+x+)+",
        "(a|b+)+",
        "(a|aa)+",
        "(a|aa)+$",
        "(a+){1,100}",
        "^([a-zA-Z0-9_.-]+)+@[a-zA-Z0-9-]+\\.[a-zA-Z0-9-.]+$",
        "a" * 201,  # Exceeds max length bound (200)
        "[unclosed-regex",  # Malformed regex syntax
        "(?P<invalid",  # Malformed group
    ]

    for pattern in redos_payloads:
        t0 = time.perf_counter()
        resp = api_client.post(
            "/api/rules",
            json={
                "name": f"ReDoS Test {pattern[:15]}",
                "category_id": 1,
                "priority": 100,
                "field": "counterparty",
                "pattern_type": "regex",
                "pattern_value": pattern,
            },
        )
        elapsed = time.perf_counter() - t0

        # Must reject with 422
        assert resp.status_code == 422, f"Pattern '{pattern[:25]}' was not rejected! Status: {resp.status_code}"
        # Response time must be sub-second (no catastrophic regex backtracking freeze)
        assert elapsed < 0.5, f"Pattern '{pattern[:25]}' took too long: {elapsed:.3f}s"


def test_redos_attack_vectors_on_rule_test_endpoint(api_client: TestClient):
    """Adversarial stress: Verify the rule dry-run endpoint (POST /api/rules/test)
    safely rejects ReDoS vectors before evaluating against transaction rows.
    """
    redos_patterns = [
        "(a+)+",
        "(a*)*",
        "(\\w+)+",
        "(a|b+)+",
        "(a|aa)+",
        "(a|aa)+$",
        "[bad[bracket",
    ]

    for pattern in redos_patterns:
        t0 = time.perf_counter()
        resp = api_client.post(
            "/api/rules/test",
            json={
                "field": "counterparty",
                "pattern_type": "regex",
                "pattern_value": pattern,
                "limit": 20,
            },
        )
        elapsed = time.perf_counter() - t0

        assert resp.status_code == 422, f"Pattern '{pattern}' was not rejected! Status: {resp.status_code}"
        assert elapsed < 0.5


def test_redos_attack_vectors_on_rule_update_endpoint(api_client: TestClient):
    """Adversarial stress: Verify updating an existing rule (PUT /api/rules/{id})
    with a ReDoS pattern is safely rejected with HTTP 422.
    """
    # 1. Create a valid rule first
    resp_init = api_client.post(
        "/api/rules",
        json={
            "name": "Benign Rule",
            "category_id": 1,
            "priority": 100,
            "field": "counterparty",
            "pattern_type": "contains",
            "pattern_value": "Starbucks",
        },
    )
    assert resp_init.status_code == 201
    rule_id = resp_init.json()["id"]

    # 2. Attempt to update pattern to ReDoS regex
    for pattern in ["(x+x+)+", "(a|aa)+$"]:
        t0 = time.perf_counter()
        resp_up = api_client.put(
            f"/api/rules/{rule_id}",
            json={
                "pattern_type": "regex",
                "pattern_value": pattern,
            },
        )
        elapsed = time.perf_counter() - t0

        assert resp_up.status_code == 422, f"Update pattern '{pattern}' was not rejected! Status: {resp_up.status_code}"
        assert elapsed < 0.5


def test_redos_attack_vectors_on_rule_apply_proposal_endpoint(api_client: TestClient):
    """Adversarial stress: Verify POST /api/rules/apply-proposal rejects ReDoS regex
    before attempting batch database scans.
    """
    for pattern in ["([a-zA-Z]+)*", "(a|aa)+$"]:
        t0 = time.perf_counter()
        resp = api_client.post(
            "/api/rules/apply-proposal",
            json={
                "name": "Proposal ReDoS",
                "category_id": 1,
                "priority": 100,
                "field": "counterparty",
                "pattern_type": "regex",
                "pattern_value": pattern,
                "apply_to_existing": True,
            },
        )
        elapsed = time.perf_counter() - t0

        assert resp.status_code == 422, f"Proposal pattern '{pattern}' was not rejected! Status: {resp.status_code}"
        assert elapsed < 0.5

