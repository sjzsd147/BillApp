"""Adversarial stress-testing suite for Milestone 4:
P4 Classification Engine & Universal LLM Pipeline (Features 22, 23, 24).

Adversarial Stress Vectors Tested:
1. Zero-PII Lookaround Penetration & False Positive Boundary Stress
2. Network Resilience, Exponential Backoff Simulation & Malformed JSON Responses
3. SQLite LLM Cache Concurrency, Key Collisions & Table Corruption Integrity
4. 5 Specialized LLM Tasks Offline Degradation & Schema Conformance Invariants
5. Windows DPAPI Corrupted Ciphertexts, Tag Invalidation & Safe Settings Loading
"""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
from typing import Any, Dict, List, Optional
import httpx
import pytest

from billapp.core.db import init_db
from billapp.core.secrets import (
    SecretDecryptionError,
    decrypt_secret,
    encrypt_secret,
    is_encrypted_secret,
    mask_secret,
)
from billapp.classify.categories import CategoryService
from billapp.llm.client import (
    LLMClient,
    LLMConfig,
    extract_json,
)
from billapp.llm.prompts import (
    build_classify_fallback_prompt,
    build_dedup_arbitration_prompt,
    build_mapping_suggest_prompt,
    build_merchant_normalize_prompt,
    build_review_classification_prompt,
    sanitize_prompt_text,
    sanitize_transaction_for_llm,
)
from billapp.llm.tasks import (
    ArbitrationResult,
    ClassificationResult,
    ProfileSuggestionResult,
    ReviewResult,
    classify_fallback_async,
    classify_fallback_sync,
    dedup_arbitration_async,
    dedup_arbitration_sync,
    merchant_normalize_async,
    merchant_normalize_sync,
    review_classification_async,
    review_classification_sync,
    suggest_profile_mapping_async,
    suggest_profile_mapping_sync,
)


@pytest.fixture
def db_conn() -> sqlite3.Connection:
    """In-memory SQLite connection with initialized schema and categories."""
    conn = init_db(":memory:")
    CategoryService.init_builtin_categories(conn)
    yield conn
    conn.close()


# =========================================================================
# 1. Zero-PII Lookaround Penetration & False Positive Stress
# =========================================================================

class TestAdversarialZeroPIILookaroundAndFalsePositives:
    """Adversarial penetration tests for Zero-PII sanitization and lookaround boundaries."""

    def test_chinese_embedded_card_numbers_and_tail_preservation(self):
        """Card numbers (15-19 digits) must be redacted while 4-digit tails are preserved."""
        # 19 digits UnionPay embedded in Chinese
        assert sanitize_prompt_text("卡号6222021000123456789支付") == "卡号[CARD]支付"

        # 4-digit tail with full card number in parentheses: tail must be preserved, full card redacted
        assert sanitize_prompt_text("尾号8801(6222081234567890123)") == "尾号8801([CARD])"

        # Short 4-digit tails separated by slash: must NOT be redacted
        assert sanitize_prompt_text("工行8801/建行5678") == "工行8801/建行5678"

        # 16 digits Visa/Mastercard embedded
        assert sanitize_prompt_text("招商银行信用卡4111111111111111消费扣款") == "招商银行信用卡[CARD]消费扣款"

        # 15 digits American Express
        assert sanitize_prompt_text("运通卡378282246310005快捷支付") == "运通卡[CARD]快捷支付"

    def test_chinese_citizen_id_cards_adversarial(self):
        """18-char Chinese ID cards ending in digit or X/x must be redacted to [ID_CARD]."""
        # 18 digits ending in digit
        assert sanitize_prompt_text("110101199003072345") == "[ID_CARD]"

        # 18 chars ending in upper X
        assert sanitize_prompt_text("11010119900307234X") == "[ID_CARD]"

        # 18 chars ending in lower x
        assert sanitize_prompt_text("11010119900307234x") == "[ID_CARD]"

        # Embedded in Chinese sentence
        text = "用户实名信息：张三（身份证11010119900307234X）已完成核验"
        assert sanitize_prompt_text(text, holder_name="张三") == "用户实名信息：[USER]（身份证[ID_CARD]）已完成核验"

    def test_chinese_mobile_phones_adversarial(self):
        """11-digit mobile numbers embedded in text and formats must be sanitized."""
        # Standard mobile in Chinese
        assert sanitize_prompt_text("充值手机号13812345678话费") == "充值手机号[PHONE]话费"

        # Slashed dual phones
        assert sanitize_prompt_text("13812345678/13987654321") == "[PHONE]/[PHONE]"

        # International prefixes
        assert sanitize_prompt_text("联系方式+8613812345678") == "联系方式[PHONE]"
        assert sanitize_prompt_text("联系方式+86-13812345678") == "联系方式[PHONE]"
        assert sanitize_prompt_text("联系方式+86 13812345678") == "联系方式[PHONE]"

    def test_order_ids_and_alphanumerics(self):
        """Order IDs (20-32 digits) must be redacted to [ORDER_ID]."""
        # 28 digits (Alipay/WeChat standard order length)
        assert sanitize_prompt_text("2023111022001412341412345678") == "[ORDER_ID]"

        # Minimum 20 digits
        assert sanitize_prompt_text("12345678901234567890") == "[ORDER_ID]"

        # Maximum 32 digits
        assert sanitize_prompt_text("12345678901234567890123456789012") == "[ORDER_ID]"

        # Embedded with alphanumeric prefix
        assert sanitize_prompt_text("商户订单号T2023111022001412341412345678交易成功") == "商户订单号T[ORDER_ID]交易成功"

    def test_emails_and_holder_names_sanitization(self):
        """Emails and holder names must be redacted without corrupting text."""
        # Standard email addresses separated by punctuation/space
        assert sanitize_prompt_text("联系邮箱: bill.app.service@example.com.cn 请查收") == "联系邮箱: [EMAIL] 请查收"
        assert sanitize_prompt_text("抄送: user_test@sub.domain.org 和 finance-dept@company.com") == "抄送: [EMAIL] 和 [EMAIL]"

        # Holder name masking
        assert sanitize_prompt_text("转账给王五，备注还款", holder_name="王五") == "转账给[USER]，备注还款"

        # None or empty holder name does not corrupt string
        assert sanitize_prompt_text("王五的账单", holder_name=None) == "王五的账单"
        assert sanitize_prompt_text("王五的账单", holder_name="") == "王五的账单"
        assert sanitize_prompt_text("王五的账单", holder_name="   ") == "王五的账单"

    def test_vulnerability_contiguous_chinese_email_swallowing(self):
        r"""Vulnerability Investigation:
        re.sub(r'[\w.-]+@[\w.-]+\.\w+', '[EMAIL]', text) matches Unicode \w,
        which greedily swallows contiguous Chinese ideographs before '@' and after '.domain'.
        """
        raw = "发票至service@example.com请查收"
        sanitized = sanitize_prompt_text(raw)
        # Expected Zero-PII contract: Chinese context must be preserved, only email redacted
        assert sanitized == "发票至[EMAIL]请查收", (
            f"VULNERABILITY REPRODUCED: Contiguous Chinese characters swallowed by Unicode \\w. "
            f"Expected '发票至[EMAIL]请查收', got {sanitized!r}"
        )


    def test_non_pii_boundary_preservation_stress(self):
        """Crucial False Positive Stress: non-PII financial fields must NOT be corrupted."""
        # Dates must remain untouched
        assert sanitize_prompt_text("2023-11-10") == "2023-11-10"
        assert sanitize_prompt_text("2024/05/20") == "2024/05/20"
        assert sanitize_prompt_text("2025.12.31") == "2025.12.31"

        # Timestamps must remain untouched
        assert sanitize_prompt_text("2023-11-10 12:30:45") == "2023-11-10 12:30:45"
        assert sanitize_prompt_text("2024-01-01T08:15:30Z") == "2024-01-01T08:15:30Z"

        # Amounts must remain untouched
        assert sanitize_prompt_text("100.00") == "100.00"
        assert sanitize_prompt_text("99999.99") == "99999.99"
        assert sanitize_prompt_text("0.01") == "0.01"
        assert sanitize_prompt_text("消费￥500.00元") == "消费￥500.00元"
        assert sanitize_prompt_text("-34.50") == "-34.50"

        # Merchant brand names with digits must remain untouched
        assert sanitize_prompt_text("7-Eleven便利店") == "7-Eleven便利店"
        assert sanitize_prompt_text("1点点奶茶") == "1点点奶茶"
        assert sanitize_prompt_text("361度官方旗舰店") == "361度官方旗舰店"
        assert sanitize_prompt_text("999感冒灵颗粒") == "999感冒灵颗粒"
        assert sanitize_prompt_text("85度C咖啡烘焙") == "85度C咖啡烘焙"

        # Short internal IDs must remain untouched
        assert sanitize_prompt_text("交易编号101") == "交易编号101"
        assert sanitize_prompt_text("流水号9999") == "流水号9999"

    def test_sanitize_transaction_for_llm_extreme_cases(self):
        """Sanitizes dicts and objects with missing, None, and edge-case values."""
        # Dict with None values
        txn_none = {
            "id": 999,
            "counterparty": None,
            "merchant_norm": None,
            "description": None,
            "platform_category": None,
            "amount_cents": None,
            "direction": None,
        }
        res = sanitize_transaction_for_llm(txn_none)
        assert res["id"] == 999
        assert res["counterparty"] == ""
        assert res["merchant_norm"] == ""
        assert res["description"] == ""
        assert res["amount_yuan"] == 0.0
        assert res["direction"] == "expense"

        # Emojis and unicode
        txn_emoji = {
            "id": 1,
            "counterparty": "🍔 麦当劳 🍟 (卡号6222081234567890123)",
            "amount_cents": 3500,
            "description": "午餐 🎉",
        }
        res_emoji = sanitize_transaction_for_llm(txn_emoji)
        assert "🍔 麦当劳 🍟" in res_emoji["counterparty"]
        assert "[CARD]" in res_emoji["counterparty"]
        assert "6222081234567890123" not in res_emoji["counterparty"]
        assert res_emoji["description"] == "午餐 🎉"
        assert res_emoji["amount_yuan"] == 35.0


# =========================================================================
# 2. Network Resilience, Backoff & Malformed JSON Responses
# =========================================================================

class TestAdversarialNetworkResilienceAndBackoff:
    """Stress tests network timeouts, HTTP retry codes, hard auth errors, and JSON extraction."""

    def test_transient_error_http_429_exponential_backoff(self, db_conn: sqlite3.Connection):
        """HTTP 429 Too Many Requests must back off and retry until success."""
        attempts = 0

        def rate_limited_handler(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            if attempts <= 2:
                return httpx.Response(status_code=429, text="Rate limit exceeded")
            return httpx.Response(
                status_code=200,
                json={"choices": [{"message": {"content": '{"rate_limit_recovered": true}'}}]},
            )

        client = LLMClient(
            config=LLMConfig(base_url="https://api.test.com/v1", api_key="sk-test", max_retries=2, enabled=True),
            conn=db_conn,
            transport=httpx.MockTransport(rate_limited_handler),
        )

        resp = client.chat_completion_sync("classify", [{"role": "user", "content": "ping"}])
        assert resp == {"rate_limit_recovered": True}
        assert attempts == 3  # 1 initial + 2 retries
        assert client.total_requests == 3

    def test_transient_error_http_500_502_503_recovery(self, db_conn: sqlite3.Connection):
        """HTTP 500/502/503 server errors must trigger backoff retry and recover."""
        attempts = 0

        def server_error_handler(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return httpx.Response(status_code=500, text="Internal Server Error")
            if attempts == 2:
                return httpx.Response(status_code=502, text="Bad Gateway")
            return httpx.Response(
                status_code=200,
                json={"choices": [{"message": {"content": '{"server_recovered": true}'}}]},
            )

        client = LLMClient(
            config=LLMConfig(base_url="https://api.test.com/v1", api_key="sk-test", max_retries=2, enabled=True),
            conn=db_conn,
            transport=httpx.MockTransport(server_error_handler),
        )

        resp = client.chat_completion_sync("classify", [{"role": "user", "content": "ping"}])
        assert resp == {"server_recovered": True}
        assert attempts == 3

    def test_transient_error_network_timeouts_recovery(self, db_conn: sqlite3.Connection):
        """Network timeout exceptions must trigger retry and succeed upon recovery."""
        attempts = 0

        def timeout_handler(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise httpx.ReadTimeout("Read timed out", request=request)
            return httpx.Response(
                status_code=200,
                json={"choices": [{"message": {"content": '{"timeout_recovered": true}'}}]},
            )

        client = LLMClient(
            config=LLMConfig(base_url="https://api.test.com/v1", api_key="sk-test", max_retries=2, enabled=True),
            conn=db_conn,
            transport=httpx.MockTransport(timeout_handler),
        )

        resp = client.chat_completion_sync("classify", [{"role": "user", "content": "ping"}])
        assert resp == {"timeout_recovered": True}
        assert attempts == 2

    def test_transient_error_exhaustion_raises_cleanly(self, db_conn: sqlite3.Connection):
        """When retries are exhausted, client raises httpx.HTTPStatusError cleanly."""
        attempts = 0

        def persistent_error_handler(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            return httpx.Response(status_code=503, text="Service Unavailable")

        client = LLMClient(
            config=LLMConfig(base_url="https://api.test.com/v1", api_key="sk-test", max_retries=2, enabled=True),
            conn=db_conn,
            transport=httpx.MockTransport(persistent_error_handler),
        )

        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            client.chat_completion_sync("classify", [{"role": "user", "content": "ping"}])

        assert exc_info.value.response.status_code == 503
        assert attempts == 3  # Initial + 2 retries

    def test_non_retryable_http_401_and_403_aborts_immediately(self, db_conn: sqlite3.Connection):
        """HTTP 401 Unauthorized and 403 Forbidden must abort immediately with 0 retries."""
        attempts_401 = 0

        def auth_401_handler(request: httpx.Request) -> httpx.Response:
            nonlocal attempts_401
            attempts_401 += 1
            return httpx.Response(status_code=401, text="Invalid API Key")

        client_401 = LLMClient(
            config=LLMConfig(base_url="https://api.test.com/v1", api_key="bad-key", max_retries=3, enabled=True),
            conn=db_conn,
            transport=httpx.MockTransport(auth_401_handler),
        )

        with pytest.raises(httpx.HTTPStatusError):
            client_401.chat_completion_sync("classify", [{"role": "user", "content": "ping"}])

        assert attempts_401 == 1  # Aborted immediately

        attempts_403 = 0

        def auth_403_handler(request: httpx.Request) -> httpx.Response:
            nonlocal attempts_403
            attempts_403 += 1
            return httpx.Response(status_code=403, text="Permission Denied")

        client_403 = LLMClient(
            config=LLMConfig(base_url="https://api.test.com/v1", api_key="bad-key", max_retries=3, enabled=True),
            conn=db_conn,
            transport=httpx.MockTransport(auth_403_handler),
        )

        with pytest.raises(httpx.HTTPStatusError):
            client_403.chat_completion_sync("classify", [{"role": "user", "content": "ping"}])

        assert attempts_403 == 1

    def test_robust_json_extraction_stress_matrix(self):
        """Extracts JSON across markdown code fences, trailing commas, unescaped whitespace."""
        # 1. Standard markdown code fences
        fence_json = "```json\n{\"answer\": 42, \"category\": \"food\"}\n```"
        assert extract_json(fence_json) == {"answer": 42, "category": "food"}

        # 2. Markdown fence without 'json' tag
        fence_plain = "```\n{\"answer\": 42}\n```"
        assert extract_json(fence_plain) == {"answer": 42}

        # 3. Conversational preamble and postamble around code fence
        chatty_fence = "Hello! Here is the JSON result you requested:\n```json\n{\"valid\": true}\n```\nHope it helps!"
        assert extract_json(chatty_fence) == {"valid": True}

        # 4. Trailing commas in arrays and objects
        trailing = "{\"items\": [1, 2, 3, ], \"meta\": {\"count\": 3, }, }"
        assert extract_json(trailing) == {"items": [1, 2, 3], "meta": {"count": 3}}

        # 5. Raw conversational text without any braces: raises ValueError cleanly
        raw_text = "I apologize, but I am an AI assistant and cannot perform this classification."
        with pytest.raises(ValueError) as exc_info:
            extract_json(raw_text)
        assert "Could not extract JSON from LLM response" in str(exc_info.value)


# =========================================================================
# 3. SQLite LLM Cache Concurrency & Integrity
# =========================================================================

class TestAdversarialSQLiteLLMCacheIntegrity:
    """Stress tests SHA-256 hash collision resistance, cache bypass, and table corruption."""

    def test_cache_key_collision_resistance_sha256(self, db_conn: sqlite3.Connection):
        """Different parameters must yield strictly different SHA-256 hashes."""
        client = LLMClient(conn=db_conn)

        base_msgs = [{"role": "user", "content": "麦当劳"}]
        h1 = client.compute_cache_key("classify", "deepseek-chat", 0.1, base_msgs)

        # Different task type
        h2 = client.compute_cache_key("review", "deepseek-chat", 0.1, base_msgs)
        assert h1 != h2

        # Different model
        h3 = client.compute_cache_key("classify", "qwen-plus", 0.1, base_msgs)
        assert h1 != h3

        # Different temperature
        h4 = client.compute_cache_key("classify", "deepseek-chat", 0.7, base_msgs)
        assert h1 != h4

        # Different prompt content
        h5 = client.compute_cache_key("classify", "deepseek-chat", 0.1, [{"role": "user", "content": "肯德基"}])
        assert h1 != h5

        # Identical parameters must produce deterministic identical key
        h1_repeat = client.compute_cache_key("classify", "deepseek-chat", 0.1, base_msgs)
        assert h1 == h1_repeat

    def test_cache_bypass_when_disabled(self):
        """When conn=None, client operates without caching and hits network every time."""
        call_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            return httpx.Response(
                status_code=200,
                json={"choices": [{"message": {"content": '{"bypassed": true}'}}]},
            )

        client_no_conn = LLMClient(
            config=LLMConfig(base_url="https://api.test.com/v1", api_key="sk-test", enabled=True),
            conn=None,  # No cache storage
            transport=httpx.MockTransport(handler),
        )

        res1 = client_no_conn.chat_completion_sync("classify", [{"role": "user", "content": "prompt"}])
        assert res1 == {"bypassed": True}
        assert call_count == 1

        # Second call: must NOT be cached because conn is None
        res2 = client_no_conn.chat_completion_sync("classify", [{"role": "user", "content": "prompt"}])
        assert res2 == {"bypassed": True}
        assert call_count == 2
        assert client_no_conn.cache_hits == 0

    def test_vulnerability_cache_bypass_use_cache_false_parameter(self, db_conn: sqlite3.Connection):
        """Vulnerability Investigation:
        Worker M4 omitted the specified 'use_cache: bool = True' parameter from
        chat_completion_sync() and chat_completion_async(), preventing per-call cache bypass.
        """
        client = LLMClient(
            config=LLMConfig(base_url="https://api.test.com/v1", api_key="sk-test", enabled=True),
            conn=db_conn,
        )
        # Attempt per-call cache bypass as specified in dispatch instructions
        try:
            client.chat_completion_sync("classify", [{"role": "user", "content": "test"}], use_cache=False)
        except TypeError as exc:
            pytest.fail(
                f"VULNERABILITY REPRODUCED: chat_completion_sync() does not support 'use_cache' parameter. Error: {exc}"
            )
        except Exception:
            pass  # HTTP errors or mock transport errors are acceptable; TypeError is the missing parameter bug


    def test_corrupted_cache_table_row_handling(self, db_conn: sqlite3.Connection):
        """Corrupted JSON in llm_cache must degrade safely to a cache miss without crashing."""
        client = LLMClient(
            config=LLMConfig(base_url="https://api.test.com/v1", api_key="sk-test", enabled=True),
            conn=db_conn,
        )

        test_msgs = [{"role": "user", "content": "corrupt_test"}]
        input_hash = client.compute_cache_key("classify", "deepseek-chat", 0.1, test_msgs)

        # Manually inject malformed, unparseable JSON into llm_cache table
        db_conn.execute(
            """
            INSERT INTO llm_cache (task_type, input_hash, response_json, model, prompt_tokens, completion_tokens)
            VALUES (?, ?, ?, ?, 10, 5)
            """,
            ("classify", input_hash, "MALFORMED_NON_JSON_{{@@", "deepseek-chat"),
        )
        db_conn.commit()

        # get_from_cache should catch the JSONDecodeError, log debug, and return None
        cached = client.get_from_cache(input_hash)
        assert cached is None

    def test_cache_table_dropped_graceful_handling(self, db_conn: sqlite3.Connection):
        """If llm_cache table is dropped, get_from_cache and save_to_cache degrade safely."""
        client = LLMClient(
            config=LLMConfig(base_url="https://api.test.com/v1", api_key="sk-test", enabled=True),
            conn=db_conn,
        )

        # Drop table
        db_conn.execute("DROP TABLE llm_cache")
        db_conn.commit()

        # Reading from dropped table: returns None safely
        cached = client.get_from_cache("dummy_hash")
        assert cached is None

        # Writing to dropped table: suppresses error safely without crash
        client.save_to_cache("classify", "dummy_hash", {"test": 1}, "deepseek-chat", 5, 5)


# =========================================================================
# 4. 5 Specialized LLM Tasks Offline Resilience & Schema Conformance
# =========================================================================

class TestAdversarialFiveSpecializedTasksOfflineResilience:
    """Stress tests the 5 specialized tasks under offline, disabled, network disconnect, and malformed inputs."""

    def test_task1_classify_fallback_offline_and_adversarial(self, db_conn: sqlite3.Connection):
        """Task 1: classify_fallback must return deterministic source='none' fallbacks on failures."""
        key_map = {"food.dining": 10, "transport.taxi": 20}
        txns = [
            {"id": 1, "counterparty": "未知商户A", "amount_cents": 1200},
            {"id": 2, "counterparty": "未知商户B", "amount_cents": 3400},
        ]

        # 1. LLM disabled globally
        client_disabled = LLMClient(config=LLMConfig(enabled=False), conn=db_conn)
        res_disabled = classify_fallback_sync(txns, client_disabled, key_map)
        assert len(res_disabled) == 2
        for r in res_disabled:
            assert isinstance(r, ClassificationResult)
            assert r.category_id is None
            assert r.source == "none"
            assert r.confidence == 0.0

        # 2. Network disconnect simulation
        def disconnect_handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("Network unreachable", request=request)

        client_disconnect = LLMClient(
            config=LLMConfig(base_url="https://api.test.com/v1", api_key="sk-key", enabled=True),
            conn=db_conn,
            transport=httpx.MockTransport(disconnect_handler),
        )
        res_disconnect = classify_fallback_sync(txns, client_disconnect, key_map)
        assert len(res_disconnect) == 2
        for r in res_disconnect:
            assert r.category_id is None
            assert r.source == "none"

        # 3. LLM returns hallucinated category key
        def hallucination_handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                status_code=200,
                json={
                    "results": [
                        {"id": 1, "category_key": "hallucinated.fake", "confidence": 0.99, "reason": "hallucinated"}
                    ]
                },
            )

        client_hallucination = LLMClient(
            config=LLMConfig(base_url="https://api.test.com/v1", api_key="sk-key", enabled=True),
            conn=db_conn,
            transport=httpx.MockTransport(hallucination_handler),
        )
        res_hallucination = classify_fallback_sync([txns[0]], client_hallucination, key_map)
        assert len(res_hallucination) == 1
        assert res_hallucination[0].category_id is None
        assert res_hallucination[0].source == "none"

    def test_task2_review_classification_offline_and_adversarial(self, db_conn: sqlite3.Connection):
        """Task 2: review_classification must return empty list safely when offline or failing."""
        key_map = {"transport.taxi": 20}
        txns = [{"id": 10, "counterparty": "滴滴出行", "amount_cents": 1800}]

        # 1. LLM disabled
        client_disabled = LLMClient(config=LLMConfig(enabled=False), conn=db_conn)
        assert review_classification_sync(txns, client_disabled, key_map) == []

        # 2. Network error
        def network_err(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("Timeout", request=request)

        client_err = LLMClient(
            config=LLMConfig(base_url="https://api.test.com/v1", api_key="sk-key", enabled=True),
            conn=db_conn,
            transport=httpx.MockTransport(network_err),
        )
        assert review_classification_sync(txns, client_err, key_map) == []

        # 3. Malformed JSON response: empty review list without crash
        def malformed_resp(request: httpx.Request) -> httpx.Response:
            return httpx.Response(status_code=200, text="Sorry, cannot review this.")

        client_malformed = LLMClient(
            config=LLMConfig(base_url="https://api.test.com/v1", api_key="sk-key", enabled=True),
            conn=db_conn,
            transport=httpx.MockTransport(malformed_resp),
        )
        assert review_classification_sync(txns, client_malformed, key_map) == []

    def test_task3_dedup_arbitration_offline_and_adversarial(self, db_conn: sqlite3.Connection):
        """Task 3: dedup_arbitration must return ArbitrationResult with 'unsure' verdict on failure."""
        p_txn = {"id": 1, "counterparty": "麦当劳", "amount_cents": 2500}
        s_txn = {"id": 2, "counterparty": "金拱门", "amount_cents": 2500}

        # 1. Disabled
        client_disabled = LLMClient(config=LLMConfig(enabled=False), conn=db_conn)
        arb_disabled = dedup_arbitration_sync(p_txn, s_txn, 0.75, client_disabled)
        assert isinstance(arb_disabled, ArbitrationResult)
        assert arb_disabled.verdict == "unsure"
        assert arb_disabled.confidence == 0.0

        # 2. Network error
        def network_err(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("Connection refused", request=request)

        client_err = LLMClient(
            config=LLMConfig(base_url="https://api.test.com/v1", api_key="sk-key", enabled=True),
            conn=db_conn,
            transport=httpx.MockTransport(network_err),
        )
        arb_err = dedup_arbitration_sync(p_txn, s_txn, 0.75, client_err)
        assert arb_err.verdict == "unsure"
        assert arb_err.confidence == 0.0

        # 3. Invalid verdict string sanitized to 'unsure'
        def invalid_verdict_handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                status_code=200,
                json={"verdict": "unknown_random_verdict", "confidence": 0.8},
            )

        client_invalid = LLMClient(
            config=LLMConfig(base_url="https://api.test.com/v1", api_key="sk-key", enabled=True),
            conn=db_conn,
            transport=httpx.MockTransport(invalid_verdict_handler),
        )
        arb_inv = dedup_arbitration_sync(p_txn, s_txn, 0.75, client_invalid)
        assert arb_inv.verdict == "unsure"

    def test_task4_merchant_normalize_offline_and_adversarial(self, db_conn: sqlite3.Connection):
        """Task 4: merchant_normalize must fall back deterministically to rule-cleaned merchant names."""
        cps = ["支付宝-麦当劳（五角场店）", "财付通-美团外卖"]

        # 1. Disabled: returns rule-cleaned baseline
        client_disabled = LLMClient(config=LLMConfig(enabled=False), conn=db_conn)
        norm_disabled = merchant_normalize_sync(cps, client_disabled)
        assert isinstance(norm_disabled, dict)
        assert norm_disabled["支付宝-麦当劳（五角场店）"] == "麦当劳"
        assert norm_disabled["财付通-美团外卖"] == "美团外卖"

        # 2. Empty input
        assert merchant_normalize_sync([], client_disabled) == {}

        # 3. Partial network failure: keeps baseline
        def fail_handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(status_code=500, text="Internal Error")

        client_fail = LLMClient(
            config=LLMConfig(base_url="https://api.test.com/v1", api_key="sk-key", enabled=True),
            conn=db_conn,
            transport=httpx.MockTransport(fail_handler),
        )
        norm_fail = merchant_normalize_sync(cps, client_fail)
        assert norm_fail["支付宝-麦当劳（五角场店）"] == "麦当劳"

    def test_task5_suggest_profile_mapping_offline_and_adversarial(self, db_conn: sqlite3.Connection):
        """Task 5: suggest_profile_mapping must filter hallucinated columns and sanitize amounts."""
        header = ["交易日期", "金额", "交易对手", "摘要"]
        sample_rows = [["2023-11-10", "-45.00", "麦当劳", "午餐"]]

        # 1. Disabled: returns default ProfileSuggestionResult
        client_disabled = LLMClient(config=LLMConfig(enabled=False), conn=db_conn)
        sug_disabled = suggest_profile_mapping_sync(header, sample_rows, client_disabled)
        assert isinstance(sug_disabled, ProfileSuggestionResult)
        assert sug_disabled.columns == {}
        assert sug_disabled.confidence == 0.0

        # 2. LLM hallucinates non-existent header column: must be safely dropped
        def hallucinate_col_handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                status_code=200,
                json={
                    "columns": {
                        "date": "交易日期",
                        "amount": "不存在的金额列",
                        "counterparty": "交易对手",
                    },
                    "amount_mode": "invalid_mode",
                    "confidence": 0.90,
                },
            )

        client_hallucinate = LLMClient(
            config=LLMConfig(base_url="https://api.test.com/v1", api_key="sk-key", enabled=True),
            conn=db_conn,
            transport=httpx.MockTransport(hallucinate_col_handler),
        )
        sug_res = suggest_profile_mapping_sync(header, sample_rows, client_hallucinate)
        assert sug_res.columns["date"] == "交易日期"
        assert sug_res.columns["counterparty"] == "交易对手"
        # Non-existent column dropped
        assert "amount" not in sug_res.columns
        # Invalid amount_mode defaults safely to 'signed'
        assert sug_res.amount_mode == "signed"

    def test_all_five_tasks_offline_consistency(self, db_conn: sqlite3.Connection):
        """All 5 tasks must return valid schema types and deterministic defaults when LLM is offline."""
        client_disabled = LLMClient(config=LLMConfig(enabled=False), conn=db_conn)

        # Task 1: classify_fallback_sync
        res1 = classify_fallback_sync([{"id": 1, "counterparty": "test"}], client_disabled, {})
        assert len(res1) == 1
        assert isinstance(res1[0], ClassificationResult)
        assert res1[0].source == "none"

        # Task 2: review_classification_sync
        res2 = review_classification_sync([{"id": 1}], client_disabled, {})
        assert res2 == []

        # Task 3: dedup_arbitration_sync
        res3 = dedup_arbitration_sync({"id": 1}, {"id": 2}, 0.6, client_disabled)
        assert isinstance(res3, ArbitrationResult)
        assert res3.verdict == "unsure"

        # Task 4: merchant_normalize_sync
        res4 = merchant_normalize_sync(["支付宝-麦当劳"], client_disabled)
        assert isinstance(res4, dict)
        assert res4["支付宝-麦当劳"] == "麦当劳"

        # Task 5: suggest_profile_mapping_sync
        res5 = suggest_profile_mapping_sync(["日期"], [["2023-10-01"]], client_disabled)
        assert isinstance(res5, ProfileSuggestionResult)
        assert res5.columns == {}


# =========================================================================
# 5. Windows DPAPI Corrupted Secret Handling
# =========================================================================

class TestAdversarialDPAPICorruptedSecretHandling:
    """Stress tests corrupted DPAPI ciphertexts, truncated base64, and settings persistence resilience."""

    def test_dpapi_corrupted_ciphertext_matrix(self):
        """Corrupted DPAPI inputs must raise SecretDecryptionError without unhandled crash."""
        corrupted_cases = [
            "dpapi:YWJj",                   # Valid base64 ("abc"), but invalid DPAPI blob
            "dpapi:!@#$%",                   # Malformed base64 symbols
            "dpapi:AP/+EjRW",                # Random binary bytes
            "dpapi:",                        # Empty DPAPI payload
            "dpapi:1234567890abcdef",        # Hex digits payload
            "b64:invalid-base64-content!@#$%", # Corrupted base64 payload
            "b64:\xff\xfe",                  # Non-ASCII characters in base64 string
        ]

        for bad_payload in corrupted_cases:
            with pytest.raises(SecretDecryptionError):
                decrypt_secret(bad_payload)

    def test_settings_table_corrupted_key_load_from_db(self, db_conn: sqlite3.Connection):
        """When settings table contains corrupted DPAPI ciphertext, load_from_db degrades cleanly."""
        # Inject corrupted encrypted key into settings table
        corrupt_payload = {
            "base_url": "https://api.deepseek.com/v1",
            "api_key_encrypted": "dpapi:corrupted_invalid_secret_blob_12345",
            "model": "deepseek-chat",
            "enabled": True,
        }
        db_conn.execute(
            """
            INSERT INTO settings (key, value)
            VALUES ('llm_config', ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (json.dumps(corrupt_payload),),
        )
        db_conn.commit()

        # Loading from DB must catch the SecretDecryptionError and return a clean default LLMConfig
        loaded_config = LLMConfig.load_from_db(db_conn)
        assert isinstance(loaded_config, LLMConfig)
        # Should gracefully return default without throwing unhandled exception
        assert loaded_config.api_key == ""

    def test_mask_secret_boundary_cases(self):
        """mask_secret must handle all lengths, prefixes, and edge cases safely."""
        assert mask_secret("") == ""
        assert mask_secret("a") == "****"
        assert mask_secret("12345678") == "****"
        assert mask_secret("123456789") == "****6789"
        assert mask_secret("sk-12345") == "****"
        assert mask_secret("sk-1234567890abcdef") == "sk-****cdef"
        assert mask_secret("long_custom_token_987654321") == "****4321"
