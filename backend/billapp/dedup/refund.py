"""L2 In-Source Refund Linking Engine.

Handles:
- Alipay: Order ID prefix matching ({orig_order_id}*F-refund... or {orig_order_id}_{seq}).
- WeChat: 3-tuple heuristic matching (counterparty, amount, +/-10 min time window).
- Neutral propagation: If the original order was cancelled ('交易关闭' / neutral), the refund
  is also set to neutral to prevent negative expense distortion.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime
from typing import List, Optional, Sequence

from billapp.core.models import TransactionCreate


def extract_alipay_parent_order_id(refund_order_id: str) -> str:
    """Extract original purchase order ID from an Alipay refund order ID."""
    clean = refund_order_id.strip()
    return re.split(r"[*_]", clean)[0]


def _parse_time(time_str: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(time_str)
    except Exception:
        return None


def link_in_memory_refunds(transactions: Sequence[TransactionCreate]) -> int:
    """Link refunds to original transactions within an in-memory batch.

    Updates refund_of (set to index or temporary ref), refunded_cents, and applies
    neutral propagation.
    """
    linked_count = 0

    # Build lookup indices
    alipay_orders: dict[str, TransactionCreate] = {}
    wechat_expenses: list[TransactionCreate] = []

    for txn in transactions:
        if txn.source == "alipay" and txn.order_id:
            clean_id = txn.order_id.strip()
            # If not a refund order ID, index it
            if "*F-" not in clean_id and not re.search(r"_\d{10,}$", clean_id):
                alipay_orders[clean_id] = txn
        elif txn.source == "wechat" and txn.direction in ("expense", "neutral"):
            wechat_expenses.append(txn)

    # Process Alipay refunds
    for txn in transactions:
        if txn.source == "alipay" and txn.direction == "refund" and txn.order_id:
            parent_id = extract_alipay_parent_order_id(txn.order_id)
            orig = alipay_orders.get(parent_id)
            if orig:
                # Neutral propagation rule
                if orig.direction == "neutral" or orig.status == "交易关闭":
                    orig.direction = "neutral"
                    txn.direction = "neutral"
                else:
                    orig.refunded_cents = min(orig.amount_cents, orig.refunded_cents + txn.amount_cents)
                linked_count += 1

    # Process WeChat refunds
    for txn in transactions:
        if txn.source == "wechat" and txn.direction == "refund":
            refund_dt = _parse_time(txn.txn_time)
            best_match: Optional[TransactionCreate] = None
            min_delta_sec = 601.0  # 10 minutes limit

            for cand in wechat_expenses:
                if cand.refunded_cents >= cand.amount_cents:
                    continue
                if cand.counterparty == txn.counterparty and cand.amount_cents == txn.amount_cents:
                    cand_dt = _parse_time(cand.txn_time)
                    if cand_dt and refund_dt:
                        delta = abs((refund_dt - cand_dt).total_seconds())
                        if delta <= 600 and delta < min_delta_sec:
                            min_delta_sec = delta
                            best_match = cand

            if best_match:
                if best_match.status in ("已全额退款", "已退款") or best_match.direction == "neutral":
                    best_match.direction = "neutral"
                    txn.direction = "neutral"
                else:
                    best_match.refunded_cents = min(best_match.amount_cents, best_match.refunded_cents + txn.amount_cents)
                if best_match.refunded_cents >= best_match.amount_cents or best_match.direction == "neutral":
                    wechat_expenses.remove(best_match)
                linked_count += 1

    return linked_count


def link_in_source_refunds(conn: sqlite3.Connection, batch_id: Optional[int] = None) -> int:
    """Link refunds in SQLite database, updating refund_of, refunded_cents, and directions."""
    linked_count = 0
    consumed_ids: set[int] = set()

    # 1. Alipay refunds
    query = """
        SELECT id, order_id, amount_cents, status
        FROM transactions
        WHERE source = 'alipay' AND direction = 'refund' AND refund_of IS NULL
    """
    params = []
    if batch_id is not None:
        query += " AND batch_id = ?"
        params.append(batch_id)

    refunds = conn.execute(query, params).fetchall()
    for ref in refunds:
        ref_id = ref["id"]
        ref_order = ref["order_id"]
        ref_cents = ref["amount_cents"]
        if not ref_order:
            continue
        parent_order = extract_alipay_parent_order_id(ref_order)

        orig = conn.execute(
            """
            SELECT id, direction, status, amount_cents, refunded_cents
            FROM transactions
            WHERE source = 'alipay' AND order_id = ?
            LIMIT 1
            """,
            (parent_order,),
        ).fetchone()

        if orig:
            orig_id = orig["id"]
            if orig["direction"] == "neutral" or orig["status"] == "交易关闭":
                # Propagate neutral
                conn.execute(
                    "UPDATE transactions SET refund_of = ?, direction = 'neutral' WHERE id = ?",
                    (orig_id, ref_id),
                )
                conn.execute(
                    "UPDATE transactions SET direction = 'neutral' WHERE id = ?",
                    (orig_id,),
                )
            else:
                new_refunded = min(orig["amount_cents"], orig["refunded_cents"] + ref_cents)
                conn.execute(
                    "UPDATE transactions SET refund_of = ? WHERE id = ?",
                    (orig_id, ref_id),
                )
                conn.execute(
                    "UPDATE transactions SET refunded_cents = ? WHERE id = ?",
                    (new_refunded, orig_id),
                )
            linked_count += 1

    # 2. WeChat refunds
    wechat_query = """
        SELECT id, counterparty, amount_cents, txn_time, status
        FROM transactions
        WHERE source = 'wechat' AND direction = 'refund' AND refund_of IS NULL
    """
    w_params = []
    if batch_id is not None:
        wechat_query += " AND batch_id = ?"
        w_params.append(batch_id)

    w_refunds = conn.execute(wechat_query, w_params).fetchall()

    for ref in w_refunds:
        ref_id = ref["id"]
        cparty = ref["counterparty"]
        ref_cents = ref["amount_cents"]
        ref_time = ref["txn_time"]

        orig_candidates = conn.execute(
            """
            SELECT id, direction, status, amount_cents, refunded_cents, txn_time,
                   abs(strftime('%s', txn_time) - strftime('%s', ?)) AS delta_sec
            FROM transactions
            WHERE source = 'wechat'
              AND counterparty = ?
              AND amount_cents = ?
              AND direction IN ('expense', 'neutral')
              AND refunded_cents < amount_cents
              AND abs(strftime('%s', txn_time) - strftime('%s', ?)) <= 600
            ORDER BY delta_sec ASC
            """,
            (ref_time, cparty, ref_cents, ref_time),
        ).fetchall()

        orig = None
        for cand in orig_candidates:
            if cand["id"] not in consumed_ids:
                orig = cand
                break

        if orig:
            orig_id = orig["id"]
            if orig["status"] in ("已全额退款", "已退款") or orig["direction"] == "neutral":
                conn.execute(
                    "UPDATE transactions SET refund_of = ?, direction = 'neutral' WHERE id = ?",
                    (orig_id, ref_id),
                )
                conn.execute(
                    "UPDATE transactions SET direction = 'neutral' WHERE id = ?",
                    (orig_id,),
                )
                consumed_ids.add(orig_id)
            else:
                new_refunded = min(orig["amount_cents"], orig["refunded_cents"] + ref_cents)
                conn.execute(
                    "UPDATE transactions SET refund_of = ? WHERE id = ?",
                    (orig_id, ref_id),
                )
                conn.execute(
                    "UPDATE transactions SET refunded_cents = ? WHERE id = ?",
                    (new_refunded, orig_id),
                )
                if new_refunded >= orig["amount_cents"]:
                    consumed_ids.add(orig_id)
            linked_count += 1

    # 3. Bank in-source refunds (BOC, ICBC, and generic profile banks)
    bank_query = """
        SELECT id, account_id, source, counterparty, merchant_norm, amount_cents, txn_time, status
        FROM transactions
        WHERE source NOT IN ('alipay', 'wechat')
          AND direction = 'refund'
          AND refund_of IS NULL
    """
    b_params = []
    if batch_id is not None:
        bank_query += " AND batch_id = ?"
        b_params.append(batch_id)

    b_refunds = conn.execute(bank_query, b_params).fetchall()

    for ref in b_refunds:
        ref_id = ref["id"]
        ref_acc = ref["account_id"]
        ref_src = ref["source"]
        ref_cents = ref["amount_cents"]
        ref_time = ref["txn_time"]
        ref_cp = ref["counterparty"] or ""
        ref_norm = ref["merchant_norm"] or ""

        # Query expense candidates on the same account/source within 60 days before the refund
        if ref_acc is not None:
            acc_clause = "account_id = ?"
            acc_val = ref_acc
        else:
            acc_clause = "source = ?"
            acc_val = ref_src

        cands = conn.execute(
            f"""
            SELECT id, direction, status, amount_cents, refunded_cents, txn_time,
                   counterparty, merchant_norm,
                   abs(strftime('%s', txn_time) - strftime('%s', ?)) AS delta_sec
            FROM transactions
            WHERE {acc_clause}
              AND id != ?
              AND direction IN ('expense', 'neutral')
              AND refunded_cents < amount_cents
              AND (strftime('%s', ?) - strftime('%s', txn_time)) BETWEEN -60 AND (60 * 86400)
            ORDER BY delta_sec ASC
            """,
            (ref_time, acc_val, ref_id, ref_time),
        ).fetchall()

        orig = None
        for cand in cands:
            if cand["id"] in consumed_ids:
                continue
            cand_cents = cand["amount_cents"] - cand["refunded_cents"]
            if cand_cents < ref_cents:
                continue

            cand_cp = cand["counterparty"] or ""
            cand_norm = cand["merchant_norm"] or ""

            # Check merchant similarity
            matched_merchant = False
            if cand_cp == ref_cp or (cand_norm and ref_norm and cand_norm == ref_norm):
                matched_merchant = True
            elif cand_norm and ref_norm and (cand_norm in ref_norm or ref_norm in cand_norm):
                matched_merchant = True
            elif cand_cp and ref_cp and (cand_cp in ref_cp or ref_cp in cand_cp):
                matched_merchant = True
            else:
                from billapp.dedup.cross_source import compute_name_sim
                if (
                    compute_name_sim(ref_norm or ref_cp, cand_cp, cand_norm) >= 0.5
                    or compute_name_sim(cand_norm or cand_cp, ref_cp, ref_norm) >= 0.5
                ):
                    matched_merchant = True

            if matched_merchant:
                orig = cand
                break

        if orig:
            orig_id = orig["id"]
            if orig["status"] in ("已全额退款", "已退款", "交易关闭") or orig["direction"] == "neutral":
                conn.execute(
                    "UPDATE transactions SET refund_of = ?, direction = 'neutral' WHERE id = ?",
                    (orig_id, ref_id),
                )
                conn.execute(
                    "UPDATE transactions SET direction = 'neutral' WHERE id = ?",
                    (orig_id,),
                )
                consumed_ids.add(orig_id)
            else:
                new_refunded = min(orig["amount_cents"], orig["refunded_cents"] + ref_cents)
                conn.execute(
                    "UPDATE transactions SET refund_of = ? WHERE id = ?",
                    (orig_id, ref_id),
                )
                conn.execute(
                    "UPDATE transactions SET refunded_cents = ? WHERE id = ?",
                    (new_refunded, orig_id),
                )
                if new_refunded >= orig["amount_cents"]:
                    consumed_ids.add(orig_id)
            linked_count += 1

    return linked_count
