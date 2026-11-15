"""OpenPyXL Excel dual-sheet export API endpoint (/api/export.xlsx)."""

from __future__ import annotations

from datetime import datetime
import sqlite3
from typing import Optional

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse

from billapp.app import get_db_conn
from billapp.export import generate_excel_export

router = APIRouter(prefix="/api", tags=["export"])


@router.get("/export.xlsx")
def export_transactions_excel(
    start_date: Optional[str] = Query(None, alias="from"),
    end_date: Optional[str] = Query(None, alias="to"),
    from_date: Optional[str] = Query(None, alias="start_date"),
    to_date: Optional[str] = Query(None, alias="end_date"),
    account_id: Optional[int] = Query(None),
    category_id: Optional[int] = Query(None),
    include_duplicates: bool = Query(True),
    db: sqlite3.Connection = Depends(get_db_conn),
):
    """Export transactions to OpenPyXL dual-sheet Excel workbook (Details + Monthly Pivot)."""
    s_date = start_date or from_date
    e_date = end_date or to_date

    bio = generate_excel_export(
        db,
        start_date=s_date,
        end_date=e_date,
        account_id=account_id,
        category_id=category_id,
        include_duplicates=include_duplicates,
    )
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"bill_export_{timestamp}.xlsx"

    return StreamingResponse(
        bio,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
