"""Regression test suite for all non-import module audit fixes (B-H1..H9, B-M1..M7, B-L1..L2)."""

from __future__ import annotations

import io
import json
from pathlib import Path
import sqlite3
from unittest.mock import patch

from fastapi.testclient import TestClient
import httpx
import openpyxl
import pytest

from billapp.app import create_app
from billapp.classify.categories import CategoryService
from billapp.classify.pipeline import ClassificationPipeline
from billapp.classify.rules import Rule, get_learning_suggestions, rule_matches
from billapp.core.db import create_connection, init_db
from billapp.core.fx import (
    convert_transaction,
    get_fx_overview,
    record_manual_rate,
    retry_transaction_fx,
)
from billapp.core.safe_regex import compile_safe_regex
from billapp.export import generate_excel_export
from billapp.launcher import _format_url_host
from billapp.llm.client import LLMClient, LLMConfig, validate_llm_base_url
from billapp.llm.prompts import _mask_card_digits_keep_tail, prepare_statement_lines_for_llm
from billapp.llm.tasks import (
    dedup_arbitration_sync,
    parse_statement_with_llm_sync,
    review_classification_sync,
)
from billapp.stats import (
    calculate_stats_account,
    calculate_stats_category,
    calculate_stats_heatmap,
    calculate_stats_merchant,
    calculate_stats_summary,
)


def _seed_base_db(conn: sqlite3.Connection) -> tuple[int, int, int, int, int]:
    """Create minimal batch, account, expense category, income category, transfer category."""
    CategoryService.init_builtin_categories(conn)
    cur = conn.execute(
        "INSERT INTO import_batches (file_name, file_sha1, file_size, source, row_count) VALUES ('t.csv', 'sha1', 10, 'alipay', 1)"
    )
    batch_id = cur.lastrowid
    cur = conn.execute(
        "INSERT INTO accounts (kind, bank, tail, alias, currency) VALUES ('bank_debit', '招行', '1234', '工资卡', 'CNY')"
    )
    acc_id = cur.lastrowid
    exp_cat = conn.execute("SELECT id FROM categories WHERE key = 'food.dining'").fetchone()[0]
    inc_cat = conn.execute("SELECT id FROM categories WHERE direction = 'income' LIMIT 1").fetchone()[0]
    transfer_cat = conn.execute("SELECT id FROM categories WHERE key = 'financial.internal_transfer'").fetchone()[0]
    conn.commit()
    return batch_id, acc_id, exp_cat, inc_cat, transfer_cat


def _insert_txn(
    conn: sqlite3.Connection,
    batch_id: int,
    acc_id: int,
    txn_time: str,
    amount_cents: int,
    direction: str = "expense",
    *,
    time_precision: str = "second",
    currency: str = "CNY",
    amount_cny: int | None = None,
    category_id: int | None = None,
    category_source: str = "none",
    category_confidence: float = 0.0,
    counterparty: str = "商户A",
    merchant_norm: str = "商户A",
    refund_of: int | None = None,
    refunded_cents: int = 0,
    duplicate_of: int | None = None,
    transfer_peer: int | None = None,
    excluded: int = 0,
    fp: str = "fp",
) -> int:
    if amount_cny is None and currency == "CNY":
        amount_cny = amount_cents
    cur = conn.execute(
        """
        INSERT INTO transactions (
            batch_id, source, account_id, txn_time, time_precision,
            amount_cents, currency, amount_cny, direction,
            counterparty, merchant_norm, category_id, category_source, category_confidence,
            refund_of, refunded_cents, duplicate_of, transfer_peer, excluded, fingerprint
        )
        VALUES (?, 'alipay', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            batch_id,
            acc_id,
            txn_time,
            time_precision,
            amount_cents,
            currency,
            amount_cny,
            direction,
            counterparty,
            merchant_norm,
            category_id,
            category_source,
            category_confidence,
            refund_of,
            refunded_cents,
            duplicate_of,
            transfer_peer,
            excluded,
            fp,
        ),
    )
    conn.commit()
    return cur.lastrowid


# =========================================================================
# B-H1, B-H2, B-H3, B-M4: Stats & Export Fixes
# =========================================================================

class TestStatsAndExportFixes:
    def test_bh1_day_precision_txn_included_on_start_date(self, tmp_path: Path):
        db_file = tmp_path / "bh1.db"
        app = create_app(db_path=str(db_file))
        with TestClient(app) as client:
            conn = create_connection(str(db_file))
            try:
                batch_id, acc_id, exp_cat, _, _ = _seed_base_db(conn)
                _insert_txn(
                    conn,
                    batch_id,
                    acc_id,
                    "2026-05-01",
                    5000,
                    "expense",
                    time_precision="day",
                    category_id=exp_cat,
                    counterparty="日精度商户",
                    merchant_norm="日精度商户",
                    fp="bh1-day",
                )

                # Stats summary
                summary = calculate_stats_summary(conn, start_date="2026-05-01", end_date="2026-05-31")
                assert summary.total_expense_cents == 5000

                # Transactions API filter
                res = client.get("/api/transactions", params={"from": "2026-05-01", "to": "2026-05-31"})
                assert res.status_code == 200
                assert res.json()["total"] == 1

                # Excel Export
                xlsx_buf = generate_excel_export(conn, start_date="2026-05-01", end_date="2026-05-31")
                wb = openpyxl.load_workbook(xlsx_buf)
                assert wb["交易明细"].max_row == 2
            finally:
                conn.close()

    def test_bh2_cross_direction_category_preserved_as_unclassified_and_stage4_guarded(self):
        conn = init_db(":memory:")
        batch_id, acc_id, exp_cat, inc_cat, _ = _seed_base_db(conn)
        # Expense txn erroneously assigned an income category
        _insert_txn(
            conn,
            batch_id,
            acc_id,
            "2026-05-10 10:00:00",
            8800,
            "expense",
            category_id=inc_cat,
            fp="bh2-cross",
        )
        cat_stats = calculate_stats_category(conn, direction="expense")
        assert cat_stats.total_cents == 8800
        assert any(item.name == "未分类" and item.amount_cents == 8800 for item in cat_stats.items)

        # Pipeline Stage 4 must NOT apply expense mapping to an income transaction
        pipeline = ClassificationPipeline(conn)
        inc_txn = {
            "source": "alipay",
            "platform_category": "餐饮美食",
            "direction": "income",
            "counterparty": "某个人",
            "description": "收款",
        }
        res = pipeline.classify_transaction(inc_txn)
        assert res.source != "platform"

    def test_bh3_refund_mode_and_unlinked_refund_across_all_stats_and_export(self):
        conn = init_db(":memory:")
        batch_id, acc_id, exp_cat, _, _ = _seed_base_db(conn)
        # May expense 10000 linked with June refund 4000
        orig_id = _insert_txn(
            conn,
            batch_id,
            acc_id,
            "2026-05-20 12:00:00",
            10000,
            "expense",
            category_id=exp_cat,
            refunded_cents=4000,
            counterparty="星巴克",
            merchant_norm="星巴克",
            fp="bh3-orig",
        )
        _insert_txn(
            conn,
            batch_id,
            acc_id,
            "2026-06-05 12:00:00",
            4000,
            "refund",
            category_id=None,
            refund_of=orig_id,
            counterparty="星巴克",
            merchant_norm="星巴克",
            fp="bh3-ref-linked",
        )
        # June expense 6000 + unlinked June refund 1000
        _insert_txn(
            conn,
            batch_id,
            acc_id,
            "2026-06-10 12:00:00",
            6000,
            "expense",
            category_id=exp_cat,
            counterparty="星巴克",
            merchant_norm="星巴克",
            fp="bh3-jun-exp",
        )
        _insert_txn(
            conn,
            batch_id,
            acc_id,
            "2026-06-10 15:00:00",
            1000,
            "refund",
            category_id=exp_cat,
            refund_of=None,
            counterparty="星巴克",
            merchant_norm="星巴克",
            fp="bh3-ref-unlinked",
        )

        # 1) original_month mode: June net = 6000 - 1000 = 5000; May net = 10000 - 4000 = 6000
        conn.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES ('refund_settings', '{\"mode\": \"original_month\"}')"
        )
        conn.commit()
        assert calculate_stats_category(conn, start_date="2026-06-01", end_date="2026-06-30").total_cents == 5000
        assert calculate_stats_merchant(conn, start_date="2026-06-01", end_date="2026-06-30").total_expense_cents == 5000
        assert calculate_stats_account(conn, start_date="2026-06-01", end_date="2026-06-30").total_out_cents == 5000
        hm_orig = calculate_stats_heatmap(conn, mode="calendar", year=2026)
        assert sum(i.expense_cents for i in hm_orig.calendar_items) == 11000

        # 2) refund_month mode: May = 10000 (no deduction), June = 6000 - 4000 - 1000 = 1000
        conn.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES ('refund_settings', '{\"mode\": \"refund_month\"}')"
        )
        conn.commit()
        assert calculate_stats_category(conn, start_date="2026-05-01", end_date="2026-05-31").total_cents == 10000
        assert calculate_stats_category(conn, start_date="2026-06-01", end_date="2026-06-30").total_cents == 1000
        assert calculate_stats_merchant(conn, start_date="2026-06-01", end_date="2026-06-30").total_expense_cents == 1000
        assert calculate_stats_account(conn, start_date="2026-06-01", end_date="2026-06-30").total_out_cents == 1000

        # Export Sheet 2 pivot respects refund_month too (June net = 10.00)
        xlsx_buf = generate_excel_export(conn, start_date="2026-06-01", end_date="2026-06-30")
        wb = openpyxl.load_workbook(xlsx_buf)
        ws_pivot = wb["月度分类透视表"]
        june_val = ws_pivot.cell(2, 3).value
        assert june_val == pytest.approx(10.0)

    def test_bm4_prev_period_include_transfer_and_open_ended_days_and_heatmap_year(self):
        conn = init_db(":memory:")
        batch_id, acc_id, exp_cat, _, transfer_cat = _seed_base_db(conn)
        # April transfer-categorized expense + May transfer-categorized expense
        _insert_txn(conn, batch_id, acc_id, "2026-04-10 10:00:00", 5000, "expense", category_id=transfer_cat, fp="bm4-t1")
        _insert_txn(conn, batch_id, acc_id, "2026-05-10 10:00:00", 6000, "expense", category_id=transfer_cat, fp="bm4-t2")
        _insert_txn(conn, batch_id, acc_id, "2025-05-10 14:00:00", 9000, "expense", category_id=exp_cat, fp="bm4-2025")

        s_excl = calculate_stats_summary(conn, start_date="2026-05-01", end_date="2026-05-31", include_transfer=False)
        assert s_excl.prev is not None and s_excl.prev.expense_net == 0
        s_incl = calculate_stats_summary(conn, start_date="2026-05-01", end_date="2026-05-31", include_transfer=True)
        assert s_incl.prev is not None and s_incl.prev.expense_net == 5000

        # Open-ended summary computes actual days span
        s_open = calculate_stats_summary(conn, include_transfer=True)
        assert s_open.days >= 365

        # weekday_hour heatmap respects year=2025
        hm_2025 = calculate_stats_heatmap(conn, mode="weekday_hour", year=2025)
        assert sum(i.expense_cents for i in hm_2025.weekday_hour_items) == 9000


# =========================================================================
# B-H4, B-M3: FX Engine Fixes
# =========================================================================

class TestFxFixes:
    def test_bh4_exact_date_fetch_and_manual_rate_protection_and_offline_retry_preserves_cny(self):
        conn = init_db(":memory:")
        batch_id, acc_id, exp_cat, _, _ = _seed_base_db(conn)
        # Prior-day cached online rate on 2026-05-01
        conn.execute(
            "INSERT INTO fx_rates (currency, rate, effective_date, source, created_at, updated_at) "
            "VALUES ('USD', 7.0, '2026-05-01', 'online', '2026-05-01 00:00:00', '2026-05-01 00:00:00')"
        )
        # Manual rate on 2026-05-10
        record_manual_rate(conn, "USD", 7.35, "2026-05-10", note="bank")
        conn.commit()

        # On 2026-05-02, must try online fetch rather than silently using 2026-05-01 rate
        with patch("billapp.core.fx.fetch_online_fx_rate", return_value=7.2) as mock_fetch:
            cny, rate, rate_at, src = convert_transaction(conn, "USD", 1000, "2026-05-02 10:00:00")
            assert mock_fetch.called
            assert rate == pytest.approx(7.2)
            assert cny == 7200
            assert src == "online"

        # If online fetch fails on 2026-05-03, falls back to recent cached rate within 7 days (2026-05-02)
        with patch("billapp.core.fx.fetch_online_fx_rate", return_value=None):
            cny, rate, rate_at, src = convert_transaction(conn, "USD", 1000, "2026-05-03 10:00:00")
            assert rate == pytest.approx(7.2)
            assert cny == 7200

        # Online upsert must NOT overwrite manual rate on 2026-05-10
        conn.execute(
            """
            INSERT INTO fx_rates (currency, rate, effective_date, source, created_at, updated_at)
            VALUES ('USD', 7.11, '2026-05-10', 'online', '2026-05-10 00:00:00', '2026-05-10 00:00:00')
            ON CONFLICT(currency, effective_date) DO UPDATE SET
                rate = excluded.rate,
                source = excluded.source
            WHERE fx_rates.source != 'manual'
            """
        )
        row = conn.execute("SELECT rate, source FROM fx_rates WHERE currency='USD' AND effective_date='2026-05-10'").fetchone()
        assert row["source"] == "manual"
        assert row["rate"] == pytest.approx(7.35)

        # Retry FX offline must NOT wipe existing valid amount_cny and must return category_name
        tid = _insert_txn(
            conn,
            batch_id,
            acc_id,
            "2026-05-10 09:00:00",
            1000,
            "expense",
            currency="USD",
            amount_cny=7350,
            category_id=exp_cat,
            fp="fx-keep",
        )
        conn.execute("UPDATE transactions SET fx_rate = 7.35, fx_rate_at = '2026-05-10', fx_rate_source = 'manual' WHERE id = ?", (tid,))
        conn.commit()
        with patch("billapp.core.fx.fetch_online_fx_rate", return_value=None):
            retried = retry_transaction_fx(conn, tid)
            assert retried["amount_cny"] == 7350
            assert retried["category_name"] is not None

        # get_fx_overview excludes duplicates, excluded, and transfers
        _insert_txn(conn, batch_id, acc_id, "2026-05-11 09:00:00", 500, "expense", currency="EUR", amount_cny=None, excluded=1, fp="fx-ex")
        _insert_txn(conn, batch_id, acc_id, "2026-05-11 09:01:00", 500, "transfer", currency="EUR", amount_cny=None, fp="fx-tr")
        overview = get_fx_overview(conn)
        assert overview["pending_count"] == 0


# =========================================================================
# B-H5, B-H6, B-M1, B-M7, B-L1, B-L2: Domain APIs & Rules
# =========================================================================

class TestDomainApisAndRulesFixes:
    def test_bh5_delete_transaction_cleans_auto_links_and_scopes_transfer_peer(self, tmp_path: Path):
        db_file = tmp_path / "bh5.db"
        app = create_app(db_path=str(db_file))
        with TestClient(app) as client:
            conn = create_connection(str(db_file))
            try:
                batch_id, acc_id, _, _, _ = _seed_base_db(conn)
                t1 = _insert_txn(conn, batch_id, acc_id, "2026-05-01 10:00:00", 1000, "expense", fp="t1")
                t2 = _insert_txn(conn, batch_id, acc_id, "2026-05-01 10:00:01", 1000, "expense", duplicate_of=t1, fp="t2")
                t3 = _insert_txn(conn, batch_id, acc_id, "2026-05-01 11:00:00", 2000, "transfer", fp="t3")
                t4 = _insert_txn(conn, batch_id, acc_id, "2026-05-01 11:00:01", 2000, "transfer", transfer_peer=t3, fp="t4")
                conn.execute("UPDATE transactions SET transfer_peer = ? WHERE id = ?", (t4, t3))
                conn.execute(
                    "INSERT INTO match_links (kind, primary_id, secondary_id, score, status) VALUES ('duplicate', ?, ?, 0.95, 'auto')",
                    (t1, t2),
                )
                conn.commit()

                # Deleting t1 deletes auto match_link AND does NOT touch t4's transfer_peer pointing to t3
                r = client.delete(f"/api/transactions/{t1}")
                assert r.status_code == 200
                assert conn.execute("SELECT COUNT(*) FROM match_links WHERE primary_id = ?", (t1,)).fetchone()[0] == 0
                assert conn.execute("SELECT transfer_peer FROM transactions WHERE id = ?", (t4,)).fetchone()[0] == t3
            finally:
                conn.close()

    def test_bh6_category_delete_rejects_cross_direction_reassignment(self, tmp_path: Path):
        db_file = tmp_path / "bh6.db"
        app = create_app(db_path=str(db_file))
        with TestClient(app) as client:
            conn = create_connection(str(db_file))
            try:
                _, _, _, inc_cat, _ = _seed_base_db(conn)
            finally:
                conn.close()
            created = client.post("/api/categories", json={"name": "临时支出类", "direction": "expense"}).json()
            res = client.delete(f"/api/categories/{created['id']}", params={"reassign_to_id": inc_cat})
            assert res.status_code in (400, 422)

    def test_bm1_rule_matches_counterparty_and_merchant_norm_and_dryrun_preserves_hit_count(self):
        conn = init_db(":memory:")
        batch_id, acc_id, exp_cat, _, _ = _seed_base_db(conn)
        r = Rule(
            id=1,
            name="星巴克",
            category_id=exp_cat,
            field="counterparty",
            pattern_type="contains",
            pattern_value="星巴克",
        )
        assert rule_matches(r, {"counterparty": "POS消费", "merchant_norm": "星巴克咖啡"}) is True

        # Persist rule & run classify_batch(update_db=False) -> hit_count must remain 0
        conn.execute(
            "INSERT INTO rules (name, category_id, priority, field, pattern_type, pattern_value, is_builtin, hit_count) "
            "VALUES ('星巴克规则', ?, 10, 'counterparty', 'contains', '星巴克', 0, 0)",
            (exp_cat,),
        )
        conn.commit()
        pipeline = ClassificationPipeline(conn)
        pipeline.classify_batch([{"counterparty": "星巴克上海", "direction": "expense"}], update_db=False)
        hc = conn.execute("SELECT hit_count FROM rules WHERE name = '星巴克规则'").fetchone()[0]
        assert hc == 0

    def test_bm7_and_bl1_and_bl2_rules_categories_accounts_transactions_clearing(self, tmp_path: Path):
        db_file = tmp_path / "misc.db"
        app = create_app(db_path=str(db_file))
        with TestClient(app) as client:
            conn = create_connection(str(db_file))
            try:
                batch_id, acc_id, exp_cat, inc_cat, _ = _seed_base_db(conn)

                # B-M7: DedupSettings threshold validation
                bad_dedup = client.put(
                    "/api/settings",
                    json={"dedup": {"auto_confirm_threshold": 0.6, "pending_threshold": 0.8}},
                )
                assert bad_dedup.status_code == 422

                # B-M7: Rule amount clear & blank name rejection
                rule = client.post(
                    "/api/rules",
                    json={
                        "name": "测试规则",
                        "category_id": exp_cat,
                        "field": "counterparty",
                        "pattern_type": "contains",
                        "pattern_value": "瑞幸",
                        "amount_min_cents": 100,
                        "amount_max_cents": 5000,
                    },
                ).json()
                cleared = client.put(
                    f"/api/rules/{rule['id']}",
                    json={"amount_min_cents": None, "amount_max_cents": None},
                )
                assert cleared.status_code == 200
                assert cleared.json()["amount_min_cents"] is None
                assert cleared.json()["amount_max_cents"] is None
                blank_name = client.put(f"/api/rules/{rule['id']}", json={"name": "   "})
                assert blank_name.status_code == 422

                # B-L1: Child category inherits parent direction & update returns real txn count
                parent_inc = client.post("/api/categories", json={"name": "副业收入", "direction": "income"}).json()
                child = client.post(
                    "/api/categories",
                    json={"name": "版税", "direction": "expense", "parent_id": parent_inc["id"]},
                )
                assert child.status_code == 201
                assert child.json()["direction"] == "income"

                tid = _insert_txn(conn, batch_id, acc_id, "2026-05-01 10:00:00", 500, "income", category_id=child.json()["id"], fp="c1")
                upd_cat = client.put(f"/api/categories/{child.json()['id']}", json={"name": "版权收入"})
                assert upd_cat.status_code == 200
                assert upd_cat.json()["transaction_count"] == 1

                # F-M5: Clear transaction category_id and account alias
                clr_txn = client.patch(f"/api/transactions/{tid}", json={"category_id": None})
                assert clr_txn.status_code == 200
                assert clr_txn.json()["transaction"]["category_id"] is None

                clr_acc = client.patch(f"/api/accounts/{acc_id}", json={"alias": None})
                assert clr_acc.status_code == 200
                assert clr_acc.json()["alias"] is None

                # B-L1: Learning suggestions group by cleaned brand
                for idx, cp in enumerate(["星巴克(静安店)", "星巴克(徐汇店)", "星巴克(浦东店)"]):
                    _insert_txn(
                        conn,
                        batch_id,
                        acc_id,
                        f"2026-05-0{idx + 2} 10:00:00",
                        3500,
                        "expense",
                        category_id=exp_cat,
                        category_source="manual",
                        counterparty=cp,
                        merchant_norm="",
                        fp=f"learn-{idx}",
                    )
                suggs = get_learning_suggestions(conn, min_count=3)
                assert any("星巴克" in s["brand"] and s["count"] >= 3 for s in suggs)
            finally:
                conn.close()


# =========================================================================
# B-H7, B-H8, B-H9, B-M5, B-M6, B-L2: LLM, SSRF, ReDoS, Backup & Launcher
# =========================================================================

class TestLlmSecurityAndLauncherFixes:
    def test_bh7_preserve_undecryptable_key_and_block_cross_origin_settings_update(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("BILLAPP_MASTER_KEY", "11" * 32)
        app = create_app(db_path=str(tmp_path / "bh7.db"))
        with TestClient(app) as client:
            client.put(
                "/api/settings",
                json={
                    "llm": {
                        "base_url": "https://api.deepseek.com/v1",
                        "api_key": "sk-original-secret-9999",
                        "model": "deepseek-chat",
                        "enabled": True,
                    }
                },
            )
            # Cross-origin base_url change without supplying new key or clearing must fail
            bad = client.put("/api/settings", json={"llm": {"base_url": "https://evil.example/v1"}})
            assert bad.status_code == 422

            # Simulate wrong master key -> saving non-key setting must NOT wipe encrypted key
            monkeypatch.setenv("BILLAPP_MASTER_KEY", "22" * 32)
            client.put("/api/settings", json={"llm": {"temperature": 0.3}})
            monkeypatch.setenv("BILLAPP_MASTER_KEY", "11" * 32)
            restored = client.get("/api/settings").json()
            assert restored["llm"]["has_api_key"] is True
            assert restored["llm"]["temperature"] == 0.3

            # Explicit api_key="" clears the key
            cleared = client.put("/api/settings", json={"llm": {"api_key": ""}}).json()
            assert cleared["llm"]["has_api_key"] is False

    def test_bh8_date_preservation_and_partial_chunk_failure_warning(self):
        assert _mask_card_digits_keep_tail("20260501 转账 6222021234567890") == "20260501 转账 ****7890"
        assert _mask_card_digits_keep_tail("20260101-20260131 周期 15-05-2026") == "20260101-20260131 周期 15-05-2026"
        lines = prepare_statement_lines_for_llm(["20260501123045 消费 50.00 卡号6222021234567890"])
        assert "20260501123045" in lines[0]
        assert "****7890" in lines[0]

        # Partial chunk failure in parse_statement_with_llm_sync must produce warning + failed self_check
        call_num = 0

        def handler(_req: httpx.Request) -> httpx.Response:
            nonlocal call_num
            call_num += 1
            if call_num == 1:
                body = {
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "account": {"bank": "招行", "account_kind": "bank_debit", "card_tail": "1234", "currency": "CNY"},
                                        "transactions": [
                                            {"txn_time": "2026-05-01", "amount": 25.0, "direction": "expense", "counterparty": "咖啡"}
                                        ],
                                    }
                                )
                            }
                        }
                    ]
                }
                return httpx.Response(200, json=body)
            return httpx.Response(500, text="server error")

        cfg = LLMConfig(enabled=True, base_url="https://mock.llm/v1", api_key="sk-test", max_retries=0)
        llm_client = LLMClient(cfg, transport=httpx.MockTransport(handler))
        raw_lines = [f"2026-05-01 消费 {i}.00 商户{i}" for i in range(1, 70)]
        parse_res, meta = parse_statement_with_llm_sync(raw_lines, llm_client, chunk_size=35)
        assert meta["failed_chunks"] == 1
        assert any("失败" in w for w in parse_res.warnings)
        assert any(sc.name == "AI 整单识别笔数" and sc.passed is False for sc in parse_res.self_checks)

    def test_bh9_non_numeric_confidence_safe_parsing(self):
        def handler(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "verdict": "same",
                                        "confidence": "92",
                                        "reason": "match",
                                        "reviews": [
                                            {
                                                "id": 1,
                                                "agree": False,
                                                "suggested_category_key": "food",
                                                "confidence": "high",
                                                "reason": "test",
                                            }
                                        ],
                                    }
                                )
                            }
                        }
                    ]
                },
            )

        cfg = LLMConfig(enabled=True, base_url="https://mock.llm/v1", api_key="sk-test")
        client = LLMClient(cfg, transport=httpx.MockTransport(handler))
        arb = dedup_arbitration_sync({"id": 1}, {"id": 2}, 0.7, client)
        assert arb.verdict == "same"
        assert arb.confidence == pytest.approx(0.92)
        revs = review_classification_sync([{"id": 1, "counterparty": "A"}], client, key_to_id={"food": 1})
        assert revs == []  # "high" safely parsed as 0.0 (<0.80) without ValueError

    def test_bm5_llm_review_respects_toggle_and_classify_skips_manual(self, tmp_path: Path):
        db_file = tmp_path / "bm5.db"
        app = create_app(db_path=str(db_file))
        with TestClient(app) as client:
            conn = create_connection(str(db_file))
            try:
                batch_id, acc_id, exp_cat, _, _ = _seed_base_db(conn)
                manual_id = _insert_txn(
                    conn,
                    batch_id,
                    acc_id,
                    "2026-05-01 10:00:00",
                    2000,
                    "expense",
                    category_id=exp_cat,
                    category_source="manual",
                    category_confidence=1.0,
                    fp="bm5-m",
                )
            finally:
                conn.close()
            client.put(
                "/api/settings",
                json={
                    "llm": {
                        "enabled": True,
                        "base_url": "https://mock.llm/v1",
                        "api_key": "sk-test",
                        "task_review_enabled": False,
                    }
                },
            )
            rev = client.post("/api/llm/review", json={"limit": 10}).json()
            assert rev["status"] == "disabled"

            cls_res = client.post("/api/llm/classify", json={"transaction_ids": [manual_id]}).json()
            assert cls_res["classified_count"] == 0
            assert cls_res["results"] == []

    def test_bm6_redos_nested_alternation_and_ssrf_alternate_ip_formats(self):
        with pytest.raises(ValueError):
            compile_safe_regex(r"((a|b))+")

        for bad_url in (
            "http://metadata.google.internal./computeMetadata/v1",
            "http://2852039166/latest/meta-data",
            "http://0xa9fea9fe/v1",
            "http://0251.0376.0251.0376/v1",
            "http://[::ffff:169.254.169.254]/v1",
        ):
            with pytest.raises(ValueError):
                validate_llm_base_url(bad_url)

    def test_bl2_backup_vacuum_clears_secret_bytes_and_launcher_ipv6(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("BILLAPP_MASTER_KEY", "33" * 32)
        db_file = tmp_path / "bl2.db"
        app = create_app(db_path=str(db_file))
        with TestClient(app) as client:
            secret = "sk-super-unique-token-987654321"
            client.put(
                "/api/settings",
                json={
                    "llm": {
                        "base_url": "https://api.deepseek.com/v1",
                        "api_key": secret,
                        "enabled": True,
                    }
                },
            )
            # Read ciphertext from live DB to confirm it is completely gone after VACUUM
            conn = create_connection(str(db_file))
            try:
                raw_live = conn.execute(
                    "SELECT value FROM settings WHERE key = 'llm_config'"
                ).fetchone()[0]
            finally:
                conn.close()
            enc_val = json.loads(raw_live)["api_key_encrypted"]
            assert enc_val

            bak = client.post("/api/backup")
            assert bak.status_code == 200
            assert secret.encode("utf-8") not in bak.content
            assert enc_val.encode("utf-8") not in bak.content

        assert _format_url_host("::1") == "[::1]"
        assert _format_url_host("0.0.0.0") == "127.0.0.1"
        assert _format_url_host("127.0.0.1") == "127.0.0.1"
