"""Tests for diagnostic log rotation, secret/PII redaction, and /api/logs/* export endpoints."""

from __future__ import annotations

import io
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import sqlite3
import zipfile

from fastapi.testclient import TestClient

from billapp.app import APP_VERSION, create_app
from billapp.core.logs import (
    attach_package_logging,
    format_local_log_timestamp,
    redact_log_text,
    sanitize_archive_filename,
    setup_file_logging,
)


def test_redact_log_text_always_strips_secrets_and_optional_pii():
    home_sample = str(Path.home() / "Documents" / "secret_bill.csv")
    raw = (
        "Authorization: Bearer tok_super_secret_12345 "
        "X-BillApp-Token: deadbeefcafebabe "
        "url=http://127.0.0.1:8000/?boot_token=boot123456 "
        "Cookie: billapp_session=sess987654; "
        "BILLAPP_API_TOKEN=envtok999 BILLAPP_MASTER_KEY=envmk888 "
        "key=sk-proj-abcdef1234567890 "
        "stored=encv1:QUJDREVGR0hJSktMTU5PUA== "
        "proxy=https://alice:topsecret@proxy.example.com:8080 "
        "password=my_bank_pwd "
        "holder=张伟明 card=6222 0202 0001 2345 card2=6222020200012345678 "
        "id=11010519900101123X phone=13812345678 "
        f"path={home_sample}"
    )

    # Even with redact_pii=False, ALL credentials/tokens MUST be redacted
    secrets_only = redact_log_text(raw, holder_names=["张伟明"], redact_pii=False)
    for forbidden in (
        "tok_super_secret_12345",
        "deadbeefcafebabe",
        "boot123456",
        "sess987654",
        "envtok999",
        "envmk888",
        "sk-proj-abcdef1234567890",
        "QUJDREVGR0hJSktMTU5PUA==",
        "alice:topsecret",
        "my_bank_pwd",
    ):
        assert forbidden not in secrets_only
    # PII remains intact when redact_pii=False
    assert "张伟明" in secrets_only
    assert "13812345678" in secrets_only

    # With redact_pii=True, financial & personal PII + home directory are also redacted
    full_redacted = redact_log_text(raw, holder_names=["张伟明"], redact_pii=True)
    for forbidden in (
        "张伟明",
        "6222 0202 0001 2345",
        "6222020200012345678",
        "11010519900101123X",
        "13812345678",
    ):
        assert forbidden not in full_redacted
    assert "[USER]" in full_redacted
    assert "[CARD_REDACTED]" in full_redacted
    assert "[ID_REDACTED]" in full_redacted
    assert "[PHONE_REDACTED]" in full_redacted
    if len(str(Path.home())) > 3:
        assert str(Path.home()) not in full_redacted


def test_sanitize_archive_filename_blocks_traversal():
    assert sanitize_archive_filename("../../etc/passwd") == "passwd"
    assert sanitize_archive_filename("..\\..\\windows\\win.ini") == "win.ini"
    assert sanitize_archive_filename("desktop.log.1") == "desktop.log.1"
    assert sanitize_archive_filename("   ", fallback="fallback.log") == "fallback.log"


def test_logs_info_and_export_zip_and_text_formats(tmp_path: Path):
    db_file = tmp_path / "logs_test.db"
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    primary_log = log_dir / "launcher.log"
    rotated_log = log_dir / "launcher.log.1"

    primary_log.write_text(
        "2026-11-25 10:00:00 [INFO] [billapp.launcher] Starting backend\n"
        "2026-11-25 10:00:05 [WARNING] [billapp.llm.client] Retry error Authorization: Bearer secret_abc123 "
        "key=sk-live-secret9876543210 holder=张伟明 card=6222 0202 0001 2345 phone=13900001111\n",
        encoding="utf-8",
    )
    rotated_log.write_text(
        "2026-11-24 09:00:00 [INFO] [billapp.app] Previous run X-BillApp-Token: oldtok123\n",
        encoding="utf-8",
    )

    app = create_app(db_path=str(db_file), log_path=primary_log)
    with TestClient(app) as client:
        # Seed account holder name and LLM config
        conn = sqlite3.connect(str(db_file))
        conn.execute(
            "INSERT INTO accounts (kind, bank, tail, holder) VALUES ('bank_debit', '招行', '1234', '张伟明')"
        )
        conn.commit()
        conn.close()

        client.put(
            "/api/settings",
            json={
                "llm": {
                    "base_url": "https://api.deepseek.com/v1",
                    "api_key": "sk-do-not-leak-in-diagnostics-999999",
                    "model": "deepseek-chat",
                    "enabled": True,
                }
            },
        )

        # 1. GET /api/logs/info
        info_resp = client.get("/api/logs/info", params={"tail_lines": 50, "redact_pii": True})
        assert info_resp.status_code == 200
        info = info_resp.json()
        assert info["has_logs"] is True
        assert [f["name"] for f in info["files"]] == ["launcher.log", "launcher.log.1"]
        assert info["total_size_bytes"] > 0
        tail_joined = "\n".join(info["tail_lines"])
        assert "Starting backend" in tail_joined
        assert "secret_abc123" not in tail_joined
        assert "sk-live-secret9876543210" not in tail_joined
        assert "张伟明" not in tail_joined
        assert "13900001111" not in tail_joined

        # 2. POST /api/logs/export (ZIP format)
        export_payload = {
            "format": "zip",
            "include_diagnostics": True,
            "redact_pii": True,
            "frontend_logs": [
                {
                    "timestamp": "2026-11-25T10:01:00.000Z",
                    "level": "error",
                    "source": "api",
                    "message": "POST /api/llm/test failed with sk-frontend-secret123456 for 张伟明",
                    "stack": "Error: boom\n    at test.ts:10",
                }
            ],
            "desktop_logs": [
                {
                    "name": "../../desktop.log",
                    "content": "2026-11-25 10:00:01 [INFO] [updater] Checking updates boot_token=secretboot\n",
                }
            ],
            "desktop_meta": {
                "platform": "win32",
                "arch": "x64",
                "appVersion": APP_VERSION,
                "isPackaged": True,
                "installMode": "nsis",
                "versions": {"electron": "45.3.2", "chrome": "134.0", "node": "22.0"},
            },
        }
        zip_resp = client.post("/api/logs/export", json=export_payload)
        assert zip_resp.status_code == 200
        assert "application/zip" in zip_resp.headers.get("content-type", "")
        assert ".zip" in zip_resp.headers.get("content-disposition", "")

        with zipfile.ZipFile(io.BytesIO(zip_resp.content), "r") as zf:
            names = set(zf.namelist())
            assert "diagnostics.txt" in names
            assert "diagnostics.json" in names
            assert "backend/launcher.log" in names
            assert "backend/launcher.log.1" in names
            assert "desktop/desktop.log" in names
            assert "frontend/runtime.log" in names
            # Ensure no path traversal segments survived into archive names
            assert all(".." not in n for n in names)

            diag = json.loads(zf.read("diagnostics.json").decode("utf-8"))
            assert diag["database"]["counts"]["accounts"] == 1
            assert diag["llm"]["model"] == "deepseek-chat"
            assert diag["desktop"]["installMode"] == "nsis"
            assert diag["redaction"]["secrets_redacted"] is True
            assert diag["redaction"]["pii_redacted"] is True

            # Verify zero secret/PII leakage across the entire ZIP bundle
            for entry_name in names:
                text = zf.read(entry_name).decode("utf-8")
                for secret in (
                    "secret_abc123",
                    "sk-live-secret9876543210",
                    "sk-do-not-leak-in-diagnostics-999999",
                    "sk-frontend-secret123456",
                    "oldtok123",
                    "secretboot",
                    "张伟明",
                    "6222 0202 0001 2345",
                    "13900001111",
                ):
                    assert secret not in text, f"Leaked {secret!r} in {entry_name}"

        # 3. POST /api/logs/export (plain text .log format)
        txt_resp = client.post("/api/logs/export", json={**export_payload, "format": "log"})
        assert txt_resp.status_code == 200
        assert "text/plain" in txt_resp.headers.get("content-type", "")
        assert ".log" in txt_resp.headers.get("content-disposition", "")
        txt_body = txt_resp.content.decode("utf-8")
        assert "BillApp (账单助手) 诊断日志摘要" in txt_body
        assert "--- [BACKEND LOG: launcher.log] ---" in txt_body
        assert "--- [DESKTOP LOG: desktop.log] ---" in txt_body
        assert "--- [FRONTEND RUNTIME LOG] ---" in txt_body
        assert "sk-live-secret9876543210" not in txt_body
        assert "张伟明" not in txt_body


def test_logs_export_handles_missing_log_file_gracefully(tmp_path: Path):
    db_file = tmp_path / "empty_logs.db"
    missing_log = tmp_path / "nonexistent_dir" / "launcher.log"
    app = create_app(db_path=str(db_file), log_path=missing_log)

    with TestClient(app) as client:
        info = client.get("/api/logs/info").json()
        assert info["has_logs"] is False
        assert info["files"] == []
        assert info["tail_lines"] == []

        resp_zip = client.post("/api/logs/export", json={"format": "zip"})
        assert resp_zip.status_code == 200
        with zipfile.ZipFile(io.BytesIO(resp_zip.content), "r") as zf:
            assert "diagnostics.txt" in zf.namelist()
            assert "backend/launcher.log" in zf.namelist()

        resp_log = client.post("/api/logs/export", json={"format": "log"})
        assert resp_log.status_code == 200
        assert "诊断日志摘要" in resp_log.content.decode("utf-8")


def test_logs_endpoints_enforce_token_auth_post_only_and_origin_defense(tmp_path: Path):
    db_file = tmp_path / "auth_logs.db"
    token = "b" * 64
    app = create_app(db_path=str(db_file), api_token=token, log_path=tmp_path / "launcher.log")

    with TestClient(app, base_url="http://127.0.0.1:18998") as client:
        assert client.get("/api/logs/info").status_code == 401
        assert client.post("/api/logs/export", json={"format": "log"}).status_code == 401

        # Authenticated requests succeed
        assert client.get("/api/logs/info", headers={"X-BillApp-Token": token}).status_code == 200
        assert (
            client.post(
                "/api/logs/export",
                json={"format": "log"},
                headers={"X-BillApp-Token": token},
            ).status_code
            == 200
        )

        # GET /api/logs/export must not be allowed (POST-only CSRF defense)
        get_export = client.get("/api/logs/export", headers={"X-BillApp-Token": token})
        assert get_export.status_code in (404, 405)

        # Cross-origin POST must be blocked with 403 even with valid token
        csrf_resp = client.post(
            "/api/logs/export",
            json={"format": "zip"},
            headers={"X-BillApp-Token": token, "Origin": "https://evil.example"},
        )
        assert csrf_resp.status_code == 403


def test_rotating_file_logging_captures_subpackage_logs_without_duplication(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("BILLAPP_DATA_DIR", str(tmp_path / "appdata"))
    test_launcher = logging.getLogger("billapp.launcher")
    test_pkg = logging.getLogger("billapp")
    test_app = logging.getLogger("billapp.app")
    test_sub = logging.getLogger("billapp.llm.client")

    orig_launcher_handlers = list(test_launcher.handlers)
    orig_launcher_prop = test_launcher.propagate
    orig_app_handlers = list(test_app.handlers)
    orig_app_prop = test_app.propagate
    orig_pkg_handlers = list(test_pkg.handlers)

    test_launcher.handlers = []
    test_app.handlers = []
    test_pkg.handlers = []

    created_handlers: list[logging.Handler] = []
    try:
        log_path = setup_file_logging(target_logger=test_launcher)
        assert log_path is not None
        assert log_path.name == "launcher.log"
        assert any(isinstance(h, RotatingFileHandler) for h in test_launcher.handlers)
        attach_package_logging()

        created_handlers = [
            h
            for lgr in (test_launcher, test_app, test_pkg, logging.getLogger("uvicorn.error"))
            for h in lgr.handlers
            if isinstance(h, logging.FileHandler) and str(log_path) == getattr(h, "baseFilename", "")
        ]

        test_launcher.info("UNIQUE_LAUNCHER_LINE_123")
        test_sub.warning("UNIQUE_LLM_SUBMODULE_LINE_456")

        for h in created_handlers:
            h.flush()

        content = log_path.read_text(encoding="utf-8")
        assert content.count("UNIQUE_LAUNCHER_LINE_123") == 1
        assert content.count("UNIQUE_LLM_SUBMODULE_LINE_456") == 1
        # Verify timestamps use local time with explicit millisecond + UTC offset format
        import re

        assert re.search(
            r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}[+-]\d{2}:\d{2} \[INFO\] \[billapp\.launcher\] UNIQUE_LAUNCHER_LINE_123",
            content,
        )
    finally:
        for lgr in (test_launcher, test_app, test_pkg, logging.getLogger("uvicorn.error")):
            for h in list(lgr.handlers):
                if h in created_handlers:
                    lgr.removeHandler(h)
        for h in set(created_handlers):
            h.close()
        test_launcher.handlers = orig_launcher_handlers
        test_launcher.propagate = orig_launcher_prop
        test_app.handlers = orig_app_handlers
        test_app.propagate = orig_app_prop
        test_pkg.handlers = orig_pkg_handlers


def test_import_and_security_operations_emit_sanitized_logs_with_timezone(tmp_path: Path, caplog):
    import re

    ts = format_local_log_timestamp()
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}[+-]\d{2}:\d{2}", ts)

    db_file = tmp_path / "ops_logs.db"
    token = "c" * 64
    app = create_app(db_path=str(db_file), api_token=token, log_path=tmp_path / "launcher.log")

    with caplog.at_level(logging.INFO, logger="billapp"), TestClient(
        app, base_url="http://127.0.0.1:18997"
    ) as client:
        # 1. Unauthorized POST must be logged without leaking token
        r_unauth = client.post("/api/backup")
        assert r_unauth.status_code == 401

        # 2. Import preview receipt & completion must be logged with extension/size only (no PII filename)
        csv_body = (
            "交易时间,交易分类,交易对方,对方账号,商品说明,收/支,金额,收/付款方式,交易状态,交易订单号,商家订单号,备注\n"
            "2026-10-10 12:00:00,餐饮美食,测试餐厅,,午餐,支出,35.00,余额宝,交易成功,202610100001,M001,\n"
        ).encode("utf-8")
        r_prev = client.post(
            "/api/import/preview",
            files={"file": ("张伟明_秘密账单.csv", csv_body, "text/csv")},
            headers={"X-BillApp-Token": token},
        )
        assert r_prev.status_code == 200

        # 3. Export diagnostics must include runtime timezone
        r_exp = client.post(
            "/api/logs/export",
            json={"format": "zip"},
            headers={"X-BillApp-Token": token},
        )
        assert r_exp.status_code == 200
        with zipfile.ZipFile(io.BytesIO(r_exp.content), "r") as zf:
            diag = json.loads(zf.read("diagnostics.json").decode("utf-8"))
            assert re.search(r"[+-]\d{2}:\d{2}$", diag["exported_at"])
            assert diag["runtime"].get("timezone", "").startswith("UTC")
            for bf in diag["logs"]["backend_files"]:
                assert re.search(r"[+-]\d{2}:\d{2}$", bf["modified_at"])

    joined = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "UNAUTHORIZED" in joined
    assert "Import preview received: ext=.csv" in joined
    assert "API POST /api/import/preview -> 200" in joined
    assert "张伟明" not in joined
    assert token not in joined


def test_unhandled_api_exception_is_logged_by_middleware(monkeypatch, tmp_path: Path, caplog):
    db_file = tmp_path / "err_logs.db"
    app = create_app(db_path=str(db_file), log_path=tmp_path / "launcher.log")

    def _boom(*_args, **_kwargs):
        raise RuntimeError("simulated crash")

    monkeypatch.setattr("billapp.api.logs.discover_backend_log_files", _boom)

    with caplog.at_level(logging.ERROR, logger="billapp"), TestClient(
        app, raise_server_exceptions=False
    ) as client:
        resp = client.get("/api/logs/info")
        assert resp.status_code == 500

    joined = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "API GET /api/logs/info -> 500" in joined
    assert "unhandled=RuntimeError" in joined
