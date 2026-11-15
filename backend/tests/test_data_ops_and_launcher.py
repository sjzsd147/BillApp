"""Tests for DB backup/restore/reset endpoints, active db_path reporting, and launcher proxy bypass."""

from __future__ import annotations

from pathlib import Path
import sqlite3
import sys
import urllib.request

from fastapi.testclient import TestClient
import pytest

from billapp.app import create_app
from billapp.core.db import create_connection
from billapp.launcher import wait_for_server
try:
    from backend.tests.fixtures.synthetic_alipay import generate_synthetic_alipay_csv
except ImportError:
    from tests.fixtures.synthetic_alipay import generate_synthetic_alipay_csv


def test_settings_reports_active_db_path(tmp_path: Path):
    db_file = tmp_path / "custom_active.db"
    app = create_app(db_path=str(db_file))
    with TestClient(app) as client:
        resp = client.get("/api/settings")
        assert resp.status_code == 200
        data = resp.json()
        assert data["db_path"] == str(db_file)
        assert data["db_size_bytes"] > 0


def test_backup_restore_and_reset_roundtrip(tmp_path: Path):
    db_file = tmp_path / "roundtrip.db"
    app = create_app(db_path=str(db_file))
    csv_bytes = generate_synthetic_alipay_csv()

    with TestClient(app) as client:
        # 1. Import synthetic Alipay CSV
        prev = client.post(
            "/api/import/preview",
            files={"file": ("alipay.csv", csv_bytes, "text/csv")},
        )
        assert prev.status_code == 200
        token = prev.json()["token"]
        commit = client.post("/api/import/commit", json={"token": token})
        assert commit.status_code == 200
        imported_count = client.get("/api/transactions").json()["total"]
        assert imported_count > 0

        # 2. Backup DB
        bak = client.post("/api/backup")
        assert bak.status_code == 200
        assert bak.content.startswith(b"SQLite format 3\x00")
        assert "billapp-backup-" in bak.headers.get("content-disposition", "")

        # 3. Reset requires exact confirm_text
        bad_reset = client.post("/api/data/reset", json={"confirm_text": "wrong"})
        assert bad_reset.status_code == 422

        # 4. Valid reset clears transactions, batches, accounts, while keeping builtin categories
        ok_reset = client.post(
            "/api/data/reset",
            json={"confirm_text": "确认清空", "keep_rules": True, "keep_settings": True},
        )
        assert ok_reset.status_code == 200
        assert ok_reset.json()["success"] is True
        assert client.get("/api/transactions").json()["total"] == 0
        assert client.get("/api/accounts").json() == []
        assert len(client.get("/api/categories").json()) > 0

        # 5. Reject corrupted restore payload
        bad_restore = client.post(
            "/api/restore",
            files={"file": ("bad.db", b"not-a-sqlite-file" * 10, "application/octet-stream")},
        )
        assert bad_restore.status_code == 422

        # 6. Restore valid backup brings transactions back
        ok_restore = client.post(
            "/api/restore",
            files={"file": ("backup.db", bak.content, "application/octet-stream")},
        )
        assert ok_restore.status_code == 200
        restored = ok_restore.json()
        assert restored["success"] is True
        assert restored["transactions_count"] == imported_count
        assert client.get("/api/transactions").json()["total"] == imported_count


def test_wait_for_server_bypasses_http_proxy(monkeypatch):
    """Ensure launcher health check bypasses HTTP_PROXY / system proxy for 127.0.0.1."""
    called_urls = []

    class DummyResp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9999")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9999")

    import billapp.launcher as launcher_mod

    monkeypatch.setattr(
        launcher_mod._NO_PROXY_OPENER,
        "open",
        lambda url, timeout=0.5: (called_urls.append(url), DummyResp())[1],
    )
    assert wait_for_server(18001, timeout=1.0) is True
    assert called_urls == ["http://127.0.0.1:18001/api/health"]


def _seed_unclassified_txn(conn: sqlite3.Connection, source: str, counterparty: str, desc: str, fp: str) -> int:
    conn.execute(
        "INSERT INTO accounts (kind, bank, tail, alias, currency) VALUES ('wallet', ?, NULL, ?, 'CNY')",
        (source, source),
    )
    acc_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        """
        INSERT INTO import_batches (file_name, file_sha1, file_size, source, row_count, imported_at)
        VALUES ('seed.csv', ?, 100, ?, 1, datetime('now', 'localtime'))
        """,
        (fp, source),
    )
    batch_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        """
        INSERT INTO transactions (
            batch_id, account_id, txn_time, time_precision, amount_cents, currency,
            direction, counterparty, description, source, fingerprint
        ) VALUES (?, ?, '2026-11-08 12:00:00', 'second', 2500, 'CNY', 'expense', ?, ?, ?, ?)
        """,
        (batch_id, acc_id, counterparty, desc, source, fp),
    )
    conn.commit()
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def test_classify_batch_persists_for_sqlite_rows(tmp_path):
    """Ensure ClassificationPipeline.classify_batch(..., update_db=True) persists on sqlite3.Row records."""
    from billapp.classify.pipeline import ClassificationPipeline

    db_path = tmp_path / "cls_row.db"
    with TestClient(create_app(db_path=str(db_path))):
        with create_connection(db_path) as conn:
            txn_id = _seed_unclassified_txn(conn, "alipay", "麦当劳", "午餐", "fp-row-1")
            rows = conn.execute("SELECT * FROM transactions WHERE id = ?", (txn_id,)).fetchall()
            pipeline = ClassificationPipeline(conn)
            _, summary = pipeline.classify_batch(rows, update_db=True)
            assert summary.builtin_rule_matched == 1
            saved = conn.execute("SELECT category_id, category_source FROM transactions WHERE id = ?", (txn_id,)).fetchone()
            assert saved["category_id"] is not None
            assert saved["category_source"] == "builtin_rule"


def test_llm_classify_endpoint_updates_null_category_source_rows(tmp_path, monkeypatch):
    """Ensure /api/llm/classify selects COALESCE(category_source, '') != 'manual' and persists to DB."""
    from billapp.classify.pipeline import ClassificationResult

    db_path = tmp_path / "llm_null_src.db"
    with TestClient(create_app(db_path=str(db_path))) as client:
        with create_connection(db_path) as conn:
            txn_id = _seed_unclassified_txn(conn, "wechat", "未知测试商户", "测试商品", "fp-llm-1")
            cat_id = conn.execute("SELECT id FROM categories WHERE key = 'food.dining'").fetchone()[0]

        client.put(
            "/api/settings",
            json={
                "llm": {
                    "enabled": True,
                    "base_url": "http://127.0.0.1:9999/v1",
                    "api_key": "test-key",
                    "model": "test-model",
                    "task_classify_enabled": True,
                }
            },
        )

        import billapp.api.llm as llm_api

        def fake_classify(txns, _client, key_to_id=None, holder_name=None):
            return [
                ClassificationResult(
                    category_id=cat_id,
                    source="llm",
                    confidence=0.93,
                    reason="Mock dining match",
                    category_key="food.dining",
                )
                for _ in txns
            ]

        monkeypatch.setattr(llm_api, "classify_fallback_sync", fake_classify)
        resp = client.post("/api/llm/classify", json={"limit": 10, "update_db": True})
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True
        assert body["classified_count"] == 1
        assert body["results"][0]["transaction_id"] == txn_id
        assert body["results"][0]["updated"] is True

        with create_connection(db_path) as conn:
            saved = conn.execute(
                "SELECT category_id, category_source, category_confidence FROM transactions WHERE id = ?",
                (txn_id,),
            ).fetchone()
            assert saved["category_id"] == cat_id
            assert saved["category_source"] == "llm"
            assert saved["category_confidence"] == pytest.approx(0.93)


def test_launcher_and_paths_survive_unwritable_appdata_and_temp(monkeypatch, tmp_path: Path):
    """Startup must not crash when APPDATA is unwritable and tempfile.gettempdir fails."""
    import logging
    import tempfile
    import billapp.launcher as launcher_mod
    import billapp.paths as paths_mod

    fake_home = tmp_path / "home"
    (fake_home / "AppData" / "LocalLow").mkdir(parents=True)
    monkeypatch.delenv("BILLAPP_DATA_DIR", raising=False)
    monkeypatch.setenv("APPDATA", str(tmp_path / "blocked_appdata"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "blocked_xdg"))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    real_is_writable = paths_mod.is_dir_writable

    def mock_is_writable(path: Path) -> bool:
        s = str(path)
        if (
            "blocked_appdata" in s
            or "blocked_xdg" in s
            or "Application Support" in s
            or ".local" in s
        ):
            return False
        return real_is_writable(path)

    monkeypatch.setattr(paths_mod, "is_dir_writable", mock_is_writable)

    def raise_no_temp():
        raise FileNotFoundError(2, "No usable temporary directory found")

    monkeypatch.setattr(tempfile, "gettempdir", raise_no_temp)
    monkeypatch.setattr(launcher_mod.logger, "handlers", [])
    try:
        resolved_dir = paths_mod.get_appdata_dir()
        assert resolved_dir.exists()
        assert "LocalLow" in str(resolved_dir) or ".billapp" in str(resolved_dir)
        assert "LocalLow" in str(paths_mod.get_appdata_dir(platform="win32"))

        log_path = launcher_mod._setup_file_logging()
        assert log_path is not None
        assert log_path.name == "launcher.log"

        ensured_tmp = launcher_mod._ensure_usable_tempdir()
        assert ensured_tmp is not None and ensured_tmp.exists()
    finally:
        for log_name in ("billapp.launcher", "billapp.app", "uvicorn.error"):
            target_logger = logging.getLogger(log_name)
            for h in list(target_logger.handlers):
                if isinstance(h, logging.FileHandler):
                    target_logger.removeHandler(h)
                    h.close()


def test_wait_for_server_returns_early_when_server_thread_exits():
    """wait_for_server must abort immediately instead of sleeping full timeout if thread exits."""
    import threading
    import time
    import billapp.launcher as launcher_mod

    dead_thread = threading.Thread(target=lambda: None)
    dead_thread.start()
    dead_thread.join()

    started = time.monotonic()
    ok = launcher_mod.wait_for_server(port=59999, timeout=5.0, server_thread=dead_thread)
    elapsed = time.monotonic() - started
    assert ok is False
    assert elapsed < 1.0


def test_get_appdata_dir_and_create_app_thread_safe(monkeypatch, tmp_path: Path):
    """Concurrent callers must resolve identical APPDATA path without probe-file collisions."""
    from concurrent.futures import ThreadPoolExecutor
    import billapp.paths as paths_mod
    from billapp.app import create_app

    target_appdata = tmp_path / "roaming"
    monkeypatch.delenv("BILLAPP_DATA_DIR", raising=False)
    if sys.platform == "win32":
        monkeypatch.setenv("APPDATA", str(target_appdata))
        expected_dir = target_appdata / "BillApp"
    elif sys.platform == "darwin":
        fake_home = tmp_path / "darwin_home"
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
        expected_dir = fake_home / "Library" / "Application Support" / "BillApp"
    else:
        monkeypatch.setenv("XDG_DATA_HOME", str(target_appdata))
        expected_dir = target_appdata / "BillApp"

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(lambda _: paths_mod.get_appdata_dir(), range(60)))

    assert all(p == expected_dir for p in results)

    app = create_app(db_path=None)
    assert app.state.db_path == expected_dir / "bill.db"


def test_cross_platform_paths_resolution(monkeypatch, tmp_path: Path):
    """paths.py resolves standard macOS, Linux/XDG, and explicit env override directories cleanly."""
    import billapp.paths as paths_mod

    fake_home = tmp_path / "home"
    fake_home.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    monkeypatch.delenv("BILLAPP_DATA_DIR", raising=False)
    monkeypatch.delenv("BILLAPP_DB_PATH", raising=False)
    monkeypatch.delenv("BILLAPP_FRONTEND_DIR", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)

    # macOS standard Application Support
    mac_dir = paths_mod.get_appdata_dir(platform="darwin")
    assert mac_dir == fake_home / "Library" / "Application Support" / "BillApp"
    assert mac_dir.is_dir()

    # Linux default ~/.local/share and explicit XDG_DATA_HOME
    linux_default = paths_mod.get_appdata_dir(platform="linux")
    assert linux_default == fake_home / ".local" / "share" / "BillApp"
    xdg_custom = tmp_path / "xdg_data"
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg_custom))
    assert paths_mod.get_appdata_dir(platform="linux") == xdg_custom / "BillApp"

    # Explicit overrides
    custom_data = tmp_path / "custom_data"
    custom_db = tmp_path / "custom" / "ledger.db"
    custom_dist = tmp_path / "frontend_dist"
    custom_dist.mkdir()
    monkeypatch.setenv("BILLAPP_DATA_DIR", str(custom_data))
    monkeypatch.setenv("BILLAPP_DB_PATH", str(custom_db))
    monkeypatch.setenv("BILLAPP_FRONTEND_DIR", str(custom_dist))
    assert paths_mod.get_appdata_dir(platform="darwin") == custom_data
    assert paths_mod.get_default_db_path() == custom_db
    assert paths_mod.get_frontend_dist_dir() == custom_dist


def test_sidecar_launcher_ready_signal_and_stdin_eof_exit(tmp_path: Path):
    """Sidecar emits BILLAPP_BACKEND_READY JSON, serves /api/health, and exits cleanly when stdin closes."""
    import json
    import os
    import subprocess
    import sys
    import urllib.request

    db_file = tmp_path / "sidecar_test.db"
    launcher_script = Path(__file__).resolve().parent.parent / "billapp" / "launcher.py"
    env = dict(os.environ)
    env["NO_PROXY"] = "127.0.0.1,localhost"
    env["BILLAPP_DATA_DIR"] = str(tmp_path / "appdata")

    proc = subprocess.Popen(
        [
            sys.executable,
            str(launcher_script),
            "--sidecar",
            "--watch-stdin",
            "--port",
            "0",
            "--db",
            str(db_file),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        env=env,
    )
    try:
        ready_payload = None
        for _ in range(40):
            line = proc.stdout.readline() if proc.stdout else ""
            if not line:
                break
            if line.startswith("BILLAPP_BACKEND_READY "):
                ready_payload = json.loads(line[len("BILLAPP_BACKEND_READY "):].strip())
                break

        assert ready_payload is not None, f"Missing ready payload; stderr={proc.stderr.read() if proc.stderr else ''}"
        port = int(ready_payload["port"])
        assert port > 0
        assert ready_payload["url"] == f"http://127.0.0.1:{port}"
        assert Path(ready_payload["db_path"]) == db_file.resolve()

        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"{ready_payload['url']}/api/health", timeout=3.0) as resp:
            assert resp.status == 200
            health = json.loads(resp.read().decode("utf-8"))
            assert health["status"] == "ok"

        # Closing parent stdin pipe simulates Electron parent exit/crash -> sidecar must exit cleanly
        assert proc.stdin is not None
        proc.stdin.close()
        exit_code = proc.wait(timeout=10.0)
        assert exit_code == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5.0)
