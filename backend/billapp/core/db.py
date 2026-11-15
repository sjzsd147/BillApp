"""SQLite database connection management, WAL configuration, and schema initialization."""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Generator, Optional

DEFAULT_SCHEMA_PATH = Path(__file__).parent / "schema.sql"

from billapp.paths import get_default_db_path


def configure_connection(conn: sqlite3.Connection) -> sqlite3.Connection:
    """Apply required SQLite PRAGMAs: WAL mode, foreign keys, and busy timeout."""
    conn.row_factory = sqlite3.Row
    # WAL mode may return 'memory' for in-memory databases
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA busy_timeout = 5000;")
    return conn


def create_connection(db_path: Optional[str | Path] = None) -> sqlite3.Connection:
    """Create a configured SQLite connection."""
    if db_path is None:
        target_path = str(get_default_db_path())
    else:
        target_path = str(db_path)
        if target_path != ":memory:":
            Path(target_path).parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(target_path, check_same_thread=False)
    return configure_connection(conn)


def migrate_db(conn: sqlite3.Connection) -> None:
    """Safely apply schema migrations for new columns and tables on existing SQLite databases."""
    tbl = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='transactions'").fetchone()
    if tbl:
        cols = {
            row["name"] if isinstance(row, sqlite3.Row) else row[1]
            for row in conn.execute("PRAGMA table_info(transactions)").fetchall()
        }
        if "amount_cny" not in cols:
            conn.execute("ALTER TABLE transactions ADD COLUMN amount_cny INT")
        if "fx_rate" not in cols:
            conn.execute("ALTER TABLE transactions ADD COLUMN fx_rate REAL")
        if "fx_rate_at" not in cols:
            conn.execute("ALTER TABLE transactions ADD COLUMN fx_rate_at TEXT")
        if "fx_rate_source" not in cols:
            conn.execute("ALTER TABLE transactions ADD COLUMN fx_rate_source TEXT CHECK(fx_rate_source IS NULL OR fx_rate_source IN ('online', 'manual', 'none'))")

        # Backfill amount_cny for CNY transactions where missing
        conn.execute("UPDATE transactions SET amount_cny = amount_cents WHERE (currency = 'CNY' OR currency IS NULL) AND amount_cny IS NULL")

    # Ensure fx_rates table and index exist
    conn.execute("""
        CREATE TABLE IF NOT EXISTS fx_rates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            currency TEXT NOT NULL,
            rate REAL NOT NULL,
            effective_date TEXT NOT NULL,
            source TEXT NOT NULL CHECK(source IN ('online', 'manual')),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            note TEXT
        )
    """)
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_fx_rates_curr_date ON fx_rates(currency, effective_date)")


def init_db(
    db_path: Optional[str | Path] = None,
    schema_path: Optional[str | Path] = None,
) -> sqlite3.Connection:
    """Initialize database tables using schema.sql and return the connection."""
    target_schema = Path(schema_path or DEFAULT_SCHEMA_PATH)
    if not target_schema.exists():
        raise FileNotFoundError(f"Schema file not found at {target_schema}")

    sql_script = target_schema.read_text(encoding="utf-8")
    conn = create_connection(db_path)
    with conn:
        conn.executescript(sql_script)
        migrate_db(conn)
    return conn


@contextmanager
def get_db(db_path: Optional[str | Path] = None) -> Generator[sqlite3.Connection, None, None]:
    """Context manager yielding a transactional SQLite connection."""
    conn = create_connection(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
