"""Transactions listing, filtering, pagination, inline modification, and bulk operations (/api/transactions/*)."""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from billapp.app import NotFoundError, ValidationError, get_db_conn
from billapp.classify.categories import CategoryService
from billapp.classify.rules import learn_rule_from_edit

router = APIRouter(prefix="/api/transactions", tags=["transactions"])


# =========================================================================
# Transaction DTO Schemas
# =========================================================================

class MergedSourceItem(BaseModel):
    id: int
    source: str
    account_id: int
    account_bank: Optional[str] = None
    account_tail: Optional[str] = None
    account_alias: Optional[str] = None
    txn_time: str
    amount_cents: int
    currency: str = "CNY"
    direction: str
    counterparty: Optional[str] = None
    description: Optional[str] = None
    pay_method: Optional[str] = None
    channel: Optional[str] = None


class TransactionItem(BaseModel):
    id: int
    batch_id: int
    source: str
    account_id: int
    fund_account_id: Optional[int] = None
    fund_account_bank: Optional[str] = None
    fund_account_tail: Optional[str] = None
    txn_time: str
    time_precision: str
    post_date: Optional[str] = None
    amount_cents: int
    amount: int  # frontend compatibility
    amount_display: str
    net_cents: int
    currency: str
    direction: str
    counterparty: Optional[str] = None
    description: Optional[str] = None
    pay_method: Optional[str] = None
    channel: Optional[str] = None
    merchant_norm: Optional[str] = None
    order_id: Optional[str] = None
    status: Optional[str] = None
    platform_category: Optional[str] = None
    category_id: Optional[int] = None
    category_key: Optional[str] = None
    category_name: Optional[str] = None
    category_path: Optional[List[str]] = None
    category_source: Optional[str] = None
    category_confidence: Optional[float] = None
    refund_of: Optional[int] = None
    refunded_cents: int = 0
    duplicate_of: Optional[int] = None
    is_duplicate: bool = False
    merged_count: int = 0
    merged_duplicates: List[MergedSourceItem] = Field(default_factory=list)
    primary_source_txn: Optional[MergedSourceItem] = None
    transfer_peer: Optional[int] = None
    note: Optional[str] = None
    excluded: int = 0
    account_bank: Optional[str] = None
    account_tail: Optional[str] = None
    account_alias: Optional[str] = None
    # FX fields (Milestone 6)
    amount_cny: Optional[int] = None
    fx_rate: Optional[float] = None
    fx_rate_at: Optional[str] = None
    fx_rate_source: Optional[str] = None


class TransactionListResponse(BaseModel):
    items: List[TransactionItem]
    total: int
    page: int
    page_size: int


class TransactionPatchRequest(BaseModel):
    category_id: Optional[int] = None
    note: Optional[str] = None
    excluded: Optional[Literal[0, 1]] = None


class BulkPatchRequest(BaseModel):
    ids: List[int]
    category_id: Optional[int] = None
    excluded: Optional[Literal[0, 1]] = None
    note: Optional[str] = None


# =========================================================================
# Endpoints
# =========================================================================

@router.get("", response_model=TransactionListResponse)
def list_transactions(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=500),
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    account_id: Optional[int] = None,
    fund_account_id: Optional[int] = None,
    source: Optional[str] = None,
    category_id: Optional[int] = None,
    direction: Optional[str] = None,
    min_cents: Optional[int] = None,
    max_cents: Optional[int] = None,
    is_duplicate: Optional[bool] = None,
    status: Optional[str] = None,
    search: Optional[str] = Query(None, alias="q"),
    excluded: Optional[int] = None,
    unclassified: Optional[bool] = None,
    currency: Optional[str] = None,
    fx: Optional[str] = None,
    order_by: str = Query("txn_time", pattern="^(txn_time|amount_cents|amount_cny|id)$"),
    order_dir: str = Query("desc", pattern="^(asc|desc)$"),
    db: sqlite3.Connection = Depends(get_db_conn),
):
    """Retrieve paginated and filtered transactions list."""
    where_clauses = ["1=1"]
    params: List[Any] = []

    # Dates
    start = start_date or from_
    if start:
        where_clauses.append("t.txn_time >= ?")
        params.append(start if len(start) > 10 else f"{start} 00:00:00")
    end = end_date or to
    if end:
        where_clauses.append("t.txn_time <= ?")
        params.append(end if len(end) > 10 else f"{end} 23:59:59")

    if account_id is not None:
        if is_duplicate is False:
            where_clauses.append(
                "(t.account_id = ? OR t.fund_account_id = ? OR EXISTS ("
                "SELECT 1 FROM transactions d WHERE d.duplicate_of = t.id AND d.account_id = ?"
                "))"
            )
            params.extend([account_id, account_id, account_id])
        else:
            where_clauses.append("t.account_id = ?")
            params.append(account_id)
    if fund_account_id is not None:
        where_clauses.append("t.fund_account_id = ?")
        params.append(fund_account_id)
    if source:
        where_clauses.append("t.source = ?")
        params.append(source)
    if category_id is not None:
        where_clauses.append("(t.category_id = ? OR t.category_id IN (SELECT id FROM categories WHERE parent_id = ?))")
        params.extend([category_id, category_id])
    if direction:
        where_clauses.append("t.direction = ?")
        params.append(direction)
    if min_cents is not None:
        where_clauses.append("t.amount_cents >= ?")
        params.append(min_cents)
    if max_cents is not None:
        where_clauses.append("t.amount_cents <= ?")
        params.append(max_cents)
    if is_duplicate is not None:
        if is_duplicate:
            where_clauses.append("t.duplicate_of IS NOT NULL")
        else:
            where_clauses.append("t.duplicate_of IS NULL")
    if status:
        status_clean = status.strip()
        status_lower = status_clean.lower()
        if status_lower == "duplicate":
            where_clauses.append("t.duplicate_of IS NOT NULL")
        elif status_lower == "refund":
            where_clauses.append("(t.direction = 'refund' OR t.refund_of IS NOT NULL OR t.refunded_cents > 0)")
        elif status_lower == "transfer":
            where_clauses.append("(t.direction = 'transfer' OR t.transfer_peer IS NOT NULL)")
        elif status_lower == "excluded":
            where_clauses.append("t.excluded = 1")
        elif status_lower == "normal":
            where_clauses.append("(t.duplicate_of IS NULL AND t.excluded = 0)")
        else:
            where_clauses.append("t.status = ?")
            params.append(status_clean)
    if excluded is not None:
        where_clauses.append("t.excluded = ?")
        params.append(excluded)
    if unclassified:
        where_clauses.append("t.category_id IS NULL")
    if currency:
        if currency.lower() == "foreign":
            where_clauses.append("t.currency != 'CNY' AND t.currency IS NOT NULL")
        else:
            where_clauses.append("t.currency = ?")
            params.append(currency.upper())
    if fx == "missing":
        where_clauses.append("(t.currency != 'CNY' AND t.currency IS NOT NULL AND (t.amount_cny IS NULL OR t.fx_rate_source = 'none'))")
    if search:
        s_pat = f"%{search}%"
        if is_duplicate is False:
            where_clauses.append(
                "(t.counterparty LIKE ? OR t.description LIKE ? OR t.merchant_norm LIKE ? OR t.order_id LIKE ? OR t.note LIKE ? "
                "OR EXISTS (SELECT 1 FROM transactions d WHERE d.duplicate_of = t.id "
                "AND (d.counterparty LIKE ? OR d.description LIKE ? OR d.merchant_norm LIKE ? OR d.order_id LIKE ?)))"
            )
            params.extend([s_pat] * 9)
        else:
            where_clauses.append(
                "(t.counterparty LIKE ? OR t.description LIKE ? OR t.merchant_norm LIKE ? OR t.order_id LIKE ? OR t.note LIKE ?)"
            )
            params.extend([s_pat, s_pat, s_pat, s_pat, s_pat])

    where_sql = " AND ".join(where_clauses)

    # Count Total
    count_sql = f"SELECT COUNT(*) AS total FROM transactions t WHERE {where_sql}"
    total = db.execute(count_sql, params).fetchone()["total"]

    # Order expression: order by effective CNY amount when ordering by amount
    if order_by in ("amount_cents", "amount_cny"):
        sort_expr = "COALESCE(t.amount_cny, t.amount_cents)"
    else:
        sort_expr = f"t.{order_by}"

    # Page Data
    offset = (page - 1) * page_size
    query_sql = f"""
        SELECT t.*,
               c.name AS category_name, c.key AS category_key,
               a.bank AS account_bank, a.tail AS account_tail, a.alias AS account_alias,
               fa.bank AS fund_account_bank, fa.tail AS fund_account_tail
        FROM transactions t
        LEFT JOIN categories c ON t.category_id = c.id
        LEFT JOIN accounts a ON t.account_id = a.id
        LEFT JOIN accounts fa ON t.fund_account_id = fa.id
        WHERE {where_sql}
        ORDER BY {sort_expr} {order_dir.upper()}
        LIMIT ? OFFSET ?
    """
    rows = db.execute(query_sql, params + [page_size, offset]).fetchall()

    # Batch-fetch merged child duplicates and optional parent source rows for displayed page
    merged_map: Dict[int, List[MergedSourceItem]] = {}
    parent_map: Dict[int, MergedSourceItem] = {}
    if rows:
        row_ids = [r["id"] for r in rows]
        id_ph = ",".join("?" for _ in row_ids)
        child_rows = db.execute(
            f"""
            SELECT t.id, t.duplicate_of, t.source, t.account_id, t.txn_time, t.amount_cents,
                   t.currency, t.direction, t.counterparty, t.description, t.pay_method, t.channel,
                   a.bank AS account_bank, a.tail AS account_tail, a.alias AS account_alias
            FROM transactions t
            LEFT JOIN accounts a ON t.account_id = a.id
            WHERE t.duplicate_of IN ({id_ph})
            ORDER BY t.id ASC
            """,
            row_ids,
        ).fetchall()
        for cr in child_rows:
            parent_id = cr["duplicate_of"]
            item_data = {k: cr[k] for k in cr.keys() if k != "duplicate_of"}
            merged_map.setdefault(parent_id, []).append(MergedSourceItem(**item_data))

        parent_ids = list({r["duplicate_of"] for r in rows if r["duplicate_of"]})
        if parent_ids:
            p_ph = ",".join("?" for _ in parent_ids)
            p_rows = db.execute(
                f"""
                SELECT t.id, t.source, t.account_id, t.txn_time, t.amount_cents,
                       t.currency, t.direction, t.counterparty, t.description, t.pay_method, t.channel,
                       a.bank AS account_bank, a.tail AS account_tail, a.alias AS account_alias
                FROM transactions t
                LEFT JOIN accounts a ON t.account_id = a.id
                WHERE t.id IN ({p_ph})
                """,
                parent_ids,
            ).fetchall()
            for pr in p_rows:
                parent_map[pr["id"]] = MergedSourceItem(**dict(pr))

    items: List[TransactionItem] = []
    for r in rows:
        d = dict(r)
        d["amount"] = d["amount_cents"]
        d["amount_display"] = f"{d['amount_cents'] / 100:.2f}"
        d["net_cents"] = max(0, d["amount_cents"] - d["refunded_cents"]) if d["direction"] == "expense" else d["amount_cents"]
        d["is_duplicate"] = bool(d["duplicate_of"])
        children = merged_map.get(d["id"], [])
        d["merged_duplicates"] = children
        d["merged_count"] = len(children)
        if d["duplicate_of"] and d["duplicate_of"] in parent_map:
            d["primary_source_txn"] = parent_map[d["duplicate_of"]]
        if d.get("amount_cny") is None and (d.get("currency") == "CNY" or not d.get("currency")):
            d["amount_cny"] = d["amount_cents"]
        if d.get("category_id"):
            _, p_names = CategoryService.get_path(db, d["category_id"])
            d["category_path"] = p_names
        items.append(TransactionItem(**d))

    return TransactionListResponse(items=items, total=total, page=page, page_size=page_size)


@router.get("/{txn_id}")
def get_transaction_detail(
    txn_id: int,
    db: sqlite3.Connection = Depends(get_db_conn),
):
    """Retrieve single transaction detail with associated relations."""
    sql = """
        SELECT t.*,
               c.name AS category_name, c.key AS category_key,
               a.bank AS account_bank, a.tail AS account_tail, a.alias AS account_alias,
               fa.bank AS fund_account_bank, fa.tail AS fund_account_tail
        FROM transactions t
        LEFT JOIN categories c ON t.category_id = c.id
        LEFT JOIN accounts a ON t.account_id = a.id
        LEFT JOIN accounts fa ON t.fund_account_id = fa.id
        WHERE t.id = ?
    """
    row = db.execute(sql, (txn_id,)).fetchone()
    if not row:
        raise NotFoundError(f"交易记录 #{txn_id} 不存在")

    d = dict(row)
    d["amount"] = d["amount_cents"]
    d["amount_display"] = f"{d['amount_cents'] / 100:.2f}"
    d["net_cents"] = max(0, d["amount_cents"] - d["refunded_cents"]) if d["direction"] == "expense" else d["amount_cents"]
    d["is_duplicate"] = bool(d["duplicate_of"])

    # Related items (refunds, duplicate primary & children, transfer peer)
    related = []
    if d["refund_of"]:
        p_row = db.execute("SELECT id, txn_time, amount_cents, direction, counterparty FROM transactions WHERE id = ?", (d["refund_of"],)).fetchone()
        if p_row:
            related.append({"relation": "refund_parent", "txn": dict(p_row)})
    refund_children = db.execute("SELECT id, txn_time, amount_cents, direction, counterparty FROM transactions WHERE refund_of = ?", (txn_id,)).fetchall()
    for rc in refund_children:
        related.append({"relation": "refund_child", "txn": dict(rc)})

    if d["duplicate_of"]:
        dup_parent = db.execute(
            """
            SELECT t.id, t.txn_time, t.amount_cents, t.currency, t.direction,
                   t.counterparty, t.description, t.source, t.pay_method, t.channel,
                   t.account_id, a.bank AS account_bank, a.tail AS account_tail, a.alias AS account_alias
            FROM transactions t
            LEFT JOIN accounts a ON t.account_id = a.id
            WHERE t.id = ?
            """,
            (d["duplicate_of"],),
        ).fetchone()
        if dup_parent:
            p_dict = dict(dup_parent)
            d["primary_source_txn"] = p_dict
            related.append({"relation": "duplicate_primary", "txn": p_dict})

    dup_children = db.execute(
        """
        SELECT t.id, t.txn_time, t.amount_cents, t.currency, t.direction,
               t.counterparty, t.description, t.source, t.pay_method, t.channel,
               t.account_id, a.bank AS account_bank, a.tail AS account_tail, a.alias AS account_alias
        FROM transactions t
        LEFT JOIN accounts a ON t.account_id = a.id
        WHERE t.duplicate_of = ?
        ORDER BY t.id ASC
        """,
        (txn_id,),
    ).fetchall()
    dup_child_list = [dict(dc) for dc in dup_children]
    d["merged_duplicates"] = dup_child_list
    d["merged_count"] = len(dup_child_list)
    for dc in dup_child_list:
        related.append({"relation": "duplicate_child", "txn": dc})

    if d["transfer_peer"]:
        peer_row = db.execute("SELECT id, txn_time, amount_cents, direction, counterparty, source FROM transactions WHERE id = ?", (d["transfer_peer"],)).fetchone()
        if peer_row:
            related.append({"relation": "transfer_peer", "txn": dict(peer_row)})

    return {
        "transaction": d,
        "related": related,
    }


@router.patch("/bulk")
def bulk_update_transactions(
    payload: BulkPatchRequest,
    db: sqlite3.Connection = Depends(get_db_conn),
):
    """Bulk update transactions category, note, or exclusion."""
    if not payload.ids or len(payload.ids) > 500:
        raise ValidationError("批量操作单次支持 1 至 500 条记录")

    placeholders = ",".join("?" for _ in payload.ids)

    if payload.category_id is not None:
        cat = CategoryService.get_by_id(db, payload.category_id)
        if not cat:
            raise NotFoundError(f"分类 #{payload.category_id} 不存在")
        db.execute(
            f"""
            UPDATE transactions
            SET category_id = ?, category_source = 'manual', category_confidence = 1.0
            WHERE id IN ({placeholders})
            """,
            [payload.category_id] + payload.ids,
        )

    if payload.excluded is not None:
        db.execute(
            f"UPDATE transactions SET excluded = ? WHERE id IN ({placeholders})",
            [payload.excluded] + payload.ids,
        )

    if payload.note is not None:
        db.execute(
            f"UPDATE transactions SET note = ? WHERE id IN ({placeholders})",
            [payload.note] + payload.ids,
        )

    return {"updated_count": len(payload.ids)}


@router.patch("/{txn_id}")
def update_transaction(
    txn_id: int,
    payload: TransactionPatchRequest,
    db: sqlite3.Connection = Depends(get_db_conn),
):
    """Update single transaction category, note, or excluded state.
    
    If category is updated, automatically triggers rule learning proposal.
    """
    txn = db.execute("SELECT * FROM transactions WHERE id = ?", (txn_id,)).fetchone()
    if not txn:
        raise NotFoundError(f"交易记录 #{txn_id} 不存在")

    rule_proposal = None

    if payload.category_id is not None:
        cat = CategoryService.get_by_id(db, payload.category_id)
        if not cat:
            raise NotFoundError(f"分类 #{payload.category_id} 不存在")

        db.execute(
            """
            UPDATE transactions
            SET category_id = ?, category_source = 'manual', category_confidence = 1.0
            WHERE id = ?
            """,
            (payload.category_id, txn_id),
        )

        # Respect general_settings.learning_suggestions_enabled (B-03)
        learning_enabled = True
        try:
            gen_row = db.execute("SELECT value FROM settings WHERE key = 'general_settings'").fetchone()
            if gen_row and gen_row["value"]:
                gen_cfg = json.loads(gen_row["value"])
                if isinstance(gen_cfg, dict) and "learning_suggestions_enabled" in gen_cfg:
                    learning_enabled = bool(gen_cfg["learning_suggestions_enabled"])
        except Exception:
            learning_enabled = True

        if learning_enabled:
            cand = learn_rule_from_edit(db, txn_id, payload.category_id)
            if cand and cand.applicable_count > 0:
                rule_proposal = {
                    "candidate_rule": {
                        "name": cand.name,
                        "category_id": cand.category_id,
                        "field": cand.field,
                        "pattern_type": cand.pattern_type,
                        "pattern_value": cand.pattern_value,
                        "priority": cand.priority,
                    },
                    "matched_total": cand.matched_total,
                    "applicable_count": cand.applicable_count,
                    "sample_applicable_txn_ids": cand.sample_applicable_txn_ids,
                    "prompt_message": f"已将该交易分类为「{cat.name}」。检测到 {cand.applicable_count} 条包含「{cand.pattern_value}」的历史交易，是否保存为自动分类规则并批量应用？",
                }

    if payload.note is not None:
        db.execute("UPDATE transactions SET note = ? WHERE id = ?", (payload.note, txn_id))
    if payload.excluded is not None:
        db.execute("UPDATE transactions SET excluded = ? WHERE id = ?", (payload.excluded, txn_id))

    updated_txn = db.execute(
        """
        SELECT t.*, c.name AS category_name, c.key AS category_key
        FROM transactions t
        LEFT JOIN categories c ON t.category_id = c.id
        WHERE t.id = ?
        """,
        (txn_id,),
    ).fetchone()

    return {
        "transaction": dict(updated_txn),
        "rule_proposal": rule_proposal,
    }


@router.delete("/{txn_id}")
def delete_transaction(
    txn_id: int,
    db: sqlite3.Connection = Depends(get_db_conn),
):
    """Delete a single transaction atomically, reverting refund offsets, transfer peers, duplicate groups, and batch counts (B-02)."""
    target = db.execute(
        """
        SELECT id, batch_id, amount_cents, direction, refund_of, transfer_peer, duplicate_of
        FROM transactions
        WHERE id = ?
        """,
        (txn_id,),
    ).fetchone()
    if not target:
        raise NotFoundError(f"交易记录 #{txn_id} 不存在")

    with db:
        # 1. If deleting a linked refund, restore refunded_cents on original expense
        if target["refund_of"] is not None:
            db.execute(
                """
                UPDATE transactions
                SET refunded_cents = MAX(0, refunded_cents - ?)
                WHERE id = ?
                """,
                (target["amount_cents"], target["refund_of"]),
            )

        # 2. Clear refund_of references on any refunds pointing to this transaction
        db.execute("UPDATE transactions SET refund_of = NULL WHERE refund_of = ?", (txn_id,))

        # 3. Revert transfer peer direction and transfer_peer link using match_links metadata
        transfer_links = db.execute(
            """
            SELECT id, primary_id, secondary_id, features_json
            FROM match_links
            WHERE kind = 'transfer' AND (primary_id = ? OR secondary_id = ?)
            """,
            (txn_id, txn_id),
        ).fetchall()
        for link in transfer_links:
            feat = {}
            if link["features_json"]:
                try:
                    feat = json.loads(link["features_json"])
                except Exception:
                    feat = {}
            if link["primary_id"] == txn_id:
                peer_id = link["secondary_id"]
                orig_dir = feat.get("orig_dir_in")
            else:
                peer_id = link["primary_id"]
                orig_dir = feat.get("orig_dir_out")
            if orig_dir:
                db.execute(
                    "UPDATE transactions SET transfer_peer = NULL, direction = ? WHERE id = ?",
                    (orig_dir, peer_id),
                )
            else:
                db.execute(
                    "UPDATE transactions SET transfer_peer = NULL WHERE id = ?",
                    (peer_id,),
                )
        db.execute("UPDATE transactions SET transfer_peer = NULL WHERE transfer_peer = ?", (txn_id,))

        # 4. Prevent hidden duplicates from resurrecting into active stats when deleting their primary record
        db.execute(
            "UPDATE transactions SET duplicate_of = NULL, excluded = 1 WHERE duplicate_of = ?",
            (txn_id,),
        )

        # 5. Clean up associated match_links & match_blocklist rows
        db.execute(
            "DELETE FROM match_links WHERE primary_id = ? OR secondary_id = ?",
            (txn_id, txn_id),
        )
        db.execute(
            "DELETE FROM match_blocklist WHERE a_id = ? OR b_id = ?",
            (txn_id, txn_id),
        )

        # 6. Delete the transaction row itself
        db.execute("DELETE FROM transactions WHERE id = ?", (txn_id,))

        # 7. Sync import_batches.row_count
        if target["batch_id"] is not None:
            db.execute(
                "UPDATE import_batches SET row_count = MAX(0, row_count - 1) WHERE id = ?",
                (target["batch_id"],),
            )

    return {"ok": True, "deleted_id": txn_id}


@router.post("/{txn_id}/fx", response_model=TransactionItem)
def retry_single_fx(
    txn_id: int,
    db: sqlite3.Connection = Depends(get_db_conn),
):
    """Retry obtaining exchange rate for a single foreign currency transaction."""
    from billapp.core.fx import retry_transaction_fx
    res = retry_transaction_fx(db, txn_id)
    if not res:
        raise NotFoundError(f"交易记录 #{txn_id} 不存在")

    d = dict(res)
    d["amount"] = d["amount_cents"]
    d["amount_display"] = f"{d['amount_cents'] / 100:.2f}"
    d["net_cents"] = max(0, d["amount_cents"] - d["refunded_cents"]) if d["direction"] == "expense" else d["amount_cents"]
    d["is_duplicate"] = bool(d["duplicate_of"])
    if d.get("amount_cny") is None and (d.get("currency") == "CNY" or not d.get("currency")):
        d["amount_cny"] = d["amount_cents"]
    if d.get("category_id"):
        _, p_names = CategoryService.get_path(db, d["category_id"])
        d["category_path"] = p_names
    return TransactionItem(**d)
