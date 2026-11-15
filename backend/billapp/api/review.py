"""Duplicate & Transfer Candidate Review API endpoints (/api/review/*)."""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field

from billapp.app import NotFoundError, ValidationError, get_db_conn
from billapp.dedup.engine import confirm_match_link, reject_match_link

router = APIRouter(prefix="/api/review", tags=["review"])


# =========================================================================
# Review DTO Schemas
# =========================================================================

class BreakdownScore(BaseModel):
    amount: float = 0.0
    tail: float = 0.0
    time: float = 0.0
    channel: float = 0.0
    name: float = 0.0
    penalty: float = 0.0


class ReviewTxnSide(BaseModel):
    id: int
    source: str
    source_label: str
    txn_time: str
    amount_cents: int
    amount_display: str
    counterparty: Optional[str] = None
    description: Optional[str] = None
    channel: Optional[str] = None
    account_name: Optional[str] = None


class ReviewItemOut(BaseModel):
    id: int
    kind: str
    score: float
    status: str
    breakdown: BreakdownScore
    left: ReviewTxnSide
    right: ReviewTxnSide
    llm: Optional[Dict[str, Any]] = None
    created_at: str


class BulkConfirmRequest(BaseModel):
    link_ids: List[int]


# =========================================================================
# Endpoints
# =========================================================================

@router.get("", response_model=List[ReviewItemOut])
def list_pending_reviews(
    db: sqlite3.Connection = Depends(get_db_conn),
):
    """List pending duplicate and internal transfer review candidates with breakdown scoring."""
    query = """
        SELECT ml.id, ml.kind, ml.score, ml.features_json, ml.llm_verdict, ml.llm_reason, ml.status, ml.created_at,
               t1.id AS l_id, t1.source AS l_source, t1.txn_time AS l_time, t1.amount_cents AS l_amt,
               t1.counterparty AS l_party, t1.description AS l_desc, t1.channel AS l_chan,
               a1.bank AS l_bank, a1.tail AS l_tail, a1.alias AS l_alias,
               t2.id AS r_id, t2.source AS r_source, t2.txn_time AS r_time, t2.amount_cents AS r_amt,
               t2.counterparty AS r_party, t2.description AS r_desc, t2.channel AS r_chan,
               a2.bank AS r_bank, a2.tail AS r_tail, a2.alias AS r_alias
        FROM match_links ml
        JOIN transactions t1 ON ml.primary_id = t1.id
        JOIN transactions t2 ON ml.secondary_id = t2.id
        LEFT JOIN accounts a1 ON t1.account_id = a1.id
        LEFT JOIN accounts a2 ON t2.account_id = a2.id
        WHERE ml.status = 'pending'
        ORDER BY ml.score DESC, ml.id ASC
    """
    rows = db.execute(query).fetchall()
    results: List[ReviewItemOut] = []

    for r in rows:
        features = {}
        if r["features_json"]:
            try:
                features = json.loads(r["features_json"])
            except Exception:
                features = {}

        bd = features.get("breakdown", {
            "amount": 0.3, "tail": 0.25, "time": 0.3, "channel": 0.0, "name": 0.0, "penalty": 0.0
        })

        llm_info = None
        if r["llm_verdict"]:
            llm_info = {
                "verdict": r["llm_verdict"],
                "reason": r["llm_reason"],
                "confidence": r["score"],
            }

        l_name = r["l_alias"] or (f"{r['l_bank']} ({r['l_tail']})" if r["l_tail"] else (r["l_bank"] or ""))
        r_name = r["r_alias"] or (f"{r['r_bank']} ({r['r_tail']})" if r["r_tail"] else (r["r_bank"] or ""))

        results.append(
            ReviewItemOut(
                id=r["id"],
                kind=r["kind"],
                score=r["score"],
                status=r["status"],
                breakdown=BreakdownScore(**bd),
                left=ReviewTxnSide(
                    id=r["l_id"],
                    source=r["l_source"],
                    source_label="平台账单" if r["l_source"] in ("alipay", "wechat") else "主账单",
                    txn_time=r["l_time"],
                    amount_cents=r["l_amt"],
                    amount_display=f"{r['l_amt'] / 100:.2f}",
                    counterparty=r["l_party"],
                    description=r["l_desc"],
                    channel=r["l_chan"],
                    account_name=l_name.strip() or "默认账户",
                ),
                right=ReviewTxnSide(
                    id=r["r_id"],
                    source=r["r_source"],
                    source_label="银行流水" if "bank" in r["r_source"] or "debit" in r["r_source"] or "credit" in r["r_source"] else "次账单",
                    txn_time=r["r_time"],
                    amount_cents=r["r_amt"],
                    amount_display=f"{r['r_amt'] / 100:.2f}",
                    counterparty=r["r_party"],
                    description=r["r_desc"],
                    channel=r["r_chan"],
                    account_name=r_name.strip() or "默认账户",
                ),
                llm=llm_info,
                created_at=r["created_at"],
            )
        )

    return results


@router.post("/{link_id}/confirm")
def confirm_review(
    link_id: int,
    db: sqlite3.Connection = Depends(get_db_conn),
):
    """Confirm a duplicate or internal transfer match link."""
    success = confirm_match_link(db, link_id)
    if not success:
        raise NotFoundError(f"待确认配对 #{link_id} 不存在")
    return {"success": True, "link_id": link_id, "status": "confirmed"}


@router.post("/{link_id}/reject")
def reject_review(
    link_id: int,
    db: sqlite3.Connection = Depends(get_db_conn),
):
    """Reject a match link, permanently recording the pair in match_blocklist to prevent re-pairing."""
    success = reject_match_link(db, link_id)
    if not success:
        raise NotFoundError(f"待确认配对 #{link_id} 不存在")
    return {"success": True, "link_id": link_id, "status": "rejected"}


@router.post("/bulk-confirm")
def bulk_confirm_review(
    payload: BulkConfirmRequest,
    db: sqlite3.Connection = Depends(get_db_conn),
):
    """Bulk confirm multiple pending review links."""
    confirmed = 0
    for lid in payload.link_ids:
        if confirm_match_link(db, lid):
            confirmed += 1
    return {"success": True, "confirmed_count": confirmed}
