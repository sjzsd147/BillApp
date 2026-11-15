"""Comprehensive Test Suite for Deduplication Pipeline (Milestone 3).

Features Covered:
- Feature 14: L3 Cross-Source Matching (Hard constraints, scoring, breakdown, greedy bijection)
- Feature 15: Merchant Normalization & Similarity
- Feature 16: L4 Internal Transfer Pairing (3 patterns, bilateral linking, neutrality)
- Feature 17: Rejection Memory, Link Confirmation, and Safe Batch Rollback
- Edge cases: Emojis, day boundaries, foreign currency isolation, empty amounts
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta
from typing import Any, Dict, Optional

import pytest

from billapp.core.db import init_db
from billapp.dedup.cross_source import (
    compute_match_score,
    compute_match_score_with_breakdown,
    compute_name_sim,
    match_cross_source,
    normalize_merchant,
)
from billapp.dedup.refund import link_in_source_refunds
from billapp.dedup.transfer import (
    compute_transfer_score,
    extract_bank_and_tail,
    pair_internal_transfers,
)
from billapp.dedup.engine import (
    DedupSummary,
    confirm_match_link,
    is_pair_blocked,
    reject_match_link,
    rollback_import_batch,
    run_dedup_pipeline,
)


@pytest.fixture
def db() -> sqlite3.Connection:
    """Fixture providing an isolated in-memory SQLite database initialized with schema."""
    conn = init_db(":memory:")
    yield conn
    conn.close()


def create_test_batch(db: sqlite3.Connection, batch_id: int = 1, source: str = "test") -> int:
    db.execute(
        """
        INSERT OR IGNORE INTO import_batches (id, file_name, file_sha1, file_size, source, imported_at, row_count)
        VALUES (?, 'test.csv', ?, 100, ?, datetime('now', 'localtime'), 1)
        """,
        (batch_id, f"sha_{batch_id}_{source}", source),
    )
    return batch_id


def create_test_account(
    db: sqlite3.Connection,
    account_id: int = 1,
    kind: str = "bank_debit",
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


def insert_test_txn(
    db: sqlite3.Connection,
    txn_id: int,
    batch_id: int = 1,
    account_id: int = 1,
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
    create_test_batch(db, batch_id=batch_id, source=source)
    create_test_account(db, account_id=account_id)
    fp = fingerprint or f"fp_{txn_id}_{source}"
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
# 1. Feature 15: Merchant Normalization & Similarity
# ==============================================================================


class TestMerchantNormalization:
    """Feature 15 unit tests."""

    def test_strip_channel_prefixes(self):
        assert normalize_merchant("支付宝-麦当劳") == "麦当劳"
        assert normalize_merchant("财付通-美宜佳") == "美宜佳"
        assert normalize_merchant("网银在线-沙县小吃") == "沙县小吃"
        assert normalize_merchant("京东支付-京东自营") == "京东自营"
        assert normalize_merchant("美团支付-美团外卖") == "美团外卖"
        assert normalize_merchant("银联-全家便利店") == "全家"
        assert normalize_merchant("云闪付-滴滴出行") == "滴滴出行"

    def test_strip_corporate_suffixes(self):
        assert normalize_merchant("上海金拱门食品有限公司") == "上海金拱门食品"
        assert normalize_merchant("中国人寿保险股份有限公司上海市分公司") == "中国人寿保险"
        assert normalize_merchant("罗森便利店") == "罗森"
        assert normalize_merchant("大润发商行") == "大润发"
        assert normalize_merchant("星巴克（中国）有限公司") == "星巴克"

    def test_strip_parentheticals(self):
        assert normalize_merchant("罗森(中心广场)") == "罗森"
        assert normalize_merchant("罗森（中心广场店）") == "罗森"
        assert normalize_merchant("喜茶(静安大悦城特惠店)") == "喜茶"

    def test_brand_preservation(self):
        assert normalize_merchant("滴滴出行") == "滴滴出行"
        assert normalize_merchant("中国移动") == "中国移动"
        assert normalize_merchant("中国银行") == "中国银行"

    def test_edge_cases(self):
        assert normalize_merchant("") == ""
        assert normalize_merchant(None) == ""
        assert normalize_merchant("   \t  ") == ""
        # Single entity suffix fallback protects against blanking
        assert normalize_merchant("有限公司") == "有限公司"
        # Noise symbols stripped
        assert normalize_merchant("***瑞幸咖啡---") == "瑞幸咖啡"
        # Emoji and special chars
        assert normalize_merchant("☕星巴克咖啡") == "☕星巴克咖啡"

    def test_compute_name_sim(self):
        assert compute_name_sim("金拱门", "金拱门") == 1.0
        assert compute_name_sim("金拱门", "麦当劳", "金拱门套餐") == 1.0
        assert compute_name_sim("星辰大学", "星辰大学") == 1.0
        assert compute_name_sim("麦当劳", "麦当劳（上海分店）") == 1.0
        assert compute_name_sim("", "麦当劳") == 0.0
        assert compute_name_sim("金拱门", None) == 0.0
        assert compute_name_sim("完全无关商户", "某某测试公司") == 0.0


# ==============================================================================
# 2. Feature 14: L3 Cross-Source Matching Engine
# ==============================================================================


class TestCrossSourceMatching:
    """Feature 14 unit tests."""

    def test_gatekeeper_amount_equality(self):
        t1 = datetime(2026, 11, 9, 12, 0, 0)
        t2 = datetime(2026, 11, 9, 12, 0, 1)
        # Even 1 cent difference must strictly reject
        score = compute_match_score(
            p_amount_cents=1000,
            b_amount_cents=1001,
            p_tail="8801",
            b_tail="8801",
            p_time=t1,
            b_time=t2,
            b_channel="alipay",
            p_source="alipay",
            b_merchant_norm="麦当劳",
            p_counterparty="麦当劳",
        )
        assert score == 0.0

    def test_gatekeeper_zero_amount(self):
        t = datetime(2026, 11, 9, 12, 0, 0)
        score = compute_match_score(
            p_amount_cents=0,
            b_amount_cents=0,
            p_tail="8801",
            b_tail="8801",
            p_time=t,
            b_time=t,
            b_channel="alipay",
            p_source="alipay",
            b_merchant_norm="test",
            p_counterparty="test",
        )
        assert score == 0.0

    def test_gatekeeper_currency_equality(self):
        t = datetime(2026, 11, 9, 12, 0, 0)
        score = compute_match_score(
            p_amount_cents=1000,
            b_amount_cents=1000,
            p_tail="8801",
            b_tail="8801",
            p_time=t,
            b_time=t,
            b_channel="alipay",
            p_source="alipay",
            b_merchant_norm="test",
            p_counterparty="test",
            p_currency="CNY",
            b_currency="USD",
        )
        assert score == 0.0

    def test_gatekeeper_tail_conflict(self):
        t = datetime(2026, 11, 9, 12, 0, 0)
        # Both tails known and distinct -> must strictly reject
        score = compute_match_score(
            p_amount_cents=1000,
            b_amount_cents=1000,
            p_tail="8801",
            b_tail="6666",
            p_time=t,
            b_time=t,
            b_channel="alipay",
            p_source="alipay",
            b_merchant_norm="test",
            p_counterparty="test",
        )
        assert score == 0.0

    def test_gatekeeper_time_feasibility_second_precision(self):
        t1 = datetime(2026, 11, 9, 12, 0, 0)
        t_30m = datetime(2026, 11, 9, 12, 30, 0)
        t_31m = datetime(2026, 11, 9, 12, 31, 0)

        s_30m = compute_match_score(
            p_amount_cents=1000, b_amount_cents=1000, p_tail="8801", b_tail="8801",
            p_time=t1, b_time=t_30m, b_channel="alipay", p_source="alipay",
            b_merchant_norm="m", p_counterparty="m", p_precision="second", b_precision="second",
        )
        s_31m = compute_match_score(
            p_amount_cents=1000, b_amount_cents=1000, p_tail="8801", b_tail="8801",
            p_time=t1, b_time=t_31m, b_channel="alipay", p_source="alipay",
            b_merchant_norm="m", p_counterparty="m", p_precision="second", b_precision="second",
        )
        assert s_30m > 0.0
        assert s_31m == 0.0  # >1800s hard excluded

    def test_scoring_formula_and_breakdown_contract(self):
        t1 = datetime(2026, 11, 9, 11, 44, 8)
        t2 = datetime(2026, 11, 9, 11, 44, 8)
        score, bd = compute_match_score_with_breakdown(
            p_amount_cents=2747,
            b_amount_cents=2747,
            p_tail="8801",
            b_tail="8801",
            p_time=t1,
            b_time=t2,
            b_channel="alipay",
            p_source="alipay",
            b_merchant_norm="金拱门",
            p_counterparty="麦当劳",
            p_description="金拱门超值套餐",
        )
        # Authoritative breakdown:
        # Base 0.30 + Tail 0.25 + Time 0.30 + Channel 0.10 + Name 0.10 = 1.05 -> clamped 1.00
        assert score == 1.0
        assert bd["amount"] == 0.30
        assert bd["tail"] == 0.25
        assert bd["time"] == 0.30
        assert bd["channel"] == 0.10
        assert bd["name"] == 0.10
        assert bd["penalty"] == 0.0

    def test_privacy_masked_counterparty_reconciliation(self):
        # Masked Alipay counterparty ab**t vs BOC bank merchant name at 17s
        t1 = datetime(2026, 10, 16, 13, 25, 48)
        t2 = datetime(2026, 10, 16, 13, 26, 5)
        score = compute_match_score(
            p_amount_cents=2980,
            b_amount_cents=2980,
            p_tail="6666",
            b_tail="6666",
            p_time=t1,
            b_time=t2,
            b_channel="alipay",
            p_source="alipay",
            b_merchant_norm="测试数码科技",
            p_counterparty="ab**t",
        )
        # 0.30 + 0.25 + 0.30 (17s) + 0.10 + 0.00 = 0.95 >= 0.85 (auto)
        assert score == 0.95

    def test_channel_conflict_penalty(self):
        t1 = datetime(2026, 11, 9, 12, 0, 0)
        t2 = datetime(2026, 11, 9, 12, 0, 1)
        score, bd = compute_match_score_with_breakdown(
            p_amount_cents=5000,
            b_amount_cents=5000,
            p_tail="8801",
            b_tail="8801",
            p_time=t1,
            b_time=t2,
            b_channel="tenpay",  # Conflict! Bank says tenpay, platform is alipay
            p_source="alipay",
            b_merchant_norm="商户",
            p_counterparty="商户",
        )
        assert bd["channel"] == 0.0
        assert bd["penalty"] == -0.25
        # 0.30 + 0.25 + 0.30 + 0.0 + 0.10 - 0.25 = 0.70 < 0.85 (pending review)
        assert score == 0.70

    def test_greedy_bipartite_allocation_bijection(self, db: sqlite3.Connection):
        create_test_batch(db, batch_id=1, source="alipay")
        create_test_batch(db, batch_id=2, source="icbc_credit")
        create_test_account(db, account_id=1, kind="bank_credit", bank="工商银行", tail="8801")
        create_test_account(db, account_id=2, kind="platform", bank="支付宝", tail=None)

        t0 = "2026-11-09 12:00:00"
        t_1m = "2026-11-09 12:01:00"
        t_5m = "2026-11-09 12:05:00"

        insert_test_txn(
            db, txn_id=101, batch_id=1, account_id=2, fund_account_id=1,
            source="alipay", txn_time=t0, amount_cents=1560,
            counterparty="麦当劳", description="金拱门", fingerprint="fp_p1",
        )
        insert_test_txn(
            db, txn_id=102, batch_id=1, account_id=2, fund_account_id=1,
            source="alipay", txn_time=t_1m, amount_cents=1560,
            counterparty="麦当劳", description="金拱门", fingerprint="fp_p2",
        )
        insert_test_txn(
            db, txn_id=201, batch_id=2, account_id=1,
            source="icbc_credit", txn_time=t0, amount_cents=1560,
            counterparty="支付宝-麦当劳", description="消费", channel="alipay", fingerprint="fp_b1",
        )
        insert_test_txn(
            db, txn_id=202, batch_id=2, account_id=1,
            source="icbc_credit", txn_time=t_5m, amount_cents=1560,
            counterparty="支付宝-麦当劳", description="消费", channel="alipay", fingerprint="fp_b2",
        )

        matches = match_cross_source(db)
        assert len(matches) == 2

        # Check strict bijection
        matched_p = {m["p_id"] for m in matches}
        matched_b = {m["b_id"] for m in matches}
        assert matched_p == {101, 102}
        assert matched_b == {201, 202}

        # Check bank records have duplicate_of pointing to platform records
        row_b1 = db.execute("SELECT duplicate_of FROM transactions WHERE id = 201").fetchone()
        row_b2 = db.execute("SELECT duplicate_of FROM transactions WHERE id = 202").fetchone()
        assert row_b1["duplicate_of"] == 101
        assert row_b2["duplicate_of"] == 102


# ==============================================================================
# 3. Feature 16: L4 Internal Transfer Pairing
# ==============================================================================


class TestInternalTransferPairing:
    """Feature 16 unit tests."""

    def test_extract_bank_and_tail(self):
        b, t = extract_bank_and_tail("建设银行(5678)")
        assert b == "建设银行"
        assert t == "5678"

        b, t = extract_bank_and_tail("招商银行储蓄卡(1234)")
        assert b == "招商银行"
        assert t == "1234"

        b, t = extract_bank_and_tail("中国银行股份有限公司")
        assert b == "中国银行"
        assert t is None

        b, t = extract_bank_and_tail("")
        assert b is None and t is None

    def test_pattern_a_wechat_wallet_withdrawal_to_bank(self, db: sqlite3.Connection):
        create_test_account(db, account_id=1, kind="wallet", bank="微信支付", tail=None, holder="张三")
        create_test_account(db, account_id=2, kind="bank_debit", bank="建设银行", tail="5678", holder="张三")

        # WeChat withdrawal: 100.00
        insert_test_txn(
            db, txn_id=10, batch_id=1, account_id=1, source="wechat",
            txn_time="2026-11-08 14:29:13", amount_cents=10000, direction="transfer",
            counterparty="建设银行(5678)", description="经营账户提现", fingerprint="fp_w1",
        )
        # CCB card deposit: 100.00 arriving 5 minutes later
        insert_test_txn(
            db, txn_id=11, batch_id=2, account_id=2, source="boc_debit",
            txn_time="2026-11-08 14:34:13", amount_cents=10000, direction="income",
            counterparty="微信转入", description="银联入账", fingerprint="fp_b1",
        )

        matches = pair_internal_transfers(db)
        assert len(matches) == 1
        m = matches[0]
        assert m["out_id"] == 10
        assert m["in_id"] == 11
        assert m["status"] == "auto"
        assert m["score"] >= 0.85

        # Invariant: Bilateral transfer_peer and direction = 'transfer'
        t10 = db.execute("SELECT direction, transfer_peer FROM transactions WHERE id = 10").fetchone()
        t11 = db.execute("SELECT direction, transfer_peer FROM transactions WHERE id = 11").fetchone()
        assert t10["transfer_peer"] == 11
        assert t11["transfer_peer"] == 10
        assert t10["direction"] == "transfer"
        assert t11["direction"] == "transfer"

    def test_pattern_b_credit_card_repayment(self, db: sqlite3.Connection):
        # Debit Card (outflow) -> ICBC Credit Card (inflow)
        create_test_account(db, account_id=1, kind="bank_debit", bank="招商银行", tail="1234", holder="张三")
        create_test_account(db, account_id=2, kind="bank_credit", bank="工商银行", tail="8801", holder="张三")

        # Debit payment
        insert_test_txn(
            db, txn_id=20, batch_id=1, account_id=1, source="bank_csv",
            txn_time="2026-10-28 16:20:00", amount_cents=10000, direction="expense",
            counterparty="工商银行信用卡", description="信用卡还款", fingerprint="fp_d1",
        )
        # Credit card deposit (from ICBC sample)
        insert_test_txn(
            db, txn_id=21, batch_id=2, account_id=2, source="icbc_credit",
            txn_time="2026-10-28 16:21:33", amount_cents=10000, direction="transfer",
            counterparty="张三", description="转帐", fingerprint="fp_c1",
        )

        matches = pair_internal_transfers(db)
        assert len(matches) == 1
        assert matches[0]["status"] == "auto"
        assert matches[0]["score"] >= 0.85

        t20 = db.execute("SELECT direction, transfer_peer FROM transactions WHERE id = 20").fetchone()
        t21 = db.execute("SELECT direction, transfer_peer FROM transactions WHERE id = 21").fetchone()
        assert t20["transfer_peer"] == 21
        assert t21["transfer_peer"] == 20
        assert t20["direction"] == "transfer"
        assert t21["direction"] == "transfer"

    def test_pattern_c_interbank_self_transfer(self, db: sqlite3.Connection):
        # CMB Debit (outflow) -> BOC Debit (inflow)
        create_test_account(db, account_id=1, kind="bank_debit", bank="招商银行", tail="1234", holder="张三")
        create_test_account(db, account_id=2, kind="bank_debit", bank="中国银行", tail="6666", holder="张三")

        # Outflow from CMB
        insert_test_txn(
            db, txn_id=30, batch_id=1, account_id=1, source="cmb_debit",
            txn_time="2026-10-13 21:28:00", amount_cents=100000, direction="expense",
            counterparty="张三", description="跨行转账到中国银行", fingerprint="fp_cmb",
        )
        # Inflow to BOC (from real BOC sample row 6)
        insert_test_txn(
            db, txn_id=31, batch_id=2, account_id=2, source="boc_debit",
            txn_time="2026-10-13 21:28:40", amount_cents=100000, direction="transfer",
            counterparty="张三", description="跨行转账 6214000000001234 招商银行", fingerprint="fp_boc",
        )

        matches = pair_internal_transfers(db)
        assert len(matches) == 1
        assert matches[0]["status"] == "auto"
        assert matches[0]["score"] >= 0.85

    def test_transfers_excluded_from_expense_statistics(self, db: sqlite3.Connection):
        create_test_account(db, account_id=1, kind="wallet", bank="微信支付", tail=None, holder="张三")
        create_test_account(db, account_id=2, kind="bank_debit", bank="建设银行", tail="5678", holder="张三")

        insert_test_txn(
            db, txn_id=40, batch_id=1, account_id=1, source="wechat",
            txn_time="2026-11-08 14:29:13", amount_cents=10000, direction="transfer",
            counterparty="建行", description="提现", fingerprint="fp_t1",
        )
        insert_test_txn(
            db, txn_id=41, batch_id=2, account_id=2, source="bank",
            txn_time="2026-11-08 14:34:13", amount_cents=10000, direction="transfer",
            counterparty="微信", description="入账", transfer_peer=40, fingerprint="fp_t2",
        )
        db.execute("UPDATE transactions SET transfer_peer = 41 WHERE id = 40")

        # Standard net expense query invariant
        res = db.execute(
            """
            SELECT COALESCE(SUM(amount_cents - refunded_cents), 0) AS net_expense_cents
            FROM transactions
            WHERE direction = 'expense'
              AND duplicate_of IS NULL
              AND excluded = 0
              AND currency = 'CNY'
            """
        ).fetchone()
        assert res["net_expense_cents"] == 0


# ==============================================================================
# 4. Feature 17: Rejection Memory, Confirmation, & Batch Rollback
# ==============================================================================


class TestRejectionMemoryAndEngine:
    """Feature 17 unit tests."""

    def test_rejection_inserts_canonical_blocklist_and_unlinks(self, db: sqlite3.Connection):
        insert_test_txn(
            db, txn_id=50, batch_id=1, account_id=1, source="alipay",
            txn_time="2026-11-09 12:00:00", amount_cents=5000, fingerprint="fp50",
        )
        insert_test_txn(
            db, txn_id=51, batch_id=2, account_id=1, source="icbc_credit",
            txn_time="2026-11-09 12:00:01", amount_cents=5000, duplicate_of=50, fingerprint="fp51",
        )
        db.execute(
            """
            INSERT INTO match_links (id, kind, primary_id, secondary_id, score, features_json, status)
            VALUES (1, 'duplicate', 50, 51, 0.88, '{}', 'auto')
            """
        )

        # User rejects link
        success = reject_match_link(db, 1)
        assert success is True

        # Check match_blocklist has canonical min/max: a_id=50, b_id=51
        block_row = db.execute("SELECT * FROM match_blocklist WHERE kind = 'duplicate'").fetchone()
        assert block_row is not None
        assert block_row["a_id"] == 50
        assert block_row["b_id"] == 51

        # Check secondary duplicate_of is unlinked
        t51 = db.execute("SELECT duplicate_of FROM transactions WHERE id = 51").fetchone()
        assert t51["duplicate_of"] is None

        # Check link status is rejected
        link = db.execute("SELECT status FROM match_links WHERE id = 1").fetchone()
        assert link["status"] == "rejected"

        # Check is_pair_blocked
        assert is_pair_blocked(db, 50, 51, "duplicate") is True
        assert is_pair_blocked(db, 51, 50, "duplicate") is True

    def test_rejection_memory_permanence_on_dedup_rerun(self, db: sqlite3.Connection):
        insert_test_txn(
            db, txn_id=60, batch_id=1, account_id=1, source="alipay",
            txn_time="2026-11-09 12:00:00", amount_cents=2000, counterparty="测试商户",
            description="说明", fingerprint="fp60",
        )
        insert_test_txn(
            db, txn_id=61, batch_id=2, account_id=1, source="icbc_credit",
            txn_time="2026-11-09 12:00:00", amount_cents=2000, counterparty="测试商户",
            description="说明", fingerprint="fp61",
        )

        # Insert blocked pair (60, 61) now that transactions exist
        db.execute("INSERT INTO match_blocklist (a_id, b_id, kind) VALUES (60, 61, 'duplicate')")

        # Re-run cross source matching
        matches = match_cross_source(db)
        # Blocklisted pair must NEVER match
        assert not any(m["p_id"] == 60 and m["b_id"] == 61 for m in matches)
        t61 = db.execute("SELECT duplicate_of FROM transactions WHERE id = 61").fetchone()
        assert t61["duplicate_of"] is None

    def test_confirm_match_link(self, db: sqlite3.Connection):
        insert_test_txn(
            db, txn_id=70, batch_id=1, account_id=1, source="alipay",
            txn_time="2026-11-09 12:00:00", amount_cents=3000, fingerprint="fp70",
        )
        insert_test_txn(
            db, txn_id=71, batch_id=2, account_id=1, source="icbc_credit",
            txn_time="2026-11-09 12:00:00", amount_cents=3000, fingerprint="fp71",
        )
        db.execute(
            """
            INSERT INTO match_links (id, kind, primary_id, secondary_id, score, features_json, status)
            VALUES (2, 'duplicate', 70, 71, 0.75, '{}', 'pending')
            """
        )

        success = confirm_match_link(db, 2)
        assert success is True

        link = db.execute("SELECT status FROM match_links WHERE id = 2").fetchone()
        assert link["status"] == "confirmed"

        t71 = db.execute("SELECT duplicate_of FROM transactions WHERE id = 71").fetchone()
        assert t71["duplicate_of"] == 70

    def test_run_dedup_pipeline_coordinator(self, db: sqlite3.Connection):
        create_test_account(db, account_id=1, kind="bank_credit", bank="工商银行", tail="8801")
        create_test_account(db, account_id=2, kind="platform", bank="支付宝", tail=None)

        # Insert platform expense
        insert_test_txn(
            db, txn_id=80, batch_id=1, account_id=2, fund_account_id=1,
            source="alipay", txn_time="2026-11-09 11:44:08", amount_cents=2747,
            counterparty="麦当劳", description="金拱门", fingerprint="fp80",
        )
        # Insert bank expense
        insert_test_txn(
            db, txn_id=81, batch_id=2, account_id=1,
            source="icbc_credit", txn_time="2026-11-09 11:44:08", amount_cents=2747,
            counterparty="支付宝-麦当劳", description="金拱门", channel="alipay", fingerprint="fp81",
        )

        summary = run_dedup_pipeline(db)
        assert isinstance(summary, DedupSummary)
        assert summary.duplicates_auto >= 1

        # Check bank record is automatically marked duplicate_of
        t81 = db.execute("SELECT duplicate_of FROM transactions WHERE id = 81").fetchone()
        assert t81["duplicate_of"] == 80

    def test_safe_batch_rollback(self, db: sqlite3.Connection):
        # In Batch 1: Surviving purchase (ID 90, 100.00, refunded 29.80)
        # Surviving platform purchase (ID 91, 50.00)
        insert_test_txn(
            db, txn_id=90, batch_id=1, account_id=1, source="alipay",
            txn_time="2026-10-16 13:00:00", amount_cents=10000, refunded_cents=2980, fingerprint="fp90",
        )
        insert_test_txn(
            db, txn_id=91, batch_id=1, account_id=1, source="alipay",
            txn_time="2026-10-16 14:00:00", amount_cents=5000, fingerprint="fp91",
        )

        # In Batch 2 (to be rolled back):
        # Refund (ID 92) that credited txn 90 with 29.80
        # Duplicate bank record (ID 93) pointing to txn 91
        insert_test_txn(
            db, txn_id=92, batch_id=2, account_id=1, source="boc_debit",
            txn_time="2026-10-16 13:26:00", amount_cents=2980, direction="refund",
            refund_of=90, fingerprint="fp92",
        )
        insert_test_txn(
            db, txn_id=93, batch_id=2, account_id=1, source="boc_debit",
            txn_time="2026-10-16 14:00:00", amount_cents=5000, direction="expense",
            duplicate_of=91, fingerprint="fp93",
        )

        # Execute rollback on batch 2
        res = rollback_import_batch(db, batch_id=2)
        assert res["deleted_transactions"] == 2
        assert res["status"] == "rolled_back"

        # Verification 1: Batch 2 is deleted
        b2 = db.execute("SELECT * FROM import_batches WHERE id = 2").fetchone()
        assert b2 is None

        # Verification 2: Surviving transaction 90 had its refunded_cents restored from 2980 -> 0
        t90 = db.execute("SELECT refunded_cents FROM transactions WHERE id = 90").fetchone()
        assert t90["refunded_cents"] == 0

        # Verification 3: Batch 2 transactions are deleted
        txns_b2 = db.execute("SELECT * FROM transactions WHERE batch_id = 2").fetchall()
        assert len(txns_b2) == 0


# ==============================================================================
# 5. Boundary & Edge Cases
# ==============================================================================


class TestDedupEdgeCases:
    """Corner and boundary edge cases."""

    def test_leap_year_and_day_boundaries(self):
        t1 = datetime(2024, 2, 28, 23, 59, 59)
        t2 = datetime(2024, 2, 29, 0, 0, 1)
        # 2 seconds diff across leap year day boundary
        score = compute_match_score(
            p_amount_cents=1000, b_amount_cents=1000, p_tail="8801", b_tail="8801",
            p_time=t1, b_time=t2, b_channel="alipay", p_source="alipay",
            b_merchant_norm="m", p_counterparty="m",
        )
        assert score >= 0.85

    def test_unicode_and_emojis(self):
        t = datetime(2026, 11, 9, 12, 0, 0)
        norm = normalize_merchant("☕ 瑞幸咖啡（国贸特约店）🎉")
        assert "瑞幸咖啡" in norm

        score = compute_match_score(
            p_amount_cents=1500, b_amount_cents=1500, p_tail=None, b_tail=None,
            p_time=t, b_time=t, b_channel=None, p_source="wechat",
            b_merchant_norm="瑞幸咖啡", p_counterparty="☕ 瑞幸咖啡（国贸店）",
        )
        assert score >= 0.40  # base + time + name

    def test_usd_currency_isolation(self, db: sqlite3.Connection):
        # Platform has CNY txn 1497 cents (¥14.97)
        # Bank has USD rebate 1497 cents ($14.97 USD)
        insert_test_txn(
            db, txn_id=95, batch_id=1, account_id=1, source="alipay",
            txn_time="2026-10-09 22:57:06", amount_cents=1497, currency="CNY", fingerprint="fp95",
        )
        insert_test_txn(
            db, txn_id=96, batch_id=2, account_id=1, source="icbc_credit",
            txn_time="2026-10-09 22:57:06", amount_cents=1497, currency="USD", fingerprint="fp96",
        )

        matches = match_cross_source(db)
        # Currency mismatch must never match
        assert not any(m["p_id"] == 95 and m["b_id"] == 96 for m in matches)


# ==============================================================================
# 6. Milestone 3 Remediation Test Suite
# ==============================================================================


class TestDedupRemediation:
    """Remediation verification for temporal causality, dynamic cardholder, rollback reversion, and bank refunds."""

    def test_temporal_causality_rejection(self, db: sqlite3.Connection):
        """Negative deltas beyond clock skew margin (-600s) must be rejected for second precision."""
        create_test_account(db, account_id=1, kind="bank_debit", bank="招商银行", tail="1234", holder="张三")
        create_test_account(db, account_id=2, kind="bank_debit", bank="中国银行", tail="6666", holder="张三")

        # Case 1: Inflow arriving 12 hours BEFORE outflow (-43200s, second precision)
        insert_test_txn(
            db, txn_id=801, batch_id=1, account_id=1, source="cmb_debit",
            txn_time="2026-11-09 20:00:00", time_precision="second",
            amount_cents=5000, direction="expense", counterparty="中国银行(6666)",
            description="跨行转账", fingerprint="fp_801",
        )
        insert_test_txn(
            db, txn_id=802, batch_id=2, account_id=2, source="boc_debit",
            txn_time="2026-11-09 08:00:00", time_precision="second",
            amount_cents=5000, direction="income", counterparty="张三",
            description="跨行转入", fingerprint="fp_802",
        )

        matches = pair_internal_transfers(db)
        matched_pairs = {(m["out_id"], m["in_id"]) for m in matches}
        assert (801, 802) not in matched_pairs, "Inflow arriving 12 hours before outflow must NOT match"

        # Direct score check should also return 0.0
        t801 = dict(db.execute("SELECT * FROM transactions WHERE id = 801").fetchone())
        t802 = dict(db.execute("SELECT * FROM transactions WHERE id = 802").fetchone())
        score, _ = compute_transfer_score(t801, t802)
        assert score == 0.0

    def test_dynamic_cardholder_resolution_without_hardcoded_fallback(self, db: sqlite3.Connection):
        """Cardholder identity resolves dynamically without hardcoded fallback."""
        # Clean accounts with holder=None, no settings
        create_test_account(db, account_id=1, kind="bank_debit", bank="招商银行", tail="1234", holder=None)
        create_test_account(db, account_id=2, kind="bank_debit", bank="中国银行", tail="6666", holder=None)

        # Outflow and inflow where counterparty mentions '张三'
        insert_test_txn(
            db, txn_id=811, batch_id=1, account_id=1, source="cmb_debit",
            txn_time="2026-11-09 10:00:00", time_precision="second",
            amount_cents=10000, direction="expense", counterparty="张三",
            description="转账", fingerprint="fp_811",
        )
        insert_test_txn(
            db, txn_id=812, batch_id=2, account_id=2, source="boc_debit",
            txn_time="2026-11-09 10:05:00", time_precision="second",
            amount_cents=10000, direction="income", counterparty="转入",
            description="手机银行转账", fingerprint="fp_812",
        )

        # Without accounts or settings holder, holder_name resolves to None -> score = 0.80 (< 0.85 auto_threshold)
        matches = pair_internal_transfers(db)
        assert len(matches) == 1
        assert matches[0]["status"] == "pending"
        assert matches[0]["score"] == 0.80

        # Now configure settings: user_name = '张三'
        db.execute("INSERT INTO settings (key, value) VALUES ('user_name', '\"张三\"')")
        # Reset match links
        db.execute("DELETE FROM match_links")
        matches_configured = pair_internal_transfers(db)
        assert len(matches_configured) == 1
        assert matches_configured[0]["status"] == "auto"
        assert matches_configured[0]["score"] == 1.00

    def test_batch_rollback_restores_transfer_direction(self, db: sqlite3.Connection):
        """Batch rollback restores original direction of surviving transfer peer."""
        create_test_account(db, account_id=1, kind="bank_debit", bank="招商银行", tail="1234", holder="张三")
        create_test_account(db, account_id=2, kind="bank_debit", bank="中国银行", tail="6666", holder="张三")

        # Batch 1: Outflow with original direction 'expense'
        insert_test_txn(
            db, txn_id=821, batch_id=1, account_id=1, source="cmb_debit",
            txn_time="2026-11-09 10:00:00", amount_cents=15000, direction="expense",
            counterparty="中国银行(6666)", description="跨行转账", fingerprint="fp_821",
        )
        # Batch 2: Inflow with original direction 'income'
        insert_test_txn(
            db, txn_id=822, batch_id=2, account_id=2, source="boc_debit",
            txn_time="2026-11-09 10:02:00", amount_cents=15000, direction="income",
            counterparty="招商银行(1234)", description="跨行转入", fingerprint="fp_822",
        )

        # Run transfer pairing
        matches = pair_internal_transfers(db)
        assert len(matches) == 1
        assert matches[0]["status"] == "auto"

        t821 = db.execute("SELECT direction, transfer_peer FROM transactions WHERE id = 821").fetchone()
        t822 = db.execute("SELECT direction, transfer_peer FROM transactions WHERE id = 822").fetchone()
        assert t821["direction"] == "transfer" and t821["transfer_peer"] == 822
        assert t822["direction"] == "transfer" and t822["transfer_peer"] == 821

        # Roll back Batch 2
        rollback_import_batch(db, batch_id=2)

        # Transaction 822 should be deleted
        assert db.execute("SELECT * FROM transactions WHERE id = 822").fetchone() is None

        # Surviving Transaction 821 must have transfer_peer = NULL and direction restored to 'expense'
        t821_after = db.execute("SELECT direction, transfer_peer FROM transactions WHERE id = 821").fetchone()
        assert t821_after["transfer_peer"] is None
        assert t821_after["direction"] == "expense"

    def test_bank_in_source_refund_linking(self, db: sqlite3.Connection):
        """Bank statements link in-source refunds to original purchases within 60 days."""
        create_test_account(db, account_id=1, kind="bank_debit", bank="中国银行", tail="6666", holder="张三")

        # Bank debit purchase: 29.80 (2980 cents)
        insert_test_txn(
            db, txn_id=831, batch_id=1, account_id=1, source="boc_debit",
            txn_time="2026-10-16 13:26:05", amount_cents=2980, direction="expense",
            counterparty="支付宝-测试数码科技有限公司", description="网上快捷支付", fingerprint="fp_831",
        )
        # Bank debit refund: 29.80 (2980 cents), 17 seconds later
        insert_test_txn(
            db, txn_id=832, batch_id=1, account_id=1, source="boc_debit",
            txn_time="2026-10-16 13:26:22", amount_cents=2980, direction="refund",
            counterparty="支付宝-测试数码科技有限公司", description="网上快捷退款", fingerprint="fp_832",
        )

        linked = link_in_source_refunds(db, batch_id=1)
        assert linked == 1

        t832 = db.execute("SELECT refund_of FROM transactions WHERE id = 832").fetchone()
        t831 = db.execute("SELECT refunded_cents, amount_cents FROM transactions WHERE id = 831").fetchone()
        assert t832["refund_of"] == 831
        assert t831["refunded_cents"] == 2980
        # Net expense should be 0
        assert t831["amount_cents"] - t831["refunded_cents"] == 0

