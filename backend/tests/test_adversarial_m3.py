"""Adversarial Verification & Stress Test Suite for Milestone 3 (Deduplication Pipeline).

Author: challenger_m3_1 (Role: Adversarial Verifier)
Target: backend/billapp/dedup/

Mandatory Verification Dimensions:
1. Boundary Value Analysis on Time Windows:
   - 119s vs 121s (0.30 vs 0.20 score)
   - 599s vs 601s (0.20 vs 0.10 score)
   - 1799s vs 1801s (0.10 score vs hard drop 0.0)
   - Day-level boundaries (credit 5d vs debit 3d)
2. High-Concurrency / Identical Amount Clusters:
   - 10 transactions of identical amount in the same minute -> exact 1-to-1 bijection, 0 duplicate links
   - Asymmetric clusters (platform > bank and bank > platform)
   - 100 identical amount cluster stress test
3. Tail Conflict Hard Exclusion:
   - Known conflicting tails NEVER match even with 100% merchant match and 0s time diff
4. Channel Conflict Severe Penalty:
   - Bank channel tenpay vs platform alipay -> -0.25 penalty blocks false auto-match
   - Triangular candidate competition (bank channel chooses rightful platform)
5. Currency Isolation:
   - USD vs CNY transactions never pair in cross-source or transfer matching
6. Edge Case & State Invariants:
   - Expense vs refund cross-source rejection
   - Zero/negative amount rejection
   - Idempotency under multi-run pipeline execution
   - Rejection memory permanence under re-runs
"""

from __future__ import annotations

import json
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
    DedupSummary,
    is_pair_blocked,
    reject_match_link,
    run_dedup_pipeline,
)


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
    fp = fingerprint or f"fp_adv_{txn_id}_{source}"
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
# 1. Boundary Value Analysis on Time Windows
# ==============================================================================


class TestAdversarialTimeWindowBoundaries:
    """Stress tests for second-level and day-level time windows.
    
    Target boundaries:
    - 119s vs 121s (≤120s: +0.30 vs 121-600s: +0.20)
    - 599s vs 601s (≤600s: +0.20 vs 601-1800s: +0.10)
    - 1799s vs 1801s (≤1800s: +0.10 vs >1800s: hard drop 0.0)
    """

    def test_second_precision_time_score_steps(self):
        t0 = datetime(2026, 11, 9, 12, 0, 0)

        # Baseline parameters: base 0.30, tail 0.25, channel 0.10, name 0.00 -> subtotal = 0.65
        def get_score_breakdown(delta_seconds: int):
            t_target = t0 + timedelta(seconds=delta_seconds)
            return compute_match_score_with_breakdown(
                p_amount_cents=5000,
                b_amount_cents=5000,
                p_tail="8801",
                b_tail="8801",
                p_time=t0,
                b_time=t_target,
                b_channel="alipay",
                p_source="alipay",
                b_merchant_norm="",
                p_counterparty="商户",
                p_precision="second",
                b_precision="second",
            )

        # 0s boundary
        s0, bd0 = get_score_breakdown(0)
        assert bd0["time"] == 0.30
        assert s0 == 0.95  # 0.65 + 0.30 = 0.95 (auto)

        # 119s vs 120s vs 121s
        s119, bd119 = get_score_breakdown(119)
        s120, bd120 = get_score_breakdown(120)
        s121, bd121 = get_score_breakdown(121)

        assert bd119["time"] == 0.30
        assert bd120["time"] == 0.30
        assert bd121["time"] == 0.20
        assert s119 == 0.95
        assert s120 == 0.95
        assert s121 == 0.85  # Exactly at auto threshold 0.85

        # 599s vs 600s vs 601s
        s599, bd599 = get_score_breakdown(599)
        s600, bd600 = get_score_breakdown(600)
        s601, bd601 = get_score_breakdown(601)

        assert bd599["time"] == 0.20
        assert bd600["time"] == 0.20
        assert bd601["time"] == 0.10
        assert s599 == 0.85
        assert s600 == 0.85
        # 601s drops below auto threshold (0.85) to pending (0.75)!
        assert s601 == 0.75

        # 1799s vs 1800s vs 1801s (hard drop!)
        s1799, bd1799 = get_score_breakdown(1799)
        s1800, bd1800 = get_score_breakdown(1800)
        s1801, bd1801 = get_score_breakdown(1801)

        assert bd1799["time"] == 0.10
        assert bd1800["time"] == 0.10
        assert s1799 == 0.75
        assert s1800 == 0.75

        # 1801s must be hard dropped to 0.0
        assert s1801 == 0.0
        assert bd1801["time"] == 0.0
        assert bd1801["amount"] == 0.0

    def test_day_precision_time_boundaries_credit_vs_debit(self):
        t0 = datetime(2026, 11, 1, 0, 0, 0)

        def eval_day(days: int, card_kind: str):
            t_target = t0 + timedelta(days=days)
            return compute_match_score(
                p_amount_cents=1000,
                b_amount_cents=1000,
                p_tail="8801",
                b_tail="8801",
                p_time=t0,
                b_time=t_target,
                b_channel="alipay",
                p_source="alipay",
                b_merchant_norm="商户",
                p_counterparty="商户",
                p_precision="day",
                b_precision="day",
                b_card_kind=card_kind,
            )

        # Debit card: max 3 days allowed, day 4 hard dropped
        assert eval_day(0, "bank_debit") > 0.0
        assert eval_day(1, "bank_debit") > 0.0
        assert eval_day(3, "bank_debit") > 0.0
        assert eval_day(4, "bank_debit") == 0.0  # Debit card day 4 hard drop

        # Credit card: max 5 days allowed, day 6 hard dropped
        assert eval_day(3, "bank_credit") > 0.0
        assert eval_day(4, "bank_credit") > 0.0
        assert eval_day(5, "bank_credit") > 0.0
        assert eval_day(6, "bank_credit") == 0.0  # Credit card day 6 hard drop


# ==============================================================================
# 2. High-Concurrency / Identical Amount Clusters
# ==============================================================================


class TestAdversarialBipartiteBijection:
    """Stress tests for greedy 1-to-1 allocation under dense identical amount clusters.
    
    Verifies that identical transactions within the same minute form strict 1-to-1
    bijection with 0 duplicate links.
    """

    def test_concurrency_cluster_identical_amounts_10_txns(self, db: sqlite3.Connection):
        create_account(db, account_id=1, kind="platform", bank="支付宝", tail=None)
        create_account(db, account_id=2, kind="bank_credit", bank="工商银行", tail="8801")

        base_time = datetime(2026, 11, 9, 14, 0, 0)
        num_txns = 10
        amount = 1888  # 18.88 CNY

        # Insert 10 platform transactions within the same minute: 14:00:00, 14:00:05, ..., 14:00:45
        for i in range(num_txns):
            t_str = (base_time + timedelta(seconds=i * 5)).strftime("%Y-%m-%d %H:%M:%S")
            insert_txn(
                db,
                txn_id=100 + i,
                batch_id=1,
                account_id=1,
                fund_account_id=2,
                source="alipay",
                txn_time=t_str,
                amount_cents=amount,
                counterparty="奈雪的茶",
                description="饮品",
                fingerprint=f"p_cluster_{i}",
            )

        # Insert 10 bank transactions with slight clock skews: 14:00:02, 14:00:07, ..., 14:00:47
        for i in range(num_txns):
            t_str = (base_time + timedelta(seconds=i * 5 + 2)).strftime("%Y-%m-%d %H:%M:%S")
            insert_txn(
                db,
                txn_id=200 + i,
                batch_id=2,
                account_id=2,
                fund_account_id=None,
                source="icbc_credit",
                txn_time=t_str,
                amount_cents=amount,
                counterparty="支付宝-奈雪的茶",
                description="消费",
                channel="alipay",
                fingerprint=f"b_cluster_{i}",
            )

        matches = match_cross_source(db)

        # Invariant 1: Exactly 10 matches formed
        assert len(matches) == num_txns

        matched_p_ids = [m["p_id"] for m in matches]
        matched_b_ids = [m["b_id"] for m in matches]

        # Invariant 2: Strict bijection — every platform txn matched exactly once
        assert len(set(matched_p_ids)) == num_txns
        assert set(matched_p_ids) == set(range(100, 100 + num_txns))

        # Invariant 3: Strict bijection — every bank txn matched exactly once
        assert len(set(matched_b_ids)) == num_txns
        assert set(matched_b_ids) == set(range(200, 200 + num_txns))

        # Invariant 4: Zero duplicate links in database
        bank_dups = db.execute(
            "SELECT id, duplicate_of FROM transactions WHERE id >= 200 AND id < 210"
        ).fetchall()
        assigned_dups = [row["duplicate_of"] for row in bank_dups]

        assert None not in assigned_dups
        assert len(set(assigned_dups)) == num_txns
        assert set(assigned_dups) == set(range(100, 100 + num_txns))

        # Invariant 5: Platform records are primary (duplicate_of IS NULL)
        p_dups = db.execute(
            "SELECT id, duplicate_of FROM transactions WHERE id >= 100 AND id < 110"
        ).fetchall()
        for row in p_dups:
            assert row["duplicate_of"] is None

    def test_asymmetric_bipartite_matching_platform_excess(self, db: sqlite3.Connection):
        """15 platform transactions vs 10 bank transactions.
        Exactly 10 matches formed; 5 platform transactions remain unmatched (never duplicate-linked).
        """
        create_account(db, account_id=1, kind="platform", bank="支付宝", tail=None)
        create_account(db, account_id=2, kind="bank_credit", bank="工商银行", tail="8801")
        base_time = datetime(2026, 11, 9, 15, 0, 0)

        for i in range(15):
            t_str = (base_time + timedelta(seconds=i * 10)).strftime("%Y-%m-%d %H:%M:%S")
            insert_txn(
                db, txn_id=300 + i, batch_id=1, account_id=1, fund_account_id=2,
                source="alipay", txn_time=t_str, amount_cents=3000,
                counterparty="喜茶", fingerprint=f"p_asym_{i}",
            )

        for i in range(10):
            t_str = (base_time + timedelta(seconds=i * 10 + 1)).strftime("%Y-%m-%d %H:%M:%S")
            insert_txn(
                db, txn_id=400 + i, batch_id=2, account_id=2,
                source="icbc_credit", txn_time=t_str, amount_cents=3000,
                counterparty="支付宝-喜茶", channel="alipay", fingerprint=f"b_asym_{i}",
            )

        matches = match_cross_source(db)
        assert len(matches) == 10
        assert len({m["p_id"] for m in matches}) == 10
        assert len({m["b_id"] for m in matches}) == 10

        # Unmatched platform transactions remain duplicate_of NULL
        p_unmatched = db.execute(
            "SELECT count(*) as cnt FROM transactions WHERE id >= 300 AND id < 315 AND duplicate_of IS NULL"
        ).fetchone()["cnt"]
        assert p_unmatched == 15

    def test_asymmetric_bipartite_matching_bank_excess(self, db: sqlite3.Connection):
        """10 platform transactions vs 15 bank transactions.
        Exactly 10 matches formed; 5 bank transactions remain independent transactions.
        """
        create_account(db, account_id=1, kind="platform", bank="支付宝", tail=None)
        create_account(db, account_id=2, kind="bank_credit", bank="工商银行", tail="8801")
        base_time = datetime(2026, 11, 9, 16, 0, 0)

        for i in range(10):
            t_str = (base_time + timedelta(seconds=i * 10)).strftime("%Y-%m-%d %H:%M:%S")
            insert_txn(
                db, txn_id=500 + i, batch_id=1, account_id=1, fund_account_id=2,
                source="alipay", txn_time=t_str, amount_cents=2500,
                counterparty="星巴克", fingerprint=f"p_b_asym_{i}",
            )

        for i in range(15):
            t_str = (base_time + timedelta(seconds=i * 10 + 1)).strftime("%Y-%m-%d %H:%M:%S")
            insert_txn(
                db, txn_id=600 + i, batch_id=2, account_id=2,
                source="icbc_credit", txn_time=t_str, amount_cents=2500,
                counterparty="支付宝-星巴克", channel="alipay", fingerprint=f"b_b_asym_{i}",
            )

        matches = match_cross_source(db)
        assert len(matches) == 10

        # 5 bank records must remain independent (duplicate_of IS NULL)
        b_indep = db.execute(
            "SELECT count(*) as cnt FROM transactions WHERE id >= 600 AND id < 615 AND duplicate_of IS NULL"
        ).fetchone()["cnt"]
        assert b_indep == 5

    def test_stress_100_identical_amount_cluster(self, db: sqlite3.Connection):
        """Stress test: 100 simultaneous transactions of identical amount.
        Verifies performance, correctness, and bijection under heavy load.
        """
        create_account(db, account_id=1, kind="platform", bank="支付宝", tail=None)
        create_account(db, account_id=2, kind="bank_credit", bank="工商银行", tail="8801")
        base_time = datetime(2026, 11, 9, 10, 0, 0)
        n = 100

        for i in range(n):
            t_p = (base_time + timedelta(seconds=i * 2)).strftime("%Y-%m-%d %H:%M:%S")
            insert_txn(
                db, txn_id=1000 + i, batch_id=1, account_id=1, fund_account_id=2,
                source="alipay", txn_time=t_p, amount_cents=990,
                counterparty="自动售货机", fingerprint=f"p_stress_{i}",
            )
            t_b = (base_time + timedelta(seconds=i * 2 + 1)).strftime("%Y-%m-%d %H:%M:%S")
            insert_txn(
                db, txn_id=2000 + i, batch_id=2, account_id=2,
                source="icbc_credit", txn_time=t_b, amount_cents=990,
                counterparty="支付宝-自动售货机", channel="alipay", fingerprint=f"b_stress_{i}",
            )

        matches = match_cross_source(db)
        assert len(matches) == n
        assert len({m["p_id"] for m in matches}) == n
        assert len({m["b_id"] for m in matches}) == n


# ==============================================================================
# 3. Tail Conflict Hard Exclusion
# ==============================================================================


class TestAdversarialTailConflictHardExclusion:
    """Stress tests ensuring that known conflicting card tails NEVER match under any circumstances."""

    def test_tail_conflict_with_100pct_merchant_and_0s_time_diff(self):
        t = datetime(2026, 11, 9, 12, 0, 0)
        score, bd = compute_match_score_with_breakdown(
            p_amount_cents=8888,
            b_amount_cents=8888,
            p_tail="8801",  # ICBC Credit Card
            b_tail="6666",  # BOC Debit Card (Conflict!)
            p_time=t,
            b_time=t,
            b_channel="alipay",
            p_source="alipay",
            b_merchant_norm="麦当劳",
            p_counterparty="麦当劳",
            p_description="麦当劳套餐",
        )
        # Even with 100% merchant match, 0s time diff, exact amount and channel, score MUST be 0.0
        assert score == 0.0
        assert bd["amount"] == 0.0
        assert bd["tail"] == 0.0
        assert bd["time"] == 0.0

    def test_tail_conflict_in_database_pipeline(self, db: sqlite3.Connection):
        create_account(db, account_id=1, kind="platform", bank="支付宝", tail=None)
        create_account(db, account_id=2, kind="bank_credit", bank="工商银行", tail="8801")
        create_account(db, account_id=3, kind="bank_debit", bank="中国银行", tail="6666")

        t = "2026-11-09 12:00:00"
        # Platform txn charged on 8801
        insert_txn(
            db, txn_id=701, batch_id=1, account_id=1, fund_account_id=2,
            source="alipay", txn_time=t, amount_cents=6666,
            counterparty="海底捞", fingerprint="fp_tail_p",
        )
        # Bank txn in account 6666
        insert_txn(
            db, txn_id=702, batch_id=2, account_id=3,
            source="boc_debit", txn_time=t, amount_cents=6666,
            counterparty="支付宝-海底捞", channel="alipay", fingerprint="fp_tail_b",
        )

        matches = match_cross_source(db)
        assert len(matches) == 0

        row = db.execute("SELECT duplicate_of FROM transactions WHERE id = 702").fetchone()
        assert row["duplicate_of"] is None

    @pytest.mark.parametrize("tail1,tail2", [
        ("1234", "4321"),
        ("1234", "1235"),
        ("0001", "0010"),
        ("8801", "8802"),
        ("6666", "7777"),
    ])
    def test_arbitrary_tail_mismatches(self, tail1: str, tail2: str):
        t = datetime(2026, 11, 9, 12, 0, 0)
        score = compute_match_score(
            p_amount_cents=1000,
            b_amount_cents=1000,
            p_tail=tail1,
            b_tail=tail2,
            p_time=t,
            b_time=t,
            b_channel="alipay",
            p_source="alipay",
            b_merchant_norm="商户",
            p_counterparty="商户",
        )
        assert score == 0.0


# ==============================================================================
# 4. Channel Conflict Severe Penalty
# ==============================================================================


class TestAdversarialChannelConflictPenalty:
    """Stress tests verifying that channel conflicts receive -0.25 penalty and block false matches."""

    def test_channel_conflict_penalty_blocks_auto_match(self):
        t1 = datetime(2026, 11, 9, 12, 0, 0)
        t2 = datetime(2026, 11, 9, 12, 0, 10)  # 10s diff

        # Bank channel says tenpay (WeChat), but platform is alipay!
        score, bd = compute_match_score_with_breakdown(
            p_amount_cents=5000,
            b_amount_cents=5000,
            p_tail="8801",
            b_tail="8801",
            p_time=t1,
            b_time=t2,
            b_channel="tenpay",
            p_source="alipay",
            b_merchant_norm="全家便利店",
            p_counterparty="全家便利店",
        )
        # Breakdown:
        # Base 0.30 + Tail 0.25 + Time 0.30 + Name 0.10 + Penalty -0.25 = 0.70
        assert bd["penalty"] == -0.25
        assert bd["channel"] == 0.0
        assert score == 0.70
        # 0.70 < 0.85 -> Must NOT auto-match!
        assert score < 0.85

    def test_channel_conflict_without_tail_drops_below_review_threshold(self):
        t = datetime(2026, 11, 9, 12, 0, 0)
        # Tails unknown on both sides
        score, bd = compute_match_score_with_breakdown(
            p_amount_cents=5000,
            b_amount_cents=5000,
            p_tail=None,
            b_tail=None,
            p_time=t,
            b_time=t,
            b_channel="tenpay",
            p_source="alipay",
            b_merchant_norm="罗森",
            p_counterparty="罗森",
        )
        # Breakdown:
        # Base 0.30 + Tail 0.00 + Time 0.30 + Name 0.10 + Penalty -0.25 = 0.45
        assert score == 0.45
        # 0.45 < 0.55 (review_threshold) -> completely excluded from candidate list!
        assert score < 0.55

    def test_channel_conflict_triangular_arbitration(self, db: sqlite3.Connection):
        """User spends 35.00 via Alipay and 35.00 via WeChat at the same merchant.
        Bank statement records a 35.00 charge with channel='tenpay'.
        Greedy matching must cleanly link Bank to WeChat, leaving Alipay untouched!
        """
        create_account(db, account_id=1, kind="platform", bank="支付宝", tail=None)
        create_account(db, account_id=2, kind="platform", bank="微信支付", tail=None)
        create_account(db, account_id=3, kind="bank_credit", bank="工商银行", tail="8801")

        t = "2026-11-09 18:00:00"

        # Platform 1: Alipay
        insert_txn(
            db, txn_id=801, batch_id=1, account_id=1, fund_account_id=3,
            source="alipay", txn_time=t, amount_cents=3500,
            counterparty="瑞幸咖啡", fingerprint="p_ali",
        )
        # Platform 2: WeChat
        insert_txn(
            db, txn_id=802, batch_id=2, account_id=2, fund_account_id=3,
            source="wechat", txn_time=t, amount_cents=3500,
            counterparty="瑞幸咖啡", fingerprint="p_wx",
        )
        # Bank charge with channel='tenpay' (WeChat)
        insert_txn(
            db, txn_id=803, batch_id=3, account_id=3,
            source="icbc_credit", txn_time=t, amount_cents=3500,
            counterparty="财付通-瑞幸咖啡", channel="tenpay", fingerprint="b_icbc",
        )

        matches = match_cross_source(db)
        assert len(matches) == 1
        m = matches[0]

        # Bank record 803 MUST match WeChat (802), NOT Alipay (801)
        assert m["b_id"] == 803
        assert m["p_id"] == 802
        assert m["status"] == "auto"

        # Check database records
        t_b = db.execute("SELECT duplicate_of FROM transactions WHERE id = 803").fetchone()
        t_ali = db.execute("SELECT duplicate_of FROM transactions WHERE id = 801").fetchone()
        t_wx = db.execute("SELECT duplicate_of FROM transactions WHERE id = 802").fetchone()

        assert t_b["duplicate_of"] == 802
        assert t_ali["duplicate_of"] is None
        assert t_wx["duplicate_of"] is None


# ==============================================================================
# 5. Currency Isolation
# ==============================================================================


class TestAdversarialCurrencyIsolation:
    """Stress tests verifying that foreign currency transactions are strictly isolated."""

    @pytest.mark.parametrize("p_curr,b_curr", [
        ("CNY", "USD"),
        ("USD", "CNY"),
        ("EUR", "CNY"),
        ("HKD", "USD"),
        ("GBP", "JPY"),
    ])
    def test_cross_source_currency_mismatch_hard_rejection(self, p_curr: str, b_curr: str):
        t = datetime(2026, 11, 9, 12, 0, 0)
        score = compute_match_score(
            p_amount_cents=1000,
            b_amount_cents=1000,
            p_tail="8802",
            b_tail="8802",
            p_time=t,
            b_time=t,
            b_channel="alipay",
            p_source="alipay",
            b_merchant_norm="商户",
            p_counterparty="商户",
            p_currency=p_curr,
            b_currency=b_curr,
        )
        assert score == 0.0

    def test_transfer_pairing_currency_isolation(self):
        t = "2026-11-09 12:00:00"
        out_txn = {
            "id": 1,
            "source": "wechat",
            "txn_time": t,
            "amount_cents": 10000,
            "currency": "CNY",
            "direction": "transfer",
            "counterparty": "建设银行(5678)",
            "description": "提现",
        }
        in_txn = {
            "id": 2,
            "source": "ccb",
            "txn_time": t,
            "amount_cents": 10000,
            "currency": "USD",  # Currency conflict!
            "direction": "income",
            "counterparty": "微信转入",
            "description": "入账",
        }
        score, _ = compute_transfer_score(out_txn, in_txn)
        assert score == 0.0


# ==============================================================================
# 6. Direction & Pipeline State Invariants
# ==============================================================================


class TestAdversarialStateAndPipelineInvariants:
    """Stress tests on direction integrity, idempotency, and rejection memory permanence."""

    def test_expense_vs_refund_cross_source_hard_rejection(self):
        t = datetime(2026, 11, 9, 12, 0, 0)
        # Expense vs Refund should never cross-match
        score = compute_match_score(
            p_amount_cents=2980,
            b_amount_cents=2980,
            p_tail="6666",
            b_tail="6666",
            p_time=t,
            b_time=t,
            b_channel="alipay",
            p_source="alipay",
            b_merchant_norm="商户",
            p_counterparty="商户",
            p_direction="expense",
            b_direction="refund",
        )
        assert score == 0.0

    def test_zero_and_negative_amount_hard_rejection(self):
        t = datetime(2026, 11, 9, 12, 0, 0)
        assert compute_match_score(
            p_amount_cents=0, b_amount_cents=0, p_tail="8801", b_tail="8801",
            p_time=t, b_time=t, b_channel="alipay", p_source="alipay",
            b_merchant_norm="m", p_counterparty="m",
        ) == 0.0

        assert compute_match_score(
            p_amount_cents=-100, b_amount_cents=-100, p_tail="8801", b_tail="8801",
            p_time=t, b_time=t, b_channel="alipay", p_source="alipay",
            b_merchant_norm="m", p_counterparty="m",
        ) == 0.0

    def test_pipeline_idempotency_under_multi_execution(self, db: sqlite3.Connection):
        """Executing run_dedup_pipeline multiple times consecutively must be completely idempotent."""
        create_account(db, account_id=1, kind="platform", bank="支付宝", tail=None)
        create_account(db, account_id=2, kind="bank_credit", bank="工商银行", tail="8801")

        insert_txn(
            db, txn_id=901, batch_id=1, account_id=1, fund_account_id=2,
            source="alipay", txn_time="2026-11-09 12:00:00", amount_cents=1000,
            counterparty="肯德基", fingerprint="fp_idemp_p",
        )
        insert_txn(
            db, txn_id=902, batch_id=2, account_id=2,
            source="icbc_credit", txn_time="2026-11-09 12:00:00", amount_cents=1000,
            counterparty="支付宝-肯德基", channel="alipay", fingerprint="fp_idemp_b",
        )

        # Run 1: finds 1 new duplicate and establishes match
        summary1 = run_dedup_pipeline(db)
        assert summary1.duplicates_auto == 1

        links_count_1 = db.execute("SELECT count(*) as cnt FROM match_links").fetchone()["cnt"]
        assert links_count_1 == 1

        # Runs 2, 3, 4, 5: DB is already deduped; subsequent runs find 0 new matches
        # while match_links count and duplicate_of pointer remain strictly invariant
        for _ in range(4):
            summary = run_dedup_pipeline(db)
            assert summary.duplicates_auto == 0
            assert summary.duplicates_pending == 0

        links_count_after = db.execute("SELECT count(*) as cnt FROM match_links").fetchone()["cnt"]
        assert links_count_after == 1  # No duplicate links inserted

        t902 = db.execute("SELECT duplicate_of FROM transactions WHERE id = 902").fetchone()
        assert t902["duplicate_of"] == 901

    def test_rejection_permanence_under_repeated_runs(self, db: sqlite3.Connection):
        """User rejection of a match link persists permanently across subsequent pipeline runs."""
        create_account(db, account_id=1, kind="platform", bank="支付宝", tail=None)
        create_account(db, account_id=2, kind="bank_credit", bank="工商银行", tail="8801")

        insert_txn(
            db, txn_id=911, batch_id=1, account_id=1, fund_account_id=2,
            source="alipay", txn_time="2026-11-09 12:00:00", amount_cents=2000,
            counterparty="星巴克", fingerprint="fp_rej_p",
        )
        insert_txn(
            db, txn_id=912, batch_id=2, account_id=2,
            source="icbc_credit", txn_time="2026-11-09 12:00:00", amount_cents=2000,
            counterparty="支付宝-星巴克", channel="alipay", fingerprint="fp_rej_b",
        )

        run_dedup_pipeline(db)
        link = db.execute("SELECT id FROM match_links WHERE primary_id = 911 AND secondary_id = 912").fetchone()
        assert link is not None

        # User rejects link
        reject_match_link(db, link["id"])

        # Check unlinked & blocklisted
        assert is_pair_blocked(db, 911, 912) is True
        t912 = db.execute("SELECT duplicate_of FROM transactions WHERE id = 912").fetchone()
        assert t912["duplicate_of"] is None

        # Re-run pipeline 3 times
        for _ in range(3):
            run_dedup_pipeline(db)

        # Blocklist must hold permanently
        t912_after = db.execute("SELECT duplicate_of FROM transactions WHERE id = 912").fetchone()
        assert t912_after["duplicate_of"] is None

        link_after = db.execute("SELECT status FROM match_links WHERE id = ?", (link["id"],)).fetchone()
        assert link_after["status"] == "rejected"
