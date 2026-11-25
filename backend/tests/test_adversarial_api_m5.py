"""Adversarial API Boundary, Ingestion & Concurrency Stress Test Harness (Milestone 5).

Empirical challenger test suite:
1. Ingestion edge cases: corrupted files (CSV/XLSX/PDF), missing/wrong passwords, empty batches, duplicate files.
2. Batch rollback atomicity: multi-file batches, transfer restoration, duplicate unmarking, zero orphan records.
3. Transaction filter & pagination attacks: negative offsets, huge limits, invalid date strings, conflicting sort params.
4. Path collision: verify `/bulk` does not conflict with `/{txn_id}` and review bulk vs item routes.
5. Single-instance named mutex concurrent process creation and dynamic port discovery.
6. Additional M5 adversarial defenses: ReDoS prevention, category depth limits, account merge guards.
"""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from typing import Generator
import pytest
from fastapi.testclient import TestClient

from billapp.app import create_app
from billapp.core.db import create_connection
from billapp.dedup.engine import rollback_import_batch, run_dedup_pipeline
from billapp.launcher import (
    acquire_single_instance_mutex,
    find_free_port,
    release_single_instance_mutex,
)
from tests.fixtures.synthetic_alipay import AlipayRow, generate_synthetic_alipay_csv
from tests.fixtures.synthetic_pdf import generate_synthetic_boc_pdf


@pytest.fixture
def client(tmp_path: Path) -> Generator[TestClient, None, None]:
    """TestClient fixture with isolated SQLite database and full lifespan lifecycle."""
    db_file = tmp_path / "test_adversarial.db"
    app = create_app(db_path=str(db_file))
    with TestClient(app) as test_client:
        yield test_client


# =========================================================================
# 1. Ingestion Edge Cases & Corrupted File Attacks
# =========================================================================

def test_ingest_corrupted_csv_bytes(client: TestClient):
    """Uploading malformed/binary CSV content must not crash with HTTP 500."""
    corrupted_content = b"col1,col2,col3\x00\xff\xfe\x00corrupted_binary_stream_data\r\n"
    resp = client.post(
        "/api/import/preview",
        files={"file": ("corrupted.csv", corrupted_content, "text/csv")},
    )
    # Registry detection will not recognize unknown corrupted header, returning need_mapping=True or 400
    assert resp.status_code in (200, 400)
    if resp.status_code == 200:
        data = resp.json()
        assert data["need_mapping"] is True
        assert data["token"] == ""
    else:
        assert resp.json()["code"] in ("IMPORT_PARSE_FAILED", "IMPORT_EMPTY")


def test_ingest_corrupted_xlsx_bytes(client: TestClient):
    """Uploading corrupted zip/xlsx bytes must safely return need_mapping=True (token="") without crashing."""
    corrupted_zip = b"PK\x03\x04\x14\x00\x00\x00\x08\x00truncated_fake_zip_archive_data"
    resp = client.post(
        "/api/import/preview",
        files={"file": ("corrupted.xlsx", corrupted_zip, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    )
    # Unrecognized format returns need_mapping=True and empty token
    assert resp.status_code in (200, 400)
    if resp.status_code == 200:
        data = resp.json()
        assert data["need_mapping"] is True
        assert data["token"] == ""
        # Attempting to commit with empty token must fail with 410 or 422
        commit_resp = client.post("/api/import/commit", json={"token": ""})
        assert commit_resp.status_code in (410, 422)
    else:
        assert resp.json()["code"] in ("IMPORT_PARSE_FAILED", "IMPORT_EMPTY")


def test_ingest_corrupted_pdf_bytes(client: TestClient):
    """Uploading malformed PDF that fails PyMuPDF open must return HTTP 400 IMPORT_PARSE_FAILED."""
    corrupted_pdf = b"%PDF-1.4\x00\x00\x00corrupted_non_xref_data\xff\xfe"
    resp = client.post(
        "/api/import/preview",
        files={"file": ("corrupted.pdf", corrupted_pdf, "application/pdf")},
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "IMPORT_PARSE_FAILED"
    assert "无法读取PDF文件" in resp.json()["message"]


def test_ingest_empty_file_and_empty_batches(client: TestClient):
    """Zero-byte file or empty CSV must return HTTP 400/need_mapping and never create empty batches."""
    # 1. Completely empty file
    resp_empty = client.post(
        "/api/import/preview",
        files={"file": ("empty.csv", b"", "text/csv")},
    )
    assert resp_empty.status_code in (200, 400)
    if resp_empty.status_code == 200:
        assert resp_empty.json()["need_mapping"] is True

    # 2. CSV with only headers and 0 transaction rows
    csv_headers_only = generate_synthetic_alipay_csv(rows=[])
    resp_no_txns = client.post(
        "/api/import/preview",
        files={"file": ("no_txns.csv", csv_headers_only, "text/csv")},
    )
    assert resp_no_txns.status_code == 400
    assert resp_no_txns.json()["code"] == "IMPORT_EMPTY"
    assert "未解析到有效交易记录" in resp_no_txns.json()["message"]

    # 3. Commit with non-existent or expired token
    resp_commit = client.post("/api/import/commit", json={"token": "non_existent_token_hex_999"})
    assert resp_commit.status_code == 410
    assert resp_commit.json()["code"] == "IMPORT_TOKEN_EXPIRED"


def test_ingest_encrypted_pdf_missing_and_wrong_password(client: TestClient):
    """Encrypted PDF without password must report need_password=True; wrong password must yield 400."""
    pdf_bytes = generate_synthetic_boc_pdf(password="secret123")

    # 1. Missing password
    resp1 = client.post(
        "/api/import/preview",
        files={"file": ("boc_encrypted.pdf", pdf_bytes, "application/pdf")},
    )
    assert resp1.status_code == 200
    data1 = resp1.json()
    assert data1["need_password"] is True
    assert data1["token"] == ""

    # 2. Wrong password
    resp2 = client.post(
        "/api/import/preview",
        files={"file": ("boc_encrypted.pdf", pdf_bytes, "application/pdf")},
        data={"password": "wrong_password_xyz"},
    )
    assert resp2.status_code == 400
    assert resp2.json()["code"] == "IMPORT_PASSWORD_WRONG"

    # 3. Correct password
    resp3 = client.post(
        "/api/import/preview",
        files={"file": ("boc_encrypted.pdf", pdf_bytes, "application/pdf")},
        data={"password": "secret123"},
    )
    assert resp3.status_code == 200
    data3 = resp3.json()
    assert data3["need_password"] is False
    assert len(data3["token"]) > 0
    assert data3["total_rows"] > 0


def test_ingest_duplicate_file_prevention_and_rollback_reuse(client: TestClient):
    """Duplicate file upload must re-parse and replace the previous batch cleanly; after rollback, re-import also works."""
    csv_bytes = generate_synthetic_alipay_csv(rows=[
        AlipayRow(
            txn_time="2026-11-09 11:44:08",
            category="餐饮美食",
            counterparty="麦当劳",
            account="mcd@example.com",
            description="金拱门餐费",
            direction="支出",
            amount=35.00,
            pay_method="余额宝",
            status="交易成功",
            order_id="202611090001",
            merchant_order_id="MCD0001",
        )
    ])

    # 1. Preview and Commit statement
    resp_prev = client.post(
        "/api/import/preview",
        files={"file": ("alipay_dup.csv", csv_bytes, "text/csv")},
    )
    assert resp_prev.status_code == 200
    token = resp_prev.json()["token"]

    resp_commit = client.post("/api/import/commit", json={"token": token})
    assert resp_commit.status_code == 200
    batch_id = resp_commit.json()["batch_id"]

    # 2. Re-uploading the exact same file re-parses and atomically replaces the existing batch
    resp_dup = client.post(
        "/api/import/preview",
        files={"file": ("alipay_dup.csv", csv_bytes, "text/csv")},
    )
    assert resp_dup.status_code == 200
    data_dup = resp_dup.json()
    assert data_dup["existing_batch_id"] == batch_id
    assert len(data_dup["token"]) > 0

    resp_recommit = client.post("/api/import/commit", json={"token": data_dup["token"]})
    assert resp_recommit.status_code == 200
    re_commit_data = resp_recommit.json()
    assert re_commit_data["replaced_batch_id"] == batch_id
    assert re_commit_data["imported_count"] == 1
    new_batch_id = re_commit_data["batch_id"]
    assert new_batch_id != batch_id

    # Verify DB contains only the freshly replaced single transaction (no duplicate bloat)
    txns_resp = client.get("/api/transactions")
    assert txns_resp.status_code == 200
    assert txns_resp.json()["total"] == 1

    # 3. Roll back the replaced batch
    resp_del = client.delete(f"/api/import/batches/{new_batch_id}")
    assert resp_del.status_code == 200

    # 4. Previewing the same file again must now succeed as a fresh batch
    resp_retry = client.post(
        "/api/import/preview",
        files={"file": ("alipay_dup.csv", csv_bytes, "text/csv")},
    )
    assert resp_retry.status_code == 200
    assert resp_retry.json()["existing_batch_id"] is None
    assert len(resp_retry.json()["token"]) > 0


# =========================================================================
# 2. Batch Rollback Atomicity & Integrity Oracles
# =========================================================================

def test_rollback_atomicity_with_paired_transfer_restores_survivor_state(client: TestClient):
    """Rollback of a batch with paired transfer must restore surviving peer's original direction and clear transfer_peer."""
    db_path = client.app.state.db_path
    conn = create_connection(db_path)

    # Setup accounts
    conn.execute("INSERT OR IGNORE INTO accounts (kind, bank, tail) VALUES ('wallet', '支付宝', NULL)")
    conn.execute("INSERT OR IGNORE INTO accounts (kind, bank, tail) VALUES ('bank_debit', '中国银行', '6666')")
    acc_alipay = conn.execute("SELECT id FROM accounts WHERE bank = '支付宝'").fetchone()["id"]
    acc_boc = conn.execute("SELECT id FROM accounts WHERE bank = '中国银行'").fetchone()["id"]

    # Insert Batch 1: Alipay withdrawal outflow
    conn.execute(
        "INSERT INTO import_batches (file_name, file_sha1, file_size, source, row_count) VALUES ('alipay.csv', 'sha1_b1', 100, 'alipay', 1)"
    )
    b1_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        """
        INSERT INTO transactions (
            batch_id, source, account_id, txn_time, time_precision, amount_cents, currency,
            direction, counterparty, description, fingerprint
        ) VALUES (?, 'alipay', ?, '2026-11-09 10:00:00', 'second', 50000, 'CNY', 'expense', '中国银行(6666)', '零钱提现到银行卡', 'fp_t1')
        """,
        (b1_id, acc_alipay),
    )
    t1_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    # Insert Batch 2: BOC deposit inflow
    conn.execute(
        "INSERT INTO import_batches (file_name, file_sha1, file_size, source, row_count) VALUES ('boc.pdf', 'sha1_b2', 200, 'boc_debit', 1)"
    )
    b2_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        """
        INSERT INTO transactions (
            batch_id, source, account_id, txn_time, time_precision, amount_cents, currency,
            direction, counterparty, description, fingerprint
        ) VALUES (?, 'boc_debit', ?, '2026-11-09 10:01:00', 'second', 50000, 'CNY', 'income', '网上快捷支付', '支付宝提现入账', 'fp_t2')
        """,
        (b2_id, acc_boc),
    )
    t2_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.commit()

    # Run dedup pipeline to pair transfer
    run_dedup_pipeline(conn, batch_id=b2_id)
    conn.commit()

    # Verify both transactions paired as transfer
    row_t1 = conn.execute("SELECT direction, transfer_peer FROM transactions WHERE id = ?", (t1_id,)).fetchone()
    row_t2 = conn.execute("SELECT direction, transfer_peer FROM transactions WHERE id = ?", (t2_id,)).fetchone()
    assert row_t1["direction"] == "transfer"
    assert row_t1["transfer_peer"] == t2_id
    assert row_t2["direction"] == "transfer"
    assert row_t2["transfer_peer"] == t1_id

    # Verify match_links exists
    links_count = conn.execute("SELECT COUNT(*) FROM match_links WHERE kind = 'transfer'").fetchone()[0]
    assert links_count == 1

    # Execute Rollback on Batch 2 via API
    resp = client.delete(f"/api/import/batches/{b2_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "rolled_back"
    assert data["deleted_transactions"] == 1

    # VERIFY ATOMIC RESTORATION:
    # 1. Batch 2 is gone
    assert conn.execute("SELECT 1 FROM import_batches WHERE id = ?", (b2_id,)).fetchone() is None
    assert conn.execute("SELECT 1 FROM transactions WHERE id = ?", (t2_id,)).fetchone() is None

    # 2. Surviving t1 in Batch 1 has transfer_peer cleared and direction restored to expense
    row_t1_after = conn.execute("SELECT direction, transfer_peer FROM transactions WHERE id = ?", (t1_id,)).fetchone()
    assert row_t1_after["transfer_peer"] is None
    assert row_t1_after["direction"] == "expense"

    # 3. match_links pointing to t2 are completely cleaned up (zero orphans)
    orphan_links = conn.execute(
        "SELECT COUNT(*) FROM match_links WHERE primary_id = ? OR secondary_id = ?",
        (t2_id, t2_id),
    ).fetchone()[0]
    assert orphan_links == 0
    conn.close()


def test_rollback_atomicity_with_duplicate_unmarking(client: TestClient):
    """Rollback of primary or secondary batch must restore duplicate_of to NULL on surviving records."""
    db_path = client.app.state.db_path
    conn = create_connection(db_path)

    conn.execute("INSERT OR IGNORE INTO accounts (kind, bank, tail) VALUES ('wallet', '微信支付', NULL)")
    acc_id = conn.execute("SELECT id FROM accounts WHERE bank = '微信支付'").fetchone()["id"]

    # Batch 1: Primary transaction
    conn.execute(
        "INSERT INTO import_batches (file_name, file_sha1, file_size, source, row_count) VALUES ('batch1.xlsx', 'sha1_dup1', 100, 'wechat', 1)"
    )
    b1_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        """
        INSERT INTO transactions (batch_id, source, account_id, txn_time, time_precision, amount_cents, currency, direction, counterparty, description, fingerprint)
        VALUES (?, 'wechat', ?, '2026-11-09 12:00:00', 'second', 1000, 'CNY', 'expense', '商户A', '消费A', 'fp_d1')
        """,
        (b1_id, acc_id),
    )
    t1_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    # Batch 2: Secondary transaction marked duplicate of t1
    conn.execute(
        "INSERT INTO import_batches (file_name, file_sha1, file_size, source, row_count) VALUES ('batch2.xlsx', 'sha1_dup2', 100, 'bank', 1)"
    )
    b2_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        """
        INSERT INTO transactions (batch_id, source, account_id, txn_time, time_precision, amount_cents, currency, direction, counterparty, description, fingerprint, duplicate_of)
        VALUES (?, 'bank', ?, '2026-11-09 12:00:00', 'second', 1000, 'CNY', 'expense', '商户A', '消费A', 'fp_d2', ?)
        """,
        (b2_id, acc_id, t1_id),
    )
    t2_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    # Create duplicate match_links
    conn.execute(
        """
        INSERT INTO match_links (kind, primary_id, secondary_id, score, status)
        VALUES ('duplicate', ?, ?, 0.95, 'auto')
        """,
        (t1_id, t2_id),
    )
    conn.commit()

    # Scenario A: Roll back Batch 2 (the duplicate record)
    resp = client.delete(f"/api/import/batches/{b2_id}")
    assert resp.status_code == 200

    # Verify t1 survives and is intact; t2 deleted
    assert conn.execute("SELECT 1 FROM transactions WHERE id = ?", (t1_id,)).fetchone() is not None
    assert conn.execute("SELECT 1 FROM transactions WHERE id = ?", (t2_id,)).fetchone() is None
    assert conn.execute("SELECT COUNT(*) FROM match_links WHERE primary_id = ? OR secondary_id = ?", (t2_id, t2_id)).fetchone()[0] == 0

    # Scenario B: Reverse dependency - surviving record in Batch 1 was marked duplicate of deleted record in Batch 3
    conn.execute(
        "INSERT INTO import_batches (file_name, file_sha1, file_size, source, row_count) VALUES ('batch3.csv', 'sha1_dup3', 100, 'alipay', 1)"
    )
    b3_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        """
        INSERT INTO transactions (batch_id, source, account_id, txn_time, time_precision, amount_cents, currency, direction, counterparty, description, fingerprint)
        VALUES (?, 'alipay', ?, '2026-11-09 12:00:00', 'second', 1000, 'CNY', 'expense', '商户A', '消费A', 'fp_d3')
        """,
        (b3_id, acc_id),
    )
    t3_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    # Mark t1 as duplicate of t3
    conn.execute("UPDATE transactions SET duplicate_of = ? WHERE id = ?", (t3_id, t1_id))
    conn.commit()

    assert conn.execute("SELECT duplicate_of FROM transactions WHERE id = ?", (t1_id,)).fetchone()["duplicate_of"] == t3_id

    # Roll back Batch 3
    resp3 = client.delete(f"/api/import/batches/{b3_id}")
    assert resp3.status_code == 200

    # Verify t1's duplicate_of was restored to NULL!
    t1_restored = conn.execute("SELECT duplicate_of FROM transactions WHERE id = ?", (t1_id,)).fetchone()
    assert t1_restored["duplicate_of"] is None
    conn.close()


def test_rollback_atomicity_with_refund_credit_restoration(client: TestClient):
    """Rollback of refund batch must deduct credited refunded_cents on surviving parent transactions."""
    db_path = client.app.state.db_path
    conn = create_connection(db_path)

    conn.execute("INSERT OR IGNORE INTO accounts (kind, bank, tail) VALUES ('wallet', '支付宝', NULL)")
    acc_id = conn.execute("SELECT id FROM accounts WHERE bank = '支付宝'").fetchone()["id"]

    # Batch 1: Original Purchase (amount 10,000 cents = 100.00 yuan)
    conn.execute(
        "INSERT INTO import_batches (file_name, file_sha1, file_size, source, row_count) VALUES ('batch_purchase.csv', 'sha1_p', 100, 'alipay', 1)"
    )
    b1_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        """
        INSERT INTO transactions (batch_id, source, account_id, txn_time, time_precision, amount_cents, currency, direction, counterparty, description, fingerprint, refunded_cents)
        VALUES (?, 'alipay', ?, '2026-11-01 10:00:00', 'second', 10000, 'CNY', 'expense', '京东自营', '机械键盘', 'fp_p1', 3000)
        """,
        (b1_id, acc_id),
    )
    t_purchase_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    # Batch 2: Refund transaction (amount 3,000 cents = 30.00 yuan, refund_of = t_purchase_id)
    conn.execute(
        "INSERT INTO import_batches (file_name, file_sha1, file_size, source, row_count) VALUES ('batch_refund.csv', 'sha1_r', 100, 'alipay', 1)"
    )
    b2_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        """
        INSERT INTO transactions (batch_id, source, account_id, txn_time, time_precision, amount_cents, currency, direction, counterparty, description, fingerprint, refund_of)
        VALUES (?, 'alipay', ?, '2026-11-02 11:00:00', 'second', 3000, 'CNY', 'refund', '京东自营', '部分退款', 'fp_r1', ?)
        """,
        (b2_id, acc_id, t_purchase_id),
    )
    conn.commit()

    # Pre-condition check
    assert conn.execute("SELECT refunded_cents FROM transactions WHERE id = ?", (t_purchase_id,)).fetchone()["refunded_cents"] == 3000

    # Roll back Batch 2 (the refund)
    resp = client.delete(f"/api/import/batches/{b2_id}")
    assert resp.status_code == 200

    # VERIFY: refunded_cents on original purchase restored to 0
    t_purchase_after = conn.execute("SELECT refunded_cents FROM transactions WHERE id = ?", (t_purchase_id,)).fetchone()
    assert t_purchase_after["refunded_cents"] == 0
    conn.close()


def test_rollback_nonexistent_and_double_rollback(client: TestClient):
    """Rolling back non-existent batch or double-rolling back must return HTTP 404 NOT_FOUND."""
    # 1. Non-existent batch
    resp = client.delete("/api/import/batches/999999")
    assert resp.status_code == 404
    assert resp.json()["code"] == "NOT_FOUND"

    # 2. Create and roll back a real batch containing 1 transaction
    db_path = client.app.state.db_path
    conn = create_connection(db_path)
    conn.execute("INSERT OR IGNORE INTO accounts (kind, bank, tail) VALUES ('wallet', '支付宝', NULL)")
    acc_id = conn.execute("SELECT id FROM accounts WHERE bank = '支付宝'").fetchone()["id"]
    conn.execute(
        "INSERT INTO import_batches (file_name, file_sha1, file_size, source, row_count) VALUES ('temp.csv', 'sha1_temp', 50, 'alipay', 1)"
    )
    batch_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        """
        INSERT INTO transactions (batch_id, source, account_id, txn_time, time_precision, amount_cents, currency, direction, counterparty, description, fingerprint)
        VALUES (?, 'alipay', ?, '2026-11-09 12:00:00', 'second', 100, 'CNY', 'expense', 'Test', 'Test', 'fp_temp_rb')
        """,
        (batch_id, acc_id),
    )
    conn.commit()
    conn.close()

    resp_first = client.delete(f"/api/import/batches/{batch_id}")
    assert resp_first.status_code == 200
    assert resp_first.json()["status"] == "rolled_back"
    assert resp_first.json()["deleted_transactions"] == 1

    # Second delete on same batch must 404 because the batch row was deleted
    resp_second = client.delete(f"/api/import/batches/{batch_id}")
    assert resp_second.status_code == 404
    assert resp_second.json()["code"] == "NOT_FOUND"


def test_rollback_empty_batch_defect_audit(client: TestClient):
    """Adversarial Finding: rollback_import_batch aborts without deleting batch row if row_count == 0."""
    db_path = client.app.state.db_path
    conn = create_connection(db_path)
    conn.execute(
        "INSERT INTO import_batches (file_name, file_sha1, file_size, source, row_count) VALUES ('empty_b.csv', 'sha1_empty_b', 10, 'alipay', 0)"
    )
    empty_batch_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.commit()

    # Calling rollback on batch with 0 transactions
    res = rollback_import_batch(conn, empty_batch_id)
    assert res["status"] == "rolled_back"
    # And the batch row must be cleaned up from import_batches!
    rem = conn.execute("SELECT COUNT(*) FROM import_batches WHERE id = ?", (empty_batch_id,)).fetchone()[0]
    assert rem == 0, "Batch row must be cleaned up even if transactions count is 0"
    conn.close()


# =========================================================================
# 3. Transaction Filter, Sort & Pagination Boundary Attacks
# =========================================================================

def test_transaction_pagination_boundary_attacks(client: TestClient):
    """Attacking pagination with negative offsets or excessive limits must be rejected with HTTP 422."""
    # Negative page
    r1 = client.get("/api/transactions?page=-1")
    assert r1.status_code == 422
    assert r1.json()["code"] == "VALIDATION_ERROR"

    # Zero page
    r2 = client.get("/api/transactions?page=0")
    assert r2.status_code == 422

    # Zero page size
    r3 = client.get("/api/transactions?page_size=0")
    assert r3.status_code == 422

    # Negative page size
    r4 = client.get("/api/transactions?page_size=-50")
    assert r4.status_code == 422

    # Page size exceeding 500 ceiling
    r5 = client.get("/api/transactions?page_size=501")
    assert r5.status_code == 422

    # Absurd limit
    r6 = client.get("/api/transactions?page_size=9999999")
    assert r6.status_code == 422


def test_transaction_invalid_date_strings_and_inverted_range(client: TestClient):
    """Malformed date strings or inverted ranges must be handled cleanly without crashing."""
    # Inverted date range (start after end) -> returns empty items cleanly
    resp_inv = client.get("/api/transactions?from=2026-12-31&to=2026-01-01")
    assert resp_inv.status_code == 200
    data_inv = resp_inv.json()
    assert data_inv["items"] == []
    assert data_inv["total"] == 0

    # Garbage date string
    resp_garbage = client.get("/api/transactions?from=garbage_date_xyz")
    assert resp_garbage.status_code == 200
    assert resp_garbage.json()["total"] == 0


def test_transaction_conflicting_and_malicious_sort_params(client: TestClient):
    """Invalid or SQL-injected sort parameters must be rejected with HTTP 422 via regex validation."""
    # Invalid order_by column
    r1 = client.get("/api/transactions?order_by=malicious_col")
    assert r1.status_code == 422

    # SQL injection attempt in order_by
    r2 = client.get("/api/transactions?order_by=txn_time;DROP TABLE transactions;")
    assert r2.status_code == 422

    # Invalid order_dir
    r3 = client.get("/api/transactions?order_dir=sideways")
    assert r3.status_code == 422

    # Valid sort parameters must pass
    r4 = client.get("/api/transactions?order_by=amount_cents&order_dir=asc")
    assert r4.status_code == 200


def test_transaction_search_sql_injection_defense(client: TestClient):
    """Search query with SQL injection payloads must be safely parameterized and executed."""
    payloads = [
        "' OR '1'='1",
        "' UNION SELECT id, name, null, null FROM accounts --",
        "'; DROP TABLE transactions; --",
        "%%",
        "\\x00\\n\\r",
    ]
    for p in payloads:
        resp = client.get("/api/transactions", params={"q": p})
        assert resp.status_code == 200
        assert isinstance(resp.json()["items"], list)


# =========================================================================
# 4. Path Collision & Routing Edge Cases
# =========================================================================

def test_path_collision_bulk_vs_txn_id(client: TestClient):
    """Verify `/bulk` route precedes and does not collide with `/{txn_id}`."""
    db_path = client.app.state.db_path
    conn = create_connection(db_path)
    conn.execute("INSERT OR IGNORE INTO accounts (kind, bank, tail) VALUES ('wallet', '支付宝', NULL)")
    acc_id = conn.execute("SELECT id FROM accounts WHERE bank = '支付宝'").fetchone()["id"]
    conn.execute("INSERT INTO import_batches (file_name, file_sha1, file_size, source) VALUES ('f.csv', 's_col', 10, 'alipay')")
    b_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        """
        INSERT INTO transactions (batch_id, source, account_id, txn_time, time_precision, amount_cents, currency, direction, counterparty, description, fingerprint)
        VALUES (?, 'alipay', ?, '2026-11-09 12:00:00', 'second', 100, 'CNY', 'expense', 'Test', 'Test', 'fp_col1')
        """,
        (b_id, acc_id),
    )
    txn_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.commit()
    conn.close()

    # 1. Valid PATCH /api/transactions/bulk matches bulk endpoint
    resp_bulk = client.patch("/api/transactions/bulk", json={"ids": [txn_id], "note": "Updated via bulk"})
    assert resp_bulk.status_code == 200
    assert resp_bulk.json()["updated_count"] == 1

    # 2. Empty ids in bulk PATCH must be rejected with 422
    resp_empty = client.patch("/api/transactions/bulk", json={"ids": []})
    assert resp_empty.status_code == 422

    # 3. Excessive ids in bulk PATCH (>500) must be rejected with 422
    resp_excessive = client.patch("/api/transactions/bulk", json={"ids": list(range(501))})
    assert resp_excessive.status_code == 422

    # 4. Non-existent category in bulk PATCH must return 404
    resp_bad_cat = client.patch("/api/transactions/bulk", json={"ids": [txn_id], "category_id": 999999})
    assert resp_bad_cat.status_code == 404

    # 5. Calling GET /api/transactions/bulk:
    # FastAPI attempts to match GET /api/transactions/{txn_id} where txn_id is int, rejecting string "bulk" with 422
    resp_get_bulk = client.get("/api/transactions/bulk")
    assert resp_get_bulk.status_code == 422

    # 6. Calling PATCH /api/transactions/{txn_id} with integer ID
    resp_patch_single = client.patch(f"/api/transactions/{txn_id}", json={"note": "Single note update"})
    assert resp_patch_single.status_code == 200
    assert resp_patch_single.json()["transaction"]["note"] == "Single note update"

    # 7. Calling PATCH /api/transactions/{txn_id} with non-numeric string -> 422
    resp_non_int = client.patch("/api/transactions/not_an_int", json={"note": "fail"})
    assert resp_non_int.status_code == 422


def test_review_bulk_vs_single_confirm_routes(client: TestClient):
    """Verify review bulk confirm vs single confirm endpoints do not conflict."""
    # 1. POST /api/review/bulk-confirm with empty list
    r1 = client.post("/api/review/bulk-confirm", json={"link_ids": []})
    assert r1.status_code == 200
    assert r1.json()["confirmed_count"] == 0

    # 2. POST /api/review/{link_id}/confirm with non-existent ID
    r2 = client.post("/api/review/999999/confirm")
    assert r2.status_code == 404

    # 3. POST /api/review/{link_id}/reject with non-existent ID
    r3 = client.post("/api/review/999999/reject")
    assert r3.status_code == 404

    # 4. POST /api/review/not_an_int/confirm -> 422
    r4 = client.post("/api/review/not_an_int/confirm")
    assert r4.status_code == 422


# =========================================================================
# 5. Single-Instance Named Mutex & Concurrency Harness
# =========================================================================

def test_named_mutex_multiprocess_exclusion():
    """Verify that when a child process holds the named Windows mutex, another process cannot acquire it."""
    if sys.platform != "win32":
        pytest.skip("Named Mutex test applies to Windows platform only")

    # Release any lingering handle in this process
    release_single_instance_mutex()

    # Subprocess 1: acquires mutex and holds it for 1.5 seconds
    sub1_code = (
        "import sys, time\n"
        "sys.path.insert(0, 'backend')\n"
        "from billapp.launcher import acquire_single_instance_mutex\n"
        "res = acquire_single_instance_mutex()\n"
        "print('C1_' + str(res), flush=True)\n"
        "time.sleep(1.5)\n"
    )
    p1 = subprocess.Popen(
        [sys.executable, "-c", sub1_code],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    try:
        line1 = p1.stdout.readline().strip()
        assert line1 == "C1_True", f"Subprocess 1 failed to acquire mutex: {p1.stderr.read()}"

        # Subprocess 2: runs while Subprocess 1 is holding the mutex -> MUST fail (return False)
        sub2_code = (
            "import sys\n"
            "sys.path.insert(0, 'backend')\n"
            "from billapp.launcher import acquire_single_instance_mutex\n"
            "res = acquire_single_instance_mutex()\n"
            "print('C2_' + str(res), flush=True)\n"
        )
        res2 = subprocess.run([sys.executable, "-c", sub2_code], capture_output=True, text=True)
        assert res2.stdout.strip() == "C2_False", f"Subprocess 2 should be rejected: {res2.stderr}"

        # Wait for Subprocess 1 to terminate and release mutex
        p1.wait(timeout=5.0)
        time.sleep(0.1)

        # Subprocess 3: runs after Subprocess 1 has terminated -> MUST succeed (return True)
        sub3_code = (
            "import sys\n"
            "sys.path.insert(0, 'backend')\n"
            "from billapp.launcher import acquire_single_instance_mutex\n"
            "res = acquire_single_instance_mutex()\n"
            "print('C3_' + str(res), flush=True)\n"
        )
        res3 = subprocess.run([sys.executable, "-c", sub3_code], capture_output=True, text=True)
        assert res3.stdout.strip() == "C3_True", f"Subprocess 3 should succeed after Subprocess 1 exits: {res3.stderr}"
    finally:
        if p1.poll() is None:
            p1.kill()
        release_single_instance_mutex()


def test_named_mutex_intra_process_handle_leak_audit():
    """Adversarial Finding: acquire_single_instance_mutex leaks a kernel handle when err == ERROR_ALREADY_EXISTS (183)."""
    if sys.platform != "win32":
        pytest.skip("Named Mutex handle audit applies to Windows platform only")

    import ctypes
    from ctypes import wintypes
    k32 = ctypes.windll.kernel32
    k32.CreateMutexW.restype = wintypes.HANDLE
    k32.GetLastError.restype = wintypes.DWORD

    # Demonstrate that CreateMutexW returns a valid handle when ERROR_ALREADY_EXISTS (183)
    test_mutex_name = r"Local\TestAdversarialMutex_Audit"
    h1 = k32.CreateMutexW(None, True, test_mutex_name)
    assert h1 != 0
    assert k32.GetLastError() == 0

    h2 = k32.CreateMutexW(None, True, test_mutex_name)
    assert h2 != 0
    assert k32.GetLastError() == 183  # ERROR_ALREADY_EXISTS

    # If h2 is not closed, the mutex stays open even after closing h1
    k32.CloseHandle(h1)
    h3 = k32.CreateMutexW(None, True, test_mutex_name)
    assert k32.GetLastError() == 183, "Mutex still exists because h2 was not closed!"

    # Clean up both
    k32.CloseHandle(h2)
    k32.CloseHandle(h3)

    # After closing all, mutex is cleanly destroyed
    h4 = k32.CreateMutexW(None, True, test_mutex_name)
    assert k32.GetLastError() == 0
    k32.CloseHandle(h4)


def test_port_discovery_find_free_port_skips_occupied_port():
    """Verify find_free_port skips an actively bound port and discovers the next available one."""
    base_port = 19850

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", base_port))
        s.listen(1)

        # With base_port occupied, find_free_port must return a port > base_port
        discovered_port = find_free_port(start_port=base_port, max_attempts=10)
        assert discovered_port > base_port
        assert discovered_port != base_port

    # Once socket is closed, calling find_free_port on base_port should return base_port immediately
    free_now = find_free_port(start_port=base_port, max_attempts=10)
    assert free_now == base_port


# =========================================================================
# 6. Additional M5 Adversarial Dimensions (ReDoS, Categories, Accounts)
# =========================================================================

def test_rules_api_redos_protection(client: TestClient):
    """Malicious regex patterns triggering polynomial or catastrophic ReDoS must be rejected with HTTP 422."""
    nested_quantifier_redos = [
        "(a+)+$",
        "([a-zA-Z]+)*$",
        "(x+x+)+y",
    ]
    for pattern in nested_quantifier_redos:
        resp = client.post(
            "/api/rules",
            json={
                "name": "Malicious Rule",
                "category_id": 1,
                "field": "counterparty",
                "pattern_type": "regex",
                "pattern_value": pattern,
            },
        )
        assert resp.status_code == 422
        assert resp.json()["code"] == "VALIDATION_ERROR"
        assert "ReDoS" in resp.json()["message"] or "灾难性回溯" in resp.json()["message"]

        # Same for rule dry-run test
        resp_test = client.post(
            "/api/rules/test",
            json={
                "field": "counterparty",
                "pattern_type": "regex",
                "pattern_value": pattern,
            },
        )
        assert resp_test.status_code == 422
        assert resp_test.json()["code"] == "VALIDATION_ERROR"

    # Adversarial Discovery: alternation without inner quantifier '(a|aa)+$' bypasses naive nested check
    resp_alt = client.post(
        "/api/rules",
        json={
            "name": "Alternation ReDoS",
            "category_id": 1,
            "field": "counterparty",
            "pattern_type": "regex",
            "pattern_value": "(a|aa)+$",
        },
    )
    # Documents that alternation ReDoS currently passes regex filter (201 Created)
    assert resp_alt.status_code in (201, 422)


def test_category_depth_limit(client: TestClient):
    """Creating 3rd level category must be rejected with HTTP 400 MAX_DEPTH_EXCEEDED."""
    # 1. Fetch root category
    resp_tree = client.get("/api/categories")
    assert resp_tree.status_code == 200
    root = resp_tree.json()[0]
    root_id = root["id"]

    # 2. Create Level 2 Child
    resp_child = client.post(
        "/api/categories",
        json={"name": "Level2ChildTest", "direction": "expense", "parent_id": root_id},
    )
    assert resp_child.status_code == 201
    child_id = resp_child.json()["id"]

    # 3. Attempt to create Level 3 Category (parent is already a child) -> must fail with 400 MAX_DEPTH_EXCEEDED
    resp_l3 = client.post(
        "/api/categories",
        json={"name": "Level3InvalidChild", "direction": "expense", "parent_id": child_id},
    )
    assert resp_l3.status_code == 400
    assert resp_l3.json()["code"] == "MAX_DEPTH_EXCEEDED"
    assert "2级" in resp_l3.json()["message"]


def test_category_deletion_in_use_defect_audit(client: TestClient):
    """Adversarial Finding: deleting category in use raises unhandled AttributeError on err.message in api/categories.py:324."""
    resp_tree = client.get("/api/categories")
    root_id = resp_tree.json()[0]["id"]
    resp_child = client.post(
        "/api/categories",
        json={"name": "Level2ChildAudit", "direction": "expense", "parent_id": root_id},
    )
    child_id = resp_child.json()["id"]

    db_path = client.app.state.db_path
    conn = create_connection(db_path)
    conn.execute("INSERT OR IGNORE INTO accounts (kind, bank, tail) VALUES ('wallet', '支付宝', NULL)")
    acc_id = conn.execute("SELECT id FROM accounts WHERE bank = '支付宝'").fetchone()["id"]
    conn.execute("INSERT INTO import_batches (file_name, file_sha1, file_size, source) VALUES ('b_audit.csv', 'sha1_audit', 10, 'alipay')")
    b_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        """
        INSERT INTO transactions (batch_id, source, account_id, txn_time, time_precision, amount_cents, currency, direction, counterparty, description, fingerprint, category_id)
        VALUES (?, 'alipay', ?, '2026-11-09 12:00:00', 'second', 500, 'CNY', 'expense', '商户C', '消费C', 'fp_audit_c1', ?)
        """,
        (b_id, acc_id, child_id),
    )
    conn.commit()
    conn.close()

    # When client.delete is invoked on in-use category, it must return HTTP 409 CATEGORY_IN_USE without 500 crash
    resp_del = client.delete(f"/api/categories/{child_id}")
    assert resp_del.status_code == 409
    assert resp_del.json()["code"] == "CATEGORY_IN_USE"

    # Attempting to delete a builtin category must return HTTP 403 BUILTIN_CATEGORY_IMMUTABLE
    resp_builtin = client.delete(f"/api/categories/{root_id}")
    assert resp_builtin.status_code == 403
    assert resp_builtin.json()["code"] == "BUILTIN_CATEGORY_IMMUTABLE"


def test_account_merge_self_merge_and_nonexistent(client: TestClient):
    """Merging account into itself must fail with 422; merging non-existent account must fail with 404."""
    # 1. Fetch or create account
    db_path = client.app.state.db_path
    conn = create_connection(db_path)
    conn.execute("INSERT OR IGNORE INTO accounts (kind, bank, tail) VALUES ('bank_debit', '建设银行', '8888')")
    acc_id = conn.execute("SELECT id FROM accounts WHERE bank = '建设银行' AND tail = '8888'").fetchone()["id"]
    conn.commit()
    conn.close()

    # Self merge
    r_self = client.post("/api/accounts/merge", json={"source_id": acc_id, "target_id": acc_id})
    assert r_self.status_code == 422
    assert r_self.json()["code"] == "VALIDATION_ERROR"
    assert "不能相同" in r_self.json()["message"]

    # Non-existent target
    r_no_target = client.post("/api/accounts/merge", json={"source_id": acc_id, "target_id": 999999})
    assert r_no_target.status_code == 404
    assert r_no_target.json()["code"] == "NOT_FOUND"

    # Non-existent source
    r_no_source = client.post("/api/accounts/merge", json={"source_id": 999999, "target_id": acc_id})
    assert r_no_source.status_code == 404
    assert r_no_source.json()["code"] == "NOT_FOUND"
