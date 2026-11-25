"""Regression tests for the 2nd independent re-audit findings (R-BH1..4, R-BM1..6, R-BL1..4, R-FM2)."""

from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from fastapi.testclient import TestClient

from billapp.app import create_app
from billapp.classify.categories import CategoryService
from billapp.classify.pipeline import ClassificationPipeline
from billapp.classify.rules import Rule, apply_rule_to_existing
from billapp.core.db import create_connection, init_db
from billapp.core.fx import retry_transaction_fx
from billapp.core.secrets import _macos_keychain_get_or_create
from billapp.dedup.engine import confirm_match_link, rollback_import_batch
from billapp.importers.base import FileProbe
from billapp.importers.profile_engine import (
    AmountConfig,
    BankProfile,
    ColumnMappingConfig,
    ProfileImporter,
)
from billapp.llm.client import LLMClient, LLMConfig, validate_llm_base_url
from billapp.llm.prompts import _mask_preamble_cell
from billapp.llm.tasks import _parse_review_chunk_response
from billapp.stats import (
    calculate_stats_category,
    calculate_stats_heatmap,
    calculate_stats_summary,
)


def _seed_base(conn: sqlite3.Connection) -> tuple[int, int]:
    CategoryService.init_builtin_categories(conn)
    cur = conn.execute(
        "INSERT INTO import_batches (file_name, file_sha1, file_size, source, row_count) "
        "VALUES ('seed.csv', 'seedsha1', 100, 'alipay', 0)"
    )
    batch_id = cur.lastrowid
    cur = conn.execute(
        "INSERT INTO accounts (kind, bank, tail, alias, currency) "
        "VALUES ('bank_debit', '招行', '1234', '工资卡', 'CNY')"
    )
    acc_id = cur.lastrowid
    conn.commit()
    return batch_id, acc_id


def _insert_txn(
    conn: sqlite3.Connection,
    batch_id: int,
    acc_id: int,
    *,
    source: str = "alipay",
    txn_time: str = "2026-05-10 12:00:00",
    amount_cents: int = 10000,
    currency: str = "CNY",
    amount_cny: int | None = None,
    direction: str = "expense",
    counterparty: str = "测试商户",
    merchant_norm: str | None = None,
    description: str = "",
    category_id: int | None = None,
    category_source: str = "none",
    refund_of: int | None = None,
    refunded_cents: int = 0,
    duplicate_of: int | None = None,
    transfer_peer: int | None = None,
    fp: str = "fp",
) -> int:
    if amount_cny is None and currency == "CNY":
        amount_cny = amount_cents
    cur = conn.execute(
        """
        INSERT INTO transactions (
            batch_id, source, account_id, txn_time, time_precision,
            amount_cents, currency, amount_cny, direction,
            counterparty, merchant_norm, description,
            category_id, category_source, category_confidence,
            refund_of, refunded_cents, duplicate_of, transfer_peer,
            excluded, fingerprint
        ) VALUES (?, ?, ?, ?, 'second', ?, ?, ?, ?, ?, ?, ?, ?, ?, 1.0, ?, ?, ?, ?, 0, ?)
        """,
        (
            batch_id,
            source,
            acc_id,
            txn_time,
            amount_cents,
            currency,
            amount_cny,
            direction,
            counterparty,
            merchant_norm if merchant_norm is not None else counterparty,
            description,
            category_id,
            category_source,
            refund_of,
            refunded_cents,
            duplicate_of,
            transfer_peer,
            fp,
        ),
    )
    return int(cur.lastrowid)


# ---------------------------------------------------------------------------
# R-BH1: Atomic import_commit (no premature commit in ClassificationPipeline / LLMClient)
# ---------------------------------------------------------------------------

def test_r_bh1_import_commit_atomic_on_classification_error(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "atomic_commit.db"
    csv_bytes = (
        "支付宝交易记录明细查询\n"
        "账号:[test@example.com]\n"
        "----------------------------------------\n"
        "交易时间,交易分类,交易对方,对方账号,商品说明,收/支,金额,收/付款方式,交易状态,交易订单号,商家订单号,备注\n"
        "2026-05-01 10:00:00,餐饮美食,瑞幸咖啡,luckin,拿铁,支出,28.50,余额宝,交易成功,ORD001,M001,\n"
    ).encode("utf-8")

    with TestClient(create_app(db_path=str(db_path))) as client:
        # First import succeeds
        p1 = client.post("/api/import/preview", files={"file": ("alipay.csv", csv_bytes, "text/csv")})
        assert p1.status_code == 200
        c1 = client.post("/api/import/commit", json={"token": p1.json()["token"]})
        assert c1.status_code == 200
        first_batch_id = c1.json()["batch_id"]

        # Annotate the transaction with a manual note to verify it survives failed re-import
        with create_connection(db_path) as conn:
            conn.execute("UPDATE transactions SET note = '保留备注' WHERE batch_id = ?", (first_batch_id,))
            conn.commit()

        # Second import of the same file (duplicate batch replacement), but Stage 5 classification blows up
        p2 = client.post("/api/import/preview", files={"file": ("alipay.csv", csv_bytes, "text/csv")})
        assert p2.status_code == 200

        orig_classify_batch = ClassificationPipeline.classify_batch

        def _boom(self, *args, **kwargs):
            # Connection must still be inside the uncommitted outer transaction
            assert self.conn.in_transaction is True
            raise RuntimeError("simulated Stage 5 crash")

        monkeypatch.setattr(ClassificationPipeline, "classify_batch", _boom)
        with pytest.raises(RuntimeError, match="simulated Stage 5 crash"):
            client.post("/api/import/commit", json={"token": p2.json()["token"]})

        # Rollback must have restored the original batch & its transaction intact
        with create_connection(db_path) as conn:
            batches = conn.execute("SELECT id FROM import_batches").fetchall()
            txns = conn.execute("SELECT batch_id, note FROM transactions").fetchall()
            assert [b["id"] for b in batches] == [first_batch_id]
            assert len(txns) == 1
            assert txns[0]["batch_id"] == first_batch_id
            assert txns[0]["note"] == "保留备注"

        # Now restore classification and re-import cleanly -> R-BL4 preserves user note!
        monkeypatch.setattr(ClassificationPipeline, "classify_batch", orig_classify_batch)
        p3 = client.post("/api/import/preview", files={"file": ("alipay.csv", csv_bytes, "text/csv")})
        c3 = client.post("/api/import/commit", json={"token": p3.json()["token"]})
        assert c3.status_code == 200
        assert c3.json()["replaced_batch_id"] == first_batch_id
        with create_connection(db_path) as conn:
            txns = conn.execute("SELECT note FROM transactions").fetchall()
            assert len(txns) == 1
            assert txns[0]["note"] == "保留备注"


def test_r_bh1_llm_client_auto_commit_false_stays_in_transaction(tmp_path: Path):
    conn = init_db(tmp_path / "llm_tx.db")
    cfg = LLMConfig(enabled=True, base_url="https://mock.llm/v1", api_key="sk-test", model="mock")

    def _ok_handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code=200, json={"results": []})

    client = LLMClient(cfg, conn=conn, transport=httpx.MockTransport(_ok_handler), auto_commit=False)
    conn.execute("BEGIN")
    conn.execute(
        "INSERT INTO import_batches (file_name, file_sha1, file_size, source, row_count) "
        "VALUES ('x.csv', 'sha_x', 1, 'alipay', 0)"
    )
    client.chat_completion_sync(
        task_type="classify",
        messages=[{"role": "user", "content": "hi"}],
        use_json_mode=True,
    )
    assert conn.in_transaction is True
    conn.rollback()
    assert conn.execute("SELECT COUNT(*) FROM import_batches").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM llm_cache").fetchone()[0] == 0
    conn.close()


# ---------------------------------------------------------------------------
# R-BH2: rollback_import_batch must NOT corrupt surviving transfer peer via rejected link
# ---------------------------------------------------------------------------

def test_r_bh2_rollback_ignores_rejected_transfer_link(tmp_path: Path):
    conn = init_db(tmp_path / "rb_transfer.db")
    b1, acc1 = _seed_base(conn)
    b2 = conn.execute(
        "INSERT INTO import_batches (file_name, file_sha1, file_size, source, row_count) "
        "VALUES ('b2.csv', 'sha2', 10, 'cmb', 1)"
    ).lastrowid
    b3 = conn.execute(
        "INSERT INTO import_batches (file_name, file_sha1, file_size, source, row_count) "
        "VALUES ('b3.csv', 'sha3', 10, 'icbc', 1)"
    ).lastrowid

    t_b1 = _insert_txn(conn, b1, acc1, direction="expense", fp="t_b1")
    t_b2 = _insert_txn(conn, b2, acc1, direction="transfer", fp="t_b2")
    t_b3 = _insert_txn(conn, b3, acc1, direction="transfer", fp="t_b3")

    # Active transfer pair is t_b2 <-> t_b3
    conn.execute("UPDATE transactions SET transfer_peer = ? WHERE id = ?", (t_b3, t_b2))
    conn.execute("UPDATE transactions SET transfer_peer = ? WHERE id = ?", (t_b2, t_b3))
    conn.execute(
        """
        INSERT INTO match_links (kind, primary_id, secondary_id, score, status, features_json)
        VALUES ('transfer', ?, ?, 0.95, 'confirmed', ?)
        """,
        (t_b2, t_b3, json.dumps({"orig_dir_out": "expense", "orig_dir_in": "income"})),
    )
    # Historical REJECTED link between t_b1 (in b1) and t_b2 (in b2)
    conn.execute(
        """
        INSERT INTO match_links (kind, primary_id, secondary_id, score, status, features_json)
        VALUES ('transfer', ?, ?, 0.65, 'rejected', ?)
        """,
        (t_b1, t_b2, json.dumps({"orig_dir_out": "expense", "orig_dir_in": "income"})),
    )
    conn.commit()

    rollback_import_batch(conn, b1)

    row_b2 = conn.execute("SELECT direction, transfer_peer FROM transactions WHERE id = ?", (t_b2,)).fetchone()
    row_b3 = conn.execute("SELECT direction, transfer_peer FROM transactions WHERE id = ?", (t_b3,)).fetchone()
    assert row_b2["direction"] == "transfer"
    assert row_b2["transfer_peer"] == t_b3
    assert row_b3["direction"] == "transfer"
    assert row_b3["transfer_peer"] == t_b2
    conn.close()


# ---------------------------------------------------------------------------
# R-BH3 & R-BM4: ProfileImporter channel normalization & BankProfile currency normalization
# ---------------------------------------------------------------------------

def test_r_bh3_and_r_bm4_profile_channel_and_currency_normalization(tmp_path: Path):
    profile = BankProfile(
        id="test_chan_cur",
        name="渠道与币种测试",
        bank="招商银行",
        account_kind="debit",
        file_type="csv",
        currency="人民币",
        column_mapping=ColumnMappingConfig(
            date="交易日期",
            amount="交易金额",
            counterparty="交易对手",
            description="摘要",
            channel="交易渠道",
            currency="币种",
        ),
        amount_mode=AmountConfig(type="signed", amount_col="交易金额"),
    )
    assert profile.currency == "CNY"

    csv_file = tmp_path / "stmt.csv"
    csv_file.write_text(
        "交易日期,交易金额,交易对手,摘要,交易渠道,币种\n"
        "2026-05-01,-50.00,星巴克,咖啡,手机银行,人民币\n"
        "2026-05-02,-30.00,美团,外卖,支付宝,CNY\n"
        "2026-05-03,-20.00,便利店,零食,银联快捷,美元\n",
        encoding="utf-8",
    )

    importer = ProfileImporter(profile)
    res = importer.parse(FileProbe(csv_file))
    assert len(res.transactions) == 3
    assert res.transactions[0].channel is None
    assert res.transactions[0].currency == "CNY"
    assert res.transactions[1].channel == "alipay"
    assert res.transactions[2].channel == "unionpay"
    assert res.transactions[2].currency == "USD"


# ---------------------------------------------------------------------------
# R-BH4: macOS Keychain verifies stderr & read-back after `security -i`
# ---------------------------------------------------------------------------

def test_r_bh4_macos_keychain_verifies_security_i_write(monkeypatch):
    import billapp.core.secrets as secrets_mod

    monkeypatch.setattr(secrets_mod.sys, "platform", "darwin")
    monkeypatch.setattr(secrets_mod.os.path, "exists", lambda p: p == "/usr/bin/security")

    # Case 1: `security -i` exits 0 with non-empty stderr (silent failure) -> must return None
    calls_err = [
        subprocess.CompletedProcess([], 44, stdout="", stderr="SecKeychainSearchCopyNext: not found"),
        subprocess.CompletedProcess([], 0, stdout="", stderr="security: SecKeychainItemCreateFromContent: error"),
    ]
    with patch.object(secrets_mod.subprocess, "run", side_effect=calls_err):
        assert _macos_keychain_get_or_create(create=True) is None

    # Case 2: `security -i` exits 0 with empty stderr, but read-back verification fails -> must return None
    calls_readback_fail = [
        subprocess.CompletedProcess([], 44, stdout="", stderr="not found"),
        subprocess.CompletedProcess([], 0, stdout="", stderr=""),
        subprocess.CompletedProcess([], 44, stdout="", stderr="still not found"),
    ]
    with patch.object(secrets_mod.subprocess, "run", side_effect=calls_readback_fail):
        assert _macos_keychain_get_or_create(create=True) is None

    # Case 3: `security -i` succeeds AND read-back matches written hex -> returns 32-byte key
    fixed_hex = "ab" * 32
    monkeypatch.setattr(secrets_mod.secrets, "token_hex", lambda n: fixed_hex)
    calls_ok = [
        subprocess.CompletedProcess([], 44, stdout="", stderr="not found"),
        subprocess.CompletedProcess([], 0, stdout="", stderr=""),
        subprocess.CompletedProcess([], 0, stdout=fixed_hex + "\n", stderr=""),
    ]
    with patch.object(secrets_mod.subprocess, "run", side_effect=calls_ok):
        key = _macos_keychain_get_or_create(create=True)
        assert key == bytes.fromhex(fixed_hex)


# ---------------------------------------------------------------------------
# R-BM1: calculate_stats_category keeps child <= parent, pct <= 100%, matches summary
# ---------------------------------------------------------------------------

def test_r_bm1_stats_category_consistent_with_summary_and_no_over_100_pct(tmp_path: Path):
    conn = init_db(tmp_path / "stats_cat.db")
    b1, acc1 = _seed_base(conn)

    cat_daily = conn.execute("SELECT id FROM categories WHERE key = 'shopping.daily'").fetchone()[0]
    cat_elec = conn.execute("SELECT id FROM categories WHERE key = 'shopping.electronics'").fetchone()[0]
    cat_food = conn.execute("SELECT id FROM categories WHERE key = 'food.dining'").fetchone()[0]
    cat_refund = conn.execute("SELECT id FROM categories WHERE key = 'reimbursement.refund'").fetchone()[0]

    # Case A: Sibling refund in shopping.electronics (-500) + shopping.daily (+1000) + food.dining (+1000)
    _insert_txn(conn, b1, acc1, txn_time="2026-05-05 10:00:00", amount_cents=100000, direction="expense", category_id=cat_daily, fp="c1")
    _insert_txn(conn, b1, acc1, txn_time="2026-05-06 10:00:00", amount_cents=50000, direction="refund", category_id=cat_elec, fp="c2")
    _insert_txn(conn, b1, acc1, txn_time="2026-05-07 10:00:00", amount_cents=100000, direction="expense", category_id=cat_food, fp="c3")
    # Case B: Unlinked refund tagged with income category reimbursement.refund (-300)
    _insert_txn(conn, b1, acc1, txn_time="2026-05-08 10:00:00", amount_cents=30000, direction="refund", category_id=cat_refund, fp="c4")
    conn.commit()

    summary = calculate_stats_summary(conn, start_date="2026-05-01", end_date="2026-05-31")
    cat_stats = calculate_stats_category(conn, direction="expense", start_date="2026-05-01", end_date="2026-05-31")

    # Summary net expense = 100000 + 100000 - 50000 - 30000 = 120000
    assert summary.total_expense_cents == 120000
    assert cat_stats.total_cents == summary.total_expense_cents

    for item in cat_stats.items:
        assert item.amount_cents >= 0
        assert 0.0 <= item.percentage <= 100.0
        for child in item.children:
            assert child.amount_cents >= 0
            assert child.amount_cents <= item.amount_cents
            assert 0.0 <= child.percentage <= 100.0
    conn.close()


# ---------------------------------------------------------------------------
# R-BM2: Direction compatibility in Stage 3 builtin rules, Stage 5 LLM, and /api/llm/classify
# ---------------------------------------------------------------------------

def test_r_bm2_direction_compatibility_across_pipeline_and_api(tmp_path: Path, monkeypatch):
    import billapp.api.llm as llm_api

    db_path = tmp_path / "dir_compat.db"
    with TestClient(create_app(db_path=str(db_path))) as client:
        with create_connection(db_path) as conn:
            b1, acc1 = _seed_base(conn)
            # Expense containing income builtin keywords ("退货运费险", "微信转账")
            t_exp_ins = _insert_txn(conn, b1, acc1, direction="expense", counterparty="退货运费险", fp="d1")
            t_exp_wx = _insert_txn(conn, b1, acc1, direction="expense", counterparty="微信转账", fp="d2")
            # Income containing expense builtin keywords ("招商银行", "京东")
            t_inc_cmb = _insert_txn(conn, b1, acc1, direction="income", counterparty="招商银行代发", fp="d3")
            conn.commit()

            pipeline = ClassificationPipeline(conn, auto_init_builtins=False)
            rows = conn.execute("SELECT * FROM transactions WHERE id IN (?, ?, ?)", (t_exp_ins, t_exp_wx, t_inc_cmb)).fetchall()
            results, _ = pipeline.classify_batch(rows, update_db=True)
            for row, res in zip(rows, results):
                if res.category_id is not None:
                    cat_dir = conn.execute("SELECT direction FROM categories WHERE id = ?", (res.category_id,)).fetchone()[0]
                    assert cat_dir == row["direction"], f"Cross-direction category {res.category_id} ({cat_dir}) assigned to {row['direction']}"

        # Test /api/llm/classify rejects cross-direction LLM assignment (income category for expense txn)
        client.put(
            "/api/settings",
            json={
                "llm": {
                    "enabled": True,
                    "base_url": "https://mock.llm/v1",
                    "api_key": "sk-test",
                    "model": "mock",
                    "task_classify_enabled": True,
                }
            },
        )

        def _bad_dir_handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                status_code=200,
                json={
                    "results": [
                        {"id": str(t_exp_ins), "category_key": "income.salary", "confidence": 0.99, "reason": "wrong dir"}
                    ]
                },
            )

        monkeypatch.setattr(
            llm_api,
            "LLMClient",
            lambda config, conn=None, **kw: LLMClient(config=config, conn=conn, transport=httpx.MockTransport(_bad_dir_handler), **kw),
        )
        resp = client.post("/api/llm/classify", json={"transaction_ids": [t_exp_ins], "update_db": True})
        assert resp.status_code == 200
        body = resp.json()
        assert body["classified_count"] == 0
        assert body["results"][0]["category_id"] is None


# ---------------------------------------------------------------------------
# R-BM3 & R-FM2: Competing pending links retired, conflict blocked, amount_cny returned
# ---------------------------------------------------------------------------

def test_r_bm3_and_r_fm2_competing_links_and_amount_cny(tmp_path: Path):
    db_path = tmp_path / "links_and_fx.db"
    with TestClient(create_app(db_path=str(db_path))) as client:
        with create_connection(db_path) as conn:
            b1, acc1 = _seed_base(conn)
            t1 = _insert_txn(conn, b1, acc1, source="alipay", currency="USD", amount_cents=1000, amount_cny=7200, direction="expense", fp="l1")
            t2 = _insert_txn(conn, b1, acc1, source="cmb", currency="USD", amount_cents=1000, amount_cny=7200, direction="income", fp="l2")
            t3 = _insert_txn(conn, b1, acc1, source="icbc", currency="USD", amount_cents=1000, amount_cny=7200, direction="income", fp="l3")

            # Two competing pending transfer links for t1: (t1, t2) and (t1, t3)
            link1 = conn.execute(
                "INSERT INTO match_links (kind, primary_id, secondary_id, score, status) VALUES ('transfer', ?, ?, 0.72, 'pending')",
                (t1, t2),
            ).lastrowid
            link2 = conn.execute(
                "INSERT INTO match_links (kind, primary_id, secondary_id, score, status) VALUES ('transfer', ?, ?, 0.68, 'pending')",
                (t1, t3),
            ).lastrowid
            conn.commit()

        # Before confirming, GET /api/review exposes amount_cny on both sides (R-FM2)
        rev_before = client.get("/api/review")
        assert rev_before.status_code == 200
        items_before = rev_before.json()
        assert len(items_before) == 2
        assert items_before[0]["left"]["amount_cny"] == 7200
        assert items_before[0]["right"]["amount_cny"] == 7200

        # Confirming link1 retires competing pending link2 (R-BM3)
        with create_connection(db_path) as conn:
            assert confirm_match_link(conn, link1) is True
            assert conn.execute("SELECT id FROM match_links WHERE id = ?", (link2,)).fetchone() is None

            # Even if a stale competing pending link exists manually, confirm_match_link refuses overwrite
            stale_link = conn.execute(
                "INSERT INTO match_links (kind, primary_id, secondary_id, score, status) VALUES ('transfer', ?, ?, 0.68, 'pending')",
                (t1, t3),
            ).lastrowid
            conn.commit()
            assert confirm_match_link(conn, stale_link) is False

        # GET /api/review and GET /api/meta exclude already-matched rows (R-BM3)
        assert len(client.get("/api/review").json()) == 0
        assert client.get("/api/meta").json()["pending_review_count"] == 0

        # R-FM2: Merged duplicate in /api/transactions and /api/transactions/{id} exposes amount_cny
        with create_connection(db_path) as conn:
            t_dup_primary = _insert_txn(conn, b1, acc1, source="alipay", currency="USD", amount_cents=2500, amount_cny=18000, direction="expense", fp="d_p")
            _insert_txn(conn, b1, acc1, source="cmb", currency="USD", amount_cents=2500, amount_cny=18000, direction="expense", duplicate_of=t_dup_primary, fp="d_s")
            conn.commit()

        detail = client.get(f"/api/transactions/{t_dup_primary}").json()
        assert len(detail["transaction"]["merged_duplicates"]) == 1
        assert detail["transaction"]["merged_duplicates"][0]["amount_cny"] == 18000

        list_res = client.get("/api/transactions?is_duplicate=false").json()
        matched_row = next(r for r in list_res["items"] if r["id"] == t_dup_primary)
        assert matched_row["merged_duplicates"][0]["amount_cny"] == 18000


# ---------------------------------------------------------------------------
# R-BM4: retry_transaction_fx normalizes legacy non-ISO currency labels
# ---------------------------------------------------------------------------

def test_r_bm4_retry_transaction_fx_normalizes_currency(tmp_path: Path):
    conn = init_db(tmp_path / "fx_norm.db")
    b1, acc1 = _seed_base(conn)
    t_cny = _insert_txn(conn, b1, acc1, currency="人民币", amount_cents=1234, amount_cny=None, fp="fx1")
    conn.commit()

    res = retry_transaction_fx(conn, t_cny)
    assert res is not None
    assert res["currency"] == "CNY"
    assert res["amount_cny"] == 1234
    conn.close()


# ---------------------------------------------------------------------------
# R-BM5: validate_llm_base_url rejects out-of-range port (returns 422, not 500)
# ---------------------------------------------------------------------------

def test_r_bm5_validate_llm_base_url_invalid_port(tmp_path: Path):
    with pytest.raises(ValueError):
        validate_llm_base_url("https://api.deepseek.com:99999/v1")

    with TestClient(create_app(db_path=str(tmp_path / "port_val.db"))) as client:
        resp = client.put(
            "/api/settings",
            json={"llm": {"enabled": True, "base_url": "https://api.deepseek.com:99999/v1"}},
        )
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# R-BM6: _mask_preamble_cell preserves last-4 card tail for full/grouped PANs
# ---------------------------------------------------------------------------

def test_r_bm6_mask_preamble_cell_preserves_card_tail():
    contiguous = _mask_preamble_cell("账号：6222020200012345")
    assert "622202" not in contiguous
    assert "****2345" in contiguous

    grouped = _mask_preamble_cell("卡号：6222 0202 0001 2345")
    assert "6222" not in grouped
    assert "****2345" in grouped

    # Still scrubs 18-digit national ID and 11-digit phone without leaking tail
    id_cell = _mask_preamble_cell("身份证：110101199001011234 手机：13800138000")
    assert "110101199001011234" not in id_cell
    assert "13800138000" not in id_cell


# ---------------------------------------------------------------------------
# R-BL1: _parse_review_chunk_response tolerates malformed id and category keys
# ---------------------------------------------------------------------------

def test_r_bl1_parse_review_chunk_response_malformed_items():
    resp = {
        "reviews": [
            {"id": "tx_not_int", "agree": False, "suggested_category_key": "food.dining", "confidence": 0.95},
            {"id": True, "agree": False, "suggested_category_key": "food.dining", "confidence": 0.95},
            {"id": 101, "agree": False, "suggested_category_key": ["food.dining"], "confidence": 0.95, "reason": "bad key"},
            {"id": 102, "agree": False, "suggested_category_key": "food.dining", "confidence": 0.92, "reason": "valid"},
        ]
    }
    parsed = _parse_review_chunk_response(resp, {"food.dining": 7})
    assert [r.id for r in parsed] == [101, 102]
    assert parsed[0].suggested_category_id is None
    assert parsed[1].suggested_category_id == 7


# ---------------------------------------------------------------------------
# R-BL2: calculate_stats_heatmap skips malformed non-ISO txn_time rows
# ---------------------------------------------------------------------------

def test_r_bl2_stats_heatmap_skips_malformed_txn_time(tmp_path: Path):
    conn = init_db(tmp_path / "heatmap.db")
    b1, acc1 = _seed_base(conn)
    _insert_txn(conn, b1, acc1, txn_time="not-a-date", amount_cents=5000, direction="expense", fp="hm1")
    _insert_txn(conn, b1, acc1, txn_time="2026-05-12 09:00:00", amount_cents=8000, direction="expense", fp="hm2")
    conn.commit()

    # original_month mode
    hm_orig = calculate_stats_heatmap(conn, year=2026)
    assert len(hm_orig.calendar_items) == 1
    assert hm_orig.calendar_items[0].date == "2026-05-12"
    assert hm_orig.calendar_items[0].expense_cents == 8000

    # refund_month mode
    conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('refund_settings', '{\"mode\": \"refund_month\"}')")
    conn.commit()
    hm_ref = calculate_stats_heatmap(conn, year=2026)
    assert len(hm_ref.calendar_items) == 1
    assert hm_ref.calendar_items[0].date == "2026-05-12"
    assert hm_ref.calendar_items[0].expense_cents == 8000
    conn.close()


# ---------------------------------------------------------------------------
# R-BL3: apply_rule_to_existing handles padded & non-ASCII case patterns
# ---------------------------------------------------------------------------

def test_r_bl3_apply_rule_to_existing_padded_and_non_ascii(tmp_path: Path):
    conn = init_db(tmp_path / "rules_unicode.db")
    b1, acc1 = _seed_base(conn)
    cat_food = conn.execute("SELECT id FROM categories WHERE key = 'food.dining'").fetchone()[0]

    _insert_txn(conn, b1, acc1, counterparty="星巴克咖啡", fp="r1")
    _insert_txn(conn, b1, acc1, counterparty="CAFÉ PARIS", fp="r2")
    conn.commit()

    rule_padded = Rule(
        id=None,
        name="padded",
        field="counterparty",
        pattern_type="contains",
        pattern_value=" 星巴克 ",
        category_id=cat_food,
    )
    res1 = apply_rule_to_existing(conn, rule_padded)
    assert res1["updated_count"] == 1

    rule_unicode = Rule(
        id=None,
        name="unicode",
        field="counterparty",
        pattern_type="contains",
        pattern_value="café",
        category_id=cat_food,
    )
    res2 = apply_rule_to_existing(conn, rule_unicode)
    assert res2["updated_count"] == 1
    conn.close()
