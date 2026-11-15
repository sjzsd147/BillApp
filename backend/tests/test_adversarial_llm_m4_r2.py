"""Milestone 4 Round 2 Adversarial Stress Verification Suite.

Validates:
1. Email regex boundary & Chinese context preservation (e.g., '发送至user@domain.com的通知')
2. chat_completion_sync & chat_completion_async cache bypass (use_cache=False neither reads nor writes)
3. Zero-PII leakage across all 5 prompt builders under adversarial PII inputs
4. Corrupted DPAPI secret handling (no segfaults/crashes, clean SecretDecryptionError)
"""

import asyncio
import base64
import json
import re
import sqlite3
import pytest
import httpx

from billapp.core.secrets import (
    DPAPI_PREFIX,
    B64_PREFIX,
    SecretDecryptionError,
    decrypt_secret,
    encrypt_secret,
    mask_secret,
)
from billapp.llm.client import LLMClient, LLMConfig
from billapp.llm.prompts import (
    sanitize_prompt_text,
    sanitize_transaction_for_llm,
    build_classify_fallback_prompt,
    build_review_classification_prompt,
    build_dedup_arbitration_prompt,
    build_merchant_normalize_prompt,
    build_mapping_suggest_prompt,
)


# =========================================================================
# 1. Email Regex & Chinese Context Boundary Tests
# =========================================================================

class TestEmailRegexChineseBoundaries:
    """Adversarially test email regex behavior with contiguous Chinese characters and symbols."""

    @pytest.mark.parametrize(
        "raw_text,expected_text",
        [
            ("发送至user@domain.com的通知", "发送至[EMAIL]的通知"),
            ("发票至service@example.com请查收", "发票至[EMAIL]请查收"),
            ("通知邮箱admin_test.123+tag@vip.qq.com已发送完成", "通知邮箱[EMAIL]已发送完成"),
            ("账单已发往billing-dept_2026@sub.corp-bank.com.cn请查核", "账单已发往[EMAIL]请查核"),
            ("联系alice@company.com或bob@personal.org获取帮助", "联系[EMAIL]或[EMAIL]获取帮助"),
            ("工单123号发送至user@abc.cn请查收456", "工单123号发送至[EMAIL]请查收456"),
            ("user@abc.cn已绑定为默认联系邮箱", "[EMAIL]已绑定为默认联系邮箱"),
            ("紧急发件人support@cloud.io", "紧急发件人[EMAIL]"),
            ("（收件人：alert+service@monitor.internal.net）", "（收件人：[EMAIL]）"),
            ("《通知：notice@test.com》", "《通知：[EMAIL]》"),
            ("“user.name+filter@domain.co.uk”", "“[EMAIL]”"),
            ("，test@example.org，", "，[EMAIL]，"),
            ("邮件地址:a.b.c_d%e+f-g@h-i.j.k.edu.cn", "邮件地址:[EMAIL]"),
        ],
    )
    def test_email_contiguous_chinese_preservation(self, raw_text: str, expected_text: str):
        """Confirm Chinese characters, punctuation, and digits adjacent to emails are preserved."""
        result = sanitize_prompt_text(raw_text)
        assert result == expected_text, f"Mismatch for '{raw_text}': got '{result}', expected '{expected_text}'"

    def test_email_with_multiple_plus_addressing(self):
        text = "订单转发至 user+billing+urgent@domain.com 请加急"
        sanitized = sanitize_prompt_text(text)
        assert sanitized == "订单转发至 [EMAIL] 请加急"

    def test_non_email_chinese_at_symbol(self):
        # '@' used as mention in Chinese without valid domain
        text = "请@张三 确认一下"
        sanitized = sanitize_prompt_text(text)
        assert sanitized == "请@张三 确认一下"


# =========================================================================
# 2. LLM Client Cache Bypass (use_cache=False)
# =========================================================================

class TestLLMCacheBypassSyncAndAsync:
    """Adversarially verify use_cache=False completely bypasses cache (reads and writes)."""

    @pytest.fixture
    def db_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(":memory:")
        conn.execute("""
            CREATE TABLE llm_cache (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_type TEXT NOT NULL,
                input_hash TEXT UNIQUE NOT NULL,
                response_json TEXT NOT NULL,
                model TEXT NOT NULL,
                prompt_tokens INTEGER DEFAULT 0,
                completion_tokens INTEGER DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now', 'localtime'))
            )
        """)
        conn.commit()
        return conn

    def test_sync_cache_bypass_no_write_and_no_read(self, db_conn: sqlite3.Connection):
        """chat_completion_sync with use_cache=False must not write to cache and not read from cache."""
        call_count = 0

        def mock_handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            return httpx.Response(
                status_code=200,
                json={
                    "choices": [{
                        "message": {
                            "content": json.dumps({"category_key": f"test_cat_{call_count}", "confidence": 0.99})
                        }
                    }],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 15},
                },
            )

        transport = httpx.MockTransport(mock_handler)
        client = LLMClient(
            config=LLMConfig(base_url="https://api.test.com/v1", api_key="sk-test", enabled=True),
            conn=db_conn,
            transport=transport,
        )

        messages = [{"role": "user", "content": "classify this txn"}]

        # 1. First call with use_cache=False
        resp1 = client.chat_completion_sync("classify", messages, use_cache=False)
        assert resp1["category_key"] == "test_cat_1"
        assert call_count == 1

        # Check DB: Cache table must remain completely EMPTY
        cached_count = db_conn.execute("SELECT COUNT(*) FROM llm_cache").fetchone()[0]
        assert cached_count == 0, f"Expected 0 cached rows when use_cache=False, but found {cached_count}"

        # 2. Second call with use_cache=False -> MUST make a new network call
        resp2 = client.chat_completion_sync("classify", messages, use_cache=False)
        assert resp2["category_key"] == "test_cat_2"
        assert call_count == 2
        cached_count_2 = db_conn.execute("SELECT COUNT(*) FROM llm_cache").fetchone()[0]
        assert cached_count_2 == 0

        # 3. Pre-populate cache with a poisoned entry
        input_hash = client.compute_cache_key("classify", client.config.model, client.config.temperature, messages)
        poisoned_content = json.dumps({"category_key": "POISONED_CACHE", "confidence": 0.0})
        db_conn.execute(
            "INSERT INTO llm_cache (task_type, input_hash, response_json, model, prompt_tokens, completion_tokens) VALUES (?, ?, ?, ?, ?, ?)",
            ("classify", input_hash, poisoned_content, client.config.model, 1, 1),
        )
        db_conn.commit()

        # 4. Calling with use_cache=True should return POISONED_CACHE
        resp_cached = client.chat_completion_sync("classify", messages, use_cache=True)
        assert resp_cached["category_key"] == "POISONED_CACHE"
        assert call_count == 2  # No new network call

        # 5. Calling with use_cache=False MUST BYPASS the poisoned entry and execute network call!
        resp_bypassed = client.chat_completion_sync("classify", messages, use_cache=False)
        assert resp_bypassed["category_key"] == "test_cat_3"
        assert call_count == 3  # Executed fresh network call!

    def test_async_cache_bypass_verified_empirically(self, db_conn: sqlite3.Connection):
        """Verify chat_completion_async signature and that use_cache=False bypasses cache lookup."""
        import inspect
        from unittest.mock import MagicMock

        # 1. Parameter signature verification
        sig = inspect.signature(LLMClient.chat_completion_async)
        assert "use_cache" in sig.parameters
        assert sig.parameters["use_cache"].default is True

        client = LLMClient(
            config=LLMConfig(base_url="https://api.test.com/v1", api_key="sk-test", enabled=True),
            conn=db_conn,
        )

        messages = [{"role": "user", "content": "async classify test"}]
        input_hash = client.compute_cache_key("classify", client.config.model, client.config.temperature, messages)

        # 2. Poison cache with known result
        poisoned_content = json.dumps({"category_key": "CACHED_VAL", "confidence": 0.99})
        db_conn.execute(
            "INSERT INTO llm_cache (task_type, input_hash, response_json, model, prompt_tokens, completion_tokens) VALUES (?, ?, ?, ?, ?, ?)",
            ("classify", input_hash, poisoned_content, client.config.model, 10, 10),
        )
        db_conn.commit()

        # 3. Running with use_cache=True must hit cache immediately on first step (no await)
        coro_hit = client.chat_completion_async("classify", messages, use_cache=True)
        try:
            coro_hit.send(None)
            pytest.fail("Expected StopIteration containing cached result")
        except StopIteration as exc:
            res = exc.value
            assert res["category_key"] == "CACHED_VAL"

        # 4. Running with use_cache=False must completely skip get_from_cache and proceed to network setup
        real_get_cache = client.get_from_cache
        mock_get_cache = MagicMock(side_effect=real_get_cache)
        client.get_from_cache = mock_get_cache

        class CacheBypassSentinel(Exception):
            pass

        # Mock normalize_url (the first line after cache check) to raise sentinel
        client.normalize_url = MagicMock(side_effect=CacheBypassSentinel("Successfully bypassed cache!"))

        coro_bypass = client.chat_completion_async("classify", messages, use_cache=False)
        with pytest.raises(CacheBypassSentinel):
            coro_bypass.send(None)
        
        # get_from_cache must NOT have been called at all!
        mock_get_cache.assert_not_called()






# =========================================================================
# 3. Zero-PII Leakage Across All 5 Specialized Prompts
# =========================================================================

class TestZeroPIILoopholesAcrossAllPrompts:
    """Stress-test prompt generation for all 5 tasks against a barrage of sensitive PII data."""

    PII_RAW_SAMPLES = {
        "card_unionpay_19": "6222021000123456789",
        "card_visa_16": "4111111111111111",
        "card_amex_15": "378282246310005",
        "citizen_id_x": "11010119900307234X",
        "citizen_id_num": "440301199512151234",
        "mobile_chinese": "13912345678",
        "mobile_plus86": "+86-13887654321",
        "email_standard": "victim_user@secret-corp.cn",
        "email_tagged": "billing+confidential@sub.bank.com",
        "order_alipay_28": "2023111022001412341412345678",
        "holder_name": "张伟强",
    }

    def _assert_zero_pii(self, text: str):
        """Assert that no plain PII values exist in text."""
        for pii_name, pii_val in self.PII_RAW_SAMPLES.items():
            assert pii_val not in text, f"PII LEAK DETECTED! {pii_name} ('{pii_val}') leaked in prompt:\n{text}"

    def test_task1_classify_fallback_prompt_zero_pii(self):
        txns = [
            {
                "id": 101,
                "counterparty": f"美团外卖(送至{self.PII_RAW_SAMPLES['holder_name']}, 手机{self.PII_RAW_SAMPLES['mobile_chinese']})",
                "merchant_norm": "美团",
                "description": f"单号{self.PII_RAW_SAMPLES['order_alipay_28']} 卡号{self.PII_RAW_SAMPLES['card_unionpay_19']}扣款",
                "platform_category": "餐饮美食",
                "amount_cents": 3500,
                "direction": "expense",
            },
            {
                "id": 102,
                "counterparty": f"发票发送给{self.PII_RAW_SAMPLES['email_tagged']}",
                "merchant_norm": "滴滴出行",
                "description": f"身份证{self.PII_RAW_SAMPLES['citizen_id_x']} 快捷支付{self.PII_RAW_SAMPLES['card_visa_16']}",
                "platform_category": "交通出行",
                "amount_cents": 4200,
                "direction": "expense",
            },
        ]
        candidates = [
            {"key": "food", "name": "餐饮", "direction": "expense", "subcategories": [{"key": "food.dining", "name": "正餐"}]},
            {"key": "transport", "name": "交通", "direction": "expense", "subcategories": [{"key": "transport.taxi", "name": "打车"}]},
        ]
        sys_p, user_p = build_classify_fallback_prompt(txns, candidates, holder_name=self.PII_RAW_SAMPLES["holder_name"])
        combined = sys_p + "\n" + user_p
        self._assert_zero_pii(combined)
        assert "[USER]" in combined
        assert "[PHONE]" in combined
        assert "[CARD]" in combined
        assert "[ORDER_ID]" in combined
        assert "[EMAIL]" in combined
        assert "[ID_CARD]" in combined

    def test_task2_review_classification_prompt_zero_pii(self):
        txns = [
            {
                "id": 201,
                "counterparty": f"用户{self.PII_RAW_SAMPLES['holder_name']}充值",
                "merchant_norm": "AppStore",
                "description": f"卡号{self.PII_RAW_SAMPLES['card_amex_15']} 电话{self.PII_RAW_SAMPLES['mobile_plus86']}",
                "platform_category": "数字生活",
                "amount_cents": 64800,
                "direction": "expense",
            }
        ]
        candidates = [{"key": "digital", "name": "数码娱乐", "subcategories": [{"key": "digital.game", "name": "游戏"}]}]
        sys_p, user_p = build_review_classification_prompt(txns, candidates, holder_name=self.PII_RAW_SAMPLES["holder_name"])
        combined = sys_p + "\n" + user_p
        self._assert_zero_pii(combined)
        assert "[CARD]" in combined
        assert "[PHONE]" in combined
        assert "[USER]" in combined

    def test_task3_dedup_arbitration_prompt_zero_pii(self):
        primary = {
            "id": 301,
            "counterparty": f"支付宝-麦当劳({self.PII_RAW_SAMPLES['email_standard']})",
            "merchant_norm": "麦当劳",
            "description": f"订单{self.PII_RAW_SAMPLES['order_alipay_28']}",
            "amount_cents": 4500,
            "direction": "expense",
        }
        secondary = {
            "id": 302,
            "counterparty": f"特约金拱门持卡人{self.PII_RAW_SAMPLES['holder_name']}",
            "merchant_norm": "金拱门",
            "description": f"卡号{self.PII_RAW_SAMPLES['card_unionpay_19']} 身份证{self.PII_RAW_SAMPLES['citizen_id_num']}",
            "amount_cents": 4500,
            "direction": "expense",
        }
        sys_p, user_p = build_dedup_arbitration_prompt(primary, secondary, score=0.88, holder_name=self.PII_RAW_SAMPLES["holder_name"])
        combined = sys_p + "\n" + user_p
        self._assert_zero_pii(combined)
        assert "[EMAIL]" in combined
        assert "[ORDER_ID]" in combined
        assert "[USER]" in combined
        assert "[CARD]" in combined
        assert "[ID_CARD]" in combined

    def test_task4_merchant_normalize_prompt_zero_pii(self):
        cps = [
            f"支付宝-美团外卖(通知{self.PII_RAW_SAMPLES['email_tagged']})",
            f"财付通-特约商户(单号{self.PII_RAW_SAMPLES['order_alipay_28']})",
            f"中国移动充值(号码{self.PII_RAW_SAMPLES['mobile_chinese']})",
            f"卡号{self.PII_RAW_SAMPLES['card_visa_16']}快捷扣款",
        ]
        sys_p, user_p = build_merchant_normalize_prompt(cps)
        combined = sys_p + "\n" + user_p
        self._assert_zero_pii(combined)
        assert "[EMAIL]" in combined
        assert "[ORDER_ID]" in combined
        assert "[PHONE]" in combined
        assert "[CARD]" in combined

    def test_task5_mapping_suggest_prompt_zero_pii(self):
        header = ["交易日期", "卡号", "对方户名", "交易金额", "摘要", "身份证号", "联系电话"]
        samples = [
            [
                "2026-10-15",
                self.PII_RAW_SAMPLES["card_unionpay_19"],
                "餐饮消费",
                "158.00",
                f"账单单号{self.PII_RAW_SAMPLES['order_alipay_28']}",
                self.PII_RAW_SAMPLES["citizen_id_x"],
                self.PII_RAW_SAMPLES["mobile_chinese"],
            ],
            [
                "2026-10-16",
                self.PII_RAW_SAMPLES["card_amex_15"],
                f"通知{self.PII_RAW_SAMPLES['email_standard']}",
                "1250.50",
                "网上支付",
                self.PII_RAW_SAMPLES["citizen_id_num"],
                self.PII_RAW_SAMPLES["mobile_plus86"],
            ],
        ]
        sys_p, user_p = build_mapping_suggest_prompt(header, samples)
        combined = sys_p + "\n" + user_p
        self._assert_zero_pii(combined)
        assert "[CARD]" in combined
        assert "[ORDER_ID]" in combined
        assert "[ID_CARD]" in combined
        assert "[PHONE]" in combined
        assert "[EMAIL]" in combined
        assert "XX.XX" in combined  # Money amount masked


# =========================================================================
# 4. Corrupted DPAPI Secret Handling Tests
# =========================================================================

class TestDPAPICorruptionResilience:
    """Adversarially verify that corrupted DPAPI or base64 secrets raise cleanly without process crashes."""

    CORRUPTED_SECRETS_MATRIX = [
        "dpapi:",                              # Empty payload
        "dpapi:YWJj",                          # Non-DPAPI base64 string ('abc')
        "dpapi:!@#$%^&*()_+",                  # Invalid base64 characters
        "dpapi:AP/+EjRW",                      # Truncated 6-byte pseudo-DPAPI header
        "dpapi:" + "A" * 100,                  # Invalid payload of 75 zero bytes
        "dpapi:" + base64.b64encode(b"\x00" * 32).decode("ascii"), # Null bytes
        "dpapi:" + base64.b64encode(b"\xff" * 64).decode("ascii"), # 0xFF bytes
        "dpapi:" + base64.b64encode(b"BOGUS_DPAPI_BLOB_FOR_CRASH_TESTING").decode("ascii"),
        "b64:invalid-base64-content!@#$%",     # Corrupted base64 payload
        "b64:\xff\xfe",                        # Non-ASCII characters in base64 string
        "b64:abc",                             # Invalid b64 padding
    ]

    @pytest.mark.parametrize("corrupted_secret", CORRUPTED_SECRETS_MATRIX)
    def test_corrupted_secret_raises_cleanly(self, corrupted_secret: str):
        """Ensure SecretDecryptionError is raised and no crash or memory fault occurs."""
        with pytest.raises(SecretDecryptionError) as exc_info:
            decrypt_secret(corrupted_secret)
        assert exc_info.value is not None

    def test_llm_config_load_from_db_with_every_corrupted_secret(self):
        """Ensure settings table corruption with DPAPI errors cleanly yields default LLMConfig."""
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)")

        for idx, corrupted in enumerate(self.CORRUPTED_SECRETS_MATRIX):
            bad_json = json.dumps({"base_url": "https://api.test.com", "api_key_encrypted": corrupted})
            conn.execute(
                "INSERT INTO settings (key, value) VALUES ('llm_config', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (bad_json,),
            )
            conn.commit()

            cfg = LLMConfig.load_from_db(conn)
            # Must safely degrade to default config without raising unhandled exception
            assert isinstance(cfg, LLMConfig), f"Failed for case #{idx}: {corrupted}"
            assert cfg.api_key == "" or cfg.api_key is None
            assert cfg.enabled is False

    def test_dpapi_roundtrip_integrity(self):
        """Sanity test: valid roundtrip encryption and decryption functions accurately."""
        original = "sk-deepseek-live-secret-test-key-12345678"
        encrypted = encrypt_secret(original)
        assert encrypted.startswith(DPAPI_PREFIX) or encrypted.startswith(B64_PREFIX)
        decrypted = decrypt_secret(encrypted)
        assert decrypted == original
