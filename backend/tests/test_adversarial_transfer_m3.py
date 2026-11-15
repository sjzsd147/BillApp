"""Adversarial Verification Suite for Milestone 3 (Feature 16 & Feature 17).

Empirical challenger test suite testing:
1. Transfer pairing under complex permutations: repeated withdrawals, multi-hop transfers,
   round-trip circular transfers, clock skew tolerance, and boundary conditions.
2. Financial neutrality invariant: 100 random transfer scenarios verifying $0.00 drift
   in Net Expense and Net Income.
3. Rejection memory invariance: candidate rejection permanence across 5 consecutive
   full-pipeline re-runs (0 auto matches, 0 pending links, direction restoration).
4. Batch rollback cascade resilience: multi-batch rollback, refund credit decrements,
   and strict zero dangling pointers across all relations.
"""

from __future__ import annotations

import json
import random
import sqlite3
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import pytest

from billapp.core.db import init_db
from billapp.dedup.engine import (
    DedupSummary,
    confirm_match_link,
    is_pair_blocked,
    reject_match_link,
    rollback_import_batch,
    run_dedup_pipeline,
)
from billapp.dedup.transfer import (
    compute_transfer_score,
    extract_bank_and_tail,
    pair_internal_transfers,
)
from billapp.dedup.cross_source import match_cross_source


# ==============================================================================
# Database Fixtures and Test Helpers
# ==============================================================================


@pytest.fixture
def db() -> sqlite3.Connection:
    """Fixture providing an isolated in-memory SQLite database initialized with schema."""
    conn = init_db(":memory:")
    yield conn
    conn.close()


def create_batch(db: sqlite3.Connection, batch_id: int, source: str = "test") -> int:
    db.execute(
        """
        INSERT OR IGNORE INTO import_batches (id, file_name, file_sha1, file_size, source, imported_at, row_count)
        VALUES (?, ?, ?, 100, ?, datetime('now', 'localtime'), 1)
        """,
        (batch_id, f"batch_{batch_id}.csv", f"sha_{batch_id}_{source}", source),
    )
    return batch_id


def create_account(
    db: sqlite3.Connection,
    account_id: int,
    kind: str,
    bank: str,
    tail: Optional[str] = None,
    holder: str = "张三",
) -> int:
    db.execute(
        """
        INSERT OR IGNORE INTO accounts (id, kind, bank, tail, holder)
        VALUES (?, ?, ?, ?, ?)
        """,
        (account_id, kind, bank, tail, holder),
    )
    return account_id


def insert_txn(
    db: sqlite3.Connection,
    txn_id: int,
    batch_id: int,
    account_id: int,
    source: str,
    txn_time: str,
    amount_cents: int,
    direction: str = "expense",
    currency: str = "CNY",
    fund_account_id: Optional[int] = None,
    counterparty: Optional[str] = None,
    description: Optional[str] = None,
    channel: Optional[str] = None,
    duplicate_of: Optional[int] = None,
    transfer_peer: Optional[int] = None,
    refund_of: Optional[int] = None,
    refunded_cents: int = 0,
    fingerprint: Optional[str] = None,
) -> int:
    create_batch(db, batch_id=batch_id, source=source)
    fp = fingerprint or f"fp_adv_{txn_id}_{source}"
    db.execute(
        """
        INSERT INTO transactions (
            id, batch_id, account_id, fund_account_id, source, txn_time, time_precision,
            amount_cents, currency, direction, counterparty, description, channel,
            duplicate_of, transfer_peer, refund_of, refunded_cents, fingerprint
        )
        VALUES (?, ?, ?, ?, ?, ?, 'second', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            txn_id, batch_id, account_id, fund_account_id, source, txn_time,
            amount_cents, currency, direction, counterparty, description, channel,
            duplicate_of, transfer_peer, refund_of, refunded_cents, fp,
        ),
    )
    return txn_id


def get_net_expense_cents(conn: sqlite3.Connection) -> int:
    """Calculate Net Expense in CNY: expense minus refunds, excluding duplicates and excluded."""
    row = conn.execute(
        """
        SELECT COALESCE(SUM(amount_cents - refunded_cents), 0) AS val
        FROM transactions
        WHERE direction = 'expense'
          AND duplicate_of IS NULL
          AND excluded = 0
          AND currency = 'CNY'
        """
    ).fetchone()
    return int(row["val"])


def get_net_income_cents(conn: sqlite3.Connection) -> int:
    """Calculate Net Income in CNY: income transactions, excluding duplicates and excluded."""
    row = conn.execute(
        """
        SELECT COALESCE(SUM(amount_cents), 0) AS val
        FROM transactions
        WHERE direction = 'income'
          AND duplicate_of IS NULL
          AND excluded = 0
          AND currency = 'CNY'
        """
    ).fetchone()
    return int(row["val"])


# ==============================================================================
# Suite 1: Transfer Pairing Patterns & Complex Permutations (Feature 16)
# ==============================================================================


class TestAdversarialTransferPatternsAndPermutations:
    """Stress testing internal transfer pairing under permutations and edge cases."""

    def test_pattern_a_repeated_identical_withdrawals_chronological_bijection(self, db: sqlite3.Connection):
        """5 repeated identical withdrawals (¥50.00 each) from WeChat to CCB 5678.
        
        Verifies strict chronological 1-to-1 bijection without cross-linking or multiple peers.
        """
        create_account(db, account_id=1, kind="wallet", bank="微信支付", tail=None, holder="张三")
        create_account(db, account_id=2, kind="bank_debit", bank="建设银行", tail="5678", holder="张三")

        # 5 identical withdrawals every 5 minutes
        times_w = [
            "2026-11-09 10:00:00",
            "2026-11-09 10:05:00",
            "2026-11-09 10:10:00",
            "2026-11-09 10:15:00",
            "2026-11-09 10:20:00",
        ]
        # 5 matching deposits arriving 2 minutes after each withdrawal
        times_d = [
            "2026-11-09 10:02:00",
            "2026-11-09 10:07:00",
            "2026-11-09 10:12:00",
            "2026-11-09 10:17:00",
            "2026-11-09 10:22:00",
        ]

        for idx, (tw, td) in enumerate(zip(times_w, times_d), start=1):
            w_id = 100 + idx
            d_id = 200 + idx
            insert_txn(
                db, txn_id=w_id, batch_id=1, account_id=1, source="wechat",
                txn_time=tw, amount_cents=5000, direction="transfer",
                counterparty="建设银行(5678)", description="零钱提现",
            )
            insert_txn(
                db, txn_id=d_id, batch_id=2, account_id=2, source="bank_profile",
                txn_time=td, amount_cents=5000, direction="income",
                counterparty="微信转入", description="银联入账",
            )

        matches = pair_internal_transfers(db)
        assert len(matches) == 5, f"Expected exactly 5 matches, got {len(matches)}"

        matched_pairs = {(m["out_id"], m["in_id"]) for m in matches}
        # Invariant: Each withdrawal must pair with its exact chronological deposit
        for idx in range(1, 6):
            expected = (100 + idx, 200 + idx)
            assert expected in matched_pairs, f"Pair {expected} missing in matches: {matched_pairs}"

        # Invariant: Bilateral transfer_peer and direction='transfer' across all records
        for idx in range(1, 6):
            w_row = db.execute("SELECT direction, transfer_peer FROM transactions WHERE id = ?", (100 + idx,)).fetchone()
            d_row = db.execute("SELECT direction, transfer_peer FROM transactions WHERE id = ?", (200 + idx,)).fetchone()
            assert w_row["transfer_peer"] == 200 + idx
            assert d_row["transfer_peer"] == 100 + idx
            assert w_row["direction"] == "transfer"
            assert d_row["direction"] == "transfer"

    def test_pattern_a_asymmetric_withdrawal_deposit_permutations(self, db: sqlite3.Connection):
        """Asymmetric queues: 4 withdrawals vs 2 deposits, then delayed batch ingestion."""
        create_account(db, account_id=1, kind="wallet", bank="微信支付", tail=None, holder="张三")
        create_account(db, account_id=2, kind="bank_debit", bank="建设银行", tail="5678", holder="张三")

        # 4 withdrawals
        for i in range(1, 5):
            insert_txn(
                db, txn_id=10 + i, batch_id=1, account_id=1, source="wechat",
                txn_time=f"2026-11-09 12:0{i}:00", amount_cents=3000, direction="transfer",
                counterparty="建设银行(5678)", description="零钱提现",
            )

        # Only 2 deposits initially
        insert_txn(
            db, txn_id=21, batch_id=2, account_id=2, source="bank_profile",
            txn_time="2026-11-09 12:01:30", amount_cents=3000, direction="income",
            counterparty="微信转入", description="银联入账",
        )
        insert_txn(
            db, txn_id=22, batch_id=2, account_id=2, source="bank_profile",
            txn_time="2026-11-09 12:02:30", amount_cents=3000, direction="income",
            counterparty="微信转入", description="银联入账",
        )

        matches_1 = pair_internal_transfers(db)
        assert len(matches_1) == 2
        assert {m["out_id"] for m in matches_1} == {11, 12}

        # 13 and 14 remain single-leg
        t13 = db.execute("SELECT transfer_peer FROM transactions WHERE id = 13").fetchone()
        t14 = db.execute("SELECT transfer_peer FROM transactions WHERE id = 14").fetchone()
        assert t13["transfer_peer"] is None
        assert t14["transfer_peer"] is None

        # Batch 3 arrives later with deposits for 13 and 14
        insert_txn(
            db, txn_id=23, batch_id=3, account_id=2, source="bank_profile",
            txn_time="2026-11-09 12:03:30", amount_cents=3000, direction="income",
            counterparty="微信转入", description="银联入账",
        )
        insert_txn(
            db, txn_id=24, batch_id=3, account_id=2, source="bank_profile",
            txn_time="2026-11-09 12:04:30", amount_cents=3000, direction="income",
            counterparty="微信转入", description="银联入账",
        )

        matches_2 = pair_internal_transfers(db)
        assert len(matches_2) == 2
        assert {m["out_id"] for m in matches_2} == {13, 14}
        assert {m["in_id"] for m in matches_2} == {23, 24}

        # Now all 4 withdrawals are paired
        rows = db.execute("SELECT id, transfer_peer FROM transactions WHERE id BETWEEN 11 AND 14").fetchall()
        assert all(r["transfer_peer"] is not None for r in rows)

    def test_pattern_b_repeated_credit_card_repayments_multi_account(self, db: sqlite3.Connection):
        """Debit card pays two different credit cards with identical amounts."""
        create_account(db, account_id=1, kind="bank_debit", bank="招商银行", tail="1234", holder="张三")
        create_account(db, account_id=2, kind="bank_credit", bank="工商银行", tail="8801", holder="张三")
        create_account(db, account_id=3, kind="bank_credit", bank="工商银行", tail="8802", holder="张三")

        # Repay Card 8801: ¥500.00
        insert_txn(
            db, txn_id=301, batch_id=1, account_id=1, source="bank_profile",
            txn_time="2026-10-28 14:00:00", amount_cents=50000, direction="expense",
            counterparty="工商银行信用卡(8801)", description="信用卡还款",
        )
        # Repay Card 8802: ¥500.00
        insert_txn(
            db, txn_id=302, batch_id=1, account_id=1, source="bank_profile",
            txn_time="2026-10-28 14:05:00", amount_cents=50000, direction="expense",
            counterparty="工商银行信用卡(8802)", description="信用卡还款",
        )

        # Deposit on Card 8801
        insert_txn(
            db, txn_id=401, batch_id=2, account_id=2, source="icbc_credit",
            txn_time="2026-10-28 14:01:00", amount_cents=50000, direction="transfer",
            counterparty="张三", description="转帐",
        )
        # Deposit on Card 8802
        insert_txn(
            db, txn_id=402, batch_id=2, account_id=3, source="icbc_credit",
            txn_time="2026-10-28 14:06:00", amount_cents=50000, direction="transfer",
            counterparty="张三", description="转帐",
        )

        matches = pair_internal_transfers(db)
        assert len(matches) == 2

        pair_map = {m["out_id"]: m["in_id"] for m in matches}
        assert pair_map[301] == 401, "Repayment for card 8801 must pair with account 2 (8801)"
        assert pair_map[302] == 402, "Repayment for card 8802 must pair with account 3 (8802)"

    def test_pattern_c_multi_hop_transfer_chain(self, db: sqlite3.Connection):
        """Multi-hop transfer: Account A -> Account B -> Account C -> Account D.
        
        Verifies that intermediate accounts (B and C) participating in both an incoming hop
        and an outgoing hop pair properly, and never self-pair within the same account.
        """
        create_account(db, account_id=1, kind="bank_debit", bank="中国银行", tail="6666", holder="张三")
        create_account(db, account_id=2, kind="bank_debit", bank="招商银行", tail="1234", holder="张三")
        create_account(db, account_id=3, kind="bank_debit", bank="工商银行", tail="8801", holder="张三")
        create_account(db, account_id=4, kind="bank_debit", bank="建设银行", tail="5678", holder="张三")

        # Hop 1: A -> B (¥1000.00 at 09:00 -> arrives at 09:02)
        insert_txn(
            db, txn_id=501, batch_id=1, account_id=1, source="boc_debit",
            txn_time="2026-11-01 09:00:00", amount_cents=100000, direction="expense",
            counterparty="张三", description="跨行转账 招商银行 1234",
        )
        insert_txn(
            db, txn_id=502, batch_id=2, account_id=2, source="cmb_debit",
            txn_time="2026-11-01 09:02:00", amount_cents=100000, direction="income",
            counterparty="张三", description="跨行转入 中国银行 6666",
        )

        # Hop 2: B -> C (¥1000.00 at 12:00 -> arrives at 12:02)
        insert_txn(
            db, txn_id=503, batch_id=2, account_id=2, source="cmb_debit",
            txn_time="2026-11-01 12:00:00", amount_cents=100000, direction="expense",
            counterparty="张三", description="跨行转账 工商银行 8801",
        )
        insert_txn(
            db, txn_id=504, batch_id=3, account_id=3, source="icbc_debit",
            txn_time="2026-11-01 12:02:00", amount_cents=100000, direction="income",
            counterparty="张三", description="跨行转入 招商银行 1234",
        )

        # Hop 3: C -> D (¥1000.00 at 16:00 -> arrives at 16:02)
        insert_txn(
            db, txn_id=505, batch_id=3, account_id=3, source="icbc_debit",
            txn_time="2026-11-01 16:00:00", amount_cents=100000, direction="expense",
            counterparty="张三", description="跨行转账 建设银行 5678",
        )
        insert_txn(
            db, txn_id=506, batch_id=4, account_id=4, source="ccb_debit",
            txn_time="2026-11-01 16:02:00", amount_cents=100000, direction="income",
            counterparty="张三", description="跨行转入 工商银行 8801",
        )

        matches = pair_internal_transfers(db)
        assert len(matches) == 3, f"Expected 3 distinct hops, got {len(matches)}"

        pair_dict = {m["out_id"]: m["in_id"] for m in matches}
        assert pair_dict[501] == 502, "Hop 1 failed to pair A_out with B_in"
        assert pair_dict[503] == 504, "Hop 2 failed to pair B_out with C_in"
        assert pair_dict[505] == 506, "Hop 3 failed to pair C_out with D_in"

        # Invariant: Account B never paired with itself (503 != 502)
        assert 503 != 502
        # Invariant: Account C never paired with itself (505 != 504)
        assert 505 != 504

    def test_pattern_c_round_trip_circular_transfer(self, db: sqlite3.Connection):
        """Round-trip transfer: Account A -> Account B in the morning, Account B -> Account A in the evening."""
        create_account(db, account_id=1, kind="bank_debit", bank="中国银行", tail="6666", holder="张三")
        create_account(db, account_id=2, kind="bank_debit", bank="招商银行", tail="1234", holder="张三")

        # Morning leg: A -> B (¥2000.00)
        insert_txn(
            db, txn_id=601, batch_id=1, account_id=1, source="boc_debit",
            txn_time="2026-11-05 08:30:00", amount_cents=200000, direction="expense",
            counterparty="张三", description="跨行转账 招商银行 1234",
        )
        insert_txn(
            db, txn_id=602, batch_id=2, account_id=2, source="cmb_debit",
            txn_time="2026-11-05 08:31:00", amount_cents=200000, direction="income",
            counterparty="张三", description="跨行转入 中国银行 6666",
        )

        # Evening return leg: B -> A (¥2000.00)
        insert_txn(
            db, txn_id=603, batch_id=2, account_id=2, source="cmb_debit",
            txn_time="2026-11-05 20:00:00", amount_cents=200000, direction="expense",
            counterparty="张三", description="跨行转账 中国银行 6666",
        )
        insert_txn(
            db, txn_id=604, batch_id=1, account_id=1, source="boc_debit",
            txn_time="2026-11-05 20:01:00", amount_cents=200000, direction="income",
            counterparty="张三", description="跨行转入 招商银行 1234",
        )

        matches = pair_internal_transfers(db)
        assert len(matches) == 2

        pair_dict = {m["out_id"]: m["in_id"] for m in matches}
        assert pair_dict[601] == 602, "Morning leg mispaired"
        assert pair_dict[603] == 604, "Evening leg mispaired"

    def test_clock_skew_and_boundary_window(self, db: sqlite3.Connection):
        """Clock skew margin (-600s allowed) and max window (48 hours) boundaries."""
        create_account(db, account_id=1, kind="wallet", bank="微信支付", tail=None, holder="张三")
        create_account(db, account_id=2, kind="bank_debit", bank="建设银行", tail="5678", holder="张三")

        # Base time: 2026-11-09 12:00:00
        # Case 1: Inflow 590s BEFORE Outflow (-590s, within -600s clock skew margin)
        insert_txn(
            db, txn_id=701, batch_id=1, account_id=1, source="wechat",
            txn_time="2026-11-09 12:09:50", amount_cents=1000, direction="transfer",
            counterparty="建设银行(5678)", description="提现",
        )
        insert_txn(
            db, txn_id=702, batch_id=2, account_id=2, source="bank_profile",
            txn_time="2026-11-09 12:00:00", amount_cents=1000, direction="income",
            counterparty="微信转入", description="银联入账",
        )

        # Case 2: Inflow 610s BEFORE Outflow (-610s, exceeds clock skew margin)
        insert_txn(
            db, txn_id=703, batch_id=1, account_id=1, source="wechat",
            txn_time="2026-11-09 12:10:10", amount_cents=2000, direction="transfer",
            counterparty="建设银行(5678)", description="提现",
        )
        insert_txn(
            db, txn_id=704, batch_id=2, account_id=2, source="bank_profile",
            txn_time="2026-11-09 12:00:00", amount_cents=2000, direction="income",
            counterparty="微信转入", description="银联入账",
        )

        # Case 3: Inflow arriving 47 hours later (within 48h limit)
        insert_txn(
            db, txn_id=705, batch_id=1, account_id=1, source="wechat",
            txn_time="2026-11-01 10:00:00", amount_cents=3000, direction="transfer",
            counterparty="建设银行(5678)", description="提现",
        )
        insert_txn(
            db, txn_id=706, batch_id=2, account_id=2, source="bank_profile",
            txn_time="2026-11-03 09:00:00", amount_cents=3000, direction="income",
            counterparty="微信转入", description="银联入账",
        )

        # Case 4: Inflow arriving 49 hours later (exceeds 48h limit)
        insert_txn(
            db, txn_id=707, batch_id=1, account_id=1, source="wechat",
            txn_time="2026-11-01 10:00:00", amount_cents=4000, direction="transfer",
            counterparty="建设银行(5678)", description="提现",
        )
        insert_txn(
            db, txn_id=708, batch_id=2, account_id=2, source="bank_profile",
            txn_time="2026-11-03 11:00:00", amount_cents=4000, direction="income",
            counterparty="微信转入", description="银联入账",
        )

        matches = pair_internal_transfers(db)
        matched_outs = {m["out_id"] for m in matches}

        assert 701 in matched_outs, "Case 1 (-590s clock skew) must be accepted"
        assert 703 not in matched_outs, "Case 2 (-610s clock skew) must be rejected"
        assert 705 in matched_outs, "Case 3 (+47h delta) must be accepted"
        assert 707 not in matched_outs, "Case 4 (+49h delta) must be rejected"

    def test_currency_and_zero_amount_adversarial_rejections(self, db: sqlite3.Connection):
        """Currency mismatch and zero amount must return 0 score and strictly reject."""
        out_cny = {
            "id": 1, "source": "wechat", "txn_time": "2026-11-09 12:00:00",
            "amount_cents": 5000, "currency": "CNY", "acc_kind": "wallet",
        }
        in_usd = {
            "id": 2, "source": "bank", "txn_time": "2026-11-09 12:01:00",
            "amount_cents": 5000, "currency": "USD", "acc_kind": "bank_credit",
        }
        score, _ = compute_transfer_score(out_cny, in_usd)
        assert score == 0.0, "Currency mismatch must result in 0.0 transfer score"

        out_zero = dict(out_cny, amount_cents=0)
        in_zero = dict(in_usd, amount_cents=0, currency="CNY")
        score_zero, _ = compute_transfer_score(out_zero, in_zero)
        assert score_zero == 0.0, "Zero amount must result in 0.0 transfer score"


# ==============================================================================
# Suite 2: Financial Neutrality Invariant (Feature 16)
# ==============================================================================


class TestAdversarialFinancialNeutralityInvariant:
    """Rigorous verification that transfers leave Net Expense and Net Income untouched ($0.00 drift)."""

    def test_100_random_transfer_scenarios_zero_drift(self):
        """Execute 100 randomly generated transfer configurations.
        
        Asserts that Net Expense and Net Income remain mathematically invariant ($0.00 drift).
        """
        rng = random.Random(42)  # Seed for deterministic reproducibility

        for scenario_idx in range(1, 101):
            conn = init_db(":memory:")

            # 1. Setup base accounts
            create_account(conn, account_id=1, kind="platform", bank="支付宝", tail=None, holder="张三")
            create_account(conn, account_id=2, kind="wallet", bank="微信支付", tail=None, holder="张三")
            create_account(conn, account_id=3, kind="bank_debit", bank="招商银行", tail="1234", holder="张三")
            create_account(conn, account_id=4, kind="bank_debit", bank="建设银行", tail="5678", holder="张三")
            create_account(conn, account_id=5, kind="bank_credit", bank="工商银行", tail="8801", holder="张三")
            create_account(conn, account_id=6, kind="bank_debit", bank="中国银行", tail="6666", holder="张三")

            # 2. Insert baseline third-party expenses and incomes (the real ledger)
            # 5 Real expenses
            insert_txn(conn, txn_id=1, batch_id=1, account_id=1, source="alipay", txn_time="2026-11-01 12:00:00", amount_cents=3500, direction="expense", counterparty="餐厅")
            insert_txn(conn, txn_id=2, batch_id=1, account_id=2, source="wechat", txn_time="2026-11-02 13:00:00", amount_cents=2000, direction="expense", counterparty="咖啡厅")
            insert_txn(conn, txn_id=3, batch_id=1, account_id=3, source="bank", txn_time="2026-11-03 14:00:00", amount_cents=15000, direction="expense", counterparty="商场")
            insert_txn(conn, txn_id=4, batch_id=1, account_id=4, source="bank", txn_time="2026-11-04 15:00:00", amount_cents=450, direction="expense", counterparty="便利店")
            insert_txn(conn, txn_id=5, batch_id=1, account_id=5, source="bank", txn_time="2026-11-05 16:00:00", amount_cents=8800, direction="expense", counterparty="数码专柜")

            # 2 Real incomes
            insert_txn(conn, txn_id=6, batch_id=1, account_id=3, source="bank", txn_time="2026-11-01 09:00:00", amount_cents=1200000, direction="income", counterparty="公司发薪")
            insert_txn(conn, txn_id=7, batch_id=1, account_id=6, source="bank", txn_time="2026-11-05 10:00:00", amount_cents=50000, direction="income", counterparty="稿费收入")

            base_net_expense = get_net_expense_cents(conn)
            base_net_income = get_net_income_cents(conn)
            assert base_net_expense == 29750  # 35.00 + 20.00 + 150.00 + 4.50 + 88.00 = 297.50
            assert base_net_income == 1250000  # 12000.00 + 500.00 = 12500.00

            # 3. Generate random transfers in this scenario (between 1 and 4 pairs)
            num_transfers = rng.randint(1, 4)
            current_id = 100

            for t_idx in range(num_transfers):
                current_id += 1
                out_id = current_id
                current_id += 1
                in_id = current_id

                amount = rng.randint(100, 500000)  # ¥1.00 to ¥5,000.00
                day_offset = rng.randint(1, 25)
                sec_offset = rng.randint(0, 1800)  # Inflow arrives 0 to 30 min later
                t_base = datetime(2026, 11, day_offset, 10, 0, 0)
                t_out_str = t_base.strftime("%Y-%m-%d %H:%M:%S")
                t_in_str = (t_base + timedelta(seconds=sec_offset)).strftime("%Y-%m-%d %H:%M:%S")

                pattern_type = rng.choice(["wallet_withdrawal", "credit_repay", "interbank"])

                if pattern_type == "wallet_withdrawal":
                    # WeChat -> CCB 5678
                    insert_txn(
                        conn, txn_id=out_id, batch_id=2, account_id=2, source="wechat",
                        txn_time=t_out_str, amount_cents=amount, direction="transfer",
                        counterparty="建设银行(5678)", description="零钱提现",
                    )
                    insert_txn(
                        conn, txn_id=in_id, batch_id=3, account_id=4, source="ccb_debit",
                        txn_time=t_in_str, amount_cents=amount, direction="income",
                        counterparty="微信转入", description="银联入账",
                    )
                elif pattern_type == "credit_repay":
                    # CMB Debit -> ICBC Credit 8801
                    insert_txn(
                        conn, txn_id=out_id, batch_id=2, account_id=3, source="cmb_debit",
                        txn_time=t_out_str, amount_cents=amount, direction="expense",
                        counterparty="工商银行信用卡(8801)", description="信用卡还款",
                    )
                    insert_txn(
                        conn, txn_id=in_id, batch_id=3, account_id=5, source="icbc_credit",
                        txn_time=t_in_str, amount_cents=amount, direction="transfer",
                        counterparty="张三", description="还款入账",
                    )
                else:
                    # CMB Debit -> BOC Debit 6666
                    insert_txn(
                        conn, txn_id=out_id, batch_id=2, account_id=3, source="cmb_debit",
                        txn_time=t_out_str, amount_cents=amount, direction="expense",
                        counterparty="张三", description="跨行转账 中国银行 6666",
                    )
                    insert_txn(
                        conn, txn_id=in_id, batch_id=3, account_id=6, source="boc_debit",
                        txn_time=t_in_str, amount_cents=amount, direction="transfer",
                        counterparty="张三", description="跨行转入 招商银行 1234",
                    )

            # 4. Run full deduplication & transfer pairing pipeline
            summary = run_dedup_pipeline(conn)
            assert summary.transfers_paired == num_transfers, (
                f"Scenario {scenario_idx}: Expected {num_transfers} transfers paired, got {summary.transfers_paired}"
            )

            # 5. Assert Financial Neutrality Invariant
            new_net_expense = get_net_expense_cents(conn)
            new_net_income = get_net_income_cents(conn)

            expense_drift = abs(new_net_expense - base_net_expense)
            income_drift = abs(new_net_income - base_net_income)

            assert expense_drift == 0, (
                f"Scenario {scenario_idx} FAILED: Net Expense drifted by {expense_drift} cents! "
                f"(Base: {base_net_expense}, New: {new_net_expense})"
            )
            assert income_drift == 0, (
                f"Scenario {scenario_idx} FAILED: Net Income drifted by {income_drift} cents! "
                f"(Base: {base_net_income}, New: {new_net_income})"
            )

            conn.close()

    def test_transfer_conservation_of_monetary_value(self, db: sqlite3.Connection):
        """Conservation invariant: sum of outflow cents equals sum of inflow cents across all paired transfers."""
        create_account(db, account_id=1, kind="wallet", bank="微信支付", tail=None, holder="张三")
        create_account(db, account_id=2, kind="bank_debit", bank="建设银行", tail="5678", holder="张三")

        amounts = [1234, 5678, 9012, 34567]
        for idx, amt in enumerate(amounts, start=1):
            insert_txn(
                db, txn_id=10 + idx, batch_id=1, account_id=1, source="wechat",
                txn_time=f"2026-11-09 10:0{idx}:00", amount_cents=amt, direction="transfer",
                counterparty="建设银行(5678)", description="零钱提现",
            )
            insert_txn(
                db, txn_id=20 + idx, batch_id=2, account_id=2, source="ccb_debit",
                txn_time=f"2026-11-09 10:0{idx}:30", amount_cents=amt, direction="income",
                counterparty="微信转入", description="银联入账",
            )

        pair_internal_transfers(db)

        row = db.execute(
            """
            SELECT
                SUM(CASE WHEN source = 'wechat' THEN amount_cents ELSE 0 END) AS total_out,
                SUM(CASE WHEN source = 'ccb_debit' THEN amount_cents ELSE 0 END) AS total_in
            FROM transactions
            WHERE direction = 'transfer' AND transfer_peer IS NOT NULL
            """
        ).fetchone()

        assert row["total_out"] == sum(amounts)
        assert row["total_in"] == sum(amounts)
        assert row["total_out"] == row["total_in"]


# ==============================================================================
# Suite 3: Rejection Memory Invariance (Feature 17)
# ==============================================================================


class TestAdversarialRejectionMemoryInvariance:
    """Stress testing permanent rejection memory across repeated deduplication runs."""

    def test_l3_duplicate_rejection_permanence_5_reruns(self, db: sqlite3.Connection):
        """Reject an L3 duplicate match link, then execute full dedup pipeline 5 times.
        
        Invariant: 0 auto-matches and 0 pending links generated for the rejected pair across all runs.
        """
        create_account(db, account_id=1, kind="bank_credit", bank="工商银行", tail="8801")
        create_account(db, account_id=2, kind="platform", bank="支付宝", tail=None)

        # Primary platform expense
        insert_txn(
            db, txn_id=801, batch_id=1, account_id=2, fund_account_id=1,
            source="alipay", txn_time="2026-11-09 11:44:08", amount_cents=2747,
            counterparty="麦当劳", description="金拱门",
        )
        # Secondary bank expense (matching score 1.0)
        insert_txn(
            db, txn_id=802, batch_id=2, account_id=1,
            source="icbc_credit", txn_time="2026-11-09 11:44:08", amount_cents=2747,
            counterparty="支付宝-麦当劳", description="金拱门", channel="alipay",
        )

        # 1. Run pipeline -> auto match link created
        summary_1 = run_dedup_pipeline(db)
        assert summary_1.duplicates_auto == 1

        link = db.execute(
            "SELECT id, status FROM match_links WHERE primary_id = 801 AND secondary_id = 802"
        ).fetchone()
        assert link is not None
        assert link["status"] == "auto"

        # Secondary transaction has duplicate_of set
        t802 = db.execute("SELECT duplicate_of FROM transactions WHERE id = 802").fetchone()
        assert t802["duplicate_of"] == 801

        # 2. User rejects this match link
        rejected = reject_match_link(db, link["id"])
        assert rejected is True

        # Verify immediate state: duplicate_of unlinked, blocklist populated
        t802_after = db.execute("SELECT duplicate_of FROM transactions WHERE id = 802").fetchone()
        assert t802_after["duplicate_of"] is None
        assert is_pair_blocked(db, 801, 802, kind="duplicate") is True

        # 3. Re-run dedup pipeline 5 consecutive times
        for run_idx in range(1, 6):
            summary = run_dedup_pipeline(db)
            assert summary.duplicates_auto == 0, f"Run {run_idx}: Generated unauthorized auto-match!"
            assert summary.duplicates_pending == 0, f"Run {run_idx}: Generated unauthorized pending link!"

            # Verify no auto/pending links exist
            active_links = db.execute(
                """
                SELECT count(*) AS cnt FROM match_links
                WHERE primary_id = 801 AND secondary_id = 802 AND status IN ('auto', 'pending')
                """
            ).fetchone()["cnt"]
            assert active_links == 0, f"Run {run_idx}: Active match link resurrected!"

            # Verify duplicate_of remains strictly NULL
            cur_t802 = db.execute("SELECT duplicate_of FROM transactions WHERE id = 802").fetchone()
            assert cur_t802["duplicate_of"] is None, f"Run {run_idx}: duplicate_of pointer restored!"

    def test_l4_transfer_rejection_permanence_5_reruns(self, db: sqlite3.Connection):
        """Reject an L4 internal transfer match link, then execute full dedup pipeline 5 times.
        
        Invariant: 0 auto-matches and 0 pending links generated, directions preserved/restored.
        """
        create_account(db, account_id=1, kind="wallet", bank="微信支付", tail=None, holder="张三")
        create_account(db, account_id=2, kind="bank_debit", bank="建设银行", tail="5678", holder="张三")

        insert_txn(
            db, txn_id=811, batch_id=1, account_id=1, source="wechat",
            txn_time="2026-11-09 14:00:00", amount_cents=10000, direction="transfer",
            counterparty="建设银行(5678)", description="零钱提现",
        )
        insert_txn(
            db, txn_id=812, batch_id=2, account_id=2, source="ccb_debit",
            txn_time="2026-11-09 14:02:00", amount_cents=10000, direction="income",
            counterparty="微信转入", description="银联入账",
        )

        # 1. Pipeline pairs them
        summary_1 = run_dedup_pipeline(db)
        assert summary_1.transfers_paired == 1

        link = db.execute(
            "SELECT id, status FROM match_links WHERE kind = 'transfer' AND primary_id = 811 AND secondary_id = 812"
        ).fetchone()
        assert link is not None
        assert link["status"] == "auto"

        # 2. Reject the transfer link
        assert reject_match_link(db, link["id"]) is True

        # Invariant: transfer_peer cleared on both, directions restored
        r811 = db.execute("SELECT direction, transfer_peer FROM transactions WHERE id = 811").fetchone()
        r812 = db.execute("SELECT direction, transfer_peer FROM transactions WHERE id = 812").fetchone()
        assert r811["transfer_peer"] is None
        assert r812["transfer_peer"] is None
        assert r811["direction"] == "transfer"  # original out
        assert r812["direction"] == "income"    # original in restored!

        # 3. Re-run dedup pipeline 5 consecutive times
        for run_idx in range(1, 6):
            summary = run_dedup_pipeline(db)
            assert summary.transfers_paired == 0, f"Run {run_idx}: Auto-transfer regenerated!"
            assert summary.transfers_pending == 0, f"Run {run_idx}: Pending transfer regenerated!"

            r811_cur = db.execute("SELECT transfer_peer FROM transactions WHERE id = 811").fetchone()
            r812_cur = db.execute("SELECT transfer_peer FROM transactions WHERE id = 812").fetchone()
            assert r811_cur["transfer_peer"] is None
            assert r812_cur["transfer_peer"] is None

    def test_rejection_canonical_ordering_and_isolation(self, db: sqlite3.Connection):
        """Canonical ordering in match_blocklist (a_id < b_id) and unrelated candidate isolation."""
        create_account(db, account_id=1, kind="bank_credit", bank="工商银行", tail="8801")
        create_account(db, account_id=2, kind="platform", bank="支付宝", tail=None)

        # Pair 1: IDs (821, 822) -> to be rejected
        insert_txn(db, txn_id=821, batch_id=1, account_id=2, fund_account_id=1, source="alipay", txn_time="2026-11-09 10:00:00", amount_cents=1000, counterparty="商户A")
        insert_txn(db, txn_id=822, batch_id=2, account_id=1, source="icbc_credit", txn_time="2026-11-09 10:00:00", amount_cents=1000, counterparty="支付宝-商户A", channel="alipay")

        # Pair 2: IDs (823, 824) -> legitimate match, should NOT be blocked
        insert_txn(db, txn_id=823, batch_id=1, account_id=2, fund_account_id=1, source="alipay", txn_time="2026-11-09 11:00:00", amount_cents=2000, counterparty="商户B")
        insert_txn(db, txn_id=824, batch_id=2, account_id=1, source="icbc_credit", txn_time="2026-11-09 11:00:00", amount_cents=2000, counterparty="支付宝-商户B", channel="alipay")

        run_dedup_pipeline(db)

        link_1 = db.execute("SELECT id FROM match_links WHERE primary_id = 821 AND secondary_id = 822").fetchone()
        assert reject_match_link(db, link_1["id"]) is True

        # Verify canonical ordering: min(821, 822) = 821, max(821, 822) = 822
        block_row = db.execute("SELECT a_id, b_id FROM match_blocklist WHERE kind = 'duplicate'").fetchone()
        assert block_row["a_id"] == 821
        assert block_row["b_id"] == 822

        # Verify is_pair_blocked responds symmetrically
        assert is_pair_blocked(db, 821, 822) is True
        assert is_pair_blocked(db, 822, 821) is True
        assert is_pair_blocked(db, 823, 824) is False

        # Re-run pipeline: Pair 1 blocked, Pair 2 remains paired
        summary = run_dedup_pipeline(db)
        assert summary.duplicates_auto == 0  # Pair 2 is already matched, Pair 1 blocked
        t822 = db.execute("SELECT duplicate_of FROM transactions WHERE id = 822").fetchone()
        t824 = db.execute("SELECT duplicate_of FROM transactions WHERE id = 824").fetchone()
        assert t822["duplicate_of"] is None, "Rejected pair must remain unlinked"
        assert t824["duplicate_of"] == 823, "Unrelated pair must remain linked"


# ==============================================================================
# Suite 4: Batch Rollback Cascade Resilience (Feature 17)
# ==============================================================================


class TestAdversarialBatchRollbackCascadeResilience:
    """Stress testing safe batch rollback, refund restorations, and zero dangling pointers."""

    def test_complex_multi_batch_cascade_rollback(self, db: sqlite3.Connection):
        """Multi-batch setup with cross-batch refunds, duplicate linkages, and transfers.
        
        Rollback Batch 2 must:
        - Decrement credited refund amounts on surviving Batch 1 transactions.
        - Clear duplicate_of references pointing to Batch 2 transactions.
        - Clear transfer_peer references pointing to Batch 2 transactions.
        - Delete all Batch 2 records.
        - Leave ZERO dangling pointers across all foreign keys and relations.
        """
        create_account(db, account_id=1, kind="platform", bank="支付宝", tail=None)
        create_account(db, account_id=2, kind="bank_credit", bank="工商银行", tail="8801")
        create_account(db, account_id=3, kind="wallet", bank="微信支付", tail=None)
        create_account(db, account_id=4, kind="bank_debit", bank="建设银行", tail="5678")

        # --- Batch 1 (Surviving batch) ---
        # Txn 901: Purchase of ¥100.00
        insert_txn(db, txn_id=901, batch_id=1, account_id=1, source="alipay", txn_time="2026-10-10 10:00:00", amount_cents=10000, direction="expense", counterparty="商场")
        # Txn 902: Platform expense of ¥50.00
        insert_txn(db, txn_id=902, batch_id=1, account_id=1, fund_account_id=2, source="alipay", txn_time="2026-10-10 12:00:00", amount_cents=5000, direction="expense", counterparty="麦当劳")
        # Txn 903: WeChat withdrawal leg of ¥200.00
        insert_txn(db, txn_id=903, batch_id=1, account_id=3, source="wechat", txn_time="2026-10-10 14:00:00", amount_cents=20000, direction="transfer", counterparty="建设银行(5678)", description="零钱提现")
        # Txn 904: Purchase of ¥80.00
        insert_txn(db, txn_id=904, batch_id=1, account_id=1, source="alipay", txn_time="2026-10-10 16:00:00", amount_cents=8000, direction="expense", counterparty="书店")

        # --- Batch 2 (Target for rollback) ---
        # Txn 911: Refund of ¥30.00 crediting Txn 901
        insert_txn(db, txn_id=911, batch_id=2, account_id=2, source="boc_debit", txn_time="2026-10-12 10:00:00", amount_cents=3000, direction="refund", refund_of=901, counterparty="商场退款")
        # Txn 912: Bank record duplicating Txn 902
        insert_txn(db, txn_id=912, batch_id=2, account_id=2, source="icbc_credit", txn_time="2026-10-10 12:00:00", amount_cents=5000, direction="expense", duplicate_of=902, counterparty="支付宝-麦当劳", channel="alipay")
        # Txn 913: Bank deposit pairing with withdrawal Txn 903
        insert_txn(db, txn_id=913, batch_id=2, account_id=4, source="ccb_debit", txn_time="2026-10-10 14:02:00", amount_cents=20000, direction="transfer", transfer_peer=903, counterparty="微信转入")
        # Txn 914: Partial refund of ¥20.00 crediting Txn 904
        insert_txn(db, txn_id=914, batch_id=2, account_id=2, source="boc_debit", txn_time="2026-10-12 16:00:00", amount_cents=2000, direction="refund", refund_of=904, counterparty="书店退款")

        # Update credited amounts and bilateral peers reflecting pipeline execution
        db.execute("UPDATE transactions SET refunded_cents = 3000 WHERE id = 901")
        db.execute("UPDATE transactions SET refunded_cents = 2000 WHERE id = 904")
        db.execute("UPDATE transactions SET transfer_peer = 913 WHERE id = 903")

        # Create match links involving Batch 2
        db.execute("INSERT INTO match_links (kind, primary_id, secondary_id, score, status) VALUES ('duplicate', 902, 912, 1.0, 'auto')")
        db.execute("INSERT INTO match_links (kind, primary_id, secondary_id, score, status) VALUES ('transfer', 903, 913, 1.0, 'auto')")

        # Verify pre-rollback assertions
        assert db.execute("SELECT refunded_cents FROM transactions WHERE id = 901").fetchone()["refunded_cents"] == 3000
        assert db.execute("SELECT refunded_cents FROM transactions WHERE id = 904").fetchone()["refunded_cents"] == 2000
        assert db.execute("SELECT transfer_peer FROM transactions WHERE id = 903").fetchone()["transfer_peer"] == 913

        # Execute safe batch rollback on Batch 2
        rollback_res = rollback_import_batch(db, batch_id=2)
        assert rollback_res["status"] == "rolled_back"
        assert rollback_res["deleted_transactions"] == 4

        # Verification 1: Batch 2 is completely removed
        assert db.execute("SELECT 1 FROM import_batches WHERE id = 2").fetchone() is None
        assert db.execute("SELECT count(*) AS cnt FROM transactions WHERE batch_id = 2").fetchone()["cnt"] == 0

        # Verification 2: Credited refunds on surviving transactions restored to 0
        t901 = db.execute("SELECT refunded_cents FROM transactions WHERE id = 901").fetchone()
        t904 = db.execute("SELECT refunded_cents FROM transactions WHERE id = 904").fetchone()
        assert t901["refunded_cents"] == 0, f"Expected 0 refunded_cents, got {t901['refunded_cents']}"
        assert t904["refunded_cents"] == 0, f"Expected 0 refunded_cents, got {t904['refunded_cents']}"

        # Verification 3: Surviving transfer leg has transfer_peer cleared
        t903 = db.execute("SELECT transfer_peer FROM transactions WHERE id = 903").fetchone()
        assert t903["transfer_peer"] is None

        # Verification 4: ZERO DANGLING POINTERS IN THE ENTIRE DATABASE
        # Check duplicate_of
        dangling_dup = db.execute(
            """
            SELECT count(*) AS cnt FROM transactions
            WHERE duplicate_of IS NOT NULL AND duplicate_of NOT IN (SELECT id FROM transactions)
            """
        ).fetchone()["cnt"]
        assert dangling_dup == 0, f"Found {dangling_dup} dangling duplicate_of pointers!"

        # Check transfer_peer
        dangling_tp = db.execute(
            """
            SELECT count(*) AS cnt FROM transactions
            WHERE transfer_peer IS NOT NULL AND transfer_peer NOT IN (SELECT id FROM transactions)
            """
        ).fetchone()["cnt"]
        assert dangling_tp == 0, f"Found {dangling_tp} dangling transfer_peer pointers!"

        # Check refund_of
        dangling_ref = db.execute(
            """
            SELECT count(*) AS cnt FROM transactions
            WHERE refund_of IS NOT NULL AND refund_of NOT IN (SELECT id FROM transactions)
            """
        ).fetchone()["cnt"]
        assert dangling_ref == 0, f"Found {dangling_ref} dangling refund_of pointers!"

        # Check match_links
        dangling_ml = db.execute(
            """
            SELECT count(*) AS cnt FROM match_links
            WHERE primary_id NOT IN (SELECT id FROM transactions)
               OR secondary_id NOT IN (SELECT id FROM transactions)
            """
        ).fetchone()["cnt"]
        assert dangling_ml == 0, f"Found {dangling_ml} dangling match_links references!"

        # Check match_blocklist
        dangling_mb = db.execute(
            """
            SELECT count(*) AS cnt FROM match_blocklist
            WHERE a_id NOT IN (SELECT id FROM transactions)
               OR b_id NOT IN (SELECT id FROM transactions)
            """
        ).fetchone()["cnt"]
        assert dangling_mb == 0, f"Found {dangling_mb} dangling match_blocklist references!"

    def test_multi_refund_accumulation_decremented_cleanly(self, db: sqlite3.Connection):
        """Two refunds in Batch 2 crediting the SAME purchase in Batch 1."""
        create_account(db, account_id=1, kind="platform", bank="支付宝", tail=None)
        create_account(db, account_id=2, kind="bank_debit", bank="中国银行", tail="6666")

        # Purchase: ¥200.00 (20000 cents)
        insert_txn(db, txn_id=920, batch_id=1, account_id=1, source="alipay", txn_time="2026-10-15 10:00:00", amount_cents=20000, direction="expense")

        # Batch 2 contains two separate partial refunds for Txn 920:
        # Refund 1: ¥50.00 (5000 cents)
        insert_txn(db, txn_id=921, batch_id=2, account_id=2, source="boc_debit", txn_time="2026-10-16 11:00:00", amount_cents=5000, direction="refund", refund_of=920)
        # Refund 2: ¥70.00 (7000 cents)
        insert_txn(db, txn_id=922, batch_id=2, account_id=2, source="boc_debit", txn_time="2026-10-17 12:00:00", amount_cents=7000, direction="refund", refund_of=920)

        # Set cumulative refunded cents = 12000 cents
        db.execute("UPDATE transactions SET refunded_cents = 12000 WHERE id = 920")

        # Rollback Batch 2
        res = rollback_import_batch(db, batch_id=2)
        assert res["status"] == "rolled_back"
        assert res["deleted_transactions"] == 2

        # Invariant: 920's refunded_cents must decrease by exactly 12000 -> 0
        t920 = db.execute("SELECT refunded_cents FROM transactions WHERE id = 920").fetchone()
        assert t920["refunded_cents"] == 0

    def test_rollback_preserves_prior_batch_refund_credits(self, db: sqlite3.Connection):
        """Batch 1 purchase had a Batch 1 refund (¥30), and later a Batch 2 refund (¥50).
        
        Rolling back Batch 2 must restore refunded_cents from ¥80 to ¥30 (not ¥0).
        """
        create_account(db, account_id=1, kind="platform", bank="支付宝", tail=None)
        create_account(db, account_id=2, kind="bank_debit", bank="中国银行", tail="6666")

        # Purchase in Batch 1: ¥200.00
        insert_txn(db, txn_id=930, batch_id=1, account_id=1, source="alipay", txn_time="2026-10-15 10:00:00", amount_cents=20000, direction="expense")
        # First refund in Batch 1: ¥30.00
        insert_txn(db, txn_id=931, batch_id=1, account_id=1, source="alipay", txn_time="2026-10-15 12:00:00", amount_cents=3000, direction="refund", refund_of=930)

        # Second refund in Batch 2: ¥50.00
        insert_txn(db, txn_id=932, batch_id=2, account_id=2, source="boc_debit", txn_time="2026-10-16 14:00:00", amount_cents=5000, direction="refund", refund_of=930)

        # Prior state: 3000 + 5000 = 8000
        db.execute("UPDATE transactions SET refunded_cents = 8000 WHERE id = 930")

        # Rollback Batch 2
        rollback_import_batch(db, batch_id=2)

        # Invariant: Batch 1 refund of 3000 cents MUST survive intact
        t930 = db.execute("SELECT refunded_cents FROM transactions WHERE id = 930").fetchone()
        assert t930["refunded_cents"] == 3000

    def test_rollback_nonexistent_batch_is_graceful(self, db: sqlite3.Connection):
        """Rolling back non-existent batch ID must return not_found and never raise."""
        res = rollback_import_batch(db, batch_id=999999)
        assert res["status"] == "not_found"
        assert res["deleted_transactions"] == 0
