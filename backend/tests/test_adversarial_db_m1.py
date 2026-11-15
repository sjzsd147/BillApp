"""Empirical Adversarial Test Suite for Milestone 1 Database Layer.

Focus Areas:
1. SQLite concurrent access & busy timeout under parallel threads (WAL mode).
2. Foreign keys cascade & deletion integrity.
3. Check constraint on negative amounts and invalid values.
4. Unique constraints on file_sha1, transaction fingerprint, and account tuples.
5. Rollback semantics on partial batch insertion failure.
6. Empirical discovery tests for schema vulnerabilities and boundary conditions.
"""

from __future__ import annotations

import concurrent.futures
import os
import sqlite3
import tempfile
import threading
import time
from pathlib import Path
from typing import List, Optional

import pytest
from pydantic import ValidationError

from billapp.core.db import (
    configure_connection,
    create_connection,
    get_db,
    init_db,
)
from billapp.core.models import (
    AccountCreate,
    ImportBatchCreate,
    TransactionCreate,
)
from billapp.core.money import (
    format_money,
    parse_signed_amount,
    to_cents,
    to_display,
)


@pytest.fixture
def temp_db_path(tmp_path: Path) -> Path:
    """Fixture providing a real file-based SQLite database with initialized schema."""
    db_file = tmp_path / "test_adversarial.db"
    conn = init_db(db_file)
    conn.close()
    return db_file


# ==============================================================================
# Challenge 1: SQLite Concurrent Access & Busy Timeout Under Parallel Threads
# ==============================================================================


def test_concurrent_parallel_writers(temp_db_path: Path) -> None:
    """Stress test: 16 concurrent worker threads simultaneously inserting batches and transactions.

    In SQLite WAL mode with busy_timeout=5000ms, all serializable short writes must succeed
    without 'sqlite3.OperationalError: database is locked'.
    """
    thread_count = 16
    txns_per_thread = 10
    errors: List[Exception] = []

    # Insert a shared account first
    with get_db(temp_db_path) as db:
        cur = db.execute(
            "INSERT INTO accounts (kind, bank, tail, currency) VALUES ('platform', '支付宝', '0000', 'CNY')"
        )
        acc_id = cur.lastrowid

    def worker_insert(thread_idx: int) -> None:
        try:
            with get_db(temp_db_path) as db:
                batch_sha1 = f"batch_sha1_worker_{thread_idx}"
                cur = db.execute(
                    """
                    INSERT INTO import_batches (file_name, file_sha1, file_size, source, row_count)
                    VALUES (?, ?, ?, 'alipay', ?)
                    """,
                    (f"file_{thread_idx}.csv", batch_sha1, 1024, txns_per_thread),
                )
                batch_id = cur.lastrowid

                for i in range(txns_per_thread):
                    fp = f"fp_t{thread_idx}_row{i}"
                    db.execute(
                        """
                        INSERT INTO transactions (
                            batch_id, source, account_id, txn_time, time_precision,
                            amount_cents, currency, direction, fingerprint
                        ) VALUES (?, 'alipay', ?, '2026-11-09 12:00:00', 'second', ?, 'CNY', 'expense', ?)
                        """,
                        (batch_id, acc_id, 100 * (i + 1), fp),
                    )
        except Exception as exc:
            errors.append(exc)

    with concurrent.futures.ThreadPoolExecutor(max_workers=thread_count) as executor:
        futures = [executor.submit(worker_insert, idx) for idx in range(thread_count)]
        concurrent.futures.wait(futures)

    assert len(errors) == 0, f"Concurrent worker writes threw errors: {errors}"

    # Verify that all 16 batches and 160 transactions are present and uncorrupted
    with get_db(temp_db_path) as db:
        batch_count = db.execute("SELECT count(*) FROM import_batches").fetchone()[0]
        txn_count = db.execute("SELECT count(*) FROM transactions").fetchone()[0]

    assert batch_count == thread_count
    assert txn_count == thread_count * txns_per_thread


def test_concurrent_readers_during_active_write_lock(temp_db_path: Path) -> None:
    """Stress test: Concurrent readers must NOT be blocked by an active, uncommitted write transaction.

    In SQLite WAL mode, readers do not block writers, and writers do not block readers.
    """
    # Pre-populate 5 transactions
    with get_db(temp_db_path) as db:
        acc_id = db.execute(
            "INSERT INTO accounts (kind, bank, tail) VALUES ('wallet', '微信支付', '8888')"
        ).lastrowid
        batch_id = db.execute(
            "INSERT INTO import_batches (file_name, file_sha1, file_size, source) VALUES ('f1', 'sha_init', 10, 'wechat')"
        ).lastrowid
        for i in range(5):
            db.execute(
                """
                INSERT INTO transactions (batch_id, source, account_id, txn_time, time_precision, amount_cents, direction, fingerprint)
                VALUES (?, 'wechat', ?, '2026-11-09 10:00:00', 'second', 1000, 'expense', ?)
                """,
                (batch_id, acc_id, f"fp_init_{i}"),
            )

    writer_started = threading.Event()
    writer_can_commit = threading.Event()
    reader_results: List[int] = []
    reader_errors: List[Exception] = []

    def writer_thread() -> None:
        conn = create_connection(temp_db_path)
        try:
            conn.execute("BEGIN IMMEDIATE;")
            # Insert a new batch inside uncommitted transaction
            conn.execute(
                "INSERT INTO import_batches (file_name, file_sha1, file_size, source) VALUES ('f2', 'sha_writer', 20, 'wechat')"
            )
            writer_started.set()
            # Wait for readers to query
            writer_can_commit.wait(timeout=5.0)
            conn.commit()
        finally:
            conn.close()

    def reader_thread() -> None:
        writer_started.wait(timeout=5.0)
        # Attempt multiple read operations while writer is holding write transaction
        try:
            for _ in range(10):
                conn = create_connection(temp_db_path)
                try:
                    cnt = conn.execute("SELECT count(*) FROM import_batches").fetchone()[0]
                    reader_results.append(cnt)
                finally:
                    conn.close()
                time.sleep(0.01)
        except Exception as e:
            reader_errors.append(e)

    t_writer = threading.Thread(target=writer_thread)
    t_reader = threading.Thread(target=reader_thread)

    t_writer.start()
    t_reader.start()

    # Wait for reader to complete its reads while writer is uncommitted
    time.sleep(0.2)
    writer_can_commit.set()
    t_writer.join(timeout=5.0)
    t_reader.join(timeout=5.0)

    assert len(reader_errors) == 0, f"Reader was blocked or threw error: {reader_errors}"
    assert len(reader_results) == 10
    # Snapshot isolation: Readers during writer's uncommitted phase saw exactly 1 batch (the committed one)
    assert all(r == 1 for r in reader_results), f"Reader saw uncommitted data: {reader_results}"

    # After commit, new reads must see 2 batches
    with get_db(temp_db_path) as db:
        final_count = db.execute("SELECT count(*) FROM import_batches").fetchone()[0]
    assert final_count == 2


def test_busy_timeout_expiration_under_excessive_lock(temp_db_path: Path) -> None:
    """Stress test: If a writer holds the lock longer than busy_timeout (5000ms),

    another writer MUST time out and raise sqlite3.OperationalError: database is locked.
    """
    writer_locked = threading.Event()
    stop_writer = threading.Event()
    contending_error: List[Exception] = []

    def holding_writer() -> None:
        conn = create_connection(temp_db_path)
        try:
            conn.execute("BEGIN EXCLUSIVE;")
            writer_locked.set()
            stop_writer.wait(timeout=7.0)
            conn.rollback()
        finally:
            conn.close()

    def contending_writer() -> None:
        writer_locked.wait(timeout=5.0)
        # Try to begin immediate/exclusive on separate connection
        conn = create_connection(temp_db_path)
        start_time = time.monotonic()
        try:
            conn.execute("BEGIN IMMEDIATE;")
            conn.rollback()
        except sqlite3.OperationalError as exc:
            elapsed = time.monotonic() - start_time
            contending_error.append(exc)
            # Verify it actually waited close to the busy_timeout (~5s)
            assert elapsed >= 4.5, f"Timeout triggered too fast ({elapsed:.2f}s < 4.5s), busy_timeout not respected!"
        finally:
            conn.close()

    t_holder = threading.Thread(target=holding_writer)
    t_contender = threading.Thread(target=contending_writer)

    t_holder.start()
    t_contender.start()

    t_contender.join(timeout=8.0)
    stop_writer.set()
    t_holder.join(timeout=2.0)

    assert len(contending_error) == 1, "Expected sqlite3.OperationalError for busy timeout expiration"
    assert "locked" in str(contending_error[0]).lower()


# ==============================================================================
# Challenge 2: Foreign Keys Cascade & Deletion Integrity
# ==============================================================================


def test_foreign_key_cascade_deletion_tree(temp_db_path: Path) -> None:
    """Adversarial challenge: Deep cascade deletion across 5 interrelated tables:

    import_batches -> transactions -> match_links & match_blocklist & refund_of / duplicate_of.
    When an import batch is deleted, all dependent transactions, links, and blocklist records
    must be deleted, while external referencing transactions must have fields SET TO NULL.
    """
    with get_db(temp_db_path) as db:
        acc_id = db.execute(
            "INSERT INTO accounts (kind, bank, tail) VALUES ('platform', '支付宝', '1111')"
        ).lastrowid

        # Batch 1 (Target to be deleted)
        b1_id = db.execute(
            "INSERT INTO import_batches (file_name, file_sha1, file_size, source) VALUES ('b1.csv', 'sha_b1', 100, 'alipay')"
        ).lastrowid

        # Batch 2 (Independent batch)
        b2_id = db.execute(
            "INSERT INTO import_batches (file_name, file_sha1, file_size, source) VALUES ('b2.csv', 'sha_b2', 200, 'alipay')"
        ).lastrowid

        # Transactions in Batch 1
        t1 = db.execute(
            """
            INSERT INTO transactions (batch_id, source, account_id, txn_time, time_precision, amount_cents, direction, fingerprint)
            VALUES (?, 'alipay', ?, '2026-11-09 10:00:00', 'second', 5000, 'expense', 'fp_t1')
            """,
            (b1_id, acc_id),
        ).lastrowid

        t2 = db.execute(
            """
            INSERT INTO transactions (batch_id, source, account_id, txn_time, time_precision, amount_cents, direction, fingerprint)
            VALUES (?, 'alipay', ?, '2026-11-09 10:05:00', 'second', 5000, 'refund', 'fp_t2')
            """,
            (b1_id, acc_id),
        ).lastrowid

        # Transaction in Batch 2 (references t1 from batch 1)
        t_ext = db.execute(
            """
            INSERT INTO transactions (batch_id, source, account_id, txn_time, time_precision, amount_cents, direction, fingerprint, refund_of, duplicate_of, transfer_peer)
            VALUES (?, 'alipay', ?, '2026-11-09 11:00:00', 'second', 5000, 'expense', 'fp_text', ?, ?, ?)
            """,
            (b2_id, acc_id, t1, t1, t1),
        ).lastrowid

        # Match links referencing t1 and t2
        ml_id = db.execute(
            """
            INSERT INTO match_links (kind, primary_id, secondary_id, score, status)
            VALUES ('refund', ?, ?, 0.95, 'auto')
            """,
            (t1, t2),
        ).lastrowid

        # Match blocklist referencing t1 and t2
        db.execute(
            """
            INSERT INTO match_blocklist (a_id, b_id, kind)
            VALUES (?, ?, 'duplicate')
            """,
            (t1, t2),
        )

    # ACTION: Delete Batch 1
    with get_db(temp_db_path) as db:
        db.execute("DELETE FROM import_batches WHERE id = ?", (b1_id,))

    # VERIFY CASCADES
    with get_db(temp_db_path) as db:
        # Transactions in B1 must be deleted
        b1_txns = db.execute("SELECT count(*) FROM transactions WHERE batch_id = ?", (b1_id,)).fetchone()[0]
        assert b1_txns == 0

        # Match links referencing t1 or t2 must be cascade-deleted
        ml_count = db.execute("SELECT count(*) FROM match_links WHERE primary_id = ? OR secondary_id = ?", (t1, t2)).fetchone()[0]
        assert ml_count == 0

        # Match blocklist referencing t1 or t2 must be cascade-deleted
        bl_count = db.execute("SELECT count(*) FROM match_blocklist WHERE a_id = ? OR b_id = ?", (t1, t2)).fetchone()[0]
        assert bl_count == 0

        # Transaction in Batch 2 must still exist, but refund_of, duplicate_of, transfer_peer set to NULL
        ext_row = db.execute("SELECT id, refund_of, duplicate_of, transfer_peer FROM transactions WHERE id = ?", (t_ext,)).fetchone()
        assert ext_row is not None
        assert ext_row["refund_of"] is None
        assert ext_row["duplicate_of"] is None
        assert ext_row["transfer_peer"] is None

        # Accounts table must NOT be deleted
        acc_count = db.execute("SELECT count(*) FROM accounts WHERE id = ?", (acc_id,)).fetchone()[0]
        assert acc_count == 1


def test_foreign_key_account_delete_restriction(temp_db_path: Path) -> None:
    """Adversarial challenge: Attempting to delete an account when transactions reference it

    MUST FAIL with sqlite3.IntegrityError: FOREIGN KEY constraint failed (NO CASCADE on account).
    """
    with get_db(temp_db_path) as db:
        acc_id = db.execute(
            "INSERT INTO accounts (kind, bank, tail) VALUES ('bank_debit', '中国银行', '6666')"
        ).lastrowid
        batch_id = db.execute(
            "INSERT INTO import_batches (file_name, file_sha1, file_size, source) VALUES ('b.csv', 'sha_fk_acc', 10, 'boc_debit')"
        ).lastrowid
        db.execute(
            """
            INSERT INTO transactions (batch_id, source, account_id, txn_time, time_precision, amount_cents, direction, fingerprint)
            VALUES (?, 'boc_debit', ?, '2026-11-09 10:00:00', 'second', 100, 'expense', 'fp_fk_acc')
            """,
            (batch_id, acc_id),
        )

        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY constraint failed"):
            db.execute("DELETE FROM accounts WHERE id = ?", (acc_id,))


def test_category_cascade_and_rule_cascade(temp_db_path: Path) -> None:
    """Adversarial challenge: Deleting a parent category cascades to child category and rules,

    while transactions referencing it have category_id SET TO NULL.
    """
    with get_db(temp_db_path) as db:
        acc_id = db.execute("INSERT INTO accounts (kind, bank) VALUES ('platform', '支付宝')").lastrowid
        batch_id = db.execute("INSERT INTO import_batches (file_name, file_sha1, file_size, source) VALUES ('f', 'sha_cat', 1, 'alipay')").lastrowid

        # Parent category
        c_parent = db.execute("INSERT INTO categories (name, direction) VALUES ('餐饮美食', 'expense')").lastrowid
        # Child category
        c_child = db.execute("INSERT INTO categories (name, parent_id, direction) VALUES ('咖啡奶茶', ?, 'expense')", (c_parent,)).lastrowid

        # Rule referencing child category
        r_id = db.execute(
            "INSERT INTO rules (name, category_id, field, pattern_type, pattern_value) VALUES ('Starbucks', ?, 'counterparty', 'contains', '星巴克')",
            (c_child,),
        ).lastrowid

        # Transaction referencing child category
        t_id = db.execute(
            """
            INSERT INTO transactions (batch_id, source, account_id, txn_time, time_precision, amount_cents, direction, fingerprint, category_id)
            VALUES (?, 'alipay', ?, '2026-11-09 10:00:00', 'second', 3500, 'expense', 'fp_cat', ?)
            """,
            (batch_id, acc_id, c_child),
        ).lastrowid

    # ACTION: Delete parent category
    with get_db(temp_db_path) as db:
        db.execute("DELETE FROM categories WHERE id = ?", (c_parent,))

    # VERIFY
    with get_db(temp_db_path) as db:
        # Child category must be deleted by CASCADE
        assert db.execute("SELECT count(*) FROM categories WHERE id = ?", (c_child,)).fetchone()[0] == 0
        # Rule must be deleted by CASCADE
        assert db.execute("SELECT count(*) FROM rules WHERE id = ?", (r_id,)).fetchone()[0] == 0
        # Transaction must exist, but category_id set to NULL
        t_row = db.execute("SELECT category_id FROM transactions WHERE id = ?", (t_id,)).fetchone()
        assert t_row["category_id"] is None


def test_invalid_foreign_key_insert_rejections(temp_db_path: Path) -> None:
    """Adversarial challenge: Inserting records with non-existent foreign keys must immediately fail."""
    with get_db(temp_db_path) as db:
        acc_id = db.execute("INSERT INTO accounts (kind, bank) VALUES ('platform', '微信支付')").lastrowid
        batch_id = db.execute("INSERT INTO import_batches (file_name, file_sha1, file_size, source) VALUES ('f', 'sha_fk_inv', 1, 'wechat')").lastrowid

        # Invalid batch_id
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            db.execute(
                "INSERT INTO transactions (batch_id, source, account_id, txn_time, time_precision, amount_cents, direction, fingerprint) VALUES (999999, 'wechat', ?, '2026-11-09 10:00:00', 'second', 10, 'expense', 'fp_bad_b')",
                (acc_id,),
            )

        # Invalid account_id
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            db.execute(
                "INSERT INTO transactions (batch_id, source, account_id, txn_time, time_precision, amount_cents, direction, fingerprint) VALUES (?, 'wechat', 999999, '2026-11-09 10:00:00', 'second', 10, 'expense', 'fp_bad_a')",
                (batch_id,),
            )

        # Invalid fund_account_id
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            db.execute(
                "INSERT INTO transactions (batch_id, source, account_id, fund_account_id, txn_time, time_precision, amount_cents, direction, fingerprint) VALUES (?, 'wechat', ?, 999999, '2026-11-09 10:00:00', 'second', 10, 'expense', 'fp_bad_f')",
                (batch_id, acc_id),
            )

        # Invalid category_id
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            db.execute(
                "INSERT INTO transactions (batch_id, source, account_id, category_id, txn_time, time_precision, amount_cents, direction, fingerprint) VALUES (?, 'wechat', ?, 999999, '2026-11-09 10:00:00', 'second', 10, 'expense', 'fp_bad_c')",
                (batch_id, acc_id),
            )


# ==============================================================================
# Challenge 3: Check Constraint on Negative Amounts & Schema Invariants
# ==============================================================================


def test_negative_amounts_check_constraint_db(temp_db_path: Path) -> None:
    """Adversarial challenge: Attempting to insert negative amount_cents into transactions

    MUST be blocked by SQLite CHECK(amount_cents >= 0).
    """
    with get_db(temp_db_path) as db:
        acc_id = db.execute("INSERT INTO accounts (kind, bank) VALUES ('platform', '支付宝')").lastrowid
        batch_id = db.execute("INSERT INTO import_batches (file_name, file_sha1, file_size, source) VALUES ('f', 'sha_chk', 1, 'alipay')").lastrowid

        # -1 cents
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed: amount_cents >= 0"):
            db.execute(
                """
                INSERT INTO transactions (batch_id, source, account_id, txn_time, time_precision, amount_cents, direction, fingerprint)
                VALUES (?, 'alipay', ?, '2026-11-09 12:00:00', 'second', -1, 'expense', 'fp_neg_1')
                """,
                (batch_id, acc_id),
            )

        # -1,000,000 cents
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed: amount_cents >= 0"):
            db.execute(
                """
                INSERT INTO transactions (batch_id, source, account_id, txn_time, time_precision, amount_cents, direction, fingerprint)
                VALUES (?, 'alipay', ?, '2026-11-09 12:00:00', 'second', -1000000, 'expense', 'fp_neg_large')
                """,
                (batch_id, acc_id),
            )

        # Updating to negative cents must also be blocked
        db.execute(
            """
            INSERT INTO transactions (batch_id, source, account_id, txn_time, time_precision, amount_cents, direction, fingerprint)
            VALUES (?, 'alipay', ?, '2026-11-09 12:00:00', 'second', 500, 'expense', 'fp_zero_to_neg')
            """,
            (batch_id, acc_id),
        )
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed: amount_cents >= 0"):
            db.execute("UPDATE transactions SET amount_cents = -50 WHERE fingerprint = 'fp_zero_to_neg'")

        # Boundary: 0 cents must be allowed (e.g. 0-amount promotional transaction)
        db.execute(
            """
            INSERT INTO transactions (batch_id, source, account_id, txn_time, time_precision, amount_cents, direction, fingerprint)
            VALUES (?, 'alipay', ?, '2026-11-09 12:00:00', 'second', 0, 'neutral', 'fp_zero')
            """,
            (batch_id, acc_id),
        )


def test_pydantic_model_negative_amounts_validation() -> None:
    """Adversarial challenge: Pydantic TransactionCreate model must reject negative amount_cents at boundary."""
    with pytest.raises(ValidationError):
        TransactionCreate(
            source="alipay",
            txn_time="2026-11-09 12:00:00",
            time_precision="second",
            amount_cents=-1,
            direction="expense",
        )

    # Valid boundary 0
    t = TransactionCreate(
        source="alipay",
        txn_time="2026-11-09 12:00:00",
        time_precision="second",
        amount_cents=0,
        direction="neutral",
    )
    assert t.amount_cents == 0


def test_valid_enum_check_constraints_in_schema(temp_db_path: Path) -> None:
    """Adversarial challenge: Verify SQLite CHECK constraints that work correctly on NOT NULL enum columns."""
    with get_db(temp_db_path) as db:
        acc_id = db.execute("INSERT INTO accounts (kind, bank) VALUES ('platform', '支付宝')").lastrowid
        batch_id = db.execute("INSERT INTO import_batches (file_name, file_sha1, file_size, source) VALUES ('f', 'sha_enum', 1, 'alipay')").lastrowid

        # Invalid direction
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
            db.execute(
                "INSERT INTO transactions (batch_id, source, account_id, txn_time, time_precision, amount_cents, direction, fingerprint) VALUES (?, 'alipay', ?, '2026-11-09', 'second', 10, 'invalid_dir', 'fp_enum_1')",
                (batch_id, acc_id),
            )

        # Invalid time_precision
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
            db.execute(
                "INSERT INTO transactions (batch_id, source, account_id, txn_time, time_precision, amount_cents, direction, fingerprint) VALUES (?, 'alipay', ?, '2026-11-09', 'hour', 10, 'expense', 'fp_enum_2')",
                (batch_id, acc_id),
            )

        # Invalid excluded (must be 0 or 1)
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
            db.execute(
                "INSERT INTO transactions (batch_id, source, account_id, txn_time, time_precision, amount_cents, direction, excluded, fingerprint) VALUES (?, 'alipay', ?, '2026-11-09', 'second', 10, 'expense', 2, 'fp_enum_4')",
                (batch_id, acc_id),
            )

        # Invalid account kind
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
            db.execute("INSERT INTO accounts (kind, bank) VALUES ('crypto_wallet', 'BTC')")

        # Invalid match_link status
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
            db.execute(
                "INSERT INTO match_links (kind, primary_id, secondary_id, score, status) VALUES ('duplicate', 1, 2, 0.9, 'invalid_status')"
            )


def test_vulnerability_channel_and_category_source_check_bypass(temp_db_path: Path) -> None:
    """Verify that channel and category_source check constraints reject invalid strings."""
    with get_db(temp_db_path) as db:
        acc_id = db.execute("INSERT INTO accounts (kind, bank) VALUES ('platform', '支付宝')").lastrowid
        batch_id = db.execute("INSERT INTO import_batches (file_name, file_sha1, file_size, source) VALUES ('f_vuln', 'sha_vuln', 1, 'alipay')").lastrowid

        # Invalid channel 'paypal' MUST be rejected by CHECK constraint:
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
            db.execute(
                """
                INSERT INTO transactions (batch_id, source, account_id, txn_time, time_precision, amount_cents, direction, channel, category_source, fingerprint)
                VALUES (?, 'alipay', ?, '2026-11-09', 'second', 100, 'expense', 'paypal', NULL, 'fp_vuln_bypass_1')
                """,
                (batch_id, acc_id),
            )

        # Invalid category_source MUST be rejected by CHECK constraint:
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
            db.execute(
                """
                INSERT INTO transactions (batch_id, source, account_id, txn_time, time_precision, amount_cents, direction, channel, category_source, fingerprint)
                VALUES (?, 'alipay', ?, '2026-11-09', 'second', 100, 'expense', NULL, 'completely_invalid_source', 'fp_vuln_bypass_2')
                """,
                (batch_id, acc_id),
            )

        # Valid channel and valid category_source succeed:
        db.execute(
            """
            INSERT INTO transactions (batch_id, source, account_id, txn_time, time_precision, amount_cents, direction, channel, category_source, fingerprint)
            VALUES (?, 'alipay', ?, '2026-11-09', 'second', 100, 'expense', 'alipay', 'manual', 'fp_vuln_valid')
            """,
            (batch_id, acc_id),
        )
        row = db.execute("SELECT channel, category_source FROM transactions WHERE fingerprint = 'fp_vuln_valid'").fetchone()
        assert row["channel"] == "alipay"
        assert row["category_source"] == "manual"


def test_vulnerability_missing_refunded_cents_check_constraint(temp_db_path: Path) -> None:
    """Verify that refunded_cents CHECK(refunded_cents >= 0) rejects negative refunded cents."""
    with get_db(temp_db_path) as db:
        acc_id = db.execute("INSERT INTO accounts (kind, bank) VALUES ('platform', '支付宝')").lastrowid
        batch_id = db.execute("INSERT INTO import_batches (file_name, file_sha1, file_size, source) VALUES ('f_ref', 'sha_ref', 1, 'alipay')").lastrowid

        # Negative refunded_cents (-5000) MUST be rejected by CHECK constraint:
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
            db.execute(
                """
                INSERT INTO transactions (batch_id, source, account_id, txn_time, time_precision, amount_cents, refunded_cents, direction, fingerprint)
                VALUES (?, 'alipay', ?, '2026-11-09', 'second', 1000, -5000, 'expense', 'fp_neg_refund')
                """,
                (batch_id, acc_id),
            )


# ==============================================================================
# Challenge 4: Unique Constraints on file_sha1 and Transaction Fingerprint
# ==============================================================================


def test_unique_constraint_file_sha1(temp_db_path: Path) -> None:
    """Adversarial challenge: Inserting two import batches with identical file_sha1

    MUST raise sqlite3.IntegrityError: UNIQUE constraint failed: import_batches.file_sha1.
    """
    with get_db(temp_db_path) as db:
        db.execute(
            "INSERT INTO import_batches (file_name, file_sha1, file_size, source) VALUES ('file1.csv', 'sha1_dup_test', 100, 'alipay')"
        )

        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed: import_batches.file_sha1"):
            db.execute(
                "INSERT INTO import_batches (file_name, file_sha1, file_size, source) VALUES ('file2.csv', 'sha1_dup_test', 200, 'alipay')"
            )


def test_unique_constraint_transaction_fingerprint(temp_db_path: Path) -> None:
    """Adversarial challenge: Inserting two transactions with identical fingerprint

    MUST raise sqlite3.IntegrityError: UNIQUE constraint failed: transactions.fingerprint.
    """
    with get_db(temp_db_path) as db:
        acc_id = db.execute("INSERT INTO accounts (kind, bank) VALUES ('platform', '支付宝')").lastrowid
        batch_id = db.execute("INSERT INTO import_batches (file_name, file_sha1, file_size, source) VALUES ('f', 'sha1_fp_test', 1, 'alipay')").lastrowid

        db.execute(
            """
            INSERT INTO transactions (batch_id, source, account_id, txn_time, time_precision, amount_cents, direction, fingerprint)
            VALUES (?, 'alipay', ?, '2026-11-09 12:00:00', 'second', 100, 'expense', 'identical_fp_12345')
            """,
            (batch_id, acc_id),
        )

        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed: transactions.fingerprint"):
            db.execute(
                """
                INSERT INTO transactions (batch_id, source, account_id, txn_time, time_precision, amount_cents, direction, fingerprint)
                VALUES (?, 'alipay', ?, '2026-11-09 12:01:00', 'second', 200, 'expense', 'identical_fp_12345')
                """,
                (batch_id, acc_id),
            )


def test_unique_constraint_accounts_and_null_tail_semantics(temp_db_path: Path) -> None:
    """Adversarial challenge: Unique constraint on accounts(kind, bank, tail) and NULL tail semantics."""
    with get_db(temp_db_path) as db:
        # Accounts unique constraint with tail: Enforced properly
        db.execute("INSERT INTO accounts (kind, bank, tail) VALUES ('bank_debit', '中国银行', '6666')")
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed: accounts.kind, accounts.bank, accounts.tail"):
            db.execute("INSERT INTO accounts (kind, bank, tail) VALUES ('bank_debit', '中国银行', '6666')")

        # Accounts unique index with NULL tail: Enforced by idx_accounts_unique_notail partial unique index
        db.execute("INSERT INTO accounts (kind, bank, tail) VALUES ('platform', '微信支付', NULL)")
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"):
            db.execute("INSERT INTO accounts (kind, bank, tail) VALUES ('platform', '微信支付', NULL)")


# ==============================================================================
# Challenge 5: Rollback Semantics on Partial Batch Failure
# ==============================================================================


def test_rollback_on_partial_batch_failure(temp_db_path: Path) -> None:
    """Adversarial challenge: When a transaction insertion fails midway through a batch,

    get_db context manager must execute conn.rollback(), leaving 0 orphan records.
    """
    with get_db(temp_db_path) as db:
        acc_id = db.execute("INSERT INTO accounts (kind, bank) VALUES ('platform', '支付宝')").lastrowid

    # Count before failed batch
    with get_db(temp_db_path) as db:
        initial_batches = db.execute("SELECT count(*) FROM import_batches").fetchone()[0]
        initial_txns = db.execute("SELECT count(*) FROM transactions").fetchone()[0]

    # Attempt inserting a batch where row 5 fails due to duplicate fingerprint
    failed = False
    try:
        with get_db(temp_db_path) as db:
            cur = db.execute(
                "INSERT INTO import_batches (file_name, file_sha1, file_size, source) VALUES ('batch_fail.csv', 'sha_fail_1', 100, 'alipay')"
            )
            batch_id = cur.lastrowid

            for i in range(10):
                if i == 5:
                    # Intentionally inject invalid constraint: negative amount
                    amount = -50
                else:
                    amount = 1000

                db.execute(
                    """
                    INSERT INTO transactions (batch_id, source, account_id, txn_time, time_precision, amount_cents, direction, fingerprint)
                    VALUES (?, 'alipay', ?, '2026-11-09 12:00:00', 'second', ?, 'expense', ?)
                    """,
                    (batch_id, acc_id, amount, f"fp_batch_fail_{i}"),
                )
    except sqlite3.IntegrityError:
        failed = True

    assert failed, "Batch insertion did not raise expected IntegrityError on invalid row"

    # VERIFY TOTAL ROLLBACK: Neither the batch header nor any of the 4 prior transactions leaked!
    with get_db(temp_db_path) as db:
        post_batches = db.execute("SELECT count(*) FROM import_batches").fetchone()[0]
        post_txns = db.execute("SELECT count(*) FROM transactions").fetchone()[0]

    assert post_batches == initial_batches, f"Orphan import batch leaked into DB! Expected {initial_batches}, got {post_batches}"
    assert post_txns == initial_txns, f"Orphan transactions leaked into DB! Expected {initial_txns}, got {post_txns}"


def test_rollback_on_multi_table_cascade_failure(temp_db_path: Path) -> None:
    """Adversarial challenge: Complex multi-table transaction rollback.

    If an error occurs while writing match links or categories, all updates to transactions
    and batches within that transaction unit are reverted cleanly.
    """
    with get_db(temp_db_path) as db:
        acc_id = db.execute("INSERT INTO accounts (kind, bank) VALUES ('platform', '支付宝')").lastrowid
        batch_id = db.execute("INSERT INTO import_batches (file_name, file_sha1, file_size, source) VALUES ('b.csv', 'sha_multi', 10, 'alipay')").lastrowid
        t1 = db.execute(
            "INSERT INTO transactions (batch_id, source, account_id, txn_time, time_precision, amount_cents, direction, fingerprint) VALUES (?, 'alipay', ?, '2026-11-09', 'second', 500, 'expense', 'fp_m1')",
            (batch_id, acc_id),
        ).lastrowid

    # Verify initial state
    with get_db(temp_db_path) as db:
        assert db.execute("SELECT note FROM transactions WHERE id = ?", (t1,)).fetchone()[0] is None

    # Multi-statement transaction that fails on step 3
    failed = False
    try:
        with get_db(temp_db_path) as db:
            # Step 1: Update transaction note
            db.execute("UPDATE transactions SET note = 'Should be rolled back' WHERE id = ?", (t1,))
            # Step 2: Insert category
            cat_id = db.execute("INSERT INTO categories (name, direction) VALUES ('TempCat', 'expense')").lastrowid
            # Step 3: Insert cache row
            db.execute("INSERT INTO llm_cache (task_type, input_hash, response_json, model) VALUES ('classify', 'hash_dup', '{}', 'gpt-4o')")
            # Step 4: Duplicate insertion on unique key
            db.execute("INSERT INTO llm_cache (task_type, input_hash, response_json, model) VALUES ('classify', 'hash_dup', '{}', 'gpt-4o')")
    except sqlite3.IntegrityError:
        failed = True

    assert failed

    # Assert Step 1 and Step 2 were completely rolled back
    with get_db(temp_db_path) as db:
        t_row = db.execute("SELECT note FROM transactions WHERE id = ?", (t1,)).fetchone()
        assert t_row["note"] is None, "Transaction update was not rolled back!"
        cat_count = db.execute("SELECT count(*) FROM categories WHERE name = 'TempCat'").fetchone()[0]
        assert cat_count == 0, "Category was not rolled back!"
        cache_count = db.execute("SELECT count(*) FROM llm_cache WHERE input_hash = 'hash_dup'").fetchone()[0]
        assert cache_count == 0, "Cache record was not rolled back!"


# ==============================================================================
# Challenge 6: Money Module Extreme Edge Cases & Precision Limits
# ==============================================================================


def test_money_extreme_values_and_sanitization() -> None:
    """Adversarial challenge: Stress-testing to_cents, to_display, and format_money on extreme values."""
    # Enormous monetary values (e.g. 100 billion yuan)
    cents_huge = to_cents("100,000,000,000.00")
    assert cents_huge == 10000000000000
    assert to_display(cents_huge) == "100000000000.00"

    # Exotic formatted inputs with currency prefixes, spaces, trailing symbols
    assert to_cents("  ¥ 1,234,567.89 元 ") == 123456789
    assert to_cents("$-9,876.54") == -987654
    assert to_cents("+00012.3400") == 1234

    # Half-way rounding cases
    assert to_cents("0.005") == 1  # 0.005 rounds up to 0.01 (1 cent)
    assert to_cents("0.004") == 0
    assert to_cents("-0.005") == -1

    # Empty, whitespace, None, and pure alphabetic strings sanitize to 0
    assert to_cents(None) == 0
    assert to_cents("") == 0
    assert to_cents("   ") == 0
    assert to_cents("-") == 0
    # Note: Pure alphabetic string has all chars stripped by regex, leaving empty string which returns 0
    assert to_cents("abc") == 0
    assert to_cents("N/A") == 0

    # Malformed numbers with multiple decimals or multiple signs raise ValueError
    with pytest.raises(ValueError):
        to_cents("12.34.56")

    with pytest.raises(ValueError):
        to_cents("--50")

    # Display formatting negative edge cases
    assert to_display(-1) == "-0.01"
    assert to_display(-99) == "-0.99"
    assert to_display(-100) == "-1.00"
    assert to_display(-105) == "-1.05"
    assert format_money(-105, "CNY") == "-¥1.05"
    assert format_money(-105, "USD") == "-$1.05"

