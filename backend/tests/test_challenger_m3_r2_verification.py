"""Empirical Verification & Stress Test Suite by Challenger 2 (Milestone 3 Iteration 2).

Verifies:
1. Strict temporal causality and skew boundaries [-600s, +48h] for second precision.
2. Symmetric ±48h window for date-only transactions.
3. Batch rollback direction symmetry and restoration across multiple scenarios.
4. Dynamic cardholder resolution across all 3 tiers with zero fallback leakage.
5. Bank in-source refund linking and edge cases.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

import pytest

from billapp.core.db import init_db
from billapp.dedup.engine import (
    confirm_match_link,
    reject_match_link,
    rollback_import_batch,
    run_dedup_pipeline,
)
from billapp.dedup.refund import link_in_source_refunds
from billapp.dedup.transfer import compute_transfer_score, pair_internal_transfers


@pytest.fixture
def test_db() -> sqlite3.Connection:
    """Fixture providing an isolated in-memory SQLite database initialized with schema."""
    conn = init_db(":memory:")
    yield conn
    conn.close()


def make_account(
    db: sqlite3.Connection,
    acc_id: int,
    kind: str,
    bank: str,
    tail: Optional[str] = None,
    holder: Optional[str] = None,
) -> int:
    db.execute(
        "INSERT INTO accounts (id, kind, bank, tail, holder) VALUES (?, ?, ?, ?, ?)",
        (acc_id, kind, bank, tail, holder),
    )
    return acc_id


def make_batch(db: sqlite3.Connection, batch_id: int, source: str = "test") -> int:
    db.execute(
        """
        INSERT INTO import_batches (id, file_name, file_sha1, file_size, source, imported_at, row_count)
        VALUES (?, ?, ?, 100, ?, datetime('now', 'localtime'), 1)
        """,
        (batch_id, f"batch_{batch_id}.csv", f"sha_{batch_id}_{source}", source),
    )
    return batch_id


def make_txn(
    db: sqlite3.Connection,
    txn_id: int,
    batch_id: int,
    account_id: int,
    source: str,
    txn_time: str,
    amount_cents: int,
    direction: str = "expense",
    currency: str = "CNY",
    time_precision: str = "second",
    counterparty: Optional[str] = None,
    description: Optional[str] = None,
    channel: Optional[str] = None,
    merchant_norm: Optional[str] = None,
    status: Optional[str] = None,
    fingerprint: Optional[str] = None,
    transfer_peer: Optional[int] = None,
    refund_of: Optional[int] = None,
    refunded_cents: int = 0,
    duplicate_of: Optional[int] = None,
) -> int:
    fp = fingerprint or f"fp_{txn_id}"
    db.execute(
        """
        INSERT INTO transactions (
            id, batch_id, account_id, source, txn_time, time_precision, amount_cents,
            currency, direction, counterparty, description, channel, merchant_norm,
            status, fingerprint, transfer_peer, refund_of, refunded_cents, duplicate_of
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            txn_id, batch_id, account_id, source, txn_time, time_precision, amount_cents,
            currency, direction, counterparty, description, channel, merchant_norm,
            status, fp, transfer_peer, refund_of, refunded_cents, duplicate_of
        ),
    )
    return txn_id


class TestTemporalCausalityEmpirical:
    """Rigorous empirical verification of temporal causality gating and clock skew limits."""

    def test_clock_skew_exact_boundaries(self):
        """Test exact mathematical boundary conditions for clock skew [-600s, +172800s]."""
        base_out = {
            "id": 1, "source": "wechat", "txn_time": "2026-11-09 12:00:00",
            "time_precision": "second", "amount_cents": 5000, "currency": "CNY",
            "acc_kind": "wallet", "counterparty": "建设银行(5678)", "description": "提现",
        }
        base_in = {
            "id": 2, "source": "bank_profile", "time_precision": "second",
            "amount_cents": 5000, "currency": "CNY", "acc_kind": "bank_debit",
            "acc_bank": "建设银行", "acc_tail": "5678", "counterparty": "微信转入",
        }

        # Case 1: Exactly -600.0s (11:50:00 vs 12:00:00) -> Must pass
        in_minus_600 = dict(base_in, txn_time="2026-11-09 11:50:00")
        score_600, _ = compute_transfer_score(base_out, in_minus_600)
        assert score_600 >= 0.85, f"Exact -600s boundary must be accepted, got {score_600}"

        # Case 2: -600.5s / -601.0s (11:49:59 vs 12:00:00) -> Must strictly return 0.0
        in_minus_601 = dict(base_in, txn_time="2026-11-09 11:49:59")
        score_601, _ = compute_transfer_score(base_out, in_minus_601)
        assert score_601 == 0.0, f"Boundary violation -601s must return 0.0, got {score_601}"

        # Case 3: Reverse-causal large deltas (-1 hour, -12 hours, -24 hours) -> All 0.0
        for neg_sec in (3600, 43200, 86400):
            t_neg = (datetime(2026, 11, 9, 12, 0, 0) - timedelta(seconds=neg_sec)).strftime("%Y-%m-%d %H:%M:%S")
            in_neg = dict(base_in, txn_time=t_neg)
            score_neg, _ = compute_transfer_score(base_out, in_neg)
            assert score_neg == 0.0, f"Negative delta -{neg_sec}s must return 0.0, got {score_neg}"

        # Case 4: Exactly +172800s (+48 hours: 2026-11-11 12:00:00) -> Must pass
        in_plus_48h = dict(base_in, txn_time="2026-11-11 12:00:00")
        score_48h, _ = compute_transfer_score(base_out, in_plus_48h)
        assert score_48h > 0.0, f"Exact +48h must be accepted, got {score_48h}"

        # Case 5: +172801s (+48 hours and 1 second) -> Must return 0.0
        in_plus_48h1s = dict(base_in, txn_time="2026-11-11 12:00:01")
        score_48h1s, _ = compute_transfer_score(base_out, in_plus_48h1s)
        assert score_48h1s == 0.0, f"+48h+1s must return 0.0, got {score_48h1s}"

    def test_date_only_symmetry(self):
        """Date-only transactions (time_precision='day' or 00:00:00) preserve symmetric +-48h window."""
        base_out = {
            "id": 1, "source": "cmb_debit", "txn_time": "2026-11-09 00:00:00",
            "time_precision": "day", "amount_cents": 10000, "currency": "CNY",
            "acc_kind": "bank_debit", "counterparty": "中国银行(6666)", "description": "跨行转账",
        }
        base_in = {
            "id": 2, "source": "boc_debit", "txn_time": "2026-11-08 00:00:00",  # 24h earlier!
            "time_precision": "day", "amount_cents": 10000, "currency": "CNY",
            "acc_kind": "bank_debit", "acc_bank": "中国银行", "acc_tail": "6666",
            "counterparty": "招商银行(1234)", "description": "跨行转入",
        }

        # Date-only allows -24h because bank settlement clearing dates can vary by calendar day
        score, _ = compute_transfer_score(base_out, base_in)
        assert score >= 0.55, f"Date-only -24h should be permitted, got {score}"

        # Exceeding 48h (e.g. -49h or -72h) must reject
        base_in_72h = dict(base_in, txn_time="2026-11-06 00:00:00")
        score_72h, _ = compute_transfer_score(base_out, base_in_72h)
        assert score_72h == 0.0, f"Date-only -72h must return 0.0, got {score_72h}"


class TestRollbackDirectionSymmetryEmpirical:
    """Verify bidirectional restoration of authentic transaction direction upon batch rollback."""

    def test_inflow_batch_rollback_reverts_outflow_direction(self, test_db: sqlite3.Connection):
        """When Inflow batch is rolled back, Outflow reverts from 'transfer' to 'expense'."""
        make_batch(test_db, 10, "wechat")
        make_batch(test_db, 20, "bank")
        make_account(test_db, 1, "wallet", "微信", None, "李四")
        make_account(test_db, 2, "bank_debit", "建设银行", "5678", "李四")

        make_txn(
            test_db, txn_id=101, batch_id=10, account_id=1, source="wechat",
            txn_time="2026-11-09 10:00:00", amount_cents=8000, direction="expense",
            counterparty="建设银行(5678)", description="提现",
        )
        make_txn(
            test_db, txn_id=201, batch_id=20, account_id=2, source="bank",
            txn_time="2026-11-09 10:05:00", amount_cents=8000, direction="income",
            counterparty="微信转入", description="银联入账",
        )

        matches = pair_internal_transfers(test_db)
        assert len(matches) == 1
        assert matches[0]["status"] == "auto"

        # Both transactions are 'transfer'
        t101 = test_db.execute("SELECT direction, transfer_peer FROM transactions WHERE id = 101").fetchone()
        t201 = test_db.execute("SELECT direction, transfer_peer FROM transactions WHERE id = 201").fetchone()
        assert t101["direction"] == "transfer" and t101["transfer_peer"] == 201
        assert t201["direction"] == "transfer" and t201["transfer_peer"] == 101

        # Roll back batch 20 (inflow)
        res = rollback_import_batch(test_db, batch_id=20)
        assert res["deleted_transactions"] == 1

        # Outflow 101 must revert to direction='expense' and transfer_peer=None
        t101_after = test_db.execute("SELECT direction, transfer_peer FROM transactions WHERE id = 101").fetchone()
        assert t101_after["transfer_peer"] is None
        assert t101_after["direction"] == "expense"

    def test_outflow_batch_rollback_reverts_inflow_direction(self, test_db: sqlite3.Connection):
        """When Outflow batch is rolled back, Inflow reverts from 'transfer' to 'income'."""
        make_batch(test_db, 30, "wechat")
        make_batch(test_db, 40, "bank")
        make_account(test_db, 3, "wallet", "微信", None, "王五")
        make_account(test_db, 4, "bank_debit", "招商银行", "1234", "王五")

        make_txn(
            test_db, txn_id=301, batch_id=30, account_id=3, source="wechat",
            txn_time="2026-11-09 10:00:00", amount_cents=12000, direction="expense",
            counterparty="招商银行(1234)", description="提现",
        )
        make_txn(
            test_db, txn_id=401, batch_id=40, account_id=4, source="bank",
            txn_time="2026-11-09 10:02:00", amount_cents=12000, direction="income",
            counterparty="微信转入", description="入账",
        )

        matches = pair_internal_transfers(test_db)
        assert len(matches) == 1
        assert matches[0]["status"] == "auto"

        # Roll back batch 30 (outflow)
        res = rollback_import_batch(test_db, batch_id=30)
        assert res["deleted_transactions"] == 1

        # Inflow 401 must revert to direction='income' and transfer_peer=None
        t401_after = test_db.execute("SELECT direction, transfer_peer FROM transactions WHERE id = 401").fetchone()
        assert t401_after["transfer_peer"] is None
        assert t401_after["direction"] == "income"

    def test_partial_multi_pair_batch_rollback_isolation(self, test_db: sqlite3.Connection):
        """Rolling back one batch only affects transfer links connected to it."""
        make_batch(test_db, 50, "batch_a")
        make_batch(test_db, 60, "batch_b")
        make_batch(test_db, 70, "batch_c")
        make_account(test_db, 5, "bank_debit", "招商银行", "1234", "赵六")
        make_account(test_db, 6, "bank_debit", "中国银行", "6666", "赵六")
        make_account(test_db, 7, "bank_debit", "工商银行", "8801", "赵六")

        # Batch 50 has 2 outflows
        make_txn(
            test_db, txn_id=501, batch_id=50, account_id=5, source="cmb",
            txn_time="2026-11-09 09:00:00", amount_cents=5000, direction="expense",
            counterparty="中国银行(6666)", description="转账",
        )
        make_txn(
            test_db, txn_id=502, batch_id=50, account_id=5, source="cmb",
            txn_time="2026-11-09 09:10:00", amount_cents=6000, direction="expense",
            counterparty="工商银行(8801)", description="转账",
        )

        # Batch 60 has inflow matching 501
        make_txn(
            test_db, txn_id=601, batch_id=60, account_id=6, source="boc",
            txn_time="2026-11-09 09:01:00", amount_cents=5000, direction="income",
            counterparty="招商银行(1234)", description="转入",
        )

        # Batch 70 has inflow matching 502
        make_txn(
            test_db, txn_id=701, batch_id=70, account_id=7, source="icbc",
            txn_time="2026-11-09 09:11:00", amount_cents=6000, direction="income",
            counterparty="招商银行(1234)", description="转入",
        )

        matches = pair_internal_transfers(test_db)
        assert len(matches) == 2

        # Roll back batch 60
        rollback_import_batch(test_db, batch_id=60)

        # 501 should revert to 'expense'
        t501 = test_db.execute("SELECT direction, transfer_peer FROM transactions WHERE id = 501").fetchone()
        assert t501["direction"] == "expense" and t501["transfer_peer"] is None

        # 502 and 701 should remain paired as 'transfer'
        t502 = test_db.execute("SELECT direction, transfer_peer FROM transactions WHERE id = 502").fetchone()
        t701 = test_db.execute("SELECT direction, transfer_peer FROM transactions WHERE id = 701").fetchone()
        assert t502["direction"] == "transfer" and t502["transfer_peer"] == 701
        assert t701["direction"] == "transfer" and t701["transfer_peer"] == 502


class TestDynamicCardholderResolutionEmpirical:
    """Verify dynamic cardholder resolution without hardcoded fallback."""

    def test_empty_string_accounts_holder_falls_through_to_settings(self, test_db: sqlite3.Connection):
        """An account with holder='' or '   ' must fall through to settings."""
        make_batch(test_db, 1, "test")
        # Empty whitespace holder
        make_account(test_db, 1, "bank_debit", "招商银行", "1234", "   ")
        make_account(test_db, 2, "bank_debit", "中国银行", "6666", None)

        # Settings has holder_name
        test_db.execute("INSERT INTO settings (key, value) VALUES ('holder_name', '\"孙悟空\"')")

        make_txn(
            test_db, txn_id=1, batch_id=1, account_id=1, source="cmb",
            txn_time="2026-11-09 10:00:00", amount_cents=3000, direction="expense",
            counterparty="中国银行(6666)", description="转账 孙悟空",
        )
        make_txn(
            test_db, txn_id=2, batch_id=1, account_id=2, source="boc",
            txn_time="2026-11-09 10:01:00", amount_cents=3000, direction="income",
            counterparty="招商银行(1234)", description="转入 孙悟空",
        )

        matches = pair_internal_transfers(test_db)
        assert len(matches) == 1
        assert matches[0]["status"] == "auto"
        assert matches[0]["score"] == 1.00

    def test_settings_json_dict_format(self, test_db: sqlite3.Connection):
        """Settings formatted as JSON dict `{"name": "猪八戒"}` resolves correctly."""
        make_batch(test_db, 1, "test")
        make_account(test_db, 1, "bank_debit", "招商银行", "1234", None)
        make_account(test_db, 2, "bank_debit", "中国银行", "6666", None)

        test_db.execute("INSERT INTO settings (key, value) VALUES ('cardholder_name', '{\"name\": \"猪八戒\"}')")

        make_txn(
            test_db, txn_id=10, batch_id=1, account_id=1, source="cmb",
            txn_time="2026-11-09 10:00:00", amount_cents=3000, direction="expense",
            counterparty="中国银行(6666)", description="转账 猪八戒",
        )
        make_txn(
            test_db, txn_id=11, batch_id=1, account_id=2, source="boc",
            txn_time="2026-11-09 10:01:00", amount_cents=3000, direction="income",
            counterparty="招商银行(1234)", description="转入 猪八戒",
        )

        matches = pair_internal_transfers(test_db)
        assert len(matches) == 1
        assert matches[0]["score"] == 1.00

    def test_unconfigured_stranger_never_receives_unearned_auto_pairing(self, test_db: sqlite3.Connection):
        """When user is unconfigured or named someone else, a counterparty mentioning a stranger name gets NO bonus."""
        make_batch(test_db, 1, "test")
        make_account(test_db, 1, "bank_debit", "招商银行", None, None)
        make_account(test_db, 2, "bank_debit", "中国银行", None, None)

        # No settings configured!
        make_txn(
            test_db, txn_id=20, batch_id=1, account_id=1, source="cmb",
            txn_time="2026-11-09 10:00:00", amount_cents=5000, direction="expense",
            counterparty="赵钱孙", description="转账给赵钱孙",
        )
        make_txn(
            test_db, txn_id=21, batch_id=1, account_id=2, source="boc",
            txn_time="2026-11-09 10:01:00", amount_cents=5000, direction="income",
            counterparty="赵钱孙", description="赵钱孙转入",
        )

        matches = pair_internal_transfers(test_db)
        assert len(matches) == 1
        # Without tail match and without cardholder match, score is only base(0.30) + time(0.15) + pattern(0.20) + keyword(0.15) = 0.80 (< 0.85 auto)
        assert matches[0]["status"] == "pending"
        assert matches[0]["score"] == 0.80


class TestBankRefundEdgeCasesEmpirical:
    """Empirical verification of bank in-source refund linking and edge cases."""

    def test_bank_refund_beyond_60_days_rejected(self, test_db: sqlite3.Connection):
        """A bank refund occurring 61 days after original purchase must NOT link."""
        make_batch(test_db, 1, "boc")
        make_account(test_db, 1, "bank_debit", "中国银行", "6666", "测试")

        # Purchase on 2026-08-01
        make_txn(
            test_db, txn_id=1, batch_id=1, account_id=1, source="boc_debit",
            txn_time="2026-08-01 12:00:00", amount_cents=1000, direction="expense",
            counterparty="商户A", merchant_norm="商户A",
        )
        # Refund on 2026-10-02 (62 days later)
        make_txn(
            test_db, txn_id=2, batch_id=1, account_id=1, source="boc_debit",
            txn_time="2026-10-02 12:00:00", amount_cents=1000, direction="refund",
            counterparty="商户A", merchant_norm="商户A",
        )

        linked = link_in_source_refunds(test_db, batch_id=1)
        assert linked == 0
        t2 = test_db.execute("SELECT refund_of FROM transactions WHERE id = 2").fetchone()
        assert t2["refund_of"] is None

    def test_bank_refund_with_closed_original_propagates_neutral(self, test_db: sqlite3.Connection):
        """When original transaction was neutral (e.g. closed), refund becomes neutral."""
        make_batch(test_db, 1, "boc")
        make_account(test_db, 1, "bank_debit", "中国银行", "6666", "测试")

        make_txn(
            test_db, txn_id=10, batch_id=1, account_id=1, source="boc_debit",
            txn_time="2026-10-16 13:26:05", amount_cents=2980, direction="neutral",
            status="交易关闭", counterparty="商户B", merchant_norm="商户B",
        )
        make_txn(
            test_db, txn_id=11, batch_id=1, account_id=1, source="boc_debit",
            txn_time="2026-10-16 13:26:22", amount_cents=2980, direction="refund",
            status="退款成功", counterparty="商户B", merchant_norm="商户B",
        )

        linked = link_in_source_refunds(test_db, batch_id=1)
        assert linked == 1

        t11 = test_db.execute("SELECT refund_of, direction FROM transactions WHERE id = 11").fetchone()
        assert t11["refund_of"] == 10
        assert t11["direction"] == "neutral"
