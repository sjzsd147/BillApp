"""Account entities, alias management, and balance aggregation API endpoints (/api/accounts/*)."""

from __future__ import annotations

import sqlite3
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from billapp.app import NotFoundError, ValidationError, get_db_conn

router = APIRouter(prefix="/api/accounts", tags=["accounts"])


# =========================================================================
# Account DTO Schemas
# =========================================================================

class AccountOut(BaseModel):
    id: int
    kind: str
    bank: str
    tail: Optional[str] = None
    alias: Optional[str] = None
    display_name: str
    holder: Optional[str] = None
    is_self: bool = False
    currency: str = "CNY"
    created_at: Optional[str] = None
    transaction_count: int = 0
    funded_count: int = 0
    computed_balance_cents: int = 0
    first_txn_time: Optional[str] = None
    last_txn_time: Optional[str] = None


class AccountUpdateRequest(BaseModel):
    alias: Optional[str] = Field(None, min_length=1, max_length=50)
    holder: Optional[str] = Field(None, min_length=1, max_length=50)


class AccountMergeRequest(BaseModel):
    source_id: int
    target_id: int


class AccountMergeResponse(BaseModel):
    ok: bool = True
    migrated_count: int
    merged_into_id: int


# =========================================================================
# Endpoints
# =========================================================================

@router.get("", response_model=List[AccountOut])
def list_accounts(
    db: sqlite3.Connection = Depends(get_db_conn),
):
    """List all accounts with aggregated transaction metrics and computed balance."""
    sql = """
        SELECT 
            a.id, a.kind, a.bank, a.tail, a.alias, a.holder, a.currency, a.created_at,
            (SELECT COUNT(*) FROM transactions WHERE account_id = a.id) AS txn_count,
            (SELECT COUNT(*) FROM transactions WHERE fund_account_id = a.id) AS funded_count,
            COALESCE((
                SELECT SUM(CASE 
                    WHEN direction = 'income' THEN COALESCE(amount_cny, amount_cents)
                    WHEN direction = 'expense' THEN CASE
                        WHEN fx_rate IS NOT NULL AND currency != 'CNY' THEN -ROUND((amount_cents - refunded_cents) * fx_rate)
                        ELSE -(COALESCE(amount_cny, amount_cents) - refunded_cents)
                    END
                    ELSE 0 
                END)
                FROM transactions
                WHERE account_id = a.id
                  AND duplicate_of IS NULL
                  AND excluded = 0
                  AND transfer_peer IS NULL
                  AND (currency = 'CNY' OR currency IS NULL OR amount_cny IS NOT NULL)
            ), 0) AS computed_balance_cents,
            (SELECT MIN(txn_time) FROM transactions WHERE account_id = a.id) AS first_txn_time,
            (SELECT MAX(txn_time) FROM transactions WHERE account_id = a.id) AS last_txn_time
        FROM accounts a
        ORDER BY a.kind, a.bank, a.tail
    """
    rows = db.execute(sql).fetchall()
    results: List[AccountOut] = []

    for r in rows:
        d_name = r["alias"] or (f"{r['bank']} ({r['tail']})" if r["tail"] else r["bank"])
        results.append(
            AccountOut(
                id=r["id"],
                kind=r["kind"],
                bank=r["bank"],
                tail=r["tail"],
                alias=r["alias"],
                display_name=d_name,
                holder=r["holder"],
                is_self=False,
                currency=r["currency"],
                created_at=r["created_at"],
                transaction_count=int(r["txn_count"]),
                funded_count=int(r["funded_count"]),
                computed_balance_cents=int(r["computed_balance_cents"]),
                first_txn_time=r["first_txn_time"],
                last_txn_time=r["last_txn_time"],
            )
        )

    return results


def _update_account_record(account_id: int, payload: AccountUpdateRequest, db: sqlite3.Connection) -> AccountOut:
    existing = db.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
    if not existing:
        raise NotFoundError(f"账户 #{account_id} 不存在")

    update_fields = []
    update_vals: List[Any] = []

    if payload.alias is not None:
        update_fields.append("alias = ?")
        update_vals.append(payload.alias)
    if payload.holder is not None:
        update_fields.append("holder = ?")
        update_vals.append(payload.holder)

    if update_fields:
        update_vals.append(account_id)
        db.execute(f"UPDATE accounts SET {', '.join(update_fields)} WHERE id = ?", update_vals)

    all_accounts = list_accounts(db)
    for a in all_accounts:
        if a.id == account_id:
            return a
    raise NotFoundError(f"账户 #{account_id} 不存在")


@router.patch("/{account_id}", response_model=AccountOut)
def patch_account(
    account_id: int,
    payload: AccountUpdateRequest,
    db: sqlite3.Connection = Depends(get_db_conn),
):
    """Partially update account alias or holder."""
    return _update_account_record(account_id, payload, db)


@router.put("/{account_id}", response_model=AccountOut)
def put_account(
    account_id: int,
    payload: AccountUpdateRequest,
    db: sqlite3.Connection = Depends(get_db_conn),
):
    """Update account alias or holder."""
    return _update_account_record(account_id, payload, db)


@router.post("/merge", response_model=AccountMergeResponse)
def merge_accounts(
    payload: AccountMergeRequest,
    db: sqlite3.Connection = Depends(get_db_conn),
):
    """Atomically merge two accounts, reassigning transactions and deleting source account."""
    if payload.source_id == payload.target_id:
        raise ValidationError("源账户与目标账户不能相同")

    source = db.execute("SELECT id FROM accounts WHERE id = ?", (payload.source_id,)).fetchone()
    target = db.execute("SELECT id FROM accounts WHERE id = ?", (payload.target_id,)).fetchone()

    if not source:
        raise NotFoundError(f"源账户 #{payload.source_id} 不存在")
    if not target:
        raise NotFoundError(f"目标账户 #{payload.target_id} 不存在")

    with db:
        c1 = db.execute(
            "UPDATE transactions SET account_id = ? WHERE account_id = ?",
            (payload.target_id, payload.source_id),
        ).rowcount
        c2 = db.execute(
            "UPDATE transactions SET fund_account_id = ? WHERE fund_account_id = ?",
            (payload.target_id, payload.source_id),
        ).rowcount
        db.execute("DELETE FROM accounts WHERE id = ?", (payload.source_id,))

    return AccountMergeResponse(
        ok=True,
        migrated_count=c1 + c2,
        merged_into_id=payload.target_id,
    )
