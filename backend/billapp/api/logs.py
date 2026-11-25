"""Diagnostic log inspection and export API endpoints (/api/logs/*)."""

from __future__ import annotations

import logging
from pathlib import Path
import sqlite3
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, Field

from billapp.app import APP_VERSION, get_db_conn
from billapp.core.db import get_default_db_path
from billapp.core.logs import (
    build_log_export_bundle,
    discover_backend_log_files,
    flush_active_logs,
    get_active_log_path,
    get_default_log_path,
    get_log_file_metadata,
    read_backend_log_tail,
)
from billapp.llm.prompts import collect_known_holder_names

logger = logging.getLogger("billapp.api.logs")
router = APIRouter(prefix="/api/logs", tags=["logs"])


class LogFileItemOut(BaseModel):
    name: str
    size_bytes: int
    modified_at: str


class LogsInfoOut(BaseModel):
    log_dir: str
    active_log_path: str
    files: List[LogFileItemOut]
    total_size_bytes: int
    tail_lines: List[str]
    has_logs: bool


class FrontendLogEntryIn(BaseModel):
    timestamp: str = Field("", max_length=64)
    level: str = Field("error", max_length=16)
    source: str = Field("frontend", max_length=64)
    message: str = Field("", max_length=2000)
    stack: Optional[str] = Field(None, max_length=4000)


class DesktopLogFileIn(BaseModel):
    name: str = Field("desktop.log", max_length=128)
    content: str = Field("", max_length=4 * 1024 * 1024)


class LogExportRequest(BaseModel):
    format: Literal["zip", "log"] = "zip"
    include_diagnostics: bool = True
    redact_pii: bool = True
    frontend_logs: List[FrontendLogEntryIn] = Field(default_factory=list, max_length=500)
    desktop_logs: List[DesktopLogFileIn] = Field(default_factory=list, max_length=10)
    desktop_meta: Optional[Dict[str, Any]] = None


def _resolve_request_log_path(request: Request) -> Path:
    explicit = getattr(request.app.state, "log_path", None)
    if explicit:
        return Path(explicit)
    active = get_active_log_path()
    if active is not None:
        return active
    return get_default_log_path()


@router.get("/info", response_model=LogsInfoOut)
def get_logs_info(
    request: Request,
    tail_lines: int = Query(120, ge=0, le=500),
    redact_pii: bool = Query(True),
    db: sqlite3.Connection = Depends(get_db_conn),
):
    """Return active backend log files metadata and a sanitized tail preview."""
    flush_active_logs()
    primary_path = _resolve_request_log_path(request)
    files = discover_backend_log_files(primary_path)
    meta_items = get_log_file_metadata(files)
    total_size = sum(item["size_bytes"] for item in meta_items)
    holder_names = collect_known_holder_names(db) if redact_pii else []
    tail = read_backend_log_tail(
        files,
        max_lines=tail_lines,
        holder_names=holder_names,
        redact_pii=redact_pii,
    )

    display_active = str(files[0] if files else primary_path)
    display_dir = str((files[0] if files else primary_path).parent)
    return LogsInfoOut(
        log_dir=display_dir,
        active_log_path=display_active,
        files=[LogFileItemOut(**m) for m in meta_items],
        total_size_bytes=total_size,
        tail_lines=tail,
        has_logs=len(meta_items) > 0,
    )


@router.post("/export")
def export_logs(
    request: Request,
    payload: Optional[LogExportRequest] = None,
    db: sqlite3.Connection = Depends(get_db_conn),
):
    """Export sanitized diagnostic logs as a ZIP archive or merged plain-text .log file (POST-only)."""
    req = payload or LogExportRequest()
    primary_path = _resolve_request_log_path(request)
    raw_db_path = getattr(request.app.state, "db_path", None)
    db_path = str(raw_db_path or get_default_db_path())

    logger.info(
        "Exporting diagnostic logs: format=%s, include_diagnostics=%s, redact_pii=%s, frontend_entries=%d, desktop_files=%d",
        req.format,
        req.include_diagnostics,
        req.redact_pii,
        len(req.frontend_logs),
        len(req.desktop_logs),
    )

    content, media_type, filename = build_log_export_bundle(
        db,
        app_version=APP_VERSION,
        db_path=db_path,
        primary_log_path=primary_path,
        export_format=req.format,
        include_diagnostics=req.include_diagnostics,
        redact_pii=req.redact_pii,
        frontend_logs=[e.model_dump() for e in req.frontend_logs],
        desktop_logs=[d.model_dump() for d in req.desktop_logs],
        desktop_meta=req.desktop_meta,
    )

    return Response(
        content=content,
        media_type=media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-BillApp-Log-Filename": filename,
        },
    )
