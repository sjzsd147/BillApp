"""L1 Fingerprint Deduplication Engine.

Computes idempotent SHA-1 hashes for transactions:
- Global order-ID uniqueness for platform statements (Alipay, WeChat).
- Intra-file sequence counter fallback for bank statements lacking unique order IDs.
"""

from __future__ import annotations

import hashlib
import json
from typing import Optional, Sequence

from billapp.core.models import TransactionCreate


PLACEHOLDER_ORDER_IDS: frozenset[str] = frozenset({
    "",
    "/",
    "-",
    "--",
    "---",
    "——",
    "－",
    "无",
    "null",
    "none",
    "nan",
    "nil",
})


def is_valid_order_id(order_id: Optional[str]) -> bool:
    """Return True if order_id is non-empty and not a known placeholder."""
    if not order_id:
        return False
    clean = order_id.replace("\t", "").strip()
    return bool(clean and clean.lower() not in PLACEHOLDER_ORDER_IDS)


def compute_order_fingerprint(source: str, order_id: str) -> str:
    """Compute SHA1 fingerprint from platform order ID."""
    clean_id = order_id.strip()
    payload = f"{source}|{clean_id}".encode("utf-8")
    return hashlib.sha1(payload).hexdigest()


def compute_fallback_fingerprint(
    source: str,
    card_tail: Optional[str],
    txn_time: str,
    amount_cents: int,
    currency: str,
    direction: str,
    counterparty: Optional[str],
    description: Optional[str],
    seq_n: int = 0,
) -> str:
    """Compute fallback SHA1 fingerprint when order_id is absent."""
    parts = [
        source,
        card_tail or "",
        txn_time,
        amount_cents,
        currency,
        direction,
        (counterparty or "").strip(),
        (description or "").strip(),
        seq_n,
    ]
    payload = json.dumps(parts, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha1(payload).hexdigest()


def assign_fingerprints(transactions: Sequence[TransactionCreate]) -> list[TransactionCreate]:
    """Assign stable SHA1 fingerprints to all transactions in a parsed batch.

    Maintains an intra-batch duplicate counter for order-less transactions to ensure
    that legitimate duplicate charges within the same file receive deterministic,
    distinct fingerprints while identical re-imports will match exactly.
    """
    fallback_counts: dict[tuple, int] = {}

    for txn in transactions:
        if is_valid_order_id(txn.order_id):
            clean_id = txn.order_id.replace("\t", "").strip()
            txn.fingerprint = compute_order_fingerprint(txn.source, clean_id)
        else:
            if txn.order_id and not is_valid_order_id(txn.order_id):
                txn.order_id = None
            tail = None
            if txn.account_ref and txn.account_ref.tail:
                tail = txn.account_ref.tail
            elif txn.fund_account_ref and txn.fund_account_ref.tail:
                tail = txn.fund_account_ref.tail

            key = (
                txn.source,
                tail or "",
                txn.txn_time,
                txn.amount_cents,
                txn.currency,
                txn.direction,
                (txn.counterparty or "").strip(),
                (txn.description or "").strip(),
            )
            seq_n = fallback_counts.get(key, 0)
            fallback_counts[key] = seq_n + 1
            txn.fingerprint = compute_fallback_fingerprint(
                source=txn.source,
                card_tail=tail,
                txn_time=txn.txn_time,
                amount_cents=txn.amount_cents,
                currency=txn.currency,
                direction=txn.direction,
                counterparty=txn.counterparty,
                description=txn.description,
                seq_n=seq_n,
            )

    return list(transactions)
