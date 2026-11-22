"""Comprehensive security & integrity regression tests for all 20 audit findings (S-01..S-12, B-01..B-08)."""

from __future__ import annotations

import io
import json
from pathlib import Path
import sqlite3
import tempfile
import zipfile

from fastapi.testclient import TestClient
import httpx
import openpyxl
import pytest

from billapp.app import create_app
from billapp.classify.rules import clean_merchant_for_learning, compile_safe_regex, match_pattern
from billapp.core.db import init_db, migrate_db
from billapp.core.fx import fetch_online_fx_rate
from billapp.core.money import to_cents
from billapp.core.secrets import (
    ENCV1_PREFIX,
    SecretDecryptionError,
    SecretStorageError,
    decrypt_secret,
    encrypt_secret,
    get_secret_storage_info,
)
from billapp.dedup.engine import run_dedup_pipeline
from billapp.dedup.refund import link_in_source_refunds
from billapp.export import generate_excel_export, sanitize_cell_value
from billapp.importers.base import FileProbe
from billapp.importers.common import normalize_currency, parse_iso_datetime
from billapp.importers.profile_engine import BankProfile, ProfileImporter
from billapp.llm.client import LLMClient, LLMConfig, validate_llm_base_url
from billapp.llm.prompts import (
    build_full_statement_parse_prompt,
    build_review_classification_prompt,
    collect_known_holder_names,
    sanitize_prompt_text,
    sanitize_transaction_for_llm,
)
from billapp.llm.tasks import MAX_LLM_PARSE_CHUNKS, parse_statement_with_llm_sync
from billapp.stats import (
    calculate_stats_account,
    calculate_stats_category,
    calculate_stats_heatmap,
    calculate_stats_merchant,
    calculate_stats_summary,
    calculate_stats_trend,
)


# =========================================================================
# S-01: Local API Auth Token, CORS Wildcard Removal, Host/Origin Defense, POST-only Backup
# =========================================================================

class TestS01LocalApiHardening:
    def test_token_auth_and_bootstrap_cookie_and_host_origin_defense(self, tmp_path: Path):
        db_file = tmp_path / "s01.db"
        token = "a" * 64
        boot = "boot-secret-123"
        app = create_app(db_path=str(db_file), api_token=token, bootstrap_token=boot)

        with TestClient(app, base_url="http://127.0.0.1:18999") as client:
            # 1. /api/health remains accessible on loopback without token
            assert client.get("/api/health").status_code == 200

            # 2. Protected /api/* endpoints reject missing or wrong token with 401
            assert client.get("/api/meta").status_code == 401
            assert client.get("/api/meta", headers={"X-BillApp-Token": "wrong"}).status_code == 401
            assert client.post("/api/backup").status_code == 401
            assert client.get("/openapi.json").status_code == 401

            # 3. Valid X-BillApp-Token header succeeds
            ok_meta = client.get("/api/meta", headers={"X-BillApp-Token": token})
            assert ok_meta.status_code == 200

            # 4. GET /api/backup is removed (POST-only -> 404 from SPA /api guard or 405)
            get_bak = client.get("/api/backup", headers={"X-BillApp-Token": token})
            assert get_bak.status_code in (404, 405)

            # 5. DNS Rebinding defense: evil Host header rejected with 403
            rebind = client.get("/api/health", headers={"Host": "evil.attacker.com:18999"})
            assert rebind.status_code == 403

            # 6. Wildcard CORS removed: evil Origin gets no Access-Control-Allow-Origin
            cors_preflight = client.options(
                "/api/meta",
                headers={
                    "Origin": "https://evil.example",
                    "Access-Control-Request-Method": "GET",
                },
            )
            assert "access-control-allow-origin" not in {k.lower() for k in cors_preflight.headers.keys()}

            # 7. Cross-origin POST blocked with 403 even if token present
            csrf_post = client.post(
                "/api/backup",
                headers={"X-BillApp-Token": token, "Origin": "https://evil.example"},
            )
            assert csrf_post.status_code == 403

            # 8. One-time bootstrap token sets HttpOnly SameSite=Strict session cookie & strips query
            boot_resp = client.get(f"/?boot_token={boot}", follow_redirects=False)
            assert boot_resp.status_code == 302
            assert boot_resp.headers["location"] == "/"
            set_cookie = boot_resp.headers.get("set-cookie", "")
            assert "billapp_session=" in set_cookie
            assert "httponly" in set_cookie.lower()
            assert "samesite=strict" in set_cookie.lower()

            # Cookie now authenticates /api/meta
            assert client.get("/api/meta").status_code == 200

            # Replaying one-time boot_token does NOT re-issue cookie
            client.cookies.clear()
            replay = client.get(f"/?boot_token={boot}", follow_redirects=False)
            assert "billapp_session=" not in replay.headers.get("set-cookie", "")


# =========================================================================
# S-02: LLM SSRF Protection & Cross-Origin Saved Key Exfiltration Prevention
# =========================================================================

class TestS02LlmSsrfAndKeyIsolation:
    def test_validate_llm_base_url_blocks_ssrf_vectors(self):
        for bad in (
            "file:///etc/passwd",
            "ftp://example.com/v1",
            "http://user:pass@api.openai.com/v1",
            "http://169.254.169.254/latest/meta-data",
            "http://metadata.google.internal/computeMetadata/v1",
            "http://0.0.0.0:8080/v1",
            "http://[fe80::1]/v1",
        ):
            with pytest.raises(ValueError):
                validate_llm_base_url(bad)

        # Valid public & local Ollama URLs remain allowed
        assert validate_llm_base_url("https://api.openai.com/v1") == "https://api.openai.com/v1"
        assert validate_llm_base_url("http://127.0.0.1:11434/v1") == "http://127.0.0.1:11434/v1"

    def test_llm_test_and_models_block_reusing_saved_key_on_different_origin(self, tmp_path: Path):
        app = create_app(db_path=str(tmp_path / "s02.db"))
        with TestClient(app) as client:
            save_res = client.put(
                "/api/settings",
                json={
                    "llm": {
                        "base_url": "https://api.deepseek.com/v1",
                        "api_key": "sk-super-secret-live-key-9999",
                        "model": "deepseek-chat",
                        "enabled": True,
                    }
                },
            )
            assert save_res.status_code == 200

            # Attempt to exfiltrate saved API key by supplying attacker base_url without api_key
            res_test = client.post(
                "/api/llm/test",
                json={"base_url": "https://attacker.invalid/v1"},
            )
            assert res_test.status_code == 200
            body_test = res_test.json()
            assert body_test["ok"] is False
            assert "禁止复用" in (body_test.get("error") or "")

            res_models = client.post(
                "/api/llm/models",
                json={"base_url": "https://attacker.invalid/v1"},
            )
            assert res_models.status_code == 200
            body_models = res_models.json()
            assert body_models["ok"] is False
            assert "禁止复用" in (body_models.get("error") or "")

            # Attempting to save IMDS SSRF URL fails validation
            bad_save = client.put(
                "/api/settings",
                json={"llm": {"base_url": "http://169.254.169.254/latest/meta-data"}},
            )
            assert bad_save.status_code == 422


# =========================================================================
# S-03 & B-07: Cross-Platform Secret Encryption, No b64 Downgrade, Backup Redaction, Config Preservation
# =========================================================================

class TestS03SecretStorageAndBackupRedaction:
    def test_encv1_master_key_roundtrip_and_tamper_detection(self, monkeypatch):
        monkeypatch.setenv("BILLAPP_MASTER_KEY", "ab" * 32)
        monkeypatch.setenv("BILLAPP_SECRET_BACKEND", "electron_safe_storage")

        secret = "sk-live-secret-token-xyz123456"
        encrypted = encrypt_secret(secret)
        assert encrypted.startswith(ENCV1_PREFIX)
        assert secret not in encrypted
        assert decrypt_secret(encrypted) == secret
        assert get_secret_storage_info()["backend"] == "electron_safe_storage"

        # Tampered ciphertext must fail HMAC verification
        tampered = encrypted[:-2] + ("AA" if not encrypted.endswith("AA") else "BB")
        with pytest.raises(SecretDecryptionError):
            decrypt_secret(tampered)

        # Different master key must fail cleanly
        monkeypatch.setenv("BILLAPP_MASTER_KEY", "cd" * 32)
        with pytest.raises(SecretDecryptionError):
            decrypt_secret(encrypted)

    def test_windows_dpapi_failure_never_silently_downgrades_to_b64(self, monkeypatch):
        monkeypatch.delenv("BILLAPP_MASTER_KEY", raising=False)
        import billapp.core.secrets as sec_mod

        monkeypatch.setattr(sec_mod.sys, "platform", "win32")

        def _boom(_raw):
            raise RuntimeError("DPAPI unavailable")

        monkeypatch.setattr(sec_mod, "_dpapi_encrypt", _boom)
        with pytest.raises(SecretStorageError):
            encrypt_secret("sk-must-not-downgrade")

    def test_backup_strips_api_key_and_cross_device_decryption_preserves_config(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("BILLAPP_MASTER_KEY", "11" * 32)
        db_file = tmp_path / "s03_backup.db"
        app = create_app(db_path=str(db_file))
        with TestClient(app) as client:
            client.put(
                "/api/settings",
                json={
                    "llm": {
                        "base_url": "https://api.deepseek.com/v1",
                        "api_key": "sk-secret-do-not-export-8888",
                        "model": "deepseek-chat",
                        "enabled": True,
                    }
                },
            )
            bak = client.post("/api/backup")
            assert bak.status_code == 200

            # Inspect exported backup SQLite: api_key_encrypted must be empty
            bak_path = tmp_path / "exported.db"
            bak_path.write_bytes(bak.content)
            conn = sqlite3.connect(str(bak_path))
            try:
                raw_cfg = conn.execute("SELECT value FROM settings WHERE key = 'llm_config'").fetchone()[0]
                parsed = json.loads(raw_cfg)
                assert parsed.get("base_url") == "https://api.deepseek.com/v1"
                assert parsed.get("api_key_encrypted", "") == ""
                assert parsed.get("api_key", "") == ""
            finally:
                conn.close()

            # Simulate cross-device key mismatch: config base_url & model must survive!
            monkeypatch.setenv("BILLAPP_MASTER_KEY", "22" * 32)
            settings_resp = client.get("/api/settings").json()
            assert settings_resp["llm"]["base_url"] == "https://api.deepseek.com/v1"
            assert settings_resp["llm"]["model"] == "deepseek-chat"
            assert settings_resp["llm"]["has_api_key"] is False
            assert settings_resp["llm"]["api_key_decryption_failed"] is True


# =========================================================================
# S-05: Deep ReDoS Protection in Rules Engine & BankProfile YAML
# =========================================================================

class TestS05ReDoSHardening:
    @pytest.mark.parametrize(
        "evil_pattern",
        [
            r"((a)+)+",
            r"(((a+)+)+)$",
            r"(a|aa)+",
            r"\w+\w+\w+!",
            r"(a+)\1+",
        ],
    )
    def test_rules_reject_nested_and_overlapping_redos_patterns(self, evil_pattern: str):
        with pytest.raises(ValueError):
            compile_safe_regex(evil_pattern, raise_on_error=True)
        assert match_pattern("regex", evil_pattern, "a" * 30 + "!") is False

    def test_bank_profile_rejects_redos_yaml_patterns(self):
        yaml_evil = """
id: evil_redos
name: Evil Bank
bank: Evil Bank
account_kind: debit
table_detection:
  header_row_pattern: "((a)+)+$"
column_mapping:
  date: "日期"
  amount: "金额"
amount_mode:
  type: signed
"""
        with pytest.raises(ValueError):
            BankProfile.from_yaml(yaml_evil)


# =========================================================================
# S-06: /api/restore Rejects Malicious Triggers/Views & Incomplete Schemas
# =========================================================================

class TestS06RestoreHardening:
    def test_restore_rejects_trigger_and_missing_tables(self, tmp_path: Path):
        app = create_app(db_path=str(tmp_path / "live.db"))
        with TestClient(app) as client:
            # Take valid backup and inject a malicious SQLite TRIGGER
            bak = client.post("/api/backup")
            assert bak.status_code == 200
            evil_db = tmp_path / "evil_trigger.db"
            evil_db.write_bytes(bak.content)
            c = sqlite3.connect(str(evil_db))
            c.execute(
                "CREATE TRIGGER evil_trig AFTER INSERT ON transactions BEGIN DELETE FROM settings; END;"
            )
            c.commit()
            c.close()

            resp = client.post(
                "/api/restore",
                files={"file": ("evil.db", evil_db.read_bytes(), "application/octet-stream")},
            )
            assert resp.status_code == 422
            assert "trigger" in resp.text.lower()

            # Incomplete DB missing core tables (e.g. rules, settings) must be rejected
            incomplete_db = tmp_path / "incomplete.db"
            c2 = sqlite3.connect(str(incomplete_db))
            for tbl in ("import_batches", "accounts", "transactions", "categories"):
                c2.execute(f"CREATE TABLE {tbl} (id INTEGER PRIMARY KEY)")
            c2.commit()
            c2.close()

            resp2 = client.post(
                "/api/restore",
                files={"file": ("inc.db", incomplete_db.read_bytes(), "application/octet-stream")},
            )
            assert resp2.status_code == 422


# =========================================================================
# S-07: ZIP Bomb Defense & Preview Store Bounding
# =========================================================================

class TestS07UploadAndZipBombDefenses:
    def test_zip_bomb_high_compression_ratio_rejected(self, tmp_path: Path):
        app = create_app(db_path=str(tmp_path / "s07.db"))
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            # 2 MB of zeros compresses to ~2 KB -> ratio ~1000x (> 100x limit)
            zf.writestr("bomb.csv", b"0" * (2 * 1024 * 1024))

        with TestClient(app) as client:
            resp = client.post(
                "/api/import/preview",
                files={"file": ("bomb.zip", buf.getvalue(), "application/zip")},
            )
            assert resp.status_code == 400
            assert resp.json()["code"] == "IMPORT_ZIP_BOMB_DETECTED"


# =========================================================================
# S-08 & B-07: Excel Formula Injection Defense & Transfer Exclusion in Sheet 2
# =========================================================================

class TestS08ExportSecurityAndTransfers:
    def test_sanitize_cell_value_handles_whitespace_and_control_prefixes(self):
        assert sanitize_cell_value(" =HYPERLINK(\"http://x\")") == "' =HYPERLINK(\"http://x\")"
        assert sanitize_cell_value("\t=1+1") == "'\t=1+1"
        assert sanitize_cell_value("\r\n+cmd") == "'\r\n+cmd"
        assert sanitize_cell_value("  @SUM(A1)") == "'  @SUM(A1)"
        assert sanitize_cell_value("正常商户") == "正常商户"

    def test_export_sanitizes_all_string_columns_and_excludes_transfers_in_pivot(self):
        conn = init_db(":memory:")
        try:
            conn.execute("INSERT INTO accounts (kind, bank, tail) VALUES ('bank_debit', '=BANK()', '1234')")
            acc_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                "INSERT INTO import_batches (file_name, file_sha1, file_size, source, row_count) VALUES ('a.csv', 'sha1', 10, '=SRC()', 2)"
            )
            batch_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute("INSERT INTO categories (key, name, direction) VALUES ('evil.cat', '=1+1', 'expense')")
            cat_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

            # Normal expense with formula-like category & source
            conn.execute(
                """
                INSERT INTO transactions (
                    batch_id, source, account_id, txn_time, time_precision, amount_cents,
                    currency, direction, counterparty, category_id, fingerprint, amount_cny
                ) VALUES (?, '=SRC()', ?, '2026-10-01 12:00:00', 'second', 5000, 'CNY', 'expense', ' =CMD()', ?, 'fp1', 5000)
                """,
                (batch_id, acc_id, cat_id),
            )
            # Expense marked as internal transfer peer -> must be excluded from Sheet 2 pivot
            conn.execute(
                """
                INSERT INTO transactions (
                    batch_id, source, account_id, txn_time, time_precision, amount_cents,
                    currency, direction, counterparty, category_id, fingerprint, amount_cny, transfer_peer
                ) VALUES (?, 'alipay', ?, '2026-10-02 12:00:00', 'second', 90000, 'CNY', 'expense', '本人转账', ?, 'fp2', 90000, 1)
                """,
                (batch_id, acc_id, cat_id),
            )
            conn.commit()

            stream = generate_excel_export(conn)
            wb = openpyxl.load_workbook(stream, data_only=False)
            ws1 = wb["交易明细"]
            ws2 = wb["月度分类透视表"]

            # Row 3 (2026-10-01) has sanitized source, counterparty, root_cat, acc_label
            sources = [ws1.cell(r, 3).value for r in (2, 3)]
            assert "'=SRC()" in sources
            cats = [ws1.cell(r, 10).value for r in (2, 3)]
            assert all(c == "'=1+1" for c in cats)

            # Sheet 2 pivot: category cell sanitized AND total is 50.00 (not 950.00!)
            assert ws2.cell(2, 1).value == "'=1+1"
            assert ws2.cell(2, 3).value == 50.0
            wb.close()
        finally:
            conn.close()


# =========================================================================
# S-09, B-04, B-05: LLM Zero-PII Multi-Holder, Spaced Cards, full_parse Cache, Review Category
# =========================================================================

class TestS09B04B05LlmPrivacyAndIntegrity:
    def test_zero_pii_masks_spaced_cards_landlines_and_multiple_db_holders(self):
        conn = init_db(":memory:")
        try:
            conn.execute("INSERT INTO accounts (kind, bank, tail, holder) VALUES ('bank_debit', '招行', '1111', '张伟明')")
            conn.execute("INSERT INTO accounts (kind, bank, tail, holder) VALUES ('bank_credit', '工行', '2222', '李小红')")
            conn.commit()
            holders = collect_known_holder_names(conn)
            assert "张伟明" in holders and "李小红" in holders

            raw = "转账给张伟明 卡号 6222 0202 0001 2345 电话 021-88886666 备忘李小红"
            masked = sanitize_prompt_text(raw, holder_name=holders)
            assert "张伟明" not in masked
            assert "李小红" not in masked
            assert "6222 0202 0001 2345" not in masked
            assert "021-88886666" not in masked
            assert "[USER]" in masked and "[CARD]" in masked and "[PHONE]" in masked
        finally:
            conn.close()

    def test_b05_review_prompt_preserves_current_category(self):
        txn = {
            "id": 42,
            "counterparty": "麦当劳",
            "merchant_norm": "麦当劳",
            "description": "午餐",
            "amount_cents": 3500,
            "direction": "expense",
            "category_key": "shopping.daily",
            "category_name": "日用百货",
        }
        sanitized = sanitize_transaction_for_llm(txn)
        assert sanitized["category_key"] == "shopping.daily"
        assert sanitized["category_name"] == "日用百货"
        _, user_prompt = build_review_classification_prompt([txn], [])
        assert "shopping.daily" in user_prompt and "日用百货" in user_prompt

    def test_b04_llm_cache_accepts_full_parse_and_migrates_legacy_db(self):
        conn = sqlite3.connect(":memory:")
        # Create legacy llm_cache WITHOUT 'full_parse' in CHECK constraint
        conn.execute(
            """
            CREATE TABLE llm_cache (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_type TEXT NOT NULL CHECK(task_type IN ('classify', 'review', 'arbitrate', 'merchant_norm', 'suggest_profile')),
                input_hash TEXT NOT NULL UNIQUE,
                response_json TEXT NOT NULL,
                model TEXT NOT NULL,
                prompt_tokens INT DEFAULT 0,
                completion_tokens INT DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
            )
            """
        )
        conn.execute(
            "INSERT INTO llm_cache (task_type, input_hash, response_json, model) VALUES ('classify', 'h1', '{}', 'm1')"
        )
        migrate_db(conn)
        # Now full_parse must succeed and legacy row 'h1' must still exist
        conn.execute(
            "INSERT INTO llm_cache (task_type, input_hash, response_json, model) VALUES ('full_parse', 'h2', '{}', 'm1')"
        )
        count = conn.execute("SELECT COUNT(*) FROM llm_cache").fetchone()[0]
        assert count == 2
        conn.close()

    def test_s09_full_parse_delimiters_and_chunk_cap(self):
        sys_p, usr_p = build_full_statement_parse_prompt(["抬头行"], ["2026-10-01 消费 10.00"])
        assert "<untrusted_statement_data>" in sys_p
        assert "<untrusted_statement_data>" in usr_p

        # Build > MAX_LLM_PARSE_CHUNKS chunks and verify LLM calls are strictly capped
        calls = 0

        def handler(_req: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            body = {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "account": {"bank": "招商银行", "account_kind": "bank_debit", "card_tail": "1234", "currency": "CNY"},
                                    "period": {"start": "2026-10-01", "end": "2026-10-31"},
                                    "transactions": [
                                        {
                                            "txn_time": f"2026-10-{(calls % 28) + 1:02d}",
                                            "amount": 10.0 + calls,
                                            "direction": "expense",
                                            "currency": "CNY",
                                            "counterparty": f"商户{calls}",
                                            "description": "消费",
                                        }
                                    ],
                                }
                            )
                        }
                    }
                ]
            }
            return httpx.Response(200, json=body)

        cfg = LLMConfig(base_url="https://api.example.com/v1", api_key="k", model="m", enabled=True)
        client = LLMClient(cfg, transport=httpx.MockTransport(handler))
        huge_lines = [f"2026-10-01 商户{i} -10.00" for i in range(1500)]
        res, _ = parse_statement_with_llm_sync(huge_lines, client, chunk_size=45)
        assert calls == MAX_LLM_PARSE_CHUNKS
        assert any("上限" in w for w in res.warnings)


# =========================================================================
# S-10: FX URL Injection, Non-Finite Infinity Rate Rejection, Currency Normalization
# =========================================================================

class TestS10FxHardening:
    def test_fx_manual_rate_rejects_infinity_nan_and_injection(self, tmp_path: Path):
        app = create_app(db_path=str(tmp_path / "s10.db"))
        with TestClient(app) as client:
            # 1e309 parses as float('inf') in Python json -> must be rejected with 422
            inf_resp = client.post(
                "/api/fx/rates",
                content='{"currency":"USD","rate":1e309,"effective_date":"2026-10-01"}',
                headers={"Content-Type": "application/json"},
            )
            assert inf_resp.status_code == 422

            # Invalid currency injection
            bad_curr = client.post(
                "/api/fx/rates",
                json={"currency": "USD&evil=1", "rate": 7.2, "effective_date": "2026-10-01"},
            )
            assert bad_curr.status_code == 422

            # Invalid calendar date
            bad_date = client.post(
                "/api/fx/rates",
                json={"currency": "USD", "rate": 7.2, "effective_date": "2026-02-30"},
            )
            assert bad_date.status_code == 422

            # Valid rate succeeds
            ok = client.post(
                "/api/fx/rates",
                json={"currency": "EUR", "rate": 7.85, "effective_date": "2026-10-01"},
            )
            assert ok.status_code == 200

        # Online fetcher rejects invalid params without making requests
        assert fetch_online_fx_rate("INVALID&x=1", "2026-10-01") is None
        assert fetch_online_fx_rate("USD", "../../evil") is None

        # Currency normalizer maps Chinese names accurately
        assert normalize_currency("美元") == "USD"
        assert normalize_currency("欧元") == "EUR"
        assert normalize_currency("港币") == "HKD"
        assert normalize_currency("日元") == "JPY"
        assert normalize_currency("人民币") == "CNY"


# =========================================================================
# B-02, B-03, B-07, B-08: Delete Atomicity, Dead Settings/Filters, Edge Cases
# =========================================================================

class TestB02B03B07B08LogicAndIntegrity:
    def test_b02_single_delete_reverts_refund_transfer_duplicate_and_batch_count(self, tmp_path: Path):
        app = create_app(db_path=str(tmp_path / "b02.db"))
        with TestClient(app) as client:
            conn = sqlite3.connect(str(tmp_path / "b02.db"))
            conn.row_factory = sqlite3.Row
            conn.execute("INSERT INTO accounts (kind, bank, tail) VALUES ('wallet', '微信', NULL)")
            acc1 = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute("INSERT INTO accounts (kind, bank, tail) VALUES ('bank_debit', '招行', '1234')")
            acc2 = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                "INSERT INTO import_batches (file_name, file_sha1, file_size, source, row_count) VALUES ('t.csv', 's1', 10, 'wechat', 6)"
            )
            bid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

            # Expense #1 (100元, refunded 30元) + Refund #2 (30元, refund_of=1)
            conn.execute(
                "INSERT INTO transactions (batch_id, source, account_id, txn_time, time_precision, amount_cents, currency, direction, refunded_cents, fingerprint, amount_cny) VALUES (?, 'wechat', ?, '2026-10-01 10:00:00', 'second', 10000, 'CNY', 'expense', 3000, 'f1', 10000)",
                (bid, acc1),
            )
            exp_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                "INSERT INTO transactions (batch_id, source, account_id, txn_time, time_precision, amount_cents, currency, direction, refund_of, fingerprint, amount_cny) VALUES (?, 'wechat', ?, '2026-10-02 10:00:00', 'second', 3000, 'CNY', 'refund', ?, 'f2', 3000)",
                (bid, acc1, exp_id),
            )
            ref_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

            # Primary #3 + Hidden duplicate #4
            conn.execute(
                "INSERT INTO transactions (batch_id, source, account_id, txn_time, time_precision, amount_cents, currency, direction, fingerprint, amount_cny) VALUES (?, 'wechat', ?, '2026-10-03 10:00:00', 'second', 5000, 'CNY', 'expense', 'f3', 5000)",
                (bid, acc1),
            )
            prim_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                "INSERT INTO transactions (batch_id, source, account_id, txn_time, time_precision, amount_cents, currency, direction, duplicate_of, fingerprint, amount_cny) VALUES (?, 'boc_debit', ?, '2026-10-03 10:00:10', 'second', 5000, 'CNY', 'expense', ?, 'f4', 5000)",
                (bid, acc2, prim_id),
            )
            dup_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

            # Transfer pair #5 <-> #6 with match_link storing orig_dir_in='income'
            conn.execute(
                "INSERT INTO transactions (batch_id, source, account_id, txn_time, time_precision, amount_cents, currency, direction, fingerprint, amount_cny) VALUES (?, 'wechat', ?, '2026-10-04 10:00:00', 'second', 20000, 'CNY', 'transfer', 'f5', 20000)",
                (bid, acc1),
            )
            tr_out = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                "INSERT INTO transactions (batch_id, source, account_id, txn_time, time_precision, amount_cents, currency, direction, transfer_peer, fingerprint, amount_cny) VALUES (?, 'boc_debit', ?, '2026-10-04 10:05:00', 'second', 20000, 'CNY', 'transfer', ?, 'f6', 20000)",
                (bid, acc2, tr_out),
            )
            tr_in = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute("UPDATE transactions SET transfer_peer = ? WHERE id = ?", (tr_in, tr_out))
            conn.execute(
                "INSERT INTO match_links (kind, primary_id, secondary_id, score, features_json, status) VALUES ('transfer', ?, ?, 0.95, ?, 'auto')",
                (tr_out, tr_in, json.dumps({"orig_dir_out": "expense", "orig_dir_in": "income"})),
            )
            conn.commit()
            conn.close()

            # Delete refund -> exp_id.refunded_cents reverts to 0
            assert client.delete(f"/api/transactions/{ref_id}").status_code == 200
            # Delete primary -> dup_id must be excluded (not resurrected!)
            assert client.delete(f"/api/transactions/{prim_id}").status_code == 200
            # Delete transfer out -> transfer in reverts to direction='income' & transfer_peer=NULL
            assert client.delete(f"/api/transactions/{tr_out}").status_code == 200

            conn = sqlite3.connect(str(tmp_path / "b02.db"))
            conn.row_factory = sqlite3.Row
            assert conn.execute("SELECT refunded_cents FROM transactions WHERE id = ?", (exp_id,)).fetchone()["refunded_cents"] == 0
            dup_row = conn.execute("SELECT duplicate_of, excluded FROM transactions WHERE id = ?", (dup_id,)).fetchone()
            assert dup_row["duplicate_of"] is None and dup_row["excluded"] == 1
            tr_row = conn.execute("SELECT transfer_peer, direction FROM transactions WHERE id = ?", (tr_in,)).fetchone()
            assert tr_row["transfer_peer"] is None and tr_row["direction"] == "income"
            assert conn.execute("SELECT row_count FROM import_batches WHERE id = ?", (bid,)).fetchone()["row_count"] == 3
            conn.close()

    def test_b03_stats_include_transfer_status_filter_and_learning_toggle(self, tmp_path: Path):
        app = create_app(db_path=str(tmp_path / "b03.db"))
        with TestClient(app) as client:
            conn = sqlite3.connect(str(tmp_path / "b03.db"))
            conn.row_factory = sqlite3.Row
            conn.execute("INSERT INTO accounts (kind, bank, tail) VALUES ('bank_debit', '中行', '8888')")
            acc = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute("INSERT INTO import_batches (file_name, file_sha1, file_size, source, row_count) VALUES ('x.csv', 'sx', 10, 'alipay', 3)")
            bid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            tr_cat = conn.execute("SELECT id FROM categories WHERE key = 'financial.internal_transfer'").fetchone()
            tr_cat_id = tr_cat["id"] if tr_cat else None
            food_cat = conn.execute("SELECT id FROM categories WHERE key = 'food.dining'").fetchone()["id"]

            conn.execute(
                "INSERT INTO transactions (batch_id, source, account_id, txn_time, time_precision, amount_cents, currency, direction, counterparty, status, category_id, fingerprint, amount_cny) VALUES (?, 'alipay', ?, '2026-10-05 12:00:00', 'second', 4000, 'CNY', 'expense', '测试小馆', '交易成功', ?, 'fx1', 4000)",
                (bid, acc, tr_cat_id),
            )
            conn.execute(
                "INSERT INTO transactions (batch_id, source, account_id, txn_time, time_precision, amount_cents, currency, direction, counterparty, status, fingerprint, amount_cny) VALUES (?, 'alipay', ?, '2026-10-05 13:00:00', 'second', 2000, 'CNY', 'expense', '咖啡店A', '交易关闭', 'fx2', 2000)",
                (bid, acc),
            )
            t2 = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                "INSERT INTO transactions (batch_id, source, account_id, txn_time, time_precision, amount_cents, currency, direction, counterparty, status, fingerprint, amount_cny) VALUES (?, 'alipay', ?, '2026-10-05 14:00:00', 'second', 2500, 'CNY', 'expense', '咖啡店A', '交易成功', 'fx3', 2500)",
                (bid, acc),
            )
            conn.commit()

            # include_transfer propagates to ALL stats endpoints
            trend_no = calculate_stats_trend(conn, include_transfer=False)
            trend_yes = calculate_stats_trend(conn, include_transfer=True)
            assert trend_yes[0].expense_cents > trend_no[0].expense_cents
            cat_no = calculate_stats_category(conn, include_transfer=False)
            cat_yes = calculate_stats_category(conn, include_transfer=True)
            assert cat_yes.total_cents > cat_no.total_cents
            mer_no = calculate_stats_merchant(conn, include_transfer=False)
            mer_yes = calculate_stats_merchant(conn, include_transfer=True)
            assert mer_yes.total_expense_cents > mer_no.total_expense_cents
            hm_no = calculate_stats_heatmap(conn, year=2026, include_transfer=False)
            hm_yes = calculate_stats_heatmap(conn, year=2026, include_transfer=True)
            assert hm_yes.calendar_items[0].expense_cents > hm_no.calendar_items[0].expense_cents
            acc_no = calculate_stats_account(conn, include_transfer=False)
            acc_yes = calculate_stats_account(conn, include_transfer=True)
            assert acc_yes.total_out_cents > acc_no.total_out_cents
            conn.close()

            # status query param actually filters SQL
            closed = client.get("/api/transactions", params={"status": "交易关闭"}).json()
            assert closed["total"] == 1
            assert closed["items"][0]["id"] == t2

            # Disabling learning_suggestions_enabled suppresses rule_proposal on PATCH
            client.put("/api/settings", json={"general": {"learning_suggestions_enabled": False}})
            patch_res = client.patch(f"/api/transactions/{t2}", json={"category_id": food_cat}).json()
            assert patch_res["rule_proposal"] is None

    def test_b07_and_b08_edge_cases(self, tmp_path: Path):
        # B-07: clean_merchant_for_learning with counterparty=None must not raise AttributeError
        assert clean_merchant_for_learning(None, "A") == ""  # type: ignore[arg-type]

        # B-07: malformed txn_time does not crash dedup pipeline
        assert parse_iso_datetime("not-a-date") is None
        conn = init_db(":memory:")
        try:
            conn.execute("INSERT INTO accounts (kind, bank, tail) VALUES ('wallet', '支付宝', NULL)")
            acc = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute("INSERT INTO import_batches (file_name, file_sha1, file_size, source, row_count) VALUES ('bad.csv', 'sb', 10, 'alipay', 1)")
            bid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                "INSERT INTO transactions (batch_id, source, account_id, txn_time, time_precision, amount_cents, currency, direction, fingerprint) VALUES (?, 'alipay', ?, 'BAD_DATE_STR', 'day', 1000, 'CNY', 'expense', 'fpbad')",
                (bid, acc),
            )
            conn.commit()
            summary = run_dedup_pipeline(conn)
            assert summary.duplicates_auto == 0

            # B-07: refund.py does not over-refund partially refunded original expense
            conn.execute(
                "INSERT INTO transactions (batch_id, source, account_id, txn_time, time_precision, amount_cents, refunded_cents, currency, direction, counterparty, merchant_norm, fingerprint) VALUES (?, 'boc_debit', ?, '2026-10-01 10:00:00', 'second', 10000, 6000, 'CNY', 'expense', '商户X', '商户X', 'fpref1')",
                (bid, acc),
            )
            conn.execute(
                "INSERT INTO transactions (batch_id, source, account_id, txn_time, time_precision, amount_cents, currency, direction, counterparty, merchant_norm, fingerprint) VALUES (?, 'boc_debit', ?, '2026-10-02 10:00:00', 'second', 10000, 'CNY', 'refund', '商户X', '商户X', 'fpref2')",
                (bid, acc),
            )
            conn.commit()
            # Remaining refundable is only 4000 < 10000 -> must NOT link!
            assert link_in_source_refunds(conn) == 0
        finally:
            conn.close()

        # B-07: BankProfile supports 'wallet' account_kind and 'channel' column mapping
        wallet_yaml = """
id: custom_wallet
name: Wallet CSV
bank: CustomWallet
account_kind: wallet
column_mapping:
  date: "日期"
  amount: "金额"
  counterparty: "商户"
  channel: "渠道"
amount_mode:
  type: signed
"""
        prof = BankProfile.from_yaml(wallet_yaml)
        assert prof.account_kind_full == "wallet"
        csv_data = "日期,金额,商户,渠道\n2026-10-01,-15.00,星巴克,alipay\n".encode("utf-8")
        res = ProfileImporter(prof).parse(FileProbe(csv_data, file_name="w.csv"))
        assert res.accounts[0].kind == "wallet"
        assert res.transactions[0].channel == "alipay"

        # B-08: to_cents rejects booleans & parses scientific notation; PATCH rejects excluded=2 with 422
        with pytest.raises(ValueError):
            to_cents(True)
        with pytest.raises(ValueError):
            to_cents(False)
        assert to_cents("1e3") == 100000

        app = create_app(db_path=str(tmp_path / "b08.db"))
        with TestClient(app) as client:
            bad_excl = client.patch("/api/transactions/999", json={"excluded": 2})
            assert bad_excl.status_code == 422
