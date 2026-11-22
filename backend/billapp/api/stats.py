"""Statistics Engine API endpoints (/api/stats/*)."""

from __future__ import annotations

import sqlite3
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, Query

from billapp.app import get_db_conn
from billapp.stats import (
    AccountStatsResponse,
    CategoryStatsResponse,
    HeatmapResponse,
    MerchantStatsResponse,
    StatsSummary,
    TrendItem,
    calculate_stats_account,
    calculate_stats_category,
    calculate_stats_heatmap,
    calculate_stats_merchant,
    calculate_stats_summary,
    calculate_stats_trend,
)

router = APIRouter(prefix="/api/stats", tags=["stats"])


@router.get("/summary", response_model=StatsSummary)
def get_stats_summary(
    start_date: Optional[str] = Query(None, alias="from"),
    end_date: Optional[str] = Query(None, alias="to"),
    from_date: Optional[str] = Query(None, alias="start_date"),
    to_date: Optional[str] = Query(None, alias="end_date"),
    account_id: Optional[int] = Query(None),
    fund_account_id: Optional[int] = Query(None),
    include_transfer: bool = Query(False),
    db: sqlite3.Connection = Depends(get_db_conn),
):
    s_date = start_date or from_date
    e_date = end_date or to_date
    return calculate_stats_summary(
        db,
        start_date=s_date,
        end_date=e_date,
        account_id=account_id,
        fund_account_id=fund_account_id,
        include_transfer=include_transfer,
    )


@router.get("/trend", response_model=List[TrendItem])
def get_stats_trend(
    granularity: Literal["monthly", "daily"] = Query("monthly"),
    start_date: Optional[str] = Query(None, alias="from"),
    end_date: Optional[str] = Query(None, alias="to"),
    from_date: Optional[str] = Query(None, alias="start_date"),
    to_date: Optional[str] = Query(None, alias="end_date"),
    account_id: Optional[int] = Query(None),
    fund_account_id: Optional[int] = Query(None),
    include_transfer: bool = Query(False),
    db: sqlite3.Connection = Depends(get_db_conn),
):
    s_date = start_date or from_date
    e_date = end_date or to_date
    return calculate_stats_trend(
        db,
        granularity=granularity,
        start_date=s_date,
        end_date=e_date,
        account_id=account_id,
        fund_account_id=fund_account_id,
        include_transfer=include_transfer,
    )


@router.get("/category", response_model=CategoryStatsResponse)
def get_stats_category(
    direction: Literal["expense", "income"] = Query("expense"),
    start_date: Optional[str] = Query(None, alias="from"),
    end_date: Optional[str] = Query(None, alias="to"),
    from_date: Optional[str] = Query(None, alias="start_date"),
    to_date: Optional[str] = Query(None, alias="end_date"),
    account_id: Optional[int] = Query(None),
    fund_account_id: Optional[int] = Query(None),
    include_transfer: bool = Query(False),
    db: sqlite3.Connection = Depends(get_db_conn),
):
    s_date = start_date or from_date
    e_date = end_date or to_date
    return calculate_stats_category(
        db,
        direction=direction,
        start_date=s_date,
        end_date=e_date,
        account_id=account_id,
        fund_account_id=fund_account_id,
        include_transfer=include_transfer,
    )


@router.get("/merchant", response_model=MerchantStatsResponse)
def get_stats_merchant(
    limit: int = Query(20, ge=1, le=100),
    start_date: Optional[str] = Query(None, alias="from"),
    end_date: Optional[str] = Query(None, alias="to"),
    from_date: Optional[str] = Query(None, alias="start_date"),
    to_date: Optional[str] = Query(None, alias="end_date"),
    account_id: Optional[int] = Query(None),
    fund_account_id: Optional[int] = Query(None),
    include_transfer: bool = Query(False),
    db: sqlite3.Connection = Depends(get_db_conn),
):
    s_date = start_date or from_date
    e_date = end_date or to_date
    return calculate_stats_merchant(
        db,
        limit=limit,
        start_date=s_date,
        end_date=e_date,
        account_id=account_id,
        fund_account_id=fund_account_id,
        include_transfer=include_transfer,
    )


@router.get("/heatmap", response_model=HeatmapResponse)
def get_stats_heatmap(
    year: Optional[int] = Query(None),
    mode: Literal["calendar", "weekday_hour"] = Query("calendar"),
    start_date: Optional[str] = Query(None, alias="from"),
    end_date: Optional[str] = Query(None, alias="to"),
    from_date: Optional[str] = Query(None, alias="start_date"),
    to_date: Optional[str] = Query(None, alias="end_date"),
    account_id: Optional[int] = Query(None),
    fund_account_id: Optional[int] = Query(None),
    include_transfer: bool = Query(False),
    db: sqlite3.Connection = Depends(get_db_conn),
):
    s_date = start_date or from_date
    e_date = end_date or to_date
    return calculate_stats_heatmap(
        db,
        year=year,
        mode=mode,
        start_date=s_date,
        end_date=e_date,
        account_id=account_id,
        fund_account_id=fund_account_id,
        include_transfer=include_transfer,
    )


@router.get("/account", response_model=AccountStatsResponse)
def get_stats_account(
    by: Literal["account", "fund_account"] = Query("account"),
    start_date: Optional[str] = Query(None, alias="from"),
    end_date: Optional[str] = Query(None, alias="to"),
    from_date: Optional[str] = Query(None, alias="start_date"),
    to_date: Optional[str] = Query(None, alias="end_date"),
    include_transfer: bool = Query(False),
    db: sqlite3.Connection = Depends(get_db_conn),
):
    s_date = start_date or from_date
    e_date = end_date or to_date
    return calculate_stats_account(
        db,
        by=by,
        start_date=s_date,
        end_date=e_date,
        include_transfer=include_transfer,
    )
