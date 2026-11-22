"""Foreign Exchange (FX) service for rate fetching, caching, conversion, and audit tracking.

Strictly adheres to Milestone 6 specifications and Frontend Design Document v1.2:
- §0/D12, §4.2, §5.1, §6.4, §6.5, §6.8.4, §8.1-§8.3, §8.6, Appendix C-5.
- Foreign currency transactions converted to CNY at transaction-time rate:
  amount_cny = round(amount_cents * fx_rate) (fx_rate with 4-6 decimal places).
- Original amount_cents and currency are ALWAYS preserved.
- Missing FX Anti-Distortion: if offline or rate missing, fx_rate_source='none', amount_cny=NULL.
  Never estimate as 0, never include in CNY stats totals.
"""

from __future__ import annotations

from datetime import datetime
import logging
import math
import re
import sqlite3
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, urlparse

import httpx

from billapp.importers.common import normalize_currency

logger = logging.getLogger("billapp.core.fx")

# Primary and fallback endpoints for free public exchange rates (ECB based)
FRANKFURTER_API_URL = "https://api.frankfurter.app/{date}?from={currency}&to=CNY"
FRANKFURTER_BACKUP_URL = "https://api.frankfurter.dev/v1/{date}?from={currency}&to=CNY"
_ALLOWED_FX_HOSTS = {"api.frankfurter.app", "api.frankfurter.dev"}
ONLINE_FETCH_TIMEOUT_SECONDS = 3.0
MAX_FX_RATE = 1_000_000.0

_ISO_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def validate_fx_currency(currency: str) -> str:
    """Validate strict 3-letter ISO-4217 currency code (e.g. USD, EUR)."""
    curr = (currency or "").strip().upper()
    if not _ISO_CURRENCY_RE.fullmatch(curr):
        raise ValueError(f"Invalid ISO currency code: {currency!r}")
    return curr


def validate_fx_date(date_str: str) -> str:
    """Validate strict YYYY-MM-DD calendar date."""
    date_part = (date_str or "").strip()[:10]
    if not _ISO_DATE_RE.fullmatch(date_part):
        raise ValueError(f"Invalid effective date format (expected YYYY-MM-DD): {date_str!r}")
    try:
        datetime.strptime(date_part, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError(f"Invalid calendar date: {date_part!r}") from exc
    return date_part


def validate_fx_rate(rate: float) -> float:
    """Validate finite, positive, bounded FX rate."""
    try:
        val = float(rate)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid FX rate: {rate!r}") from exc
    if not math.isfinite(val) or val <= 0 or val > MAX_FX_RATE:
        raise ValueError(f"FX rate must be finite and within (0, {MAX_FX_RATE:g}]: {rate!r}")
    return round(val, 6)


def fetch_online_fx_rate(currency: str, date_str: str) -> Optional[float]:
    """Query online rate from free public FX API (Frankfurter ECB).
    
    Non-blocking: times out after 3s, disables redirects, strictly validates ISO
    currency and YYYY-MM-DD date, and gracefully returns None if offline or unsupported.
    """
    curr_norm = normalize_currency(currency, default="")
    try:
        curr = validate_fx_currency(curr_norm)
        date_part = validate_fx_date(date_str)
    except ValueError as exc:
        logger.debug("Skipping online FX fetch due to invalid input: %s", exc)
        return None

    if curr == "CNY":
        return 1.0

    safe_date = quote(date_part, safe="")
    safe_curr = quote(curr, safe="")
    urls = [
        FRANKFURTER_API_URL.format(date=safe_date, currency=safe_curr),
        FRANKFURTER_BACKUP_URL.format(date=safe_date, currency=safe_curr),
    ]

    headers = {"User-Agent": "BillApp/1.0 (Desktop; OpenSource Bill Management)"}
    for url in urls:
        parsed = urlparse(url)
        if parsed.scheme != "https" or (parsed.hostname or "").lower() not in _ALLOWED_FX_HOSTS:
            continue
        try:
            resp = httpx.get(
                url,
                headers=headers,
                timeout=ONLINE_FETCH_TIMEOUT_SECONDS,
                follow_redirects=False,
            )
            if resp.status_code == 200:
                data = resp.json()
                rates = data.get("rates", {}) if isinstance(data, dict) else {}
                cny_rate = rates.get("CNY")
                if cny_rate is not None:
                    return validate_fx_rate(float(cny_rate))
        except Exception as exc:
            logger.debug("Failed online FX fetch from %s: %s", url, exc)
            continue

    return None


def get_cached_fx_rate(
    conn: sqlite3.Connection,
    currency: str,
    date_str: str,
) -> Optional[sqlite3.Row]:
    """Retrieve cached exchange rate from fx_rates table.
    
    Matches exact effective_date first; if missing, falls back to the latest
    effective rate for this currency on or before the requested date.
    """
    curr = currency.strip().upper()
    date_part = date_str[:10]

    # 1. Exact date match
    row = conn.execute(
        "SELECT * FROM fx_rates WHERE currency = ? AND effective_date = ?",
        (curr, date_part),
    ).fetchone()
    if row:
        return row

    # 2. Latest prior effective rate
    row = conn.execute(
        """
        SELECT * FROM fx_rates
        WHERE currency = ? AND effective_date <= ?
        ORDER BY effective_date DESC
        LIMIT 1
        """,
        (curr, date_part),
    ).fetchone()
    return row


def convert_transaction(
    conn: sqlite3.Connection,
    currency: str,
    amount_cents: int,
    txn_time: str,
) -> Tuple[Optional[int], Optional[float], Optional[str], Optional[str]]:
    """Convert transaction amount into CNY using cached or online exchange rate.
    
    Returns:
        (amount_cny, fx_rate, fx_rate_at, fx_rate_source)
        
    If currency is CNY:
        returns (amount_cents, None, None, None)
    If foreign currency and rate found:
        amount_cny = round(amount_cents * fx_rate)
        returns (amount_cny, fx_rate, effective_date, source)
    If rate cannot be found / offline:
        returns (None, None, None, 'none')
    """
    curr = normalize_currency(currency, default="CNY") if currency else "CNY"
    if curr == "CNY":
        return amount_cents, None, None, None

    date_part = txn_time[:10]

    # 1. Query cached rate
    cached = get_cached_fx_rate(conn, curr, date_part)
    if cached:
        rate = float(cached["rate"])
        rate_at = cached["effective_date"]
        source = cached["source"]
        amount_cny = round(amount_cents * rate)
        return amount_cny, rate, rate_at, source

    # 2. Attempt online query
    online_rate = fetch_online_fx_rate(curr, date_part)
    if online_rate is not None:
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            conn.execute(
                """
                INSERT INTO fx_rates (currency, rate, effective_date, source, created_at, updated_at, note)
                VALUES (?, ?, ?, 'online', ?, ?, 'Auto-fetched on import')
                ON CONFLICT(currency, effective_date) DO UPDATE SET
                    rate = excluded.rate,
                    source = excluded.source,
                    updated_at = excluded.updated_at
                """,
                (curr, online_rate, date_part, now_str, now_str),
            )
        except Exception as exc:
            logger.warning("Failed to cache online fx rate: %s", exc)

        amount_cny = round(amount_cents * online_rate)
        return amount_cny, online_rate, date_part, "online"

    # 3. Non-blocking fallback: rate missing
    return None, None, None, "none"


def record_manual_rate(
    conn: sqlite3.Connection,
    currency: str,
    rate: float,
    effective_date: str,
    note: Optional[str] = None,
    apply_to_all_missing: bool = False,
) -> Tuple[Dict[str, Any], int]:
    """Record manually entered FX rate and update matching missing transactions.
    
    Per §6.8.4: Saves rate into fx_rates and updates only missing transactions
    (amount_cny IS NULL or fx_rate_source = 'none'). Existing 'online' or 'manual'
    transactions remain untouched.
    """
    curr = validate_fx_currency(currency)
    date_part = validate_fx_date(effective_date)
    rounded_rate = validate_fx_rate(rate)
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    conn.execute(
        """
        INSERT INTO fx_rates (currency, rate, effective_date, source, created_at, updated_at, note)
        VALUES (?, ?, ?, 'manual', ?, ?, ?)
        ON CONFLICT(currency, effective_date) DO UPDATE SET
            rate = excluded.rate,
            source = 'manual',
            updated_at = excluded.updated_at,
            note = excluded.note
        """,
        (curr, rounded_rate, date_part, now_str, now_str, note),
    )

    # Update matching missing transactions
    if apply_to_all_missing:
        cur = conn.execute(
            """
            UPDATE transactions
            SET amount_cny = ROUND(amount_cents * ?),
                fx_rate = ?,
                fx_rate_at = ?,
                fx_rate_source = 'manual'
            WHERE currency = ?
              AND (amount_cny IS NULL OR fx_rate_source = 'none')
            """,
            (rounded_rate, rounded_rate, date_part, curr),
        )
    else:
        cur = conn.execute(
            """
            UPDATE transactions
            SET amount_cny = ROUND(amount_cents * ?),
                fx_rate = ?,
                fx_rate_at = ?,
                fx_rate_source = 'manual'
            WHERE currency = ?
              AND (amount_cny IS NULL OR fx_rate_source = 'none')
              AND substr(txn_time, 1, 10) = ?
            """,
            (rounded_rate, rounded_rate, date_part, curr, date_part),
        )

    updated_count = cur.rowcount
    rate_row = conn.execute(
        "SELECT * FROM fx_rates WHERE currency = ? AND effective_date = ?",
        (curr, date_part),
    ).fetchone()

    return dict(rate_row) if rate_row else {}, updated_count


def refresh_missing_fx(conn: sqlite3.Connection) -> Tuple[int, int]:
    """Scan and refresh exchange rates only for transactions with missing FX.
    
    Does NOT overwrite records that already have 'online' or 'manual' rates.
    Returns: (total_missing, refreshed_count)
    """
    missing_rows = conn.execute(
        """
        SELECT id, currency, amount_cents, txn_time
        FROM transactions
        WHERE currency != 'CNY'
          AND currency IS NOT NULL
          AND (amount_cny IS NULL OR fx_rate_source = 'none')
        """
    ).fetchall()

    total_missing = len(missing_rows)
    refreshed_count = 0

    for r in missing_rows:
        txn_id = r["id"]
        curr = r["currency"]
        cents = r["amount_cents"]
        ttime = r["txn_time"]

        amount_cny, rate, rate_at, source = convert_transaction(conn, curr, cents, ttime)
        if amount_cny is not None and source in ("online", "manual"):
            conn.execute(
                """
                UPDATE transactions
                SET amount_cny = ?,
                    fx_rate = ?,
                    fx_rate_at = ?,
                    fx_rate_source = ?
                WHERE id = ?
                """,
                (amount_cny, rate, rate_at, source, txn_id),
            )
            refreshed_count += 1

    return total_missing, refreshed_count


def retry_transaction_fx(conn: sqlite3.Connection, txn_id: int) -> Optional[Dict[str, Any]]:
    """Retry obtaining exchange rate for a single transaction."""
    row = conn.execute("SELECT * FROM transactions WHERE id = ?", (txn_id,)).fetchone()
    if not row:
        return None

    curr = row["currency"]
    cents = row["amount_cents"]
    ttime = row["txn_time"]

    if curr == "CNY" or curr is None:
        conn.execute("UPDATE transactions SET amount_cny = ? WHERE id = ?", (cents, txn_id))
    else:
        # First check online query directly
        date_part = ttime[:10]
        online_rate = fetch_online_fx_rate(curr, date_part)
        if online_rate is not None:
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            conn.execute(
                """
                INSERT INTO fx_rates (currency, rate, effective_date, source, created_at, updated_at, note)
                VALUES (?, ?, ?, 'online', ?, ?, 'Refetched single transaction')
                ON CONFLICT(currency, effective_date) DO UPDATE SET
                    rate = excluded.rate,
                    source = excluded.source,
                    updated_at = excluded.updated_at
                """,
                (curr, online_rate, date_part, now_str, now_str),
            )
            amount_cny = round(cents * online_rate)
            conn.execute(
                """
                UPDATE transactions
                SET amount_cny = ?, fx_rate = ?, fx_rate_at = ?, fx_rate_source = 'online'
                WHERE id = ?
                """,
                (amount_cny, online_rate, date_part, txn_id),
            )
        else:
            # Check cached rate fallback
            cached = get_cached_fx_rate(conn, curr, date_part)
            if cached:
                rate = float(cached["rate"])
                amount_cny = round(cents * rate)
                conn.execute(
                    """
                    UPDATE transactions
                    SET amount_cny = ?, fx_rate = ?, fx_rate_at = ?, fx_rate_source = ?
                    WHERE id = ?
                    """,
                    (amount_cny, rate, cached["effective_date"], cached["source"], txn_id),
                )
            else:
                conn.execute(
                    """
                    UPDATE transactions
                    SET amount_cny = NULL, fx_rate = NULL, fx_rate_source = 'none'
                    WHERE id = ?
                    """,
                    (txn_id,),
                )

    updated_row = conn.execute("SELECT * FROM transactions WHERE id = ?", (txn_id,)).fetchone()
    return dict(updated_row) if updated_row else None


def get_fx_overview(conn: sqlite3.Connection) -> Dict[str, Any]:
    """Retrieve full FX overview: rates table, currencies in use, and pending counts."""
    rates_rows = conn.execute(
        "SELECT * FROM fx_rates ORDER BY effective_date DESC, currency ASC"
    ).fetchall()

    curr_rows = conn.execute(
        "SELECT DISTINCT currency FROM transactions WHERE currency IS NOT NULL ORDER BY currency ASC"
    ).fetchall()
    currencies = [r[0] for r in curr_rows]
    if "CNY" not in currencies:
        currencies.insert(0, "CNY")

    pending_row = conn.execute(
        """
        SELECT COUNT(*)
        FROM transactions
        WHERE currency != 'CNY'
          AND currency IS NOT NULL
          AND (amount_cny IS NULL OR fx_rate_source = 'none')
        """
    ).fetchone()
    pending_count = int(pending_row[0]) if pending_row else 0

    pending_group_rows = conn.execute(
        """
        SELECT currency, COUNT(*) AS cnt, COALESCE(SUM(amount_cents), 0) AS total_cents
        FROM transactions
        WHERE currency != 'CNY'
          AND currency IS NOT NULL
          AND (amount_cny IS NULL OR fx_rate_source = 'none')
        GROUP BY currency
        ORDER BY currency ASC
        """
    ).fetchall()
    pending_by_currency = [
        {"currency": r[0], "count": int(r[1]), "amount_cents": int(r[2])}
        for r in pending_group_rows
    ]

    return {
        "rates": [dict(r) for r in rates_rows],
        "currencies": currencies,
        "pending_count": pending_count,
        "pending_by_currency": pending_by_currency,
    }
