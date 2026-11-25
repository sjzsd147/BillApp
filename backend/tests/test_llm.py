"""Hermetic unit tests for Milestone 4 LLM Infrastructure:
- Windows DPAPI & Secret Storage (secrets.py)
- Universal OpenAI-Compatible LLM Client via httpx.MockTransport (client.py)
- Zero-PII Sanitization & Prompt Templates (prompts.py)
- 5 Specialized LLM Tasks with Graceful Offline Degradation (tasks.py)
"""

import json
import sqlite3
import pytest
import httpx

from billapp.core.db import init_db
from billapp.core.secrets import (
    encrypt_secret,
    decrypt_secret,
    mask_secret,
    is_encrypted_secret,
    SecretDecryptionError,
)
from billapp.classify.categories import CategoryService
from billapp.llm.client import (
    LLMClient,
    LLMConfig,
    extract_json,
)
from billapp.llm.prompts import (
    sanitize_prompt_text,
    sanitize_transaction_for_llm,
    build_classify_fallback_prompt,
    build_dedup_arbitration_prompt,
    build_mapping_suggest_prompt,
    build_merchant_normalize_prompt,
    build_review_classification_prompt,
)
from billapp.llm.tasks import (
    classify_fallback_sync,
    review_classification_sync,
    dedup_arbitration_sync,
    merchant_normalize_sync,
    suggest_profile_mapping_sync,
)


@pytest.fixture
def db_conn() -> sqlite3.Connection:
    """In-memory SQLite connection with initialized schema."""
    conn = init_db(":memory:")
    CategoryService.init_builtin_categories(conn)
    yield conn
    conn.close()


# =========================================================================
# 1. DPAPI & Secrets Storage Tests (Feature 22 / secrets.py)
# =========================================================================

class TestDPAPISecretStorage:
    """Tests for DPAPI encryption, cross-platform fallback, and masking."""

    def test_dpapi_roundtrip(self):
        secret = "sk-test-live-key-1234567890abcdef"
        encrypted = encrypt_secret(secret)
        assert is_encrypted_secret(encrypted)
        assert encrypted != secret
        assert encrypted.startswith("dpapi:") or encrypted.startswith("encv1:")

        decrypted = decrypt_secret(encrypted)
        assert decrypted == secret

    def test_legacy_base64_migration_decrypt(self):
        import base64
        secret = "fallback-open-key-9999"
        legacy_stored = "b64:" + base64.b64encode(secret.encode("utf-8")).decode("ascii")
        assert is_encrypted_secret(legacy_stored)

        decrypted = decrypt_secret(legacy_stored)
        assert decrypted == secret

    def test_mask_secret(self):
        assert mask_secret("sk-proj-12345678abcdef") == "sk-****cdef"
        assert mask_secret("123456789") == "****6789"
        assert mask_secret("short") == "****"
        assert mask_secret("") == ""

    def test_corrupted_payload_handling(self):
        with pytest.raises(SecretDecryptionError):
            decrypt_secret("b64:invalid-base64-content!@#$%")

    def test_settings_db_persistence_with_dpapi(self, db_conn: sqlite3.Connection):
        config = LLMConfig(
            base_url="https://api.deepseek.com/v1",
            api_key="sk-real-secret-key-1234",
            model="deepseek-chat",
            enabled=True,
        )
        config.save_to_db(db_conn)

        # Inspect raw row in settings table: must be encrypted
        raw_val = db_conn.execute("SELECT value FROM settings WHERE key = 'llm_config'").fetchone()[0]
        data = json.loads(raw_val)
        assert "api_key" not in data or not data.get("api_key")
        assert "api_key_encrypted" in data
        assert is_encrypted_secret(data["api_key_encrypted"])
        assert "sk-real-secret-key-1234" not in raw_val

        # Load back from db: should transparently decrypt
        loaded = LLMConfig.load_from_db(db_conn)
        assert loaded.api_key == "sk-real-secret-key-1234"
        assert loaded.base_url == "https://api.deepseek.com/v1"
        assert loaded.enabled is True


# =========================================================================
# 2. Universal LLM Client & MockTransport Tests (Feature 22)
# =========================================================================

class TestUniversalLLMClient:
    """Hermetic tests for LLM client with caching, retry, and JSON parsing."""

    def test_url_normalization(self):
        assert LLMClient.normalize_url("https://api.deepseek.com") == "https://api.deepseek.com/v1/chat/completions"
        assert LLMClient.normalize_url("https://api.deepseek.com/v1") == "https://api.deepseek.com/v1/chat/completions"
        assert LLMClient.normalize_url("https://api.deepseek.com/v1/") == "https://api.deepseek.com/v1/chat/completions"
        assert LLMClient.normalize_url("http://localhost:11434/v1/chat/completions") == "http://localhost:11434/v1/chat/completions"

    def test_robust_json_extractor(self):
        # Direct JSON
        assert extract_json('{"status": "ok"}') == {"status": "ok"}

        # Markdown fenced JSON
        markdown_raw = "Here is the result:\n```json\n{\"answer\": 42}\n```\nHope it helps!"
        assert extract_json(markdown_raw) == {"answer": 42}

        # Trailing comma scrubbing
        trailing_comma = '{"items": [1, 2, ], "desc": "test", }'
        assert extract_json(trailing_comma) == {"items": [1, 2], "desc": "test"}

        # Array JSON
        assert extract_json('[{"id": 1}, {"id": 2}]') == [{"id": 1}, {"id": 2}]

    def test_client_chat_completion_success(self, db_conn: sqlite3.Connection):
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.headers.get("authorization") == "Bearer sk-test-key"
            payload = json.loads(request.content)
            assert payload["model"] == "deepseek-chat"
            return httpx.Response(
                status_code=200,
                json={
                    "choices": [{"message": {"content": '{"category": "food", "confidence": 0.95}'}}],
                    "usage": {"prompt_tokens": 15, "completion_tokens": 8},
                },
            )

        transport = httpx.MockTransport(handler)
        config = LLMConfig(
            base_url="https://api.test.com/v1",
            api_key="sk-test-key",
            model="deepseek-chat",
            enabled=True,
        )
        client = LLMClient(config=config, conn=db_conn, transport=transport)

        resp = client.chat_completion_sync("classify", [{"role": "user", "content": "麦当劳"}])
        assert resp == {"category": "food", "confidence": 0.95}
        assert client.total_requests == 1
        assert client.total_prompt_tokens == 15

    def test_client_sqlite_cache_hit(self, db_conn: sqlite3.Connection):
        request_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal request_count
            request_count += 1
            return httpx.Response(
                status_code=200,
                json={
                    "choices": [{"message": {"content": '{"cached_val": 100}'}}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                },
            )

        transport = httpx.MockTransport(handler)
        config = LLMConfig(base_url="https://api.test.com/v1", api_key="sk-key", enabled=True)
        client = LLMClient(config=config, conn=db_conn, transport=transport)

        # First call -> hits mock network
        res1 = client.chat_completion_sync("classify", [{"role": "user", "content": "同一提示词"}])
        assert res1 == {"cached_val": 100}
        assert request_count == 1

        # Second call -> must hit SQLite cache without calling network handler
        res2 = client.chat_completion_sync("classify", [{"role": "user", "content": "同一提示词"}])
        assert res2 == {"cached_val": 100}
        assert request_count == 1  # Network request count did NOT increase
        assert client.cache_hits == 1

    def test_client_exponential_backoff_and_recovery(self, db_conn: sqlite3.Connection):
        call_count = 0

        def flaky_handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return httpx.Response(status_code=500, text="Internal Server Error")
            return httpx.Response(
                status_code=200,
                json={"choices": [{"message": {"content": '{"recovered": true}'}}]},
            )

        transport = httpx.MockTransport(flaky_handler)
        config = LLMConfig(base_url="https://api.test.com/v1", api_key="sk-key", max_retries=2, enabled=True)
        client = LLMClient(config=config, conn=db_conn, transport=transport)

        resp = client.chat_completion_sync("classify", [{"role": "user", "content": "retry test"}])
        assert resp == {"recovered": True}
        assert call_count == 2

    def test_client_hard_auth_error_no_retry(self, db_conn: sqlite3.Connection):
        call_count = 0

        def auth_error_handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            return httpx.Response(status_code=401, text="Unauthorized: Invalid API key")

        transport = httpx.MockTransport(auth_error_handler)
        config = LLMConfig(base_url="https://api.test.com/v1", api_key="bad-key", max_retries=2, enabled=True)
        client = LLMClient(config=config, conn=db_conn, transport=transport)

        with pytest.raises(httpx.HTTPStatusError):
            client.chat_completion_sync("classify", [{"role": "user", "content": "test"}])

        assert call_count == 1  # Aborts immediately on 401 without wasting retries


# =========================================================================
# 3. Zero-PII Sanitization Tests (Feature 24 / prompts.py)
# =========================================================================

class TestZeroPIISanitization:
    """Verifies that prompts never leak card numbers, phone numbers, IDs, or holder names."""

    def test_sanitize_embedded_chinese_text(self):
        raw_text = "订单号2026101600000000000000000001银行卡6222021000123456789手机号13812345678身份证310115199001011234邮箱bill@example.com持有人张三"
        sanitized = sanitize_prompt_text(raw_text, holder_name="张三")

        assert "6222021000123456789" not in sanitized
        assert "13812345678" not in sanitized
        assert "310115199001011234" not in sanitized
        assert "bill@example.com" not in sanitized
        assert "2026101600000000000000000001" not in sanitized
        assert "张三" not in sanitized

        assert "[CARD]" in sanitized
        assert "[PHONE]" in sanitized
        assert "[ID_CARD]" in sanitized
        assert "[EMAIL]" in sanitized
        assert "[ORDER_ID]" in sanitized
        assert "[USER]" in sanitized

    def test_sanitize_transaction_for_llm(self):
        txn = {
            "id": 101,
            "counterparty": "麦当劳 张三 13912345678",
            "merchant_norm": "麦当劳",
            "description": "消费单号2026101600000000000000000001",
            "amount_cents": 2747,
            "direction": "expense",
            "platform_category": "餐饮美食",
        }
        res = sanitize_transaction_for_llm(txn, holder_name="张三")
        assert res["id"] == 101
        assert res["amount_yuan"] == 27.47
        assert "[USER]" in res["counterparty"]
        assert "[PHONE]" in res["counterparty"]
        assert "[ORDER_ID]" in res["description"]


# =========================================================================
# 4. Five Specialized LLM Tasks Tests (Feature 23 / tasks.py)
# =========================================================================

class TestFiveSpecializedLLMTasks:
    """Tests each of the 5 specialized tasks with mock transport and offline fallbacks."""

    def test_task1_classify_fallback(self, db_conn: sqlite3.Connection):
        key_map = {"food.dining": 10, "transport.taxi": 20}

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                status_code=200,
                json={
                    "results": [
                        {
                            "id": 1,
                            "category_key": "food",
                            "subcategory_key": "food.dining",
                            "confidence": 0.95,
                            "reason": "知名快餐",
                        },
                        {
                            "id": 2,
                            "category_key": "fake",
                            "subcategory_key": "fake.hallucinated",
                            "confidence": 0.99,
                            "reason": "幻觉分类",
                        },
                    ]
                },
            )

        client = LLMClient(
            config=LLMConfig(base_url="https://api.test.com/v1", api_key="sk-key", enabled=True),
            conn=db_conn,
            transport=httpx.MockTransport(handler),
        )

        txns = [
            {"id": 1, "counterparty": "老王大碗牛肉面", "amount_cents": 2500},
            {"id": 2, "counterparty": "某种未知消费", "amount_cents": 1000},
        ]

        results = classify_fallback_sync(txns, client, key_map)
        assert len(results) == 2
        # Item 1: Valid taxonomy match
        assert results[0].category_id == 10
        assert results[0].source == "llm"
        assert results[0].confidence == 0.95

        # Item 2: Hallucinated category key dropped to unclassified
        assert results[1].category_id is None
        assert results[1].source == "none"

    def test_task1_classify_fallback_offline_graceful_degradation(self, db_conn: sqlite3.Connection):
        # Client is disabled
        client = LLMClient(
            config=LLMConfig(enabled=False),
            conn=db_conn,
        )
        txns = [{"id": 1, "counterparty": "测试", "amount_cents": 500}]
        results = classify_fallback_sync(txns, client, {"food.dining": 10})
        assert len(results) == 1
        assert results[0].category_id is None
        assert results[0].source == "none"

    def test_task2_review_classification(self, db_conn: sqlite3.Connection):
        key_map = {"transport.taxi": 20}

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                status_code=200,
                json={
                    "reviews": [
                        {
                            "id": 1,
                            "agree": False,
                            "suggested_subcategory_key": "transport.taxi",
                            "confidence": 0.92,
                            "reason": "滴滴属于打车",
                        },
                        {
                            "id": 2,
                            "agree": True,
                            "confidence": 0.95,
                            "reason": "分类正确",
                        },
                    ]
                },
            )

        client = LLMClient(
            config=LLMConfig(base_url="https://api.test.com/v1", api_key="sk-key", enabled=True),
            conn=db_conn,
            transport=httpx.MockTransport(handler),
        )

        reviews = review_classification_sync([{"id": 1}, {"id": 2}], client, key_map)
        assert len(reviews) == 1
        assert reviews[0].id == 1
        assert reviews[0].suggested_category_id == 20
        assert reviews[0].confidence == 0.92

    def test_task3_dedup_arbitration(self, db_conn: sqlite3.Connection):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                status_code=200,
                json={
                    "verdict": "same",
                    "confidence": 0.94,
                    "reason": "金额尾号一致，支付宝麦当劳对应工行金拱门",
                },
            )

        client = LLMClient(
            config=LLMConfig(base_url="https://api.test.com/v1", api_key="sk-key", enabled=True),
            conn=db_conn,
            transport=httpx.MockTransport(handler),
        )

        p_txn = {"id": 1, "counterparty": "麦当劳", "amount_cents": 2747}
        s_txn = {"id": 2, "counterparty": "支付宝-金拱门（中国）有限公司", "amount_cents": 2747}

        arb = dedup_arbitration_sync(p_txn, s_txn, 0.78, client)
        assert arb.verdict == "same"
        assert arb.confidence == 0.94
        assert "金拱门" in arb.reason

    def test_task4_merchant_normalize(self, db_conn: sqlite3.Connection):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                status_code=200,
                json={
                    "normalized": {
                        "支付宝-金拱门（中国）有限公司": "麦当劳",
                        "财付通-北京三快在线科技有限公司": "美团外卖",
                    }
                },
            )

        client = LLMClient(
            config=LLMConfig(base_url="https://api.test.com/v1", api_key="sk-key", enabled=True),
            conn=db_conn,
            transport=httpx.MockTransport(handler),
        )

        norm = merchant_normalize_sync(["支付宝-金拱门（中国）有限公司", "财付通-北京三快在线科技有限公司"], client)
        assert norm["支付宝-金拱门（中国）有限公司"] == "麦当劳"
        assert norm["财付通-北京三快在线科技有限公司"] == "美团外卖"

    def test_task5_suggest_profile_mapping(self, db_conn: sqlite3.Connection):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                status_code=200,
                json={
                    "columns": {
                        "date": "交易日期",
                        "time": "交易时间",
                        "amount": "金额",
                        "counterparty": "对方户名",
                        "description": "摘要",
                        "fake_column": "不存在的列",
                    },
                    "amount_mode": "signed",
                    "confidence": 0.96,
                    "reason": "标准招商银行CSV表格",
                },
            )

        client = LLMClient(
            config=LLMConfig(base_url="https://api.test.com/v1", api_key="sk-key", enabled=True),
            conn=db_conn,
            transport=httpx.MockTransport(handler),
        )

        header = ["交易日期", "交易时间", "金额", "对方户名", "摘要", "余额"]
        sample_rows = [["2026-10-15", "12:00:00", "-50.00", "麦当劳", "午餐", "1000.00"]]

        res = suggest_profile_mapping_sync(header, sample_rows, client)
        assert res.amount_mode == "signed"
        assert res.confidence == 0.96
        assert res.columns["date"] == "交易日期"
        assert res.columns["amount"] == "金额"
        # Non-existent column dropped
        assert "fake_column" not in res.columns

    def test_list_models_normalizes_url_and_parses_openai_payload(self, db_conn: sqlite3.Connection):
        seen_urls: List[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen_urls.append(str(request.url))
            return httpx.Response(
                status_code=200,
                json={
                    "object": "list",
                    "data": [
                        {"id": "Qwen3.8-Flash", "owned_by": "qwen"},
                        {"id": "DeepSeek-V4-Pro", "owned_by": "deepseek"},
                        {"id": "Qwen3.8-Flash", "owned_by": "qwen"},  # duplicate should be deduped
                    ],
                },
            )

        assert LLMClient.normalize_models_url("http://localhost:8790/v1") == "http://localhost:8790/v1/models"
        assert LLMClient.normalize_models_url("https://api.openai.com") == "https://api.openai.com/v1/models"
        assert LLMClient.normalize_models_url("https://api.openai.com/v1/chat/completions") == "https://api.openai.com/v1/models"

        client = LLMClient(
            config=LLMConfig(base_url="http://localhost:8790/v1", api_key="sk-key", enabled=True),
            conn=db_conn,
            transport=httpx.MockTransport(handler),
        )
        res = client.list_models()
        assert res["ok"] is True
        assert [m["id"] for m in res["models"]] == ["Qwen3.8-Flash", "DeepSeek-V4-Pro"]
        assert seen_urls == ["http://localhost:8790/v1/models"]


class TestRegressionIssueFixes:
    def test_classify_handles_think_tags_string_ids_root_fallback_and_custom_categories(
        self, db_conn: sqlite3.Connection
    ):
        db_conn.execute(
            "INSERT INTO categories (parent_id, key, name, direction) VALUES (NULL, 'custom_pet', '宠物花销', 'expense')"
        )
        db_conn.commit()
        cat_rows = db_conn.execute("SELECT id, key, name FROM categories").fetchall()
        key_to_id = {r["key"]: r["id"] for r in cat_rows if r["key"]}
        name_to_id = {r["name"]: r["id"] for r in cat_rows if r["name"]}

        def handler(request: httpx.Request) -> httpx.Response:
            payload = {
                "results": [
                    {
                        "id": "101",
                        "category_key": "food.hallucinated_leaf",
                        "confidence": 92,
                        "reason": "餐饮大类兜底",
                    },
                    {
                        "id": "102",
                        "category_key": "custom_pet",
                        "confidence": 0.88,
                        "reason": "自定义分类命中",
                    },
                ]
            }
            content = "<think>internal reasoning trace</think>\n```json\n" + json.dumps(payload, ensure_ascii=False) + "\n```"
            return httpx.Response(
                status_code=200,
                json={"choices": [{"message": {"content": content}}]},
            )

        client = LLMClient(
            config=LLMConfig(base_url="https://api.test.com/v1", api_key="sk-key", enabled=True),
            conn=db_conn,
            transport=httpx.MockTransport(handler),
        )
        txns = [
            {"id": 101, "counterparty": "未知小店", "description": "午饭", "amount_cents": 2800, "direction": "expense"},
            {"id": 102, "counterparty": "萌宠医院", "description": "猫粮", "amount_cents": 19900, "direction": "expense"},
        ]
        results = classify_fallback_sync(
            txns,
            client,
            key_to_id=key_to_id,
            name_to_id=name_to_id,
            raise_on_error=True,
        )
        assert len(results) == 2
        assert results[0].category_id == key_to_id["food"]
        assert results[0].confidence == pytest.approx(0.92)
        assert results[1].category_id == key_to_id["custom_pet"]
        assert results[1].confidence == pytest.approx(0.88)

    def test_import_raw_rows_suggest_mapping_classify_and_unclassified_filter(self, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient
        import openpyxl
        from billapp.app import create_app
        from billapp.core.db import create_connection
        import billapp.api.llm as llm_api
        import billapp.api.profiles as profiles_api

        db_path = tmp_path / "regression_issues.db"
        with TestClient(create_app(db_path=str(db_path))) as client:
            # 1) Unrecognized GBK CSV -> preview returns raw_rows & extract-raw works
            gbk_csv = (
                "自定义账单导出\n"
                "记账日期,商户名称,支出金额,收入金额,资金备注\n"
                "2026-10-01,楼下牛肉面,26.50,0,工作餐\n"
                "2026-10-02,报销打款,0,150.00,差旅补贴\n"
            ).encode("gb18030")
            prev = client.post(
                "/api/import/preview",
                files={"file": ("unknown_bank.csv", gbk_csv, "text/csv")},
            )
            assert prev.status_code == 200
            pbody = prev.json()
            assert pbody["need_mapping"] is True
            assert pbody["detected_file_type"] == "csv"
            assert any("商户名称" in row for row in pbody["raw_rows"])
            assert any("楼下牛肉面" in row for row in pbody["raw_rows"])

            ext = client.post(
                "/api/import/extract-raw",
                files={"file": ("unknown_bank.csv", gbk_csv, "text/csv")},
            )
            assert ext.status_code == 200
            ebody = ext.json()
            assert ebody["file_type"] == "csv"
            assert any("楼下牛肉面" in row for row in ebody["raw_rows"])

            # 2) Unrecognized XLSX -> preview returns decoded raw_rows
            wb = openpyxl.Workbook()
            ws = wb.active
            ws.append(["流水日期", "对方", "发生额", "摘要"])
            ws.append(["2026-10-03", "地铁出行", "-5.00", "通勤"])
            xlsx_path = tmp_path / "unknown.xlsx"
            wb.save(xlsx_path)
            prev_xlsx = client.post(
                "/api/import/preview",
                files={
                    "file": (
                        "unknown.xlsx",
                        xlsx_path.read_bytes(),
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    )
                },
            )
            assert prev_xlsx.status_code == 200
            px = prev_xlsx.json()
            assert px["need_mapping"] is True
            assert px["detected_file_type"] == "xlsx"
            assert px["raw_rows"][0] == ["流水日期", "对方", "发生额", "摘要"]
            assert px["raw_rows"][1][1] == "地铁出行"

            # 3) /api/profiles/suggest reports offline when disabled
            off_resp = client.post(
                "/api/profiles/suggest",
                json={
                    "headers": ["记账日期", "商户名称", "支出金额", "收入金额", "资金备注"],
                    "sample_rows": [["2026-10-01", "楼下牛肉面", "26.50", "0", "工作餐"]],
                    "file_type": "csv",
                },
            )
            assert off_resp.status_code == 200
            assert off_resp.json()["llm_status"] == "offline"
            assert off_resp.json()["source"] == "heuristic"

            # Enable LLM with mock HTTP handler
            client.put(
                "/api/settings",
                json={
                    "llm": {
                        "enabled": True,
                        "base_url": "https://mock.llm/v1",
                        "api_key": "sk-test",
                        "model": "mock-model",
                        "task_classify_enabled": False,
                    }
                },
            )

            # 4) /api/llm/classify returns status="disabled" when task toggle is off
            dis_resp = client.post("/api/llm/classify", json={"limit": 10})
            assert dis_resp.status_code == 200
            assert dis_resp.json()["success"] is False
            assert dis_resp.json()["status"] == "disabled"

            client.put(
                "/api/settings",
                json={
                    "llm": {
                        "enabled": True,
                        "base_url": "https://mock.llm/v1",
                        "api_key": "sk-test",
                        "model": "mock-model",
                        "task_classify_enabled": True,
                    }
                },
            )

            def suggest_handler(request: httpx.Request) -> httpx.Response:
                body_str = request.content.decode("utf-8")
                # Preamble personal name & card digits must be masked while preserving bank title
                if "账单抬头摘要" in body_str:
                    assert "测试银行流水" in body_str
                    assert "6214" not in body_str
                    assert "李某人" not in body_str
                return httpx.Response(
                    status_code=200,
                    json={
                        "columns": {
                            "date": "记账日期",
                            "counterparty": "商户名称",
                            "expense": "支出金额",
                            "income": "收入金额",
                            "description": "资金备注",
                        },
                        "amount_mode": "split",
                        "bank": "本地测试银行",
                        "account_kind": "debit_card",
                        "confidence": 0.95,
                        "reason": "拆分收支识别",
                    },
                )

            captured_timeouts = []

            def _make_suggest_client(config, conn=None):
                captured_timeouts.append(config.timeout_sec)
                return LLMClient(
                    config=config,
                    conn=conn,
                    transport=httpx.MockTransport(suggest_handler),
                )

            monkeypatch.setattr(profiles_api, "LLMClient", _make_suggest_client)
            sug = client.post(
                "/api/profiles/suggest",
                json={
                    "headers": ["记账日期", "商户名称", "支出金额", "收入金额", "资金备注"],
                    "sample_rows": [["2026-10-01", "楼下牛肉面", "26.50", "0", "工作餐"]],
                    "preamble_rows": [["测试银行流水"], ["户名：某测试用户", "账号：6214000000001234"]],
                    "file_name": "测试银行20261114.pdf",
                    "file_type": "pdf",
                },
            )
            assert sug.status_code == 200
            assert captured_timeouts and captured_timeouts[0] >= 60.0
            sbody = sug.json()
            assert sbody["source"] == "llm"
            assert sbody["llm_status"] == "ok"
            assert sbody["amount_mode"] == "split"
            assert sbody["column_mapping"]["expense_amount"] == "支出金额"
            assert sbody["column_mapping"]["income_amount"] == "收入金额"
            assert sbody["bank"] == "本地测试银行"

            # 5) Error propagation on /api/llm/classify & unclassified filter on /api/transactions
            with create_connection(db_path) as conn:
                conn.execute("INSERT INTO accounts (kind, bank, tail, alias, currency) VALUES ('bank_debit', '测试银行', '0001', '测试卡', 'CNY')")
                acct_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                conn.execute(
                    "INSERT INTO import_batches (file_name, file_sha1, file_size, source, row_count) VALUES ('t.csv', 'sha-test-1', 100, 'manual', 2)"
                )
                batch_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                food_id = conn.execute("SELECT id FROM categories WHERE key = 'food.dining'").fetchone()[0]
                conn.execute(
                    """
                    INSERT INTO transactions (batch_id, source, account_id, txn_time, time_precision, amount_cents, currency, direction, counterparty, description, category_id, category_source, fingerprint)
                    VALUES (?, 'manual', ?, '2026-10-01 12:00:00', 'second', 2500, 'CNY', 'expense', '无名早点', '包子', NULL, NULL, 'fp-unclass-1')
                    """,
                    (batch_id, acct_id),
                )
                unclass_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                conn.execute(
                    """
                    INSERT INTO transactions (batch_id, source, account_id, txn_time, time_precision, amount_cents, currency, direction, counterparty, description, category_id, category_source, fingerprint)
                    VALUES (?, 'manual', ?, '2026-10-01 13:00:00', 'second', 3000, 'CNY', 'expense', '肯德基', '汉堡', ?, 'builtin_rule', 'fp-class-2')
                    """,
                    (batch_id, acct_id, food_id),
                )
                conn.commit()

            unclass_list = client.get("/api/transactions?unclassified=true")
            assert unclass_list.status_code == 200
            uids = [item["id"] for item in unclass_list.json()["items"]]
            assert uids == [unclass_id]

            def fail_handler(request: httpx.Request) -> httpx.Response:
                return httpx.Response(status_code=500, json={"error": {"message": "Upstream boom"}})

            monkeypatch.setattr(
                llm_api,
                "LLMClient",
                lambda config, conn=None: LLMClient(
                    config=config,
                    conn=conn,
                    transport=httpx.MockTransport(fail_handler),
                ),
            )
            err_cls = client.post("/api/llm/classify", json={"transaction_ids": [unclass_id], "update_db": True})
            assert err_cls.status_code == 200
            assert err_cls.json()["success"] is False
            assert err_cls.json()["status"] == "error"
            assert "500" in (err_cls.json()["reason"] or "")

            def ok_handler(request: httpx.Request) -> httpx.Response:
                return httpx.Response(
                    status_code=200,
                    json={"results": [{"id": str(unclass_id), "category_key": "food.dining", "confidence": 0.94, "reason": "早餐支出"}]},
                )

            monkeypatch.setattr(
                llm_api,
                "LLMClient",
                lambda config, conn=None: LLMClient(
                    config=config,
                    conn=conn,
                    transport=httpx.MockTransport(ok_handler),
                ),
            )
            ok_cls = client.post("/api/llm/classify", json={"transaction_ids": [unclass_id], "update_db": True})
            assert ok_cls.status_code == 200
            ob = ok_cls.json()
            assert ob["success"] is True
            assert ob["status"] == "ok"
            assert ob["classified_count"] == 1
            assert ob["results"][0]["category_key"] == "food.dining"

            after_unclass = client.get("/api/transactions?unclassified=true")
            assert after_unclass.json()["total"] == 0

    def test_top_preamble_card_tail_and_zero_pii_masking(self):
        from billapp.llm.prompts import _mask_preamble_cell, prepare_statement_lines_for_llm
        from billapp.importers.profile_engine import extract_preamble_metadata_fallback

        masked_name = _mask_preamble_cell("户\xa0\xa0名：张三")
        assert "张三" not in masked_name
        assert "户" in masked_name

        masked_acct = _mask_preamble_cell("账号：6214********1234")
        assert "6214" not in masked_acct
        assert "1234" in masked_acct

        raw_lines = [
            "招商银行交易流水",
            "2026-10-12 -- 2026-11-12",
            "户名：张三  账号：6214********1234  身份证：110101199001011234",
            "手机：13800138000",
            "2026-10-13 | CNY | -1000.00 | 转账汇款 | 张三",
        ]
        fb = extract_preamble_metadata_fallback("\n".join(raw_lines))
        assert fb["bank"] == "招商银行"
        assert fb["card_tail"] == "1234"
        assert fb["holder_name"] == "张三"
        assert fb["period_start"] == "2026-10-12"
        assert fb["period_end"] == "2026-11-12"

        sanitized = prepare_statement_lines_for_llm(raw_lines, local_holder="张三")
        joined = "\n".join(sanitized)
        assert "张三" not in joined
        assert "6214" not in joined
        assert "110101199001011234" not in joined
        assert "13800138000" not in joined
        assert "[USER]" in joined
        assert "1234" in joined
        assert "-1000.00" in joined

    def test_recognize_statement_account_and_full_llm_takeover_sync(self, db_conn):
        from billapp.llm.tasks import parse_statement_with_llm_sync, recognize_statement_account_sync

        captured_bodies = []

        def handler(request: httpx.Request) -> httpx.Response:
            body_str = request.content.decode("utf-8")
            captured_bodies.append(body_str)
            assert "6214" not in body_str
            assert "张三" not in body_str
            assert "110101199001011234" not in body_str
            assert "13800138000" not in body_str

            if "账户抬头识别专家" in body_str:
                return httpx.Response(
                    status_code=200,
                    json={
                        "bank": "招商银行",
                        "account_kind": "bank_debit",
                        "card_tail": "1234",
                        "currency": "CNY",
                        "confidence": 0.98,
                    },
                )
            return httpx.Response(
                status_code=200,
                json={
                    "account": {
                        "bank": "招商银行",
                        "account_kind": "bank_debit",
                        "card_tail": "1234",
                        "currency": "CNY",
                    },
                    "period": {"start": "2026-10-12", "end": "2026-11-12"},
                    "transactions": [
                        {
                            "txn_time": "2026-10-13",
                            "amount": 1000,
                            "direction": "expense",
                            "currency": "CNY",
                            "counterparty": "[USER]",
                            "description": "转账汇款",
                            "channel": None,
                            "card_tail": None,
                        },
                        {
                            "txn_time": "2026-10-15",
                            "amount": -28.5,
                            "currency": "CNY",
                            "counterparty": "星巴克咖啡",
                            "description": "快捷支付",
                            "channel": "alipay",
                            "card_tail": "1234",
                        },
                    ],
                },
            )

        cfg = LLMConfig(enabled=True, base_url="https://mock.llm/v1", api_key="sk-test", model="mock-model")
        client = LLMClient(config=cfg, conn=db_conn, transport=httpx.MockTransport(handler))

        rec = recognize_statement_account_sync(
            [["招商银行交易流水"], ["户名：张三", "账号：6214********1234"]],
            client,
            file_name="招商银行交易流水.pdf",
        )
        assert rec.used_llm is True
        assert rec.bank == "招商银行"
        assert rec.card_tail == "1234"
        assert rec.holder_name == "张三"

        parse_result, meta = parse_statement_with_llm_sync(
            raw_lines=[
                "招商银行交易流水",
                "2026-10-12 -- 2026-11-12",
                "户名：张三  账号：6214********1234  身份证：110101199001011234",
                "手机：13800138000",
                "2026-10-13 | CNY | -1000.00 | 转账汇款 | 张三",
                "2026-10-15 | CNY | -28.50 | 快捷支付 | 星巴克咖啡",
            ],
            client=client,
            file_name="招商银行交易流水.pdf",
        )
        assert len(captured_bodies) == 2
        assert meta["bank"] == "招商银行"
        assert meta["card_tail"] == "1234"
        assert meta["holder_name"] == "张三"
        assert parse_result.period == ("2026-10-12", "2026-11-12")
        assert parse_result.accounts[0].bank == "招商银行"
        assert parse_result.accounts[0].tail == "1234"
        assert len(parse_result.transactions) == 2
        # Integer yuan `1000` must become 100000 cents, NOT 1000 cents!
        assert parse_result.transactions[0].amount_cents == 100000
        assert parse_result.transactions[0].counterparty == "张三"
        assert parse_result.transactions[0].pay_method == "招商银行(1234)"
        assert parse_result.transactions[1].amount_cents == 2850
        assert parse_result.transactions[1].direction == "expense"

    def test_import_preview_use_llm_and_commit_overrides_e2e(self, tmp_path, monkeypatch):
        from pathlib import Path
        from fastapi.testclient import TestClient
        from billapp.app import create_app
        from billapp.core.db import create_connection
        import billapp.api.import_ as import_api
        import billapp.api.profiles as profiles_api

        db_path = tmp_path / "full_parse_e2e.db"
        with TestClient(create_app(db_path=str(db_path))) as client:
            client.put(
                "/api/settings",
                json={
                    "llm": {
                        "enabled": True,
                        "base_url": "https://mock.llm/v1",
                        "api_key": "sk-test",
                        "model": "mock-model",
                        "task_suggest_profile_enabled": True,
                        "task_full_parse_enabled": True,
                    }
                },
            )

            def mock_handler(request: httpx.Request) -> httpx.Response:
                body_str = request.content.decode("utf-8")
                if "列映射" in body_str:
                    return httpx.Response(
                        status_code=200,
                        json={
                            "columns": {"date": "记账日期", "amount": "交易金额", "description": "交易摘要", "counterparty": "对手信息"},
                            "amount_mode": "signed",
                            "bank": "招商银行",
                            "account_kind": "debit",
                            "card_tail": "1234",
                            "currency": "CNY",
                            "confidence": 0.96,
                            "reason": "CMB映射",
                        },
                    )
                return httpx.Response(
                    status_code=200,
                    json={
                        "account": {
                            "bank": "招商银行",
                            "account_kind": "bank_debit",
                            "card_tail": "1234",
                            "currency": "CNY",
                        },
                        "period": {"start": "2026-10-12", "end": "2026-11-12"},
                        "transactions": [
                            {
                                "txn_time": "2026-10-13",
                                "amount": 100.50,
                                "direction": "expense",
                                "currency": "CNY",
                                "counterparty": "瑞幸咖啡",
                                "description": "快捷支付",
                                "channel": "alipay",
                                "card_tail": None,
                            }
                        ],
                    },
                )

            def _mock_client(config, conn=None, **kwargs):
                return LLMClient(config=config, conn=conn, transport=httpx.MockTransport(mock_handler), **kwargs)

            monkeypatch.setattr(import_api, "LLMClient", _mock_client)
            monkeypatch.setattr(profiles_api, "LLMClient", _mock_client)

            # 1) Unstructured/noisy statement triggers auto LLM full takeover without use_llm=true
            noisy_txt = (
                "招商银行交易流水\n"
                "户名：张三 账号：6214********1234\n"
                "2026-10-13 消费 -100.50 瑞幸咖啡\n"
            ).encode("utf-8")
            auto_prev = client.post(
                "/api/import/preview",
                files={"file": ("noisy_cmb.txt", noisy_txt, "text/plain")},
            )
            assert auto_prev.status_code == 200
            ap = auto_prev.json()
            assert ap["need_mapping"] is False
            assert ap["source"] == "llm_statement"
            assert ap["total_rows"] == 1
            assert ap["accounts"][0]["bank"] == "招商银行"
            assert ap["accounts"][0]["tail"] == "1234"

            # 2) Explicit commit with user account_overrides updates account & pay_method in DB
            commit_res = client.post(
                "/api/import/commit",
                json={
                    "token": ap["token"],
                    "account_overrides": {"bank": "招商银行", "tail": "9988", "holder": "张三"},
                },
            )
            assert commit_res.status_code == 200
            assert commit_res.json()["imported_count"] == 1
            with create_connection(db_path) as conn:
                acc_row = conn.execute("SELECT bank, tail, holder FROM accounts").fetchone()
                txn_row = conn.execute("SELECT pay_method FROM transactions").fetchone()
                assert acc_row["bank"] == "招商银行"
                assert acc_row["tail"] == "9988"
                assert acc_row["holder"] == "张三"
                assert txn_row["pay_method"] == "招商银行(9988)"

            # 3) Synthetic CMB PDF with top-header account recognition & full LLM takeover
            import pymupdf

            doc = pymupdf.open()
            page = doc.new_page()
            font_name = "china-ss"
            page.insert_text((50, 60), "招商银行交易流水", fontsize=14, fontname=font_name)
            page.insert_text((50, 90), "户  名：张三  账号：6214********1234", fontsize=11, fontname=font_name)
            page.insert_text((50, 120), "2026-10-13  CNY  -100.50  瑞幸咖啡", fontsize=11, fontname=font_name)
            pdf_bytes = doc.tobytes()
            doc.close()

            prev_pdf = client.post(
                "/api/import/preview",
                files={"file": ("synthetic_cmb.pdf", pdf_bytes, "application/pdf")},
                data={"use_llm": "true"},
            )
            assert prev_pdf.status_code == 200
            pp = prev_pdf.json()
            assert pp["source"] == "llm_statement"
            assert pp["accounts"][0]["bank"] == "招商银行"
            assert pp["accounts"][0]["tail"] == "1234"
            assert pp["accounts"][0]["holder"] == "张三"

            sug = client.post(
                "/api/profiles/suggest",
                json={
                    "headers": ["记账日期", "货币", "交易金额", "联机余额", "交易摘要", "对手信息"],
                    "preamble_rows": [["招商银行交易流水"], ["户\xa0\xa0名：张三", "账号：6214********1234"]],
                    "file_name": "synthetic_cmb.pdf",
                },
            )
            assert sug.status_code == 200
            sb = sug.json()
            assert sb["bank"] == "招商银行"
            assert sb["card_tail"] == "1234"
