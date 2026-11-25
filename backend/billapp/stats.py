"""Effective expense engine, KPI metrics, trend & category analytics, and heatmaps."""

from __future__ import annotations

from datetime import date, datetime
import json
import sqlite3
from typing import Any, Dict, List, Literal, Optional, Tuple
from pydantic import BaseModel, ConfigDict, Field


# =========================================================================
# Statistics DTO Models
# =========================================================================

class StatsSummaryPrev(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    expense_net: int = 0
    income: int = 0
    from_date: str = Field(default="", alias="from")
    to_date: str = Field(default="", alias="to")


class FxPendingAmount(BaseModel):
    currency: str
    amount: int


class StatsSummary(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    # Primary fields
    total_expense_cents: int
    total_income_cents: int
    net_savings_cents: int
    savings_rate: float
    daily_average_cents: int
    transaction_count: int

    # Frontend Design Contract aliases (§8.2)
    expense_net: int
    income: int
    balance: int
    daily_avg: int
    from_date: str = Field(default="", alias="from")
    to_date: str = Field(default="", alias="to")
    days: int = 1
    expense_count: int = 0
    income_count: int = 0
    fx_included_count: int = 0
    fx_included_cny: int = 0
    fx_pending_count: int = 0
    fx_pending_amounts: List[FxPendingAmount] = Field(default_factory=list)
    prev: Optional[StatsSummaryPrev] = None


class TrendItem(BaseModel):
    period: str                     # "2024-10" or "2024-10-15"
    expense_cents: int              # Net expense
    income_cents: int
    net_cents: int                  # income - expense
    transaction_count: int = 0


class CategoryStatNode(BaseModel):
    id: Optional[int] = None
    key: Optional[str] = None       # e.g. 'food', 'food.dining'
    name: str                       # e.g. '餐饮美食', '正餐'
    amount_cents: int               # Net cents
    percentage: float = 0.0         # 0.0 - 100.0
    color: Optional[str] = None
    icon: Optional[str] = None
    children: List[CategoryStatNode] = Field(default_factory=list)


class CategoryStatsResponse(BaseModel):
    total_cents: int
    direction: str = "expense"
    items: List[CategoryStatNode] = Field(default_factory=list)


class MerchantRankItem(BaseModel):
    merchant: str
    expense_cents: int
    transaction_count: int
    percentage: float = 0.0


class MerchantStatsResponse(BaseModel):
    total_expense_cents: int
    items: List[MerchantRankItem] = Field(default_factory=list)
    other_expense_cents: int = 0
    other_count: int = 0


class HeatmapItem(BaseModel):
    date: str                       # 'YYYY-MM-DD'
    expense_cents: int
    count: int


class WeekdayHourHeatmapItem(BaseModel):
    weekday: int                    # 1 (Monday) .. 7 (Sunday)
    hour: int                       # 0 .. 23
    expense_cents: int
    count: int


class HeatmapResponse(BaseModel):
    mode: str = "calendar"          # 'calendar' or 'weekday_hour'
    year: Optional[int] = None
    calendar_items: List[HeatmapItem] = Field(default_factory=list)
    weekday_hour_items: List[WeekdayHourHeatmapItem] = Field(default_factory=list)


class AccountStatItem(BaseModel):
    account_id: Optional[int] = None
    name: str                       # Alias or Bank(tail)
    bank: Optional[str] = None
    tail: Optional[str] = None
    type: str = "unknown"           # 'bank_debit', 'bank_credit', 'wallet', 'unknown'
    balance_cents: Optional[int] = None
    total_in_cents: int = 0
    total_out_cents: int = 0
    net_cents: int = 0
    transaction_count: int = 0
    percentage: float = 0.0


class AccountStatsResponse(BaseModel):
    total_out_cents: int
    total_in_cents: int
    items: List[AccountStatItem] = Field(default_factory=list)


# =========================================================================
# Query Helpers
# =========================================================================

def get_refund_mode(conn: sqlite3.Connection) -> str:
    """Read refund calculation mode from settings table, defaulting to 'original_month'."""
    try:
        row = conn.execute("SELECT value FROM settings WHERE key = 'refund_settings'").fetchone()
        if row and row[0]:
            data = json.loads(row[0])
            if isinstance(data, dict):
                return data.get("mode", "original_month")
    except Exception:
        pass
    return "original_month"


# SQL expression fragments for foreign exchange (FX) conversion into CNY (Milestone 6)
SQL_CNY_AMOUNT = "COALESCE(t.amount_cny, t.amount_cents)"
SQL_CNY_NET_EXPENSE = "(CASE WHEN t.fx_rate IS NOT NULL AND t.currency != 'CNY' THEN ROUND((t.amount_cents - t.refunded_cents) * t.fx_rate) ELSE (COALESCE(t.amount_cny, t.amount_cents) - t.refunded_cents) END)"


def build_stats_filters(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    account_id: Optional[int] = None,
    fund_account_id: Optional[int] = None,
    alias: str = "t",
) -> Tuple[str, List[Any]]:
    """Generate reusable SQL WHERE clauses for valid statistics records."""
    clauses = [
        f"{alias}.duplicate_of IS NULL",
        f"{alias}.excluded = 0",
        f"({alias}.currency = 'CNY' OR {alias}.currency IS NULL OR ({alias}.currency != 'CNY' AND {alias}.amount_cny IS NOT NULL))",
    ]
    params: List[Any] = []

    if start_date:
        # Use raw 'YYYY-MM-DD' for 10-char dates so day-precision txn_time ('YYYY-MM-DD') is included
        clauses.append(f"{alias}.txn_time >= ?")
        params.append(start_date)

    if end_date:
        val = f"{end_date} 23:59:59" if len(end_date) == 10 else end_date
        clauses.append(f"{alias}.txn_time <= ?")
        params.append(val)

    if fund_account_id is not None:
        clauses.append(f"{alias}.fund_account_id = ?")
        params.append(fund_account_id)
    elif account_id is not None:
        clauses.append(f"{alias}.account_id = ?")
        params.append(account_id)

    return " AND ".join(clauses), params


def build_fx_pending_filters(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    account_id: Optional[int] = None,
    fund_account_id: Optional[int] = None,
    alias: str = "t",
) -> Tuple[str, List[Any]]:
    """Generate SQL WHERE clauses for pending foreign currency records (§8.2)."""
    clauses = [
        f"{alias}.duplicate_of IS NULL",
        f"{alias}.excluded = 0",
        f"{alias}.currency IS NOT NULL",
        f"{alias}.currency != 'CNY'",
        f"({alias}.amount_cny IS NULL OR {alias}.fx_rate_source = 'none')",
    ]
    params: List[Any] = []

    if start_date:
        # Use raw 'YYYY-MM-DD' for 10-char dates so day-precision txn_time ('YYYY-MM-DD') is included
        clauses.append(f"{alias}.txn_time >= ?")
        params.append(start_date)

    if end_date:
        val = f"{end_date} 23:59:59" if len(end_date) == 10 else end_date
        clauses.append(f"{alias}.txn_time <= ?")
        params.append(val)

    if fund_account_id is not None:
        clauses.append(f"{alias}.fund_account_id = ?")
        params.append(fund_account_id)
    elif account_id is not None:
        clauses.append(f"{alias}.account_id = ?")
        params.append(account_id)

    return " AND ".join(clauses), params


def _calculate_previous_period_summary(
    conn: sqlite3.Connection,
    start_date: str,
    end_date: str,
    days: int,
    account_id: Optional[int] = None,
    fund_account_id: Optional[int] = None,
    include_transfer: bool = False,
) -> Optional[StatsSummaryPrev]:
    """Calculate summary for preceding period of same duration for 环比 calculation."""
    try:
        d1 = date.fromisoformat(start_date[:10])
        prev_end = d1.fromordinal(d1.toordinal() - 1)
        prev_start = prev_end.fromordinal(prev_end.toordinal() - days + 1)
        prev_s_str = prev_start.isoformat()
        prev_e_str = prev_end.isoformat()

        where_prev, prev_params = build_stats_filters(
            prev_s_str, prev_e_str, account_id, fund_account_id, alias="t"
        )
        transfer_cond = _transfer_filter_sql(include_transfer)
        refund_mode = get_refund_mode(conn)
        if refund_mode == "refund_month":
            sql_exp = f"""
                SELECT COALESCE(SUM({SQL_CNY_AMOUNT}), 0)
                FROM transactions t
                LEFT JOIN categories c ON t.category_id = c.id
                WHERE {where_prev}
                  AND t.direction = 'expense'
                  {transfer_cond}
            """
            exp_row = conn.execute(sql_exp, prev_params).fetchone()
            exp_val = int(exp_row[0]) if exp_row else 0

            # In refund_month mode, all refunds occurring in previous period deduct
            sql_ref = f"""
                SELECT COALESCE(SUM({SQL_CNY_AMOUNT}), 0)
                FROM transactions t
                WHERE {where_prev}
                  AND t.direction = 'refund'
            """
            ref_row = conn.execute(sql_ref, prev_params).fetchone()
            ref_val = int(ref_row[0]) if ref_row else 0
            net_exp = max(0, exp_val - ref_val)
        else:
            sql_exp = f"""
                SELECT COALESCE(SUM({SQL_CNY_NET_EXPENSE}), 0)
                FROM transactions t
                LEFT JOIN categories c ON t.category_id = c.id
                WHERE {where_prev}
                  AND t.direction = 'expense'
                  {transfer_cond}
            """
            exp_row = conn.execute(sql_exp, prev_params).fetchone()
            exp_val = int(exp_row[0]) if exp_row else 0

            # Subtract unlinked refunds
            sql_ref = f"""
                SELECT COALESCE(SUM({SQL_CNY_AMOUNT}), 0)
                FROM transactions t
                WHERE {where_prev}
                  AND t.direction = 'refund'
                  AND t.refund_of IS NULL
            """
            ref_row = conn.execute(sql_ref, prev_params).fetchone()
            ref_val = int(ref_row[0]) if ref_row else 0
            net_exp = max(0, exp_val - ref_val)

        sql_inc = f"""
            SELECT COALESCE(SUM({SQL_CNY_AMOUNT}), 0)
            FROM transactions t
            LEFT JOIN categories c ON t.category_id = c.id
            WHERE {where_prev}
              AND t.direction = 'income'
              {transfer_cond}
        """
        inc_row = conn.execute(sql_inc, prev_params).fetchone()
        inc_val = int(inc_row[0]) if inc_row else 0

        return StatsSummaryPrev(
            expense_net=net_exp,
            income=inc_val,
            from_date=prev_s_str,
            to_date=prev_e_str,
        )
    except Exception:
        return None


# =========================================================================
# Statistics Engine Implementations
# =========================================================================

def calculate_stats_summary(
    conn: sqlite3.Connection,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    account_id: Optional[int] = None,
    fund_account_id: Optional[int] = None,
    include_transfer: bool = False,
) -> StatsSummary:
    """Calculate KPI summary metrics under the Effective Expense standard."""
    where_base, base_params = build_stats_filters(
        start_date, end_date, account_id, fund_account_id, alias="t"
    )

    transfer_cond = ""
    if not include_transfer:
        transfer_cond = """
          AND t.transfer_peer IS NULL
          AND (c.key IS NULL OR c.key != 'financial.internal_transfer')
          AND (c.name IS NULL OR c.name NOT IN ('内部转账', '资金互转'))
        """

    refund_mode = get_refund_mode(conn)
    if refund_mode == "refund_month":
        # 1. Total Net Expense under refund_month:
        sql_expense = f"""
            SELECT COALESCE(SUM({SQL_CNY_AMOUNT}), 0) AS total_expense,
                   COUNT(*) AS expense_count
            FROM transactions t
            LEFT JOIN categories c ON t.category_id = c.id
            WHERE {where_base}
              AND t.direction = 'expense'
              {transfer_cond}
        """
        row_exp = conn.execute(sql_expense, base_params).fetchone()
        expense_cents = int(row_exp[0]) if row_exp else 0
        expense_count = int(row_exp[1]) if row_exp else 0

        # All refunds in this period deduct from total expense
        sql_refund = f"""
            SELECT COALESCE(SUM({SQL_CNY_AMOUNT}), 0) AS total_refund,
                   COUNT(*) AS refund_count
            FROM transactions t
            WHERE {where_base}
              AND t.direction = 'refund'
        """
        row_ref = conn.execute(sql_refund, base_params).fetchone()
        refund_cents = int(row_ref[0]) if row_ref else 0
        net_expense_cents = max(0, expense_cents - refund_cents)
    else:
        # 1. Total Net Expense under original_month:
        sql_expense = f"""
            SELECT COALESCE(SUM({SQL_CNY_NET_EXPENSE}), 0) AS total_expense,
                   COUNT(*) AS expense_count
            FROM transactions t
            LEFT JOIN categories c ON t.category_id = c.id
            WHERE {where_base}
              AND t.direction = 'expense'
              {transfer_cond}
        """
        row_exp = conn.execute(sql_expense, base_params).fetchone()
        expense_cents = int(row_exp[0]) if row_exp else 0
        expense_count = int(row_exp[1]) if row_exp else 0

        # Unlinked refunds in this period deduct from total expense
        sql_unlinked_refund = f"""
            SELECT COALESCE(SUM({SQL_CNY_AMOUNT}), 0) AS total_refund,
                   COUNT(*) AS refund_count
            FROM transactions t
            WHERE {where_base}
              AND t.direction = 'refund'
              AND t.refund_of IS NULL
        """
        row_ref = conn.execute(sql_unlinked_refund, base_params).fetchone()
        unlinked_refund_cents = int(row_ref[0]) if row_ref else 0
        net_expense_cents = max(0, expense_cents - unlinked_refund_cents)

    # 2. Total Income: Strictly direction='income', excluding transfers
    sql_income = f"""
        SELECT COALESCE(SUM({SQL_CNY_AMOUNT}), 0) AS total_income,
               COUNT(*) AS income_count
        FROM transactions t
        LEFT JOIN categories c ON t.category_id = c.id
        WHERE {where_base}
          AND t.direction = 'income'
          {transfer_cond}
    """
    row_inc = conn.execute(sql_income, base_params).fetchone()
    income_cents = int(row_inc[0]) if row_inc else 0
    income_count = int(row_inc[1]) if row_inc else 0

    # 3. Savings metrics
    net_savings_cents = income_cents - net_expense_cents
    if income_cents > 0:
        savings_rate = round((net_savings_cents / income_cents) * 100.0, 2)
    else:
        savings_rate = 0.0

    # 4. Days & Daily Average
    days = 1
    if start_date and end_date:
        try:
            d1 = date.fromisoformat(start_date[:10])
            d2 = date.fromisoformat(end_date[:10])
            days = max(1, (d2 - d1).days + 1)
        except Exception:
            days = 1
    else:
        span_row = conn.execute(
            f"""
            SELECT MIN(substr(t.txn_time, 1, 10)), MAX(substr(t.txn_time, 1, 10))
            FROM transactions t
            LEFT JOIN categories c ON t.category_id = c.id
            WHERE {where_base}
              AND t.direction IN ('expense', 'income')
              {transfer_cond}
            """,
            base_params,
        ).fetchone()
        if span_row and span_row[0] and span_row[1]:
            try:
                d1 = date.fromisoformat(start_date[:10]) if start_date else date.fromisoformat(span_row[0])
                d2 = date.fromisoformat(end_date[:10]) if end_date else date.fromisoformat(span_row[1])
                days = max(1, (d2 - d1).days + 1)
            except Exception:
                days = 1
    daily_average_cents = round(net_expense_cents / days) if days > 0 else 0

    # 5. Period Comparison (prev) for 环比
    prev = None
    if start_date and end_date:
        prev = _calculate_previous_period_summary(
            conn,
            start_date,
            end_date,
            days,
            account_id,
            fund_account_id,
            include_transfer=include_transfer,
        )

    # 6. Foreign Currency (FX) Included calculation (§8.2) — exclude neutral & internal transfers
    sql_fx_inc = f"""
        SELECT COUNT(*) AS cnt,
               COALESCE(SUM(CASE WHEN t.direction = 'expense' THEN {SQL_CNY_NET_EXPENSE} ELSE {SQL_CNY_AMOUNT} END), 0) AS sum_cny
        FROM transactions t
        LEFT JOIN categories c ON t.category_id = c.id
        WHERE {where_base}
          AND t.direction IN ('expense', 'income')
          {transfer_cond}
          AND t.currency IS NOT NULL
          AND t.currency != 'CNY'
          AND t.amount_cny IS NOT NULL
    """
    row_fx_inc = conn.execute(sql_fx_inc, base_params).fetchone()
    fx_included_count = int(row_fx_inc[0]) if row_fx_inc else 0
    fx_included_cny = int(row_fx_inc[1]) if row_fx_inc else 0

    # 7. Foreign Currency (FX) Pending calculation (§8.2)
    where_fx, fx_params = build_fx_pending_filters(
        start_date, end_date, account_id, fund_account_id, alias="t"
    )
    sql_fx = f"""
        SELECT t.currency,
               COUNT(*) AS cnt,
               COALESCE(SUM(t.amount_cents), 0) AS sum_cents
        FROM transactions t
        LEFT JOIN categories c ON t.category_id = c.id
        WHERE {where_fx}
          AND t.direction IN ('expense', 'income')
          {transfer_cond}
        GROUP BY t.currency
        ORDER BY t.currency ASC
    """
    fx_rows = conn.execute(sql_fx, fx_params).fetchall()
    fx_pending_count = sum(int(r[1]) for r in fx_rows)
    fx_pending_amounts = [
        FxPendingAmount(currency=r[0], amount=int(r[2]))
        for r in fx_rows
    ]

    return StatsSummary(
        total_expense_cents=net_expense_cents,
        total_income_cents=income_cents,
        net_savings_cents=net_savings_cents,
        savings_rate=savings_rate,
        daily_average_cents=daily_average_cents,
        transaction_count=expense_count + income_count,
        expense_net=net_expense_cents,
        income=income_cents,
        balance=net_savings_cents,
        daily_avg=daily_average_cents,
        from_date=start_date or "",
        to_date=end_date or "",
        days=days,
        expense_count=expense_count,
        income_count=income_count,
        fx_included_count=fx_included_count,
        fx_included_cny=fx_included_cny,
        fx_pending_count=fx_pending_count,
        fx_pending_amounts=fx_pending_amounts,
        prev=prev,
    )


def _transfer_filter_sql(include_transfer: bool) -> str:
    if include_transfer:
        return ""
    return """
      AND t.transfer_peer IS NULL
      AND (c.key IS NULL OR c.key != 'financial.internal_transfer')
      AND (c.name IS NULL OR c.name NOT IN ('内部转账', '资金互转'))
    """


def calculate_stats_trend(
    conn: sqlite3.Connection,
    granularity: str = "monthly",  # 'monthly' or 'daily'
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    account_id: Optional[int] = None,
    fund_account_id: Optional[int] = None,
    include_transfer: bool = False,
) -> List[TrendItem]:
    """Calculate monthly or daily net expense and income time series."""
    where_base, base_params = build_stats_filters(
        start_date, end_date, account_id, fund_account_id, alias="t"
    )
    transfer_cond = _transfer_filter_sql(include_transfer)

    fmt = "%Y-%m" if granularity == "monthly" else "%Y-%m-%d"
    refund_mode = get_refund_mode(conn)

    if refund_mode == "refund_month":
        # In refund_month mode, expenses are amount_cents and all refunds deduct in their own period
        sql_exp = f"""
            SELECT strftime('{fmt}', t.txn_time) AS period,
                   COALESCE(SUM({SQL_CNY_AMOUNT}), 0) AS exp_cents,
                   COUNT(*) AS exp_count
            FROM transactions t
            LEFT JOIN categories c ON t.category_id = c.id
            WHERE {where_base}
              AND t.direction = 'expense'
              {transfer_cond}
            GROUP BY period
            ORDER BY period ASC
        """
        exp_rows = conn.execute(sql_exp, base_params).fetchall()
        exp_map = {r[0]: (int(r[1]), int(r[2])) for r in exp_rows}

        # Query all refunds per period
        sql_ref = f"""
            SELECT strftime('{fmt}', t.txn_time) AS period,
                   COALESCE(SUM({SQL_CNY_AMOUNT}), 0) AS ref_cents
            FROM transactions t
            WHERE {where_base}
              AND t.direction = 'refund'
            GROUP BY period
        """
        ref_rows = conn.execute(sql_ref, base_params).fetchall()
        ref_map = {r[0]: int(r[1]) for r in ref_rows}
    else:
        # Query period expenses under original_month
        sql_exp = f"""
            SELECT strftime('{fmt}', t.txn_time) AS period,
                   COALESCE(SUM({SQL_CNY_NET_EXPENSE}), 0) AS exp_cents,
                   COUNT(*) AS exp_count
            FROM transactions t
            LEFT JOIN categories c ON t.category_id = c.id
            WHERE {where_base}
              AND t.direction = 'expense'
              {transfer_cond}
            GROUP BY period
            ORDER BY period ASC
        """
        exp_rows = conn.execute(sql_exp, base_params).fetchall()
        exp_map = {r[0]: (int(r[1]), int(r[2])) for r in exp_rows}

        # Query unlinked refunds per period
        sql_ref = f"""
            SELECT strftime('{fmt}', t.txn_time) AS period,
                   COALESCE(SUM({SQL_CNY_AMOUNT}), 0) AS ref_cents
            FROM transactions t
            WHERE {where_base}
              AND t.direction = 'refund'
              AND t.refund_of IS NULL
            GROUP BY period
        """
        ref_rows = conn.execute(sql_ref, base_params).fetchall()
        ref_map = {r[0]: int(r[1]) for r in ref_rows}

    # Query period income
    sql_inc = f"""
        SELECT strftime('{fmt}', t.txn_time) AS period,
               COALESCE(SUM({SQL_CNY_AMOUNT}), 0) AS inc_cents,
               COUNT(*) AS inc_count
        FROM transactions t
        LEFT JOIN categories c ON t.category_id = c.id
        WHERE {where_base}
          AND t.direction = 'income'
          {transfer_cond}
        GROUP BY period
        ORDER BY period ASC
    """
    inc_rows = conn.execute(sql_inc, base_params).fetchall()
    inc_map = {r[0]: (int(r[1]), int(r[2])) for r in inc_rows}

    all_periods = sorted(set(exp_map.keys()) | set(inc_map.keys()) | set(ref_map.keys()))
    results: List[TrendItem] = []

    for p in all_periods:
        if not p:
            continue
        gross_exp, exp_cnt = exp_map.get(p, (0, 0))
        ref_deduct = ref_map.get(p, 0)
        net_exp = max(0, gross_exp - ref_deduct)
        inc, inc_cnt = inc_map.get(p, (0, 0))

        results.append(
            TrendItem(
                period=p,
                expense_cents=net_exp,
                income_cents=inc,
                net_cents=inc - net_exp,
                transaction_count=exp_cnt + inc_cnt,
            )
        )

    return results


def calculate_stats_category(
    conn: sqlite3.Connection,
    direction: str = "expense",
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    account_id: Optional[int] = None,
    fund_account_id: Optional[int] = None,
    include_transfer: bool = False,
) -> CategoryStatsResponse:
    """Calculate 2-tier hierarchical category breakdown for ECharts Sunburst / Donut."""
    where_base, base_params = build_stats_filters(
        start_date, end_date, account_id, fund_account_id, alias="t"
    )
    transfer_cond = _transfer_filter_sql(include_transfer)

    # 1. Fetch category taxonomy
    cat_rows = conn.execute(
        "SELECT id, key, name, parent_id, color, icon, sort_order FROM categories WHERE direction = ?",
        (direction,),
    ).fetchall()

    # 2. Query transactions grouped by category_id (respecting refund_mode & unlinked refunds)
    refund_mode = get_refund_mode(conn)
    refund_deduct_rows: List[sqlite3.Row] = []

    if direction == "expense":
        exp_expr = SQL_CNY_AMOUNT if refund_mode == "refund_month" else SQL_CNY_NET_EXPENSE
        sql_cat = f"""
            SELECT t.category_id,
                   COALESCE(SUM({exp_expr}), 0) AS net_cents,
                   COUNT(*) AS txn_count
            FROM transactions t
            LEFT JOIN categories c ON t.category_id = c.id
            WHERE {where_base}
              AND t.direction = 'expense'
              {transfer_cond}
            GROUP BY t.category_id
        """
        if refund_mode == "refund_month":
            sql_ref_cat = f"""
                SELECT COALESCE(orig.category_id, t.category_id) AS category_id,
                       COALESCE(SUM({SQL_CNY_AMOUNT}), 0) AS ref_cents
                FROM transactions t
                LEFT JOIN transactions orig ON t.refund_of = orig.id
                WHERE {where_base}
                  AND t.direction = 'refund'
                GROUP BY COALESCE(orig.category_id, t.category_id)
            """
            refund_deduct_rows = conn.execute(sql_ref_cat, base_params).fetchall()
        else:
            sql_ref_cat = f"""
                SELECT t.category_id AS category_id,
                       COALESCE(SUM({SQL_CNY_AMOUNT}), 0) AS ref_cents
                FROM transactions t
                WHERE {where_base}
                  AND t.direction = 'refund'
                  AND t.refund_of IS NULL
                GROUP BY t.category_id
            """
            refund_deduct_rows = conn.execute(sql_ref_cat, base_params).fetchall()
    else:
        sql_cat = f"""
            SELECT t.category_id,
                   COALESCE(SUM({SQL_CNY_AMOUNT}), 0) AS net_cents,
                   COUNT(*) AS txn_count
            FROM transactions t
            LEFT JOIN categories c ON t.category_id = c.id
            WHERE {where_base}
              AND t.direction = 'income'
              {transfer_cond}
            GROUP BY t.category_id
        """

    cat_txn_rows = conn.execute(sql_cat, base_params).fetchall()

    # 3. Accumulate amounts into 2-tier tree (keep root direct_cents separate from sub_nodes)
    root_nodes: Dict[int, Dict[str, Any]] = {}
    sub_nodes: Dict[int, Dict[str, Any]] = {}
    unclassified_cents = 0
    unallocated_refund_cents = 0

    for r in cat_rows:
        if r["parent_id"] is None:
            root_nodes[r["id"]] = {
                "id": r["id"],
                "key": r["key"],
                "name": r["name"],
                "direct_cents": 0,
                "amount_cents": 0,
                "color": r["color"],
                "icon": r["icon"],
                "sort_order": r["sort_order"],
                "children": [],
            }
        else:
            sub_nodes[r["id"]] = {
                "id": r["id"],
                "key": r["key"],
                "name": r["name"],
                "parent_id": r["parent_id"],
                "amount_cents": 0,
                "color": r["color"],
                "icon": r["icon"],
                "sort_order": r["sort_order"],
            }

    def _apply_delta(cid: Optional[int], delta_cents: int, is_refund: bool = False) -> None:
        nonlocal unclassified_cents, unallocated_refund_cents
        if cid is None:
            if is_refund:
                unallocated_refund_cents += -delta_cents
            else:
                unclassified_cents += delta_cents
        elif cid in sub_nodes:
            sub_nodes[cid]["amount_cents"] += delta_cents
        elif cid in root_nodes:
            root_nodes[cid]["direct_cents"] += delta_cents
        else:
            if is_refund:
                # Unlinked refund auto-tagged with an income/other category (e.g. reimbursement.refund)
                unallocated_refund_cents += -delta_cents
            else:
                # Preserve cross-direction or orphaned categories under unclassified instead of dropping
                unclassified_cents += delta_cents

    for row in cat_txn_rows:
        _apply_delta(row["category_id"], int(row["net_cents"]), is_refund=False)

    for rrow in refund_deduct_rows:
        _apply_delta(rrow["category_id"], -int(rrow["ref_cents"]), is_refund=True)

    # Clamp negative leaves BEFORE rolling up to parents so a child NEVER exceeds its parent or 100%.
    # Any negative remainder on a subcategory first reduces its own parent root proportionally,
    # and any overflow beyond that root flows into unallocated_refund_cents to keep total_cents == summary.
    parent_excess_refund: Dict[int, int] = {}
    for sub in sub_nodes.values():
        if sub["amount_cents"] < 0:
            excess = -sub["amount_cents"]
            p_id = sub["parent_id"]
            if p_id in root_nodes:
                parent_excess_refund[p_id] = parent_excess_refund.get(p_id, 0) + excess
            else:
                unallocated_refund_cents += excess
            sub["amount_cents"] = 0
    for root_id, root in root_nodes.items():
        if root["direct_cents"] < 0:
            parent_excess_refund[root_id] = parent_excess_refund.get(root_id, 0) + (-root["direct_cents"])
            root["direct_cents"] = 0
        root["amount_cents"] = root["direct_cents"]

    for sub in sub_nodes.values():
        if sub["amount_cents"] > 0:
            p_id = sub["parent_id"]
            if p_id in root_nodes:
                root_nodes[p_id]["children"].append(sub)
                root_nodes[p_id]["amount_cents"] += sub["amount_cents"]
            else:
                unclassified_cents += sub["amount_cents"]

    def _scale_root_amount(root: Dict[str, Any], new_amt: int) -> None:
        old_amt = root["amount_cents"]
        root["amount_cents"] = new_amt
        if root["children"] and old_amt > 0:
            ratio = new_amt / old_amt
            child_budget = new_amt
            for sub in root["children"]:
                sub["amount_cents"] = min(child_budget, round(sub["amount_cents"] * ratio))
                child_budget -= sub["amount_cents"]
            root["children"] = [s for s in root["children"] if s["amount_cents"] > 0]

    for root_id, excess in parent_excess_refund.items():
        root = root_nodes.get(root_id)
        if not root or excess <= 0:
            continue
        if excess >= root["amount_cents"]:
            unallocated_refund_cents += excess - root["amount_cents"]
            _scale_root_amount(root, 0)
        else:
            _scale_root_amount(root, root["amount_cents"] - excess)

    if unclassified_cents < 0:
        unallocated_refund_cents += -unclassified_cents
        unclassified_cents = 0

    # Apply unallocated expense refunds proportionally so total_cents stays consistent with summary
    # while keeping every category & child slice non-negative and child <= parent.
    active_roots = [r for r in root_nodes.values() if r["amount_cents"] > 0]
    if unclassified_cents > 0:
        active_roots.append({
            "id": None,
            "key": "unclassified",
            "name": "未分类",
            "direct_cents": unclassified_cents,
            "amount_cents": unclassified_cents,
            "color": "#94A3B8",
            "icon": "QuestionFilled",
            "children": [],
        })

    gross_total = sum(r["amount_cents"] for r in active_roots)
    if unallocated_refund_cents > 0 and gross_total > 0:
        if unallocated_refund_cents >= gross_total:
            active_roots = []
        else:
            remaining_deduct = unallocated_refund_cents
            for idx, root in enumerate(active_roots):
                old_amt = root["amount_cents"]
                if idx == len(active_roots) - 1:
                    deduct = min(old_amt, remaining_deduct)
                else:
                    deduct = min(old_amt, remaining_deduct, round(unallocated_refund_cents * old_amt / gross_total))
                remaining_deduct -= deduct
                _scale_root_amount(root, max(0, old_amt - deduct))
            # Absorb any residual cents due to rounding
            for root in active_roots:
                if remaining_deduct <= 0:
                    break
                if root["amount_cents"] > 0:
                    deduct = min(root["amount_cents"], remaining_deduct)
                    remaining_deduct -= deduct
                    _scale_root_amount(root, root["amount_cents"] - deduct)
            active_roots = [r for r in active_roots if r["amount_cents"] > 0]

    total_cents = sum(r["amount_cents"] for r in active_roots)

    # Calculate percentages
    for root in active_roots:
        root["percentage"] = round((root["amount_cents"] / total_cents) * 100.0, 2) if total_cents > 0 else 0.0
        root["children"].sort(key=lambda s: s["amount_cents"], reverse=True)
        for sub in root["children"]:
            sub["percentage"] = round((sub["amount_cents"] / total_cents) * 100.0, 2) if total_cents > 0 else 0.0

    active_roots.sort(key=lambda r: r["amount_cents"], reverse=True)

    return CategoryStatsResponse(
        total_cents=total_cents,
        direction=direction,
        items=[CategoryStatNode(**r) for r in active_roots],
    )


def calculate_stats_merchant(
    conn: sqlite3.Connection,
    limit: int = 20,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    account_id: Optional[int] = None,
    fund_account_id: Optional[int] = None,
    include_transfer: bool = False,
) -> MerchantStatsResponse:
    """Calculate top N merchants by net expense with tail aggregation."""
    where_base, base_params = build_stats_filters(
        start_date, end_date, account_id, fund_account_id, alias="t"
    )
    transfer_cond = _transfer_filter_sql(include_transfer)
    refund_mode = get_refund_mode(conn)
    exp_expr = SQL_CNY_AMOUNT if refund_mode == "refund_month" else SQL_CNY_NET_EXPENSE

    sql = f"""
        SELECT COALESCE(NULLIF(t.merchant_norm, ''), NULLIF(t.counterparty, ''), '其他') AS merchant_name,
               COALESCE(SUM({exp_expr}), 0) AS expense_cents,
               COUNT(*) AS txn_count
        FROM transactions t
        LEFT JOIN categories c ON t.category_id = c.id
        WHERE {where_base}
          AND t.direction = 'expense'
          {transfer_cond}
        GROUP BY merchant_name
        ORDER BY expense_cents DESC
    """
    rows = conn.execute(sql, base_params).fetchall()

    ref_cond = "" if refund_mode == "refund_month" else "AND t.refund_of IS NULL"
    sql_ref = f"""
        SELECT COALESCE(NULLIF(t.merchant_norm, ''), NULLIF(t.counterparty, ''), '其他') AS merchant_name,
               COALESCE(SUM({SQL_CNY_AMOUNT}), 0) AS ref_cents
        FROM transactions t
        WHERE {where_base}
          AND t.direction = 'refund'
          {ref_cond}
        GROUP BY merchant_name
    """
    ref_map = {r["merchant_name"]: int(r["ref_cents"]) for r in conn.execute(sql_ref, base_params).fetchall()}

    merged: List[Tuple[str, int, int]] = []
    for r in rows:
        m_name = r["merchant_name"]
        net_c = max(0, int(r["expense_cents"]) - ref_map.get(m_name, 0))
        if net_c > 0:
            merged.append((m_name, net_c, int(r["txn_count"])))

    merged.sort(key=lambda item: item[1], reverse=True)
    total_expense = sum(item[1] for item in merged)

    top_rows = merged[:limit]
    tail_rows = merged[limit:]

    items: List[MerchantRankItem] = []
    for m_name, cents, cnt in top_rows:
        items.append(
            MerchantRankItem(
                merchant=m_name,
                expense_cents=cents,
                transaction_count=cnt,
                percentage=round((cents / total_expense) * 100.0, 2) if total_expense > 0 else 0.0,
            )
        )

    other_cents = sum(item[1] for item in tail_rows)
    other_count = sum(item[2] for item in tail_rows)

    return MerchantStatsResponse(
        total_expense_cents=total_expense,
        items=items,
        other_expense_cents=other_cents,
        other_count=other_count,
    )


def calculate_stats_heatmap(
    conn: sqlite3.Connection,
    year: Optional[int] = None,
    mode: str = "calendar",  # 'calendar' or 'weekday_hour'
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    account_id: Optional[int] = None,
    fund_account_id: Optional[int] = None,
    include_transfer: bool = False,
) -> HeatmapResponse:
    """Calculate expense heatmap: daily calendar or 7x24 weekday-hour."""
    transfer_cond = _transfer_filter_sql(include_transfer)
    refund_mode = get_refund_mode(conn)
    exp_expr = SQL_CNY_AMOUNT if refund_mode == "refund_month" else SQL_CNY_NET_EXPENSE
    ref_cond = "" if refund_mode == "refund_month" else "AND t.refund_of IS NULL"

    if mode == "weekday_hour":
        eff_start = start_date or (f"{year}-01-01" if year is not None else None)
        eff_end = end_date or (f"{year}-12-31" if year is not None else None)
        where_base, base_params = build_stats_filters(
            eff_start, eff_end, account_id, fund_account_id, alias="t"
        )
        # Exclude date-only precision per Frontend §6.5.9
        sql = f"""
            SELECT CAST(strftime('%w', t.txn_time) AS INT) AS wday_raw,
                   CAST(strftime('%H', t.txn_time) AS INT) AS hour_num,
                   COALESCE(SUM({exp_expr}), 0) AS expense_cents,
                   COUNT(*) AS txn_count
            FROM transactions t
            LEFT JOIN categories c ON t.category_id = c.id
            WHERE {where_base}
              AND t.direction = 'expense'
              AND t.time_precision != 'day'
              {transfer_cond}
            GROUP BY wday_raw, hour_num
        """
        rows = conn.execute(sql, base_params).fetchall()
        ref_rows = conn.execute(
            f"""
            SELECT CAST(strftime('%w', t.txn_time) AS INT) AS wday_raw,
                   CAST(strftime('%H', t.txn_time) AS INT) AS hour_num,
                   COALESCE(SUM({SQL_CNY_AMOUNT}), 0) AS ref_cents
            FROM transactions t
            WHERE {where_base}
              AND t.direction = 'refund'
              AND t.time_precision != 'day'
              {ref_cond}
            GROUP BY wday_raw, hour_num
            """,
            base_params,
        ).fetchall()
        ref_wh_map = {
            (r["wday_raw"], int(r["hour_num"])): int(r["ref_cents"])
            for r in ref_rows
            if r["wday_raw"] is not None and r["hour_num"] is not None
        }

        wh_items: List[WeekdayHourHeatmapItem] = []
        for r in rows:
            if r["wday_raw"] is None or r["hour_num"] is None:
                continue
            wday = 7 if r["wday_raw"] == 0 else r["wday_raw"]
            hour_num = int(r["hour_num"])
            net_c = max(0, int(r["expense_cents"]) - ref_wh_map.get((r["wday_raw"], hour_num), 0))
            wh_items.append(
                WeekdayHourHeatmapItem(
                    weekday=wday,
                    hour=hour_num,
                    expense_cents=net_c,
                    count=int(r["txn_count"]),
                )
            )
        return HeatmapResponse(mode="weekday_hour", year=year, weekday_hour_items=wh_items)

    # Calendar mode
    target_year = year or datetime.now().year
    s_date = start_date or f"{target_year}-01-01"
    e_date = end_date or f"{target_year}-12-31"

    where_base, base_params = build_stats_filters(
        s_date, e_date, account_id, fund_account_id, alias="t"
    )

    sql = f"""
        SELECT strftime('%Y-%m-%d', t.txn_time) AS date_str,
               COALESCE(SUM({exp_expr}), 0) AS expense_cents,
               COUNT(*) AS txn_count
        FROM transactions t
        LEFT JOIN categories c ON t.category_id = c.id
        WHERE {where_base}
          AND t.direction = 'expense'
          {transfer_cond}
        GROUP BY date_str
        ORDER BY date_str ASC
    """
    rows = conn.execute(sql, base_params).fetchall()
    ref_rows = conn.execute(
        f"""
        SELECT strftime('%Y-%m-%d', t.txn_time) AS date_str,
               COALESCE(SUM({SQL_CNY_AMOUNT}), 0) AS ref_cents
        FROM transactions t
        WHERE {where_base}
          AND t.direction = 'refund'
          {ref_cond}
        GROUP BY date_str
        """,
        base_params,
    ).fetchall()
    ref_cal_map = {r["date_str"]: int(r["ref_cents"]) for r in ref_rows if r["date_str"]}

    cal_items = [
        HeatmapItem(
            date=r["date_str"],
            expense_cents=max(0, int(r["expense_cents"]) - ref_cal_map.get(r["date_str"], 0)),
            count=int(r["txn_count"]),
        )
        for r in rows
        if r["date_str"]
    ]
    return HeatmapResponse(mode="calendar", year=target_year, calendar_items=cal_items)


def calculate_stats_account(
    conn: sqlite3.Connection,
    by: str = "account",  # 'account' or 'fund_account'
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    include_transfer: bool = False,
) -> AccountStatsResponse:
    """Calculate inflow, outflow, and net balance distribution per account."""
    where_base, base_params = build_stats_filters(
        start_date, end_date, alias="t"
    )
    transfer_cond = _transfer_filter_sql(include_transfer)
    refund_mode = get_refund_mode(conn)
    exp_expr = SQL_CNY_AMOUNT if refund_mode == "refund_month" else SQL_CNY_NET_EXPENSE
    ref_case = (
        f"CASE WHEN t.direction = 'refund' THEN {SQL_CNY_AMOUNT} ELSE 0 END"
        if refund_mode == "refund_month"
        else f"CASE WHEN t.direction = 'refund' AND t.refund_of IS NULL THEN {SQL_CNY_AMOUNT} ELSE 0 END"
    )

    target_col = "t.fund_account_id" if by == "fund_account" else "t.account_id"

    sql = f"""
        SELECT COALESCE({target_col}, t.account_id) AS effective_acc_id,
               COALESCE(SUM(CASE WHEN t.direction = 'expense' THEN {exp_expr} ELSE 0 END), 0) AS gross_out,
               COALESCE(SUM({ref_case}), 0) AS refund_deduct,
               COALESCE(SUM(CASE WHEN t.direction = 'income' THEN {SQL_CNY_AMOUNT} ELSE 0 END), 0) AS total_in,
               COALESCE(SUM(CASE WHEN t.direction IN ('expense', 'income') THEN 1 ELSE 0 END), 0) AS txn_count
        FROM transactions t
        LEFT JOIN categories c ON t.category_id = c.id
        WHERE {where_base}
          {transfer_cond}
        GROUP BY effective_acc_id
    """
    rows = conn.execute(sql, base_params).fetchall()

    acc_lookup = {
        r["id"]: r
        for r in conn.execute("SELECT id, kind, bank, tail, alias, currency FROM accounts").fetchall()
    }

    computed_rows = [
        (r["effective_acc_id"], max(0, int(r["gross_out"]) - int(r["refund_deduct"])), int(r["total_in"]), int(r["txn_count"]))
        for r in rows
    ]
    grand_total_out = sum(item[1] for item in computed_rows)
    grand_total_in = sum(item[2] for item in computed_rows)

    items: List[AccountStatItem] = []
    for acc_id, out_c, in_c, cnt in computed_rows:
        meta = acc_lookup.get(acc_id)
        if meta:
            name = meta["alias"] or (f"{meta['bank']}({meta['tail']})" if meta["tail"] else meta["bank"])
            bank = meta["bank"]
            tail = meta["tail"]
            acc_type = meta["kind"]
        else:
            name = "未识别账户"
            bank = None
            tail = None
            acc_type = "unknown"

        items.append(
            AccountStatItem(
                account_id=acc_id,
                name=name,
                bank=bank,
                tail=tail,
                type=acc_type,
                total_out_cents=out_c,
                total_in_cents=in_c,
                net_cents=in_c - out_c,
                transaction_count=cnt,
                percentage=round((out_c / grand_total_out) * 100.0, 2) if grand_total_out > 0 else 0.0,
            )
        )

    items.sort(key=lambda a: a.total_out_cents, reverse=True)
    return AccountStatsResponse(
        total_out_cents=grand_total_out,
        total_in_cents=grand_total_in,
        items=items,
    )
