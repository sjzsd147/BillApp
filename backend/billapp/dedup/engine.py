"""Unified Deduplication & Transfer Pipeline Coordinator.

Feature 17 & Dedup Coordinator:
1. L1 Fingerprint check & idempotency
2. L2 In-source refund linking & neutral propagation
3. L3 Cross-source platform-to-bank matching
4. L4 Internal transfer pairing
5. Rejection memory (match_blocklist) with canonical ordering
6. Link confirmation & rejection state machine
7. Safe import batch rollback & state restoration
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Dict, Optional
from pydantic import BaseModel, ConfigDict

from billapp.dedup.fingerprint import assign_fingerprints
from billapp.dedup.refund import link_in_source_refunds
from billapp.dedup.cross_source import match_cross_source
from billapp.dedup.transfer import pair_internal_transfers


class DedupSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    batch_id: Optional[int] = None
    refunds_linked: int = 0
    duplicates_auto: int = 0
    duplicates_pending: int = 0
    transfers_paired: int = 0
    transfers_pending: int = 0


def run_dedup_pipeline(conn: sqlite3.Connection, batch_id: Optional[int] = None) -> DedupSummary:
    """Execute complete 4-stage deduplication pipeline.
    
    Pipeline stages:
    ① L1 Fingerprint check (enforced at insertion time via UNIQUE(fingerprint))
    ② L2 In-Source Refund Linking
    ③ L3 Cross-Source Platform-to-Bank Matching
    ④ L4 Internal Transfer Pairing
    """
    # Stage 2: L2 In-source refund linking
    refunds_linked = link_in_source_refunds(conn, batch_id=batch_id)

    # Stage 3: L3 Cross-source matching
    dup_matches = match_cross_source(conn)
    auto_dups = sum(1 for m in dup_matches if m.get("status") == "auto")
    pending_dups = sum(1 for m in dup_matches if m.get("status") == "pending")

    # Stage 4: L4 Internal transfer pairing
    transfer_matches = pair_internal_transfers(conn, batch_id=batch_id)
    auto_transfers = sum(1 for m in transfer_matches if m.get("status") == "auto")
    pending_transfers = sum(1 for m in transfer_matches if m.get("status") == "pending")

    return DedupSummary(
        batch_id=batch_id,
        refunds_linked=refunds_linked,
        duplicates_auto=auto_dups,
        duplicates_pending=pending_dups,
        transfers_paired=auto_transfers,
        transfers_pending=pending_transfers,
    )


def confirm_match_link(conn: sqlite3.Connection, link_id: int) -> bool:
    """User confirms a pending or auto match link."""
    link = conn.execute("SELECT * FROM match_links WHERE id = ?", (link_id,)).fetchone()
    if not link:
        return False

    with conn:
        conn.execute(
            """
            UPDATE match_links
            SET status = 'confirmed', updated_at = datetime('now', 'localtime')
            WHERE id = ?
            """,
            (link_id,),
        )

        kind = link["kind"]
        p_id = link["primary_id"]
        s_id = link["secondary_id"]

        if kind == "duplicate":
            conn.execute(
                "UPDATE transactions SET duplicate_of = ? WHERE id = ?",
                (p_id, s_id),
            )
        elif kind == "transfer":
            conn.execute(
                "UPDATE transactions SET transfer_peer = ?, direction = 'transfer' WHERE id = ?",
                (s_id, p_id),
            )
            conn.execute(
                "UPDATE transactions SET transfer_peer = ?, direction = 'transfer' WHERE id = ?",
                (p_id, s_id),
            )

    return True


def reject_match_link(conn: sqlite3.Connection, link_id: int) -> bool:
    """User rejects a match link: write to match_blocklist with canonical ordering and revert states."""
    link = conn.execute("SELECT * FROM match_links WHERE id = ?", (link_id,)).fetchone()
    if not link:
        return False

    p_id = link["primary_id"]
    s_id = link["secondary_id"]
    kind = link["kind"]
    features_json = link["features_json"]

    # Canonical ordering: a_id < b_id
    a_id, b_id = min(p_id, s_id), max(p_id, s_id)

    with conn:
        # 1. Permanent rejection memory blocklist
        conn.execute(
            """
            INSERT OR IGNORE INTO match_blocklist (a_id, b_id, kind, created_at)
            VALUES (?, ?, ?, datetime('now', 'localtime'))
            """,
            (a_id, b_id, kind),
        )

        # 2. Update match_links status
        conn.execute(
            """
            UPDATE match_links
            SET status = 'rejected', updated_at = datetime('now', 'localtime')
            WHERE id = ?
            """,
            (link_id,),
        )

        # 3. Revert transaction links
        if kind == "duplicate":
            conn.execute(
                "UPDATE transactions SET duplicate_of = NULL WHERE id = ?",
                (s_id,),
            )
        elif kind == "transfer":
            conn.execute(
                "UPDATE transactions SET transfer_peer = NULL WHERE id IN (?, ?)",
                (p_id, s_id),
            )
            # Revert original directions if preserved in features
            if features_json:
                try:
                    feat = json.loads(features_json)
                    orig_out = feat.get("orig_dir_out")
                    orig_in = feat.get("orig_dir_in")
                    if orig_out:
                        conn.execute("UPDATE transactions SET direction = ? WHERE id = ?", (orig_out, p_id))
                    if orig_in:
                        conn.execute("UPDATE transactions SET direction = ? WHERE id = ?", (orig_in, s_id))
                except Exception:
                    pass

    return True


def is_pair_blocked(conn: sqlite3.Connection, id1: int, id2: int, kind: Optional[str] = None) -> bool:
    """Check if a pair of transactions is permanently blocked by user rejection."""
    a_id, b_id = min(id1, id2), max(id1, id2)
    if kind:
        row = conn.execute(
            "SELECT 1 FROM match_blocklist WHERE a_id = ? AND b_id = ? AND kind = ?",
            (a_id, b_id, kind),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT 1 FROM match_blocklist WHERE a_id = ? AND b_id = ?",
            (a_id, b_id),
        ).fetchone()

    return row is not None


def rollback_import_batch(conn: sqlite3.Connection, batch_id: int) -> Dict[str, Any]:
    """Safely roll back an imported batch, restoring all interrelated transaction states."""
    with conn:
        # Check if batch exists in import_batches or transactions
        batch_row = conn.execute(
            "SELECT id FROM import_batches WHERE id = ?",
            (batch_id,),
        ).fetchone()

        # Step 1: Identify transactions in this batch
        batch_txns = conn.execute(
            "SELECT id, amount_cents, refund_of FROM transactions WHERE batch_id = ?",
            (batch_id,),
        ).fetchall()

        if not batch_row and not batch_txns:
            return {"batch_id": batch_id, "deleted_transactions": 0, "status": "not_found"}

        # Step 2: Restore refunded_cents on surviving transactions credited by deleted refunds
        refund_credits = conn.execute(
            """
            SELECT refund_of, SUM(amount_cents) AS total_refunded
            FROM transactions
            WHERE batch_id = ? AND refund_of IS NOT NULL
            GROUP BY refund_of
            """,
            (batch_id,),
        ).fetchall()

        for rc in refund_credits:
            orig_id = rc["refund_of"]
            credit_cents = rc["total_refunded"]
            conn.execute(
                """
                UPDATE transactions
                SET refunded_cents = MAX(0, refunded_cents - ?)
                WHERE id = ?
                """,
                (credit_cents, orig_id),
            )

        # Step 3: Clear duplicate_of references pointing to transactions in this batch
        conn.execute(
            """
            UPDATE transactions
            SET duplicate_of = NULL
            WHERE duplicate_of IN (SELECT id FROM transactions WHERE batch_id = ?)
            """,
            (batch_id,),
        )

        # Step 4: Revert transfer directions on surviving transactions and clear transfer_peer
        # 4a. Query transfer match_links involving transactions from this batch before deletion
        transfer_links = conn.execute(
            """
            SELECT ml.id, ml.primary_id, ml.secondary_id, ml.features_json,
                   t1.batch_id AS p_batch, t2.batch_id AS s_batch
            FROM match_links ml
            JOIN transactions t1 ON ml.primary_id = t1.id
            JOIN transactions t2 ON ml.secondary_id = t2.id
            WHERE ml.kind = 'transfer'
              AND (t1.batch_id = ? OR t2.batch_id = ?)
            """,
            (batch_id, batch_id),
        ).fetchall()

        for link in transfer_links:
            p_id = link["primary_id"]
            s_id = link["secondary_id"]
            p_batch = link["p_batch"]
            s_batch = link["s_batch"]
            feat = {}
            if link["features_json"]:
                try:
                    feat = json.loads(link["features_json"])
                except Exception:
                    feat = {}

            # If primary (outflow) is in this batch, secondary (inflow) survives
            if p_batch == batch_id and s_batch != batch_id:
                orig_in = feat.get("orig_dir_in")
                if orig_in:
                    conn.execute(
                        "UPDATE transactions SET transfer_peer = NULL, direction = ? WHERE id = ?",
                        (orig_in, s_id),
                    )
                else:
                    conn.execute("UPDATE transactions SET transfer_peer = NULL WHERE id = ?", (s_id,))
            # If secondary (inflow) is in this batch, primary (outflow) survives
            elif s_batch == batch_id and p_batch != batch_id:
                orig_out = feat.get("orig_dir_out")
                if orig_out:
                    conn.execute(
                        "UPDATE transactions SET transfer_peer = NULL, direction = ? WHERE id = ?",
                        (orig_out, p_id),
                    )
                else:
                    conn.execute("UPDATE transactions SET transfer_peer = NULL WHERE id = ?", (p_id,))

        # 4b. Defensive cleanup: clear any remaining transfer_peer references pointing to this batch
        conn.execute(
            """
            UPDATE transactions
            SET transfer_peer = NULL
            WHERE transfer_peer IN (SELECT id FROM transactions WHERE batch_id = ?)
            """,
            (batch_id,),
        )

        # 4c. Defensive cleanup: delete match_links involving transactions in this batch
        conn.execute(
            """
            DELETE FROM match_links
            WHERE primary_id IN (SELECT id FROM transactions WHERE batch_id = ?)
               OR secondary_id IN (SELECT id FROM transactions WHERE batch_id = ?)
            """,
            (batch_id, batch_id),
        )

        # Step 5: Clear refund_of references pointing to transactions in this batch
        conn.execute(
            """
            UPDATE transactions
            SET refund_of = NULL
            WHERE refund_of IN (SELECT id FROM transactions WHERE batch_id = ?)
            """,
            (batch_id,),
        )

        # Step 6: Delete the batch record (cascading to transactions, match_links, etc.)
        conn.execute("DELETE FROM import_batches WHERE id = ?", (batch_id,))

        # Defensive cleanup: ensure batch transactions are deleted even if FK cascade is off
        conn.execute("DELETE FROM transactions WHERE batch_id = ?", (batch_id,))

    return {
        "batch_id": batch_id,
        "deleted_transactions": len(batch_txns),
        "status": "rolled_back",
    }
