"""Foreign Exchange (FX) REST API endpoints (/api/fx/*).

Provides rates management, missing FX refresh, and manual rate entry per Milestone 6 specifications.
"""

from __future__ import annotations

from datetime import datetime
import math
import re
import sqlite3
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, Field, field_validator

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
    currency: str = Field(..., min_length=3, max_length=3)
    rate: float = Field(..., gt=0, le=1_000_000, allow_inf_nan=False)
    effective_date: str = Field(..., min_length=10, max_length=10)
    note: Optional[str] = None
    apply_to_all_missing: bool = False

    @field_validator("currency")
    @classmethod
    def _validate_currency(cls, value: str) -> str:
        curr = (value or "").strip().upper()
        if not re.fullmatch(r"^[A-Z]{3}$", curr):
            raise ValueError("币种必须是 3 位大写 ISO 字母代码（如 USD、EUR、HKD）")
        return curr

    @field_validator("effective_date")
    @classmethod
    def _validate_effective_date(cls, value: str) -> str:
        date_str = (value or "").strip()
        if not re.fullmatch(r"^\d{4}-\d{2}-\d{2}$", date_str):
            raise ValueError("生效日期格式必须为 YYYY-MM-DD")
        try:
            datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError as exc:
            raise ValueError("生效日期不是有效的日历日期") from exc
        return date_str

    @field_validator("rate")
    @classmethod
    def _validate_rate(cls, value: float) -> float:
        if not math.isfinite(value) or value <= 0 or value > 1_000_000:
            raise ValueError("汇率必须为 (0, 1000000] 范围内的有限正数")
        return value


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
