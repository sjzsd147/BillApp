"""Empirical Adversarial Gate Verification Suite for Milestone 3 Iteration 2.

Author: challenger_m3_r2_1 (Role: Dedup Adversarial Challenger)
Target: L3 cross-source scoring contract, tie-breaking, blocklist enforcement,
        greedy bipartite bijection, transfer causality, and rollback reversibility.
"""

from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime, timedelta
from typing import Optional

import pytest

from billapp.core.db import init_db
from billapp.dedup.cross_source import (
    compute_match_score,
    compute_match_score_with_breakdown,
    match_cross_source,
    normalize_merchant,
)
from billapp.dedup.transfer import (
    compute_transfer_score,
    pair_internal_transfers,
)
from billapp.dedup.engine import (
    confirm_match_link,
    is_pair_blocked,
    reject_match_link,
    rollback_import_batch,
    run_dedup_pipeline,
)
from billapp.dedup.refund import link_in_source_refunds


@pytest.fixture
def db() -> sqlite3.Connection:
    """Provide an isolated in-memory SQLite database initialized with schema."""
    conn = init_db(":memory:")
    yield conn
    conn.close()


def create_batch(db: sqlite3.Connection, batch_id: int, source: str = "test") -> int:
    db.execute(
        """
        INSERT OR IGNORE INTO import_batches (id, file_name, file_sha1, file_size, source, imported_at, row_count)
        VALUES (?, 'test.csv', ?, 100, ?, datetime('now', 'localtime'), 1)
        """,
        (batch_id, f"sha_{batch_id}_{source}", source),
    )
    return batch_id


def create_account(
    db: sqlite3.Connection,
    account_id: int,
    kind: str = "bank_credit",
    bank: str = "工商银行",
    tail: Optional[str] = "8801",
    holder: Optional[str] = "测试持卡人",
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
    fund_account_id: Optional[int] = None,
    source: str = "alipay",
    txn_time: str = "2026-11-09 12:00:00",
    time_precision: str = "second",
    amount_cents: int = 1000,
    currency: str = "CNY",
    direction: str = "expense",
    counterparty: Optional[str] = "测试商户",
    description: Optional[str] = "测试商品",
    channel: Optional[str] = None,
    duplicate_of: Optional[int] = None,
    transfer_peer: Optional[int] = None,
    refund_of: Optional[int] = None,
    refunded_cents: int = 0,
    fingerprint: Optional[str] = None,
) -> int:
    create_batch(db, batch_id=batch_id, source=source)
    fp = fingerprint or f"fp_gate_{txn_id}_{source}"
    db.execute(
        """
        INSERT INTO transactions (
            id, batch_id, account_id, fund_account_id, source, txn_time, time_precision,
            amount_cents, currency, direction, counterparty, description, channel,
            duplicate_of, transfer_peer, refund_of, refunded_cents, fingerprint
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            txn_id, batch_id, account_id, fund_account_id, source, txn_time, time_precision,
            amount_cents, currency, direction, counterparty, description, channel,
            duplicate_of, transfer_peer, refund_of, refunded_cents, fp,
        ),
    )
    return txn_id


# ==============================================================================
# 1. Candidate Scoring Contract & Arithmetic Invariant Stress
# ==============================================================================


class TestCandidateScoringContractAndArithmetic:
    """Empirical verification of scoring breakdown keys, components, and sum invariants."""

    def test_scoring_breakdown_schema_and_sum_invariant(self):
        """Breakdown must contain exactly {amount, tail, time, channel, name, penalty}

        and sum(breakdown.values()) clamped to [0.0, 1.0] must equal score.
        """
        t = datetime(2026, 11, 9, 12, 0, 0)
        expected_keys = {"amount", "tail", "time", "channel", "name", "penalty"}

        # Case A: Full match
        score, breakdown = compute_match_score_with_breakdown(
            p_amount_cents=2747,
            b_amount_cents=2747,
            p_tail="8801",
            b_tail="8801",
            p_time=t,
            b_time=t,
            b_channel="alipay",
            p_source="alipay",
            b_merchant_norm="金拱门",
            p_counterparty="麦当劳",
            p_description="金拱门餐食",
        )
        assert set(breakdown.keys()) == expected_keys
        assert breakdown["amount"] == 0.30
        assert breakdown["tail"] == 0.25
        assert breakdown["time"] == 0.30
        assert breakdown["channel"] == 0.10
        assert breakdown["name"] == 0.10
        assert breakdown["penalty"] == 0.0
        expected_total = sum(breakdown.values())
        assert math.isclose(score, min(1.0, max(0.0, round(expected_total, 4))))
        assert score == 1.0

        # Case B: Channel penalty (-0.25)
        score_pen, breakdown_pen = compute_match_score_with_breakdown(
            p_amount_cents=5000,
            b_amount_cents=5000,
            p_tail="8801",
            b_tail="8801",
            p_time=t,
            b_time=t,
            b_channel="tenpay",  # conflict with alipay platform
            p_source="alipay",
            b_merchant_norm="商户",
            p_counterparty="商户",
        )
        assert set(breakdown_pen.keys()) == expected_keys
        assert breakdown_pen["channel"] == 0.0
        assert breakdown_pen["penalty"] == -0.25
        assert math.isclose(
            score_pen,
            min(1.0, max(0.0, round(sum(breakdown_pen.values()), 4))),
        )
        assert score_pen == 0.70  # 0.30 + 0.25 + 0.30 + 0.0 + 0.10 - 0.25 = 0.70

        # Case C: Blocklisted pair returns all zeros
        score_blk, breakdown_blk = compute_match_score_with_breakdown(
            p_amount_cents=1000,
            b_amount_cents=1000,
            p_tail="8801",
            b_tail="8801",
            p_time=t,
            b_time=t,
            b_channel="alipay",
            p_source="alipay",
            b_merchant_norm="商户",
            p_counterparty="商户",
            is_blocked=True,
        )
        assert set(breakdown_blk.keys()) == expected_keys
        assert all(v == 0.0 for v in breakdown_blk.values())
        assert score_blk == 0.0

        # Case D: Amount mismatch returns all zeros
        score_mismatch, breakdown_mismatch = compute_match_score_with_breakdown(
            p_amount_cents=1000,
            b_amount_cents=2000,
            p_tail="8801",
            b_tail="8801",
            p_time=t,
            b_time=t,
            b_channel="alipay",
            p_source="alipay",
            b_merchant_norm="商户",
            p_counterparty="商户",
        )
        assert all(v == 0.0 for v in breakdown_mismatch.values())
        assert score_mismatch == 0.0

    def test_exhaustive_time_step_breakdown(self):
        """Empirically test time scores across all stepping intervals."""
        t0 = datetime(2026, 11, 9, 12, 0, 0)
        p_amt = 1000

        # Second precision stepping
        intervals = [
            (0, 0.30),
            (60, 0.30),
            (120, 0.30),
            (121, 0.20),
            (300, 0.20),
            (600, 0.20),
            (601, 0.10),
            (1200, 0.10),
            (1800, 0.10),
            (1801, 0.00),  # hard dropped
        ]
        for sec, expected_time_score in intervals:
            t_cand = t0 + timedelta(seconds=sec)
            score, breakdown = compute_match_score_with_breakdown(
                p_amount_cents=p_amt,
                b_amount_cents=p_amt,
                p_tail="8801",
                b_tail="8801",
                p_time=t0,
                b_time=t_cand,
                b_channel=None,
                p_source="alipay",
                b_merchant_norm="",
                p_counterparty="",
            )
            if sec > 1800:
                assert score == 0.0
                assert breakdown["time"] == 0.0
            else:
                assert breakdown["time"] == expected_time_score
                assert math.isclose(
                    score,
                    min(1.0, max(0.0, round(sum(breakdown.values()), 4))),
                )


# ==============================================================================
# 2. Tie-Breaking & Deterministic Greedy 1-to-1 Bipartite Matching
# ==============================================================================


class TestTieBreakingAndBipartiteDeterminism:
    """Empirical verification of greedy 1-to-1 matching and tie-breaking determinism."""

    def test_tie_breaking_by_time_diff_ascending(self, db: sqlite3.Connection):
        """When multiple platform records compete for one bank record with identical scores,

        the candidate with smaller time_diff must win greedily.
        """
        create_account(db, account_id=1, kind="platform", bank="支付宝", tail=None)
        create_account(db, account_id=2, kind="bank_credit", bank="工商银行", tail="8801")

        # Bank transaction at 12:00:00
        insert_txn(
            db, txn_id=100, batch_id=1, account_id=2,
            source="icbc_credit", txn_time="2026-11-09 12:00:00",
            amount_cents=5000, channel="alipay", counterparty="支付宝-麦当劳",
        )

        # Candidate P1 at 12:00:10 (diff = 10s)
        insert_txn(
            db, txn_id=101, batch_id=2, account_id=1, fund_account_id=2,
            source="alipay", txn_time="2026-11-09 12:00:10",
            amount_cents=5000, counterparty="麦当劳",
        )

        # Candidate P2 at 12:00:50 (diff = 50s)
        # Both P1 and P2 have identical score (1.0) because <=120s gets +0.30
        insert_txn(
            db, txn_id=102, batch_id=2, account_id=1, fund_account_id=2,
            source="alipay", txn_time="2026-11-09 12:00:50",
            amount_cents=5000, counterparty="麦当劳",
        )

        matches = match_cross_source(db)
        assert len(matches) == 1
        matched = matches[0]
        # P1 must win because time_diff (10s) < P2 time_diff (50s)
        assert matched["p_id"] == 101
        assert matched["b_id"] == 100

        # Verify duplicate_of in database
        b_row = db.execute("SELECT duplicate_of FROM transactions WHERE id = 100").fetchone()
        assert b_row["duplicate_of"] == 101

    def test_tie_breaking_by_id_determinism_when_time_diff_equal(self, db: sqlite3.Connection):
        """When score and time_diff are both identical, tie is deterministically broken by p_id."""
        create_account(db, account_id=1, kind="platform", bank="支付宝", tail=None)
        create_account(db, account_id=2, kind="bank_credit", bank="工商银行", tail="8801")

        # Bank transaction at 12:00:00
        insert_txn(
            db, txn_id=200, batch_id=1, account_id=2,
            source="icbc_credit", txn_time="2026-11-09 12:00:00",
            amount_cents=3000, channel="alipay", counterparty="支付宝-肯德基",
        )

        # Candidate P1 (id 201) at 12:00:10 (diff = 10s)
        insert_txn(
            db, txn_id=201, batch_id=2, account_id=1, fund_account_id=2,
            source="alipay", txn_time="2026-11-09 12:00:10",
            amount_cents=3000, counterparty="肯德基",
        )

        # Candidate P2 (id 202) at 11:59:50 (diff = 10s)
        insert_txn(
            db, txn_id=202, batch_id=2, account_id=1, fund_account_id=2,
            source="alipay", txn_time="2026-11-09 11:59:50",
            amount_cents=3000, counterparty="肯德基",
        )

        matches = match_cross_source(db)
        assert len(matches) == 1
        # 201 < 202 so 201 must win deterministically
        assert matches[0]["p_id"] == 201
        assert matches[0]["b_id"] == 200

    def test_dense_bipartite_bijection_20x20_stress(self, db: sqlite3.Connection):
        """Stress-test a dense 20x20 candidate matrix where all records share the exact same amount.

        Ensures strictly disjoint 1-to-1 bijection with zero duplicate links or multiple allocations.
        """
        create_account(db, account_id=1, kind="platform", bank="微信支付", tail=None)
        create_account(db, account_id=2, kind="bank_credit", bank="工商银行", tail="8801")

        # Create 20 platform and 20 bank transactions staggered by 1 second
        base_time = datetime(2026, 11, 9, 12, 0, 0)
        n = 20
        for i in range(n):
            t_iso = (base_time + timedelta(seconds=i)).isoformat()
            insert_txn(
                db, txn_id=300 + i, batch_id=1, account_id=1, fund_account_id=2,
                source="wechat", txn_time=t_iso, amount_cents=1990,
                counterparty=f"便利店_{i}",
            )
            insert_txn(
                db, txn_id=400 + i, batch_id=2, account_id=2,
                source="icbc_credit", txn_time=t_iso, amount_cents=1990,
                channel="tenpay", counterparty=f"财付通-便利店_{i}",
            )

        matches = match_cross_source(db)
        assert len(matches) == n

        matched_p = [m["p_id"] for m in matches]
        matched_b = [m["b_id"] for m in matches]

        # Bijective invariants
        assert len(matched_p) == len(set(matched_p)) == n
        assert len(matched_b) == len(set(matched_b)) == n

        # Check in DB
        dups = db.execute(
            "SELECT id, duplicate_of FROM transactions WHERE id BETWEEN 400 AND ?",
            (400 + n - 1,),
        ).fetchall()
        for row in dups:
            assert row["duplicate_of"] is not None
            assert 300 <= row["duplicate_of"] < 300 + n


# ==============================================================================
# 3. Blocklist Enforcement & Memory Permanence
# ==============================================================================


class TestBlocklistAdversarialScenarios:
    """Empirical verification that rejected pairs cannot auto-match even under optimal conditions."""

    def test_blocklisted_pair_ignored_even_with_perfect_score(self, db: sqlite3.Connection):
        """If a pair is in match_blocklist, match_cross_source must reject it completely."""
        create_account(db, account_id=1, kind="platform", bank="支付宝", tail=None)
        create_account(db, account_id=2, kind="bank_credit", bank="工商银行", tail="8801")

        p_id = insert_txn(
            db, txn_id=501, batch_id=1, account_id=1, fund_account_id=2,
            source="alipay", txn_time="2026-11-09 12:00:00", amount_cents=9900,
            counterparty="苹果专卖店",
        )
        b_id = insert_txn(
            db, txn_id=502, batch_id=2, account_id=2,
            source="icbc_credit", txn_time="2026-11-09 12:00:00", amount_cents=9900,
            channel="alipay", counterparty="支付宝-苹果专卖店",
        )

        # Pre-insert blocklist entry in canonical order
        db.execute(
            "INSERT INTO match_blocklist (a_id, b_id, kind, created_at) VALUES (?, ?, 'duplicate', datetime('now'))",
            (min(p_id, b_id), max(p_id, b_id)),
        )

        matches = match_cross_source(db)
        assert len(matches) == 0

        # Duplicate_of must remain NULL
        row = db.execute("SELECT duplicate_of FROM transactions WHERE id = ?", (b_id,)).fetchone()
        assert row["duplicate_of"] is None

    def test_user_rejection_frees_pair_and_enforces_blocklist(self, db: sqlite3.Connection):
        """User rejection of an established match link frees transactions and blocklists the pair."""
        create_account(db, account_id=1, kind="platform", bank="支付宝", tail=None)
        create_account(db, account_id=2, kind="bank_credit", bank="工商银行", tail="8801")

        p_id = insert_txn(
            db, txn_id=601, batch_id=1, account_id=1, fund_account_id=2,
            source="alipay", txn_time="2026-11-09 12:00:00", amount_cents=8800,
            counterparty="优衣库",
        )
        b_id = insert_txn(
            db, txn_id=602, batch_id=2, account_id=2,
            source="icbc_credit", txn_time="2026-11-09 12:00:00", amount_cents=8800,
            channel="alipay", counterparty="支付宝-优衣库",
        )

        # Step 1: Run pipeline -> auto matches
        summary = run_dedup_pipeline(db)
        assert summary.duplicates_auto == 1

        link = db.execute("SELECT id, status FROM match_links WHERE primary_id = ? AND secondary_id = ?", (p_id, b_id)).fetchone()
        assert link is not None
        assert link["status"] == "auto"

        # Check duplicate_of is set
        t_b = db.execute("SELECT duplicate_of FROM transactions WHERE id = ?", (b_id,)).fetchone()
        assert t_b["duplicate_of"] == p_id

        # Step 2: Reject link
        ok = reject_match_link(db, link["id"])
        assert ok is True

        # Check unlinked & blocklisted
        assert is_pair_blocked(db, p_id, b_id, kind="duplicate") is True
        t_b_after = db.execute("SELECT duplicate_of FROM transactions WHERE id = ?", (b_id,)).fetchone()
        assert t_b_after["duplicate_of"] is None

        # Step 3: Add an alternate bank transaction B2 (603) and re-run pipeline
        insert_txn(
            db, txn_id=603, batch_id=2, account_id=2,
            source="icbc_credit", txn_time="2026-11-09 12:00:05", amount_cents=8800,
            channel="alipay", counterparty="支付宝-优衣库",
        )

        summary2 = run_dedup_pipeline(db)
        assert summary2.duplicates_auto == 1  # B2 matches P, but B1 remains blocked!

        t_b1 = db.execute("SELECT duplicate_of FROM transactions WHERE id = ?", (b_id,)).fetchone()
        t_b2 = db.execute("SELECT duplicate_of FROM transactions WHERE id = 603").fetchone()

        assert t_b1["duplicate_of"] is None  # Blocked pair 601 <-> 602 never rematches
        assert t_b2["duplicate_of"] == p_id  # Alternate pair 601 <-> 603 matches


# ==============================================================================
# 4. Bank Refund Linking & Window Boundaries
# ==============================================================================


class TestBankInSourceRefundBoundaries:
    """Empirical verification of bank refund 60-day window and amount matching."""

    def test_bank_refund_60_day_window_boundaries(self, db: sqlite3.Connection):
        """Bank refund within 60 days links; refund beyond 60 days is rejected."""
        create_account(db, account_id=10, kind="bank_debit", bank="中国银行", tail="6666")

        # Purchase at 2026-09-01 10:00:00
        insert_txn(
            db, txn_id=701, batch_id=1, account_id=10,
            source="boc_debit", txn_time="2026-09-01 10:00:00", amount_cents=2980,
            direction="expense", counterparty="某商户",
        )

        # Refund 59 days later (within 60 days): 2026-10-30 10:00:00
        insert_txn(
            db, txn_id=702, batch_id=2, account_id=10,
            source="boc_debit", txn_time="2026-10-30 10:00:00", amount_cents=2980,
            direction="refund", counterparty="某商户",
        )

        linked = link_in_source_refunds(db)
        assert linked == 1

        t_orig = db.execute("SELECT refunded_cents FROM transactions WHERE id = 701").fetchone()
        t_ref = db.execute("SELECT refund_of FROM transactions WHERE id = 702").fetchone()
        assert t_orig["refunded_cents"] == 2980
        assert t_ref["refund_of"] == 701

    def test_bank_refund_beyond_60_days_not_linked(self, db: sqlite3.Connection):
        """Refund 65 days after original purchase must not link."""
        create_account(db, account_id=11, kind="bank_debit", bank="中国银行", tail="6666")

        insert_txn(
            db, txn_id=711, batch_id=1, account_id=11,
            source="boc_debit", txn_time="2026-08-01 10:00:00", amount_cents=2980,
            direction="expense", counterparty="某商户",
        )
        insert_txn(
            db, txn_id=712, batch_id=2, account_id=11,
            source="boc_debit", txn_time="2026-10-06 10:00:00", amount_cents=2980,  # 66 days
            direction="refund", counterparty="某商户",
        )

        linked = link_in_source_refunds(db)
        assert linked == 0

        t_orig = db.execute("SELECT refunded_cents FROM transactions WHERE id = 711").fetchone()
        t_ref = db.execute("SELECT refund_of FROM transactions WHERE id = 712").fetchone()
        assert t_orig["refunded_cents"] == 0
        assert t_ref["refund_of"] is None


# ==============================================================================
# 5. Transfer Causality & Clock Skew Gating
# ==============================================================================


class TestTransferCausalityGate:
    """Empirical verification of temporal causality gating and clock skew boundaries."""

    def test_clock_skew_boundary_minus_599s_vs_minus_601s(self):
        """Inflow 599s before outflow is accepted (clock skew); 601s before is strictly rejected."""
        t_out = datetime(2026, 11, 9, 12, 10, 0)
        out_txn = {
            "id": 1,
            "source": "wechat",
            "txn_time": t_out.isoformat(),
            "time_precision": "second",
            "amount_cents": 50000,
            "currency": "CNY",
            "counterparty": "零钱提现-招商银行(1234)",
            "description": "微信提现",
        }

        # 599s before (12:00:01) -> within -600s boundary
        t_in_599 = t_out - timedelta(seconds=599)
        in_txn_599 = {
            "id": 2,
            "source": "cmb_debit",
            "txn_time": t_in_599.isoformat(),
            "time_precision": "second",
            "amount_cents": 50000,
            "currency": "CNY",
            "acc_bank": "招商银行",
            "acc_tail": "1234",
            "description": "银联入账",
        }
        score_599, _ = compute_transfer_score(out_txn, in_txn_599)
        assert score_599 >= 0.85  # Accepted as auto match

        # 601s before (11:59:59) -> exceeds -600s clock skew tolerance
        t_in_601 = t_out - timedelta(seconds=601)
        in_txn_601 = {
            "id": 3,
            "source": "cmb_debit",
            "txn_time": t_in_601.isoformat(),
            "time_precision": "second",
            "amount_cents": 50000,
            "currency": "CNY",
            "acc_bank": "招商银行",
            "acc_tail": "1234",
            "description": "银联入账",
        }
        score_601, _ = compute_transfer_score(out_txn, in_txn_601)
        assert score_601 == 0.0  # Rejected by temporal causality gating

    def test_date_only_transfer_symmetric_window(self):
        """When either side is date-only (time_precision='day'), symmetric 48h window is allowed."""
        out_txn = {
            "id": 10,
            "source": "boc_debit",
            "txn_time": "2026-11-09 00:00:00",
            "time_precision": "day",
            "amount_cents": 100000,
            "currency": "CNY",
            "counterparty": "招商银行(1234)",
            "description": "转账",
        }
        in_txn_prior_day = {
            "id": 11,
            "source": "cmb_debit",
            "txn_time": "2026-11-08 00:00:00",
            "time_precision": "day",
            "amount_cents": 100000,
            "currency": "CNY",
            "acc_bank": "招商银行",
            "acc_tail": "1234",
            "description": "转账入账",
        }
        # Inflow 1 day earlier on date-only is accepted
        score, _ = compute_transfer_score(out_txn, in_txn_prior_day)
        assert score > 0.50


# ==============================================================================
# 6. Batch Rollback Direction Reversibility
# ==============================================================================


class TestRollbackDirectionReversibility:
    """Empirical verification that batch rollback restores surviving transaction directions."""

    def test_rollback_reverts_transfer_direction_to_original(self, db: sqlite3.Connection):
        """When an internal transfer pairs an expense with an income, rolling back one batch

        must restore the surviving transaction's direction back to its original expense state.
        """
        create_account(db, account_id=1, kind="bank_debit", bank="中国银行", tail="6666")
        create_account(db, account_id=2, kind="bank_credit", bank="工商银行", tail="8801")

        # Batch 1: Outflow payment (initially 'expense')
        insert_txn(
            db, txn_id=801, batch_id=1, account_id=1,
            source="boc_debit", txn_time="2026-11-09 10:00:00", amount_cents=150000,
            direction="expense", counterparty="工商银行信用卡还款",
        )

        # Batch 2: Inflow deposit (initially 'income')
        insert_txn(
            db, txn_id=802, batch_id=2, account_id=2,
            source="icbc_credit", txn_time="2026-11-09 10:02:00", amount_cents=150000,
            direction="income", counterparty="还款",
        )

        # Run pipeline -> paired as transfer
        summary = run_dedup_pipeline(db)
        assert summary.transfers_paired == 1

        t801 = db.execute("SELECT direction, transfer_peer FROM transactions WHERE id = 801").fetchone()
        t802 = db.execute("SELECT direction, transfer_peer FROM transactions WHERE id = 802").fetchone()
        assert t801["direction"] == "transfer"
        assert t801["transfer_peer"] == 802
        assert t802["direction"] == "transfer"
        assert t802["transfer_peer"] == 801

        # Now roll back Batch 2 (Inflow)
        rollback_info = rollback_import_batch(db, batch_id=2)
        assert rollback_info["deleted_transactions"] == 1

        # Transaction 801 (surviving in Batch 1) MUST have direction reverted to 'expense'!
        t801_after = db.execute("SELECT direction, transfer_peer FROM transactions WHERE id = 801").fetchone()
        assert t801_after["transfer_peer"] is None
        assert t801_after["direction"] == "expense", f"Expected 'expense' but got '{t801_after['direction']}'"

