"""OpenPyXL Streaming Dual-Sheet Excel Workbook Export Generator."""

from __future__ import annotations

from datetime import datetime
import io
import sqlite3
from typing import Any, Dict, List, Optional, Set, Tuple

import openpyxl
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from billapp.stats import build_stats_filters


def sanitize_cell_value(val: Any) -> Any:
    """Sanitize string values that could trigger CSV/Excel formula injection.

    Neutralizes leading '=', '+', '-', '@', tab/newline/CR characters, as well
    as leading whitespace followed by any formula trigger character.
    """
    if isinstance(val, str) and val:
        stripped = val.lstrip(" \t\r\n")
        if val[0] in ("\t", "\r", "\n") or (stripped and stripped[0] in ("=", "+", "-", "@")):
            return f"'{val}"
    return val


def generate_excel_export(
    conn: sqlite3.Connection,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    account_id: Optional[int] = None,
    category_id: Optional[int] = None,
    include_duplicates: bool = True,
) -> io.BytesIO:
    """Generate professional dual-sheet Excel workbook as streaming BytesIO.
    
    Sheet 1: "交易明细" with 16 columns, numeric formatting #,##0.00, status tags,
             freeze header A2, header color #2F6BFF, and strike font for excluded/duplicate.
    Sheet 2: "月度分类透视表" with dynamic Excel =SUM(...) formulas for rows and columns,
             freeze panes C2, and double underline accounting border on total.
    """
    wb = openpyxl.Workbook()

    # =========================================================================
    # Sheet 1: 交易明细
    # =========================================================================
    ws1 = wb.active
    ws1.title = "交易明细"

    headers1 = [
        "序号", "交易时间", "来源平台", "交易对手", "商品/说明", "收支方向",
        "金额(元)", "冲抵金额(元)", "净支出额(元)", "一级分类", "二级分类",
        "账户", "支付方式", "状态标签", "排除标记", "备注",
        "币种", "交易时汇率", "折算人民币(元)",
    ]
    ws1.append(headers1)

    # Styling definitions
    font_header = Font(name="微软雅黑", size=11, bold=True, color="FFFFFF")
    fill_header = PatternFill(start_color="2F6BFF", end_color="2F6BFF", fill_type="solid")
    border_thin = Border(
        left=Side(style="thin", color="E5E7EB"),
        right=Side(style="thin", color="E5E7EB"),
        top=Side(style="thin", color="E5E7EB"),
        bottom=Side(style="thin", color="E5E7EB"),
    )
    align_center = Alignment(horizontal="center", vertical="center")
    align_right = Alignment(horizontal="right", vertical="center")
    align_left = Alignment(horizontal="left", vertical="center")

    font_data = Font(name="微软雅黑", size=10)
    font_strike = Font(name="微软雅黑", size=10, strike=True, color="808080")

    # Format header row
    ws1.row_dimensions[1].height = 26
    for col_idx in range(1, len(headers1) + 1):
        cell = ws1.cell(row=1, column=col_idx)
        cell.font = font_header
        cell.fill = fill_header
        cell.alignment = align_center
        cell.border = border_thin

    # Query records
    where_clauses = ["1=1"]
    params: List[Any] = []

    if start_date:
        val = f"{start_date} 00:00:00" if len(start_date) == 10 else start_date
        where_clauses.append("t.txn_time >= ?")
        params.append(val)
    if end_date:
        val = f"{end_date} 23:59:59" if len(end_date) == 10 else end_date
        where_clauses.append("t.txn_time <= ?")
        params.append(val)
    if account_id is not None:
        where_clauses.append("t.account_id = ?")
        params.append(account_id)
    if category_id is not None:
        where_clauses.append("(t.category_id = ? OR t.category_id IN (SELECT id FROM categories WHERE parent_id = ?))")
        params.extend([category_id, category_id])
    if not include_duplicates:
        where_clauses.append("t.duplicate_of IS NULL")

    where_sql = " AND ".join(where_clauses)

    sql = f"""
        SELECT t.id, t.txn_time, t.source, t.counterparty, t.description, t.direction,
               t.amount_cents, t.refunded_cents, t.pay_method, t.duplicate_of, t.refund_of,
               t.transfer_peer, t.excluded, t.note, t.currency,
               t.amount_cny, t.fx_rate, t.fx_rate_at, t.fx_rate_source,
               c.key AS cat_key, c.name AS cat_name, p.name AS parent_cat_name,
               a.bank, a.tail, a.alias
        FROM transactions t
        LEFT JOIN categories c ON t.category_id = c.id
        LEFT JOIN categories p ON c.parent_id = p.id
        LEFT JOIN accounts a ON t.account_id = a.id
        WHERE {where_sql}
        ORDER BY t.txn_time DESC
    """
    rows = conn.execute(sql, params).fetchall()

    direction_labels = {
        "expense": "支出",
        "income": "收入",
        "refund": "退款",
        "transfer": "转账",
        "neutral": "不计收支",
    }
    source_labels = {
        "alipay": "支付宝",
        "wechat": "微信支付",
        "boc_debit": "中国银行",
        "icbc_credit": "工商银行",
    }

    months_set: Set[str] = set()
    category_month_matrix: Dict[Tuple[str, str], Dict[str, float]] = {}

    for idx, r in enumerate(rows, start=1):
        row_num = idx + 1
        ws1.row_dimensions[row_num].height = 20

        # Status tag determination
        if r["duplicate_of"] is not None:
            status_tag = "判定重复"
        elif r["refund_of"] is not None:
            status_tag = "退款关联"
        elif r["transfer_peer"] is not None or r["direction"] == "transfer":
            status_tag = "内部转账"
        else:
            status_tag = "主记录"

        amt_yuan = round(r["amount_cents"] / 100.0, 2)
        ref_yuan = round(r["refunded_cents"] / 100.0, 2)
        net_yuan = round(max(0, (r["amount_cents"] - r["refunded_cents"])) / 100.0, 2) if r["direction"] == "expense" else 0.00

        # Categories
        if r["parent_cat_name"]:
            root_cat = r["parent_cat_name"]
            sub_cat = r["cat_name"]
        elif r["cat_name"]:
            root_cat = r["cat_name"]
            sub_cat = "-"
        else:
            root_cat = "未分类"
            sub_cat = "-"

        # Account label
        if r["alias"]:
            acc_label = r["alias"]
        elif r["bank"]:
            acc_label = f"{r['bank']}({r['tail'] or '主卡'})"
        else:
            acc_label = "默认账户"

        excluded_str = "是" if r["excluded"] == 1 else "否"

        curr_code = r["currency"] or "CNY"
        fx_rate_val = r["fx_rate"] if r["fx_rate"] is not None else ("-" if curr_code == "CNY" else "待获取")
        cny_equiv_yuan = round(r["amount_cny"] / 100.0, 2) if r["amount_cny"] is not None else ("-" if curr_code != "CNY" else amt_yuan)

        row_vals = [
            idx,
            sanitize_cell_value(r["txn_time"] or ""),
            sanitize_cell_value(source_labels.get(r["source"], r["source"]) or ""),
            sanitize_cell_value(r["counterparty"] or ""),
            sanitize_cell_value(r["description"] or ""),
            sanitize_cell_value(direction_labels.get(r["direction"], r["direction"]) or ""),
            amt_yuan,
            ref_yuan,
            net_yuan,
            sanitize_cell_value(root_cat),
            sanitize_cell_value(sub_cat),
            sanitize_cell_value(acc_label),
            sanitize_cell_value(r["pay_method"] or ""),
            sanitize_cell_value(status_tag),
            sanitize_cell_value(excluded_str),
            sanitize_cell_value(r["note"] or ""),
            sanitize_cell_value(curr_code),
            fx_rate_val,
            cny_equiv_yuan,
        ]
        ws1.append(row_vals)

        # Style data row
        is_striked = (r["duplicate_of"] is not None or r["excluded"] == 1)
        cur_font = font_strike if is_striked else font_data

        for c_idx in range(1, len(row_vals) + 1):
            cell = ws1.cell(row=row_num, column=c_idx)
            cell.font = cur_font
            cell.border = border_thin
            if c_idx in (7, 8, 9, 19) and isinstance(cell.value, (int, float)):
                cell.number_format = "#,##0.00"
                cell.alignment = align_right
            elif c_idx in (1, 2, 6, 14, 15, 17):
                cell.alignment = align_center
            elif c_idx == 18:
                cell.alignment = align_right if isinstance(cell.value, (int, float)) else align_center
            else:
                cell.alignment = align_left

        # Collect for Sheet 2 aggregation if valid expense (excluding internal transfers and missing FX)
        is_internal_transfer = (
            r["transfer_peer"] is not None
            or r["cat_key"] == "financial.internal_transfer"
            or r["cat_name"] in ("内部转账", "资金互转")
        )
        if (
            r["direction"] == "expense"
            and r["duplicate_of"] is None
            and r["excluded"] == 0
            and not is_internal_transfer
        ):
            if r["currency"] == "CNY" or r["currency"] is None:
                month_str = r["txn_time"][:7]  # 'YYYY-MM'
                months_set.add(month_str)
                cat_key = (root_cat, sub_cat)
                category_month_matrix.setdefault(cat_key, {})
                category_month_matrix[cat_key][month_str] = round(
                    category_month_matrix[cat_key].get(month_str, 0.0) + net_yuan, 2
                )
            elif r["amount_cny"] is not None:
                if r["fx_rate"] is not None:
                    cny_cents = max(0, round((r["amount_cents"] - r["refunded_cents"]) * r["fx_rate"]))
                else:
                    cny_cents = max(0, r["amount_cny"] - r["refunded_cents"])
                cny_net_yuan = round(cny_cents / 100.0, 2)
                month_str = r["txn_time"][:7]  # 'YYYY-MM'
                months_set.add(month_str)
                cat_key = (root_cat, sub_cat)
                category_month_matrix.setdefault(cat_key, {})
                category_month_matrix[cat_key][month_str] = round(
                    category_month_matrix[cat_key].get(month_str, 0.0) + cny_net_yuan, 2
                )

    # Freeze header on Sheet 1
    ws1.freeze_panes = "A2"

    # Auto column width for Sheet 1
    for col in ws1.columns:
        max_len = 0
        for cell in col:
            val_str = str(cell.value or "")
            w = sum(2 if ord(c) > 127 else 1 for c in val_str)
            if w > max_len:
                max_len = w
        col_letter = get_column_letter(col[0].column)
        ws1.column_dimensions[col_letter].width = min(max(max_len + 3, 10), 45)

    # =========================================================================
    # Sheet 2: 月度分类透视表
    # =========================================================================
    ws2 = wb.create_sheet(title="月度分类透视表")
    sorted_months = sorted(months_set) if months_set else [datetime.now().strftime("%Y-%m")]

    headers2 = ["一级分类", "二级分类"] + sorted_months + ["合计"]
    ws2.append(headers2)

    ws2.row_dimensions[1].height = 26
    for c_idx in range(1, len(headers2) + 1):
        cell = ws2.cell(row=1, column=c_idx)
        cell.font = font_header
        cell.fill = fill_header
        cell.alignment = align_center
        cell.border = border_thin

    sorted_cat_keys = sorted(category_month_matrix.keys(), key=lambda k: (k[0], k[1]))
    cur_p_row = 2

    first_month_col_letter = get_column_letter(3)
    last_month_col_letter = get_column_letter(2 + len(sorted_months))
    total_col_letter = get_column_letter(3 + len(sorted_months))

    for root_cat, sub_cat in sorted_cat_keys:
        ws2.row_dimensions[cur_p_row].height = 20
        month_vals = [
            category_month_matrix[(root_cat, sub_cat)].get(m, 0.0)
            for m in sorted_months
        ]
        sum_formula = f"=SUM({first_month_col_letter}{cur_p_row}:{last_month_col_letter}{cur_p_row})"

        p_row_vals = [sanitize_cell_value(root_cat), sanitize_cell_value(sub_cat)] + month_vals + [sum_formula]
        ws2.append(p_row_vals)

        for c_idx in range(1, len(p_row_vals) + 1):
            cell = ws2.cell(row=cur_p_row, column=c_idx)
            cell.font = font_data
            cell.border = border_thin
            if c_idx >= 3:
                cell.number_format = "#,##0.00"
                cell.alignment = align_right
            else:
                cell.alignment = align_left

        cur_p_row += 1

    # Bottom summary row ("月度总支出")
    last_cat_row = cur_p_row - 1
    if last_cat_row >= 2:
        ws2.row_dimensions[cur_p_row].height = 24
        summary_cells: List[Any] = ["月度总支出", ""]
        for m_idx in range(len(sorted_months)):
            m_col = get_column_letter(3 + m_idx)
            summary_cells.append(f"=SUM({m_col}2:{m_col}{last_cat_row})")
        # Grand total sum
        summary_cells.append(f"=SUM({total_col_letter}2:{total_col_letter}{last_cat_row})")

        ws2.append(summary_cells)

        font_summary = Font(name="微软雅黑", size=11, bold=True)
        border_summary = Border(
            top=Side(style="thin", color="000000"),
            bottom=Side(style="double", color="000000"),
            left=Side(style="thin", color="E5E7EB"),
            right=Side(style="thin", color="E5E7EB"),
        )
        for c_idx in range(1, len(summary_cells) + 1):
            cell = ws2.cell(row=cur_p_row, column=c_idx)
            cell.font = font_summary
            cell.border = border_summary
            if c_idx >= 3:
                cell.number_format = "#,##0.00"
                cell.alignment = align_right
            else:
                cell.alignment = align_left

    # Freeze panes on Sheet 2 (freezes Category columns A & B)
    ws2.freeze_panes = "C2"

    # Auto column width for Sheet 2
    for col in ws2.columns:
        max_len = 0
        for cell in col:
            val_str = str(cell.value or "")
            w = sum(2 if ord(c) > 127 else 1 for c in val_str)
            if w > max_len:
                max_len = w
        col_letter = get_column_letter(col[0].column)
        ws2.column_dimensions[col_letter].width = min(max(max_len + 3, 12), 35)

    bio = io.BytesIO()
    wb.save(bio)
    bio.seek(0)
    return bio
