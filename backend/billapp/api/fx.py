"""Foreign Exchange (FX) REST API endpoints (/api/fx/*).

Provides rates management, missing FX refresh, and manual rate entry per Milestone 6 specifications.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, Field

from billapp.app import AppException, NotFoundError, ValidationError, get_db_conn
from billapp.core.fx import (
    get_fx_overview,
    record_manual_rate,
    refresh_missing_fx,
    retry_transaction_fx,
)

router = APIRouter(prefix="/api/fx", tags=["fx"])


# =========================================================================
# FX DTO Schemas
# =========================================================================

class FxRateItem(BaseModel):
    id: int
    currency: str
    rate: float
    effective_date: str
    source: str
    created_at: str
    updated_at: str
    note: Optional[str] = None


class FxPendingByCurrency(BaseModel):
    currency: str
    count: int
    amount_cents: int


class FxRatesResponse(BaseModel):
    rates: List[FxRateItem]
    currencies: List[str]
    pending_count: int
    pending_by_currency: List[FxPendingByCurrency]


class FxRefreshResponse(BaseModel):
    total_missing: int
    refreshed_count: int


class FxManualRateRequest(BaseModel):
    currency: str = Field(..., min_length=2, max_length=10)
    rate: float = Field(..., gt=0)
    effective_date: str = Field(..., min_length=10, max_length=10)
    note: Optional[str] = None
    apply_to_all_missing: bool = False


class FxManualRateResponse(BaseModel):
    success: bool
    rate: Optional[FxRateItem] = None
    updated_transactions: int


# =========================================================================
# Endpoints
# =========================================================================

@router.get("/rates", response_model=FxRatesResponse)
def get_rates(db: sqlite3.Connection = Depends(get_db_conn)):
    """Return recorded FX rates table, distinct currencies, and pending transaction counts."""
    data = get_fx_overview(db)
    return FxRatesResponse(
        rates=[FxRateItem(**r) for r in data["rates"]],
        currencies=data["currencies"],
        pending_count=data["pending_count"],
        pending_by_currency=[FxPendingByCurrency(**p) for p in data["pending_by_currency"]],
    )


@router.post("/refresh", response_model=FxRefreshResponse)
def refresh_rates(db: sqlite3.Connection = Depends(get_db_conn)):
    """Refresh only missing FX transactions (amount_cny IS NULL or fx_rate_source = 'none').
    
    Does NOT overwrite existing 'online' or 'manual' historical rates.
    """
    total_missing, refreshed_count = refresh_missing_fx(db)
    return FxRefreshResponse(total_missing=total_missing, refreshed_count=refreshed_count)


@router.post("/rates", response_model=FxManualRateResponse)
def create_manual_rate(
    payload: FxManualRateRequest,
    db: sqlite3.Connection = Depends(get_db_conn),
):
    """Manually enter/supplement exchange rate (currency, rate 4-6 decimals, effective_date, note).
    
    Updates matching missing transactions and recomputes amount_cny.
    """
    if payload.rate <= 0:
        raise ValidationError(message="汇率必须为大于 0 的数值")

    rate_dict, updated_count = record_manual_rate(
        conn=db,
        currency=payload.currency,
        rate=payload.rate,
        effective_date=payload.effective_date,
        note=payload.note,
        apply_to_all_missing=payload.apply_to_all_missing,
    )

    rate_item = FxRateItem(**rate_dict) if rate_dict else None
    return FxManualRateResponse(
        success=True,
        rate=rate_item,
        updated_transactions=updated_count,
    )
