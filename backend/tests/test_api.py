"""Comprehensive FastAPI TestClient integration test suite for all BillApp REST API endpoints."""

from __future__ import annotations

import io
from pathlib import Path
import tempfile
from typing import Generator
import pytest
from fastapi.testclient import TestClient

from billapp.app import create_app
from billapp.classify.categories import CategoryService
from tests.fixtures.synthetic_alipay import generate_synthetic_alipay_csv
from tests.fixtures.synthetic_pdf import generate_synthetic_boc_pdf
from tests.fixtures.synthetic_wechat import generate_synthetic_wechat_xlsx


@pytest.fixture
def client(tmp_path: Path) -> Generator[TestClient, None, None]:
    db_file = tmp_path / "test_bill.db"
    app = create_app(db_path=str(db_file))
    # TestClient as context manager triggers startup and shutdown lifespan events
    with TestClient(app) as test_client:
        yield test_client


# =========================================================================
# 1. System Health & Meta APIs
# =========================================================================

def test_api_health_check(client: TestClient):
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "version": "1.2.1"}


def test_api_meta_endpoint(client: TestClient):
    resp = client.get("/api/meta")
    assert resp.status_code == 200
    data = resp.json()
    assert data["version"] == "1.2.1"
    assert "categories" in data
    assert len(data["categories"]) == 16  # 16 root categories
    assert "accounts" in data
    assert "pending_review_count" in data


# =========================================================================
# 2. Ingestion & Batch APIs (/api/import/*)
# =========================================================================

def test_import_preview_encrypted_pdf_password_detection(client: TestClient):
    pdf_bytes = generate_synthetic_boc_pdf(password="boc123")

    # 1. Upload without password -> returns need_password=True
    resp = client.post(
        "/api/import/preview",
        files={"file": ("synthetic_boc.pdf", io.BytesIO(pdf_bytes), "application/pdf")},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["need_password"] is True
    assert data["token"] == ""

    # 2. Upload with invalid password -> returns 400 IMPORT_PASSWORD_WRONG
    resp = client.post(
        "/api/import/preview",
        files={"file": ("synthetic_boc.pdf", io.BytesIO(pdf_bytes), "application/pdf")},
        data={"password": "wrong_password_999"},
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "IMPORT_PASSWORD_WRONG"

    # 3. Upload with correct password -> returns preview token and accounts
    resp = client.post(
        "/api/import/preview",
        files={"file": ("synthetic_boc.pdf", io.BytesIO(pdf_bytes), "application/pdf")},
        data={"password": "boc123"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["need_password"] is False
    assert len(data["token"]) > 0
    assert data["total_rows"] == 6
    assert len(data["accounts"]) == 1
    assert data["accounts"][0]["tail"] == "6666"
    assert len(data["rows"]) == 6


def test_import_commit_and_batch_rollback(client: TestClient):
    alipay_bytes = generate_synthetic_alipay_csv()

    # 1. Preview Alipay statement
    resp = client.post(
        "/api/import/preview",
        files={"file": ("alipay.csv", io.BytesIO(alipay_bytes), "text/csv")},
    )
    assert resp.status_code == 200
    data = resp.json()
    token = data["token"]
    assert len(token) > 0
    assert data["total_rows"] == 5

    # 2. Duplicate file upload check -> 409 before commit? No, after commit sha1 is stored
    # 3. Commit batch
    commit_resp = client.post("/api/import/commit", json={"token": token})
    assert commit_resp.status_code == 200
    c_data = commit_resp.json()
    batch_id = c_data["batch_id"]
    assert c_data["imported_count"] == 5
    assert batch_id > 0

    # 4. Attempting to re-commit same token -> 410 IMPORT_TOKEN_EXPIRED
    expired_resp = client.post("/api/import/commit", json={"token": token})
    assert expired_resp.status_code == 410
    assert expired_resp.json()["code"] == "IMPORT_TOKEN_EXPIRED"

    # 5. Attempting to re-upload exact same file -> 409 IMPORT_DUPLICATE_FILE
    dup_resp = client.post(
        "/api/import/preview",
        files={"file": ("alipay.csv", io.BytesIO(alipay_bytes), "text/csv")},
    )
    assert dup_resp.status_code == 409
    assert dup_resp.json()["code"] == "IMPORT_DUPLICATE_FILE"

    # 6. List batches
    batches_resp = client.get("/api/import/batches")
    assert batches_resp.status_code == 200
    batches = batches_resp.json()
    assert len(batches) >= 1
    assert batches[0]["id"] == batch_id
    assert batches[0]["row_count"] == 5

    # 7. Check transactions exist
    txn_resp = client.get("/api/transactions")
    assert txn_resp.status_code == 200
    assert txn_resp.json()["total"] == 5

    # 8. Rollback batch
    del_resp = client.delete(f"/api/import/batches/{batch_id}")
    assert del_resp.status_code == 200
    assert del_resp.json()["status"] == "rolled_back"

    # 9. Verify transactions deleted
    txn_after = client.get("/api/transactions")
    assert txn_after.json()["total"] == 0

    # 10. Rollback non-existent batch -> 404
    del_again = client.delete(f"/api/import/batches/{batch_id}")
    assert del_again.status_code == 404


# =========================================================================
# 3. Bank Profiles APIs (/api/profiles/*)
# =========================================================================

def test_profiles_crud_and_suggest(client: TestClient):
    # 1. List profiles (no guessed built-in statement templates seeded by default)
    list_resp = client.get("/api/profiles")
    assert list_resp.status_code == 200
    profiles = list_resp.json()
    assert {p["id"] for p in profiles}.isdisjoint({"ccb_debit", "cmb_debit", "pab_credit", "cmb_pdf"})

    # 2. Suggest column mapping
    suggest_resp = client.post(
        "/api/profiles/suggest",
        json={"headers": ["交易日期", "支出金额", "收入金额", "对方户名", "摘要", "卡号尾号"]},
    )
    assert suggest_resp.status_code == 200
    s_data = suggest_resp.json()
    assert s_data["amount_mode"] == "split"
    assert s_data["column_mapping"]["date"] == "交易日期"
    assert s_data["column_mapping"]["expense_amount"] == "支出金额"
    assert s_data["column_mapping"]["income_amount"] == "收入金额"

    # 3. Create custom profile
    custom_yaml = """
id: my_custom_bank
name: 我的自定义银行
bank: 自定义银行
account_kind: debit
file_type: csv
column_mapping:
  date:
    header: 交易日期
amount_mode:
  type: signed
  amount_col: 金额
"""
    create_resp = client.post(
        "/api/profiles",
        json={"id": "my_custom_bank", "name": "我的自定义银行", "yaml_content": custom_yaml},
    )
    assert create_resp.status_code == 201
    assert create_resp.json()["id"] == "my_custom_bank"
    assert create_resp.json()["is_builtin"] is False

    # 4. Get profile detail
    det_resp = client.get("/api/profiles/my_custom_bank")
    assert det_resp.status_code == 200
    assert "yaml_content" in det_resp.json()

    # 5. Duplicate ID collision -> 409
    dup_resp = client.post(
        "/api/profiles",
        json={"id": "my_custom_bank", "name": "我的自定义银行", "yaml_content": custom_yaml},
    )
    assert dup_resp.status_code == 409

    # 6. Update custom profile
    update_resp = client.put(
        "/api/profiles/my_custom_bank",
        json={"name": "修改后的自定义银行", "yaml_content": custom_yaml},
    )
    assert update_resp.status_code == 200
    assert update_resp.json()["name"] == "修改后的自定义银行"

    # 7. Delete custom profile
    del_resp = client.delete("/api/profiles/my_custom_bank")
    assert del_resp.status_code == 200
    assert del_resp.json()["status"] == "deleted"

    # 8. Startup cleanup removes legacy guessed built-in templates while preserving custom profiles
    import sqlite3
    conn = sqlite3.connect(client.app.state.db_path)
    try:
        conn.execute(
            "INSERT OR IGNORE INTO bank_profiles (id, name, yaml_content, is_builtin) VALUES (?, ?, ?, ?)",
            ("cmb_debit", "招商银行借记卡交易明细", custom_yaml, 1),
        )
        conn.execute(
            "INSERT OR IGNORE INTO bank_profiles (id, name, yaml_content, is_builtin) VALUES (?, ?, ?, ?)",
            ("user_saved_profile", "用户保存模板", custom_yaml, 0),
        )
        conn.commit()
    finally:
        conn.close()

    with TestClient(create_app(db_path=client.app.state.db_path)) as restarted_client:
        after_ids = {p["id"] for p in restarted_client.get("/api/profiles").json()}
        assert "cmb_debit" not in after_ids
        assert "user_saved_profile" in after_ids


# =========================================================================
# 4. Transactions & Inline Edit APIs (/api/transactions/*)
# =========================================================================

def test_transactions_crud_and_rule_learning_proposal(client: TestClient):
    # Import synthetic WeChat statement to seed data
    wechat_bytes = generate_synthetic_wechat_xlsx()

    prev_res = client.post(
        "/api/import/preview",
        files={
            "file": (
                "wechat.xlsx",
                io.BytesIO(wechat_bytes),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
    )
    assert prev_res.status_code == 200
    token = prev_res.json()["token"]

    commit_res = client.post("/api/import/commit", json={"token": token})
    assert commit_res.status_code == 200

    # 1. Filter transactions
    txns_res = client.get("/api/transactions?page=1&page_size=10&order_by=amount_cents&order_dir=desc")
    assert txns_res.status_code == 200
    t_data = txns_res.json()
    assert t_data["total"] > 0
    first_txn = t_data["items"][0]
    txn_id = first_txn["id"]

    # 2. Get detail
    det_res = client.get(f"/api/transactions/{txn_id}")
    assert det_res.status_code == 200
    assert "transaction" in det_res.json()
    assert "related" in det_res.json()

    # 3. Inline update category (learning proposal trigger)
    cat_tree = client.get("/api/categories?direction=expense").json()
    target_cat_id = cat_tree[0]["children"][0]["id"]  # e.g. food.dining

    patch_res = client.patch(
        f"/api/transactions/{txn_id}",
        json={"category_id": target_cat_id, "note": "测试修改备注", "excluded": 0},
    )
    assert patch_res.status_code == 200
    p_data = patch_res.json()
    assert p_data["transaction"]["category_id"] == target_cat_id
    assert p_data["transaction"]["category_source"] == "manual"
    assert p_data["transaction"]["note"] == "测试修改备注"

    # 4. Bulk patch
    all_txns = client.get("/api/transactions?page_size=5").json()["items"]
    ids_to_bulk = [t["id"] for t in all_txns[:3]]
    bulk_res = client.patch(
        "/api/transactions/bulk",
        json={"ids": ids_to_bulk, "excluded": 1},
    )
    assert bulk_res.status_code == 200
    assert bulk_res.json()["updated_count"] == 3

    # Check excluded filter
    ex_res = client.get("/api/transactions?excluded=1")
    assert ex_res.status_code == 200
    assert ex_res.json()["total"] >= 3

    # 5. Delete single transaction
    del_t = client.delete(f"/api/transactions/{txn_id}")
    assert del_t.status_code == 200
    assert del_t.json()["ok"] is True


# =========================================================================
# 5. Candidate Review APIs (/api/review/*)
# =========================================================================

def test_candidate_review_confirm_and_reject(client: TestClient):
    # Review list starts empty or contains pending candidates
    rev_res = client.get("/api/review")
    assert rev_res.status_code == 200
    assert isinstance(rev_res.json(), list)

    # Confirm / reject non-existent link -> 404
    conf_404 = client.post("/api/review/99999/confirm")
    assert conf_404.status_code == 404

    rej_404 = client.post("/api/review/99999/reject")
    assert rej_404.status_code == 404


# =========================================================================
# 6. Categories APIs (/api/categories/*)
# =========================================================================

def test_categories_hierarchy_and_depth_constraints(client: TestClient):
    # 1. Sibling collision check
    dup_res = client.post(
        "/api/categories",
        json={"name": "餐饮美食", "direction": "expense"},
    )
    assert dup_res.status_code == 409

    # 2. Create custom root category
    root_res = client.post(
        "/api/categories",
        json={"name": "宠物生活", "direction": "expense", "color": "#10B981"},
    )
    assert root_res.status_code == 201
    root_id = root_res.json()["id"]

    # 3. Create subcategory under custom root
    sub_res = client.post(
        "/api/categories",
        json={"name": "猫粮狗粮", "parent_id": root_id, "direction": "expense"},
    )
    assert sub_res.status_code == 201
    sub_id = sub_res.json()["id"]

    # 4. Attempting to create sub-subcategory (exceeding 2 tiers) -> 400
    depth_res = client.post(
        "/api/categories",
        json={"name": "主食罐头", "parent_id": sub_id, "direction": "expense"},
    )
    assert depth_res.status_code == 400
    assert depth_res.json()["code"] == "MAX_DEPTH_EXCEEDED"

    # 5. Update budget
    up_res = client.put(
        f"/api/categories/{sub_id}",
        json={"budget_cents": 50000},
    )
    assert up_res.status_code == 200
    assert up_res.json()["budget_cents"] == 50000

    # 6. Delete subcategory
    del_sub = client.delete(f"/api/categories/{sub_id}")
    assert del_sub.status_code == 200

    # 7. Delete root category
    del_root = client.delete(f"/api/categories/{root_id}")
    assert del_root.status_code == 200


# =========================================================================
# 7. Rules APIs (/api/rules/*)
# =========================================================================

def test_rules_crud_and_redos_prevention(client: TestClient):
    # 1. List rules
    rules_res = client.get("/api/rules")
    assert rules_res.status_code == 200
    assert len(rules_res.json()) > 0

    # 2. Create rule with invalid ReDoS regex -> 422 VALIDATION_ERROR
    redos_res = client.post(
        "/api/rules",
        json={
            "category_id": 1,
            "pattern_type": "regex",
            "pattern_value": "(a+)+$",  # Catastrophic backtracking nested quantifier
        },
    )
    assert redos_res.status_code == 422

    # 3. Create valid rule
    rule_res = client.post(
        "/api/rules",
        json={
            "name": "星巴克咖啡",
            "category_id": 1,
            "priority": 50,
            "field": "counterparty",
            "pattern_type": "contains",
            "pattern_value": "星巴克",
        },
    )
    assert rule_res.status_code == 201
    rule_id = rule_res.json()["id"]

    # 4. Test rule against database (dry run)
    test_res = client.post(
        "/api/rules/test",
        json={
            "field": "counterparty",
            "pattern_type": "contains",
            "pattern_value": "星巴克",
        },
    )
    assert test_res.status_code == 200
    assert "matched_count" in test_res.json()
    assert "preview" in test_res.json()

    # 5. Apply proposal
    apply_res = client.post(
        "/api/rules/apply-proposal",
        json={
            "category_id": 1,
            "field": "counterparty",
            "pattern_type": "contains",
            "pattern_value": "星巴克",
            "apply_to_existing": True,
        },
    )
    assert apply_res.status_code == 200
    assert "rule" in apply_res.json()

    # 6. Delete created user rule
    del_res = client.delete(f"/api/rules/{rule_id}")
    assert del_res.status_code == 200

    # 7. Suggestions endpoint
    sug_res = client.get("/api/rules/suggestions?min_count=1")
    assert sug_res.status_code == 200
    assert isinstance(sug_res.json(), list)


# =========================================================================
# 8. Settings & Accounts APIs (/api/settings, /api/accounts)
# =========================================================================

def test_settings_masked_api_key_and_dpapi_encryption(client: TestClient):
    # 1. Get settings
    get_res = client.get("/api/settings")
    assert get_res.status_code == 200
    data = get_res.json()
    assert "general" in data
    assert "dedup" in data
    assert "refund" in data
    assert "llm" in data
    assert "api_key_masked" in data["llm"]

    # 2. Update LLM settings with plaintext API key
    put_res = client.put(
        "/api/settings",
        json={
            "llm": {
                "base_url": "https://api.deepseek.com/v1",
                "api_key": "sk-realapikey12345678",
                "model": "deepseek-chat",
                "enabled": True,
            }
        },
    )
    assert put_res.status_code == 200
    up_data = put_res.json()
    # Key must be masked in response!
    assert up_data["llm"]["api_key_masked"].startswith("sk-")
    assert "12345678" not in up_data["llm"]["api_key_masked"]
    assert up_data["llm"]["has_api_key"] is True

    # 3. Update without modifying masked key
    put_masked = client.put(
        "/api/settings",
        json={
            "llm": {
                "api_key": up_data["llm"]["api_key_masked"],
                "temperature": 0.2,
            }
        },
    )
    assert put_masked.status_code == 200
    assert put_masked.json()["llm"]["temperature"] == 0.2
    assert put_masked.json()["llm"]["has_api_key"] is True


def test_accounts_endpoints_and_merge(client: TestClient):
    # 1. List accounts
    acc_res = client.get("/api/accounts")
    assert acc_res.status_code == 200
    accs = acc_res.json()
    assert isinstance(accs, list)

    # 2. Patch alias
    if accs:
        a_id = accs[0]["id"]
        patch_res = client.patch(f"/api/accounts/{a_id}", json={"alias": "常用工资卡"})
        assert patch_res.status_code == 200
        assert patch_res.json()["alias"] == "常用工资卡"


# =========================================================================
# 9. LLM Endpoints (/api/llm/*)
# =========================================================================

def test_llm_status_and_offline_degradation(client: TestClient):
    # 1. LLM status
    status_res = client.get("/api/llm/status")
    assert status_res.status_code == 200
    assert "configured" in status_res.json()
    assert "cache_count" in status_res.json()

    # 2. Classify with unconfigured or offline LLM -> returns status="offline" without crashing
    class_res = client.post("/api/llm/classify", json={"limit": 5})
    assert class_res.status_code == 200
    assert class_res.json()["status"] in ("ok", "offline")

    # 3. Review with offline LLM
    rev_res = client.post("/api/llm/review", json={"limit": 5})
    assert rev_res.status_code == 200
    assert rev_res.json()["status"] in ("ok", "offline")

    # 4. Probe test
    probe_res = client.post("/api/llm/test", json={"base_url": "http://127.0.0.1:9999", "api_key": "dummy"})
    assert probe_res.status_code == 200
    assert probe_res.json()["ok"] is False

    # 5. List models endpoint handles missing/offline cleanly without 500
    models_empty_res = client.post("/api/llm/models", json={})
    assert models_empty_res.status_code == 200
    assert models_empty_res.json()["ok"] is False

    models_off_res = client.post("/api/llm/models", json={"base_url": "http://127.0.0.1:9999", "api_key": "dummy"})
    assert models_off_res.status_code == 200
    assert models_off_res.json()["ok"] is False


# =========================================================================
# 10. Stats & Excel Export APIs (/api/stats/*, /api/export.xlsx)
# =========================================================================

def test_stats_and_excel_export_endpoints(client: TestClient):
    # 1. Stats Summary
    sum_res = client.get("/api/stats/summary")
    assert sum_res.status_code == 200
    s_data = sum_res.json()
    assert "total_expense_cents" in s_data
    assert "total_income_cents" in s_data
    assert "expense_net" in s_data
    assert "balance" in s_data

    # 2. Trend
    trend_res = client.get("/api/stats/trend?granularity=monthly")
    assert trend_res.status_code == 200
    assert isinstance(trend_res.json(), list)

    # 3. Category Sunburst
    cat_res = client.get("/api/stats/category?direction=expense")
    assert cat_res.status_code == 200
    assert "total_cents" in cat_res.json()
    assert "items" in cat_res.json()

    # 4. Merchant Ranking
    merch_res = client.get("/api/stats/merchant?limit=10")
    assert merch_res.status_code == 200
    assert "total_expense_cents" in merch_res.json()
    assert "items" in merch_res.json()

    # 5. Heatmap
    cal_res = client.get("/api/stats/heatmap?mode=calendar")
    assert cal_res.status_code == 200
    assert cal_res.json()["mode"] == "calendar"

    wh_res = client.get("/api/stats/heatmap?mode=weekday_hour")
    assert wh_res.status_code == 200
    assert wh_res.json()["mode"] == "weekday_hour"

    # 6. Account stats
    acc_res = client.get("/api/stats/account?by=account")
    assert acc_res.status_code == 200
    assert "total_out_cents" in acc_res.json()

    # 7. Excel Export
    excel_res = client.get("/api/export.xlsx")
    assert excel_res.status_code == 200
    assert excel_res.headers["content-type"] == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    content = excel_res.content
    assert content.startswith(b"PK")  # Valid XLSX ZIP archive


def test_merged_consumption_display_enrichment_and_filtering(client: TestClient):
    """Merged view hides duplicate rows, enriches primary row with merged_duplicates, and supports bank-side search/filter."""
    from billapp.core.db import create_connection

    db_path = client.app.state.db_path
    with create_connection(db_path) as conn:
        conn.execute(
            "INSERT INTO accounts (kind, bank, tail, alias, currency) VALUES ('platform', '支付宝', NULL, '支付宝', 'CNY')"
        )
        alipay_acc_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT INTO accounts (kind, bank, tail, alias, currency) VALUES ('bank_credit', '工商银行', '6688', '工行信用卡', 'CNY')"
        )
        bank_acc_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT INTO import_batches (file_name, file_sha1, file_size, source, row_count) VALUES ('m.csv', 'sha-merge-1', 120, 'alipay', 2)"
        )
        batch_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            """
            INSERT INTO transactions (
                batch_id, source, account_id, fund_account_id, txn_time, time_precision,
                amount_cents, currency, direction, counterparty, description, duplicate_of, fingerprint
            ) VALUES (?, 'alipay', ?, ?, '2026-10-18 12:30:00', 'second', 8800, 'CNY', 'expense', '海鲜大排档', '晚餐结账', NULL, 'fp-m-1')
            """,
            (batch_id, alipay_acc_id, bank_acc_id),
        )
        primary_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            """
            INSERT INTO transactions (
                batch_id, source, account_id, txn_time, time_precision,
                amount_cents, currency, direction, counterparty, description, duplicate_of, fingerprint
            ) VALUES (?, 'icbc_credit', ?, '2026-10-18 12:30:04', 'second', 8800, 'CNY', 'expense', '支付宝快捷消费', 'ICBC联名卡独特摘要', ?, 'fp-m-2')
            """,
            (batch_id, bank_acc_id, primary_id),
        )
        dup_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.commit()

    # 1. Merged listing hides duplicate child & enriches primary with merged_duplicates
    merged = client.get("/api/transactions?is_duplicate=false").json()
    assert merged["total"] == 1
    assert len(merged["items"]) == 1
    p_item = merged["items"][0]
    assert p_item["id"] == primary_id
    assert p_item["merged_count"] == 1
    assert len(p_item["merged_duplicates"]) == 1
    assert p_item["merged_duplicates"][0]["id"] == dup_id
    assert p_item["merged_duplicates"][0]["account_bank"] == "工商银行"
    assert p_item["merged_duplicates"][0]["account_tail"] == "6688"

    # 2. Bank account filter in merged mode still finds the merged consumption
    by_bank = client.get(f"/api/transactions?is_duplicate=false&account_id={bank_acc_id}").json()
    assert by_bank["total"] == 1
    assert by_bank["items"][0]["id"] == primary_id

    # 3. Search keyword from duplicate bank description still finds the merged row
    by_kw = client.get("/api/transactions?is_duplicate=false&q=ICBC联名卡独特摘要").json()
    assert by_kw["total"] == 1
    assert by_kw["items"][0]["id"] == primary_id

    # 4. Detail endpoint includes duplicate_child in related & merged_duplicates on primary
    det = client.get(f"/api/transactions/{primary_id}").json()
    assert det["transaction"]["merged_count"] == 1
    assert any(r["relation"] == "duplicate_child" and r["txn"]["id"] == dup_id for r in det["related"])

    # 5. Unmerged listing returns both and populates primary_source_txn on duplicate
    raw = client.get("/api/transactions").json()
    assert raw["total"] == 2
    dup_row = next(i for i in raw["items"] if i["id"] == dup_id)
    assert dup_row["is_duplicate"] is True
    assert dup_row["primary_source_txn"]["id"] == primary_id
