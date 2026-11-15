"""Application settings and configuration API endpoints (/api/settings/*)."""

from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from typing import Any, Dict, Literal, Optional

from fastapi import APIRouter, Depends, File, Request, Response, UploadFile, status
from pydantic import BaseModel, Field

from billapp.app import ValidationError, get_db_conn
from billapp.classify.categories import CategoryService
from billapp.core.db import get_default_db_path, migrate_db
from billapp.core.secrets import encrypt_secret, mask_secret
from billapp.llm.client import LLMConfig

router = APIRouter(prefix="/api/settings", tags=["settings"])
data_router = APIRouter(prefix="/api", tags=["data"])


# =========================================================================
# Settings DTO Schemas
# =========================================================================

class GeneralSettings(BaseModel):
    table_density: Literal["standard", "compact"] = "standard"
    amount_privacy: bool = False
    remember_window: bool = True
    learning_suggestions_enabled: bool = True


class DedupSettings(BaseModel):
    auto_confirm_threshold: float = Field(0.85, ge=0.5, le=1.0)
    pending_threshold: float = Field(0.55, ge=0.0, le=0.85)
    second_window_minutes: int = Field(30, ge=1, le=120)
    day_window_debit_days: int = Field(3, ge=1, le=14)
    day_window_credit_days: int = Field(5, ge=1, le=30)
    llm_arbitrate_enabled: bool = True
    llm_auto_confirm: bool = False
    llm_auto_confirm_threshold: float = Field(0.90, ge=0.7, le=1.0)


class RefundSettings(BaseModel):
    mode: Literal["original_month", "refund_month"] = "original_month"


class LLMSettingsOut(BaseModel):
    base_url: str = ""
    model: str = "deepseek-chat"
    temperature: float = 0.1
    timeout_sec: float = 60.0
    enabled: bool = False
    task_classify_enabled: bool = True
    task_review_enabled: bool = True
    task_arbitrate_enabled: bool = True
    task_merchant_norm_enabled: bool = True
    task_suggest_profile_enabled: bool = True
    task_full_parse_enabled: bool = True
    auto_confirm_dedup: bool = False
    auto_confirm_threshold: float = 0.90
    api_key_masked: str = ""
    has_api_key: bool = False


class LLMSettingsUpdate(BaseModel):
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    model: Optional[str] = None
    temperature: Optional[float] = Field(None, ge=0.0, le=2.0)
    timeout_sec: Optional[float] = Field(None, ge=1.0, le=180.0)
    enabled: Optional[bool] = None
    task_classify_enabled: Optional[bool] = None
    task_review_enabled: Optional[bool] = None
    task_arbitrate_enabled: Optional[bool] = None
    task_merchant_norm_enabled: Optional[bool] = None
    task_suggest_profile_enabled: Optional[bool] = None
    task_full_parse_enabled: Optional[bool] = None
    auto_confirm_dedup: Optional[bool] = None
    auto_confirm_threshold: Optional[float] = Field(None, ge=0.5, le=1.0)


class SettingsOut(BaseModel):
    general: GeneralSettings
    dedup: DedupSettings
    refund: RefundSettings
    llm: LLMSettingsOut
    db_path: str
    db_size_bytes: int


class SettingsUpdateRequest(BaseModel):
    general: Optional[GeneralSettings] = None
    dedup: Optional[DedupSettings] = None
    refund: Optional[RefundSettings] = None
    llm: Optional[LLMSettingsUpdate] = None


# =========================================================================
# Helpers
# =========================================================================

def _get_setting_json(db: sqlite3.Connection, key: str, default: Dict[str, Any]) -> Dict[str, Any]:
    row = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    if row and row["value"]:
        try:
            return json.loads(row["value"])
        except Exception:
            return default
    return default


def _set_setting_json(db: sqlite3.Connection, key: str, val: Dict[str, Any]):
    db.execute(
        """
        INSERT INTO settings (key, value, updated_at)
        VALUES (?, ?, datetime('now', 'localtime'))
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
        """,
        (key, json.dumps(val, ensure_ascii=False)),
    )


# =========================================================================
# Endpoints
# =========================================================================

@router.get("", response_model=SettingsOut)
def get_settings(
    request: Request,
    db: sqlite3.Connection = Depends(get_db_conn),
):
    """Retrieve full application settings with masked API keys and database metrics."""
    gen_data = _get_setting_json(db, "general_settings", {})
    dedup_data = _get_setting_json(db, "dedup_settings", {})
    refund_data = _get_setting_json(db, "refund_settings", {})

    llm_conf = LLMConfig.load_from_db(db)

    # Database file stats from active application state
    raw_db_path = getattr(request.app.state, "db_path", None)
    db_path = str(raw_db_path or get_default_db_path())
    db_size = 0
    if db_path != ":memory:" and os.path.exists(db_path):
        try:
            db_size = os.path.getsize(db_path)
        except Exception:
            db_size = 0

    return SettingsOut(
        general=GeneralSettings(**gen_data),
        dedup=DedupSettings(**dedup_data),
        refund=RefundSettings(**refund_data),
        llm=LLMSettingsOut(
            base_url=llm_conf.base_url,
            model=llm_conf.model,
            temperature=llm_conf.temperature,
            timeout_sec=llm_conf.timeout_sec,
            enabled=llm_conf.enabled,
            task_classify_enabled=llm_conf.task_classify_enabled,
            task_review_enabled=llm_conf.task_review_enabled,
            task_arbitrate_enabled=llm_conf.task_arbitrate_enabled,
            task_merchant_norm_enabled=llm_conf.task_merchant_norm_enabled,
            task_suggest_profile_enabled=llm_conf.task_suggest_profile_enabled,
            task_full_parse_enabled=llm_conf.task_full_parse_enabled,
            auto_confirm_dedup=llm_conf.auto_confirm_dedup,
            auto_confirm_threshold=llm_conf.auto_confirm_threshold,
            api_key_masked=mask_secret(llm_conf.api_key) if llm_conf.api_key else "",
            has_api_key=bool(llm_conf.api_key and llm_conf.api_key.strip()),
        ),
        db_path=db_path,
        db_size_bytes=db_size,
    )


@router.put("", response_model=SettingsOut)
def update_settings(
    request: Request,
    payload: SettingsUpdateRequest,
    db: sqlite3.Connection = Depends(get_db_conn),
):
    """Update application configurations and encrypt API secrets with Windows DPAPI."""
    if payload.general is not None:
        _set_setting_json(db, "general_settings", payload.general.model_dump())

    if payload.dedup is not None:
        _set_setting_json(db, "dedup_settings", payload.dedup.model_dump())

    if payload.refund is not None:
        _set_setting_json(db, "refund_settings", payload.refund.model_dump())

    if payload.llm is not None:
        conf = LLMConfig.load_from_db(db)
        if payload.llm.base_url is not None:
            conf.base_url = payload.llm.base_url
        if payload.llm.model is not None:
            conf.model = payload.llm.model
        if payload.llm.temperature is not None:
            conf.temperature = payload.llm.temperature
        if payload.llm.timeout_sec is not None:
            conf.timeout_sec = payload.llm.timeout_sec
        if payload.llm.enabled is not None:
            conf.enabled = payload.llm.enabled
        if payload.llm.task_classify_enabled is not None:
            conf.task_classify_enabled = payload.llm.task_classify_enabled
        if payload.llm.task_review_enabled is not None:
            conf.task_review_enabled = payload.llm.task_review_enabled
        if payload.llm.task_arbitrate_enabled is not None:
            conf.task_arbitrate_enabled = payload.llm.task_arbitrate_enabled
        if payload.llm.task_merchant_norm_enabled is not None:
            conf.task_merchant_norm_enabled = payload.llm.task_merchant_norm_enabled
        if payload.llm.task_suggest_profile_enabled is not None:
            conf.task_suggest_profile_enabled = payload.llm.task_suggest_profile_enabled
        if payload.llm.task_full_parse_enabled is not None:
            conf.task_full_parse_enabled = payload.llm.task_full_parse_enabled
        if payload.llm.auto_confirm_dedup is not None:
            conf.auto_confirm_dedup = payload.llm.auto_confirm_dedup
        if payload.llm.auto_confirm_threshold is not None:
            conf.auto_confirm_threshold = payload.llm.auto_confirm_threshold

        # Update API key if provided and not masked
        if payload.llm.api_key is not None:
            raw_key = payload.llm.api_key.strip()
            if raw_key and not raw_key.startswith("****") and not raw_key.startswith("sk-****"):
                conf.api_key = raw_key

        conf.save_to_db(db)

    return get_settings(request, db)


# =========================================================================
# Data Backup, Restore & Reset Endpoints (/api/backup, /api/restore, /api/data/reset)
# =========================================================================

class RestoreResponse(BaseModel):
    success: bool
    transactions_count: int
    accounts_count: int


class ResetDataRequest(BaseModel):
    confirm_text: str
    keep_rules: bool = True
    keep_settings: bool = True


class ResetDataResponse(BaseModel):
    success: bool
    message: str


@data_router.post("/backup")
@data_router.get("/backup")
def backup_database(db: sqlite3.Connection = Depends(get_db_conn)):
    """Create a consistent SQLite snapshot using sqlite3 backup API and return as .db download."""
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    filename = f"billapp-backup-{timestamp}.db"

    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".db")
    os.close(tmp_fd)
    try:
        dst_conn = sqlite3.connect(tmp_path)
        try:
            db.backup(dst_conn)
            dst_conn.commit()
        finally:
            dst_conn.close()

        data = Path(tmp_path).read_bytes()
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    return Response(
        content=data,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@data_router.post("/restore", response_model=RestoreResponse)
async def restore_database(
    file: UploadFile = File(...),
    db: sqlite3.Connection = Depends(get_db_conn),
):
    """Validate and restore an uploaded SQLite backup database safely via sqlite3.backup."""
    content = await file.read()
    if len(content) < 100 or not content.startswith(b"SQLite format 3\x00"):
        raise ValidationError(message="上传文件不是有效的 SQLite 数据库备份")

    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".db")
    os.close(tmp_fd)
    src_conn: Optional[sqlite3.Connection] = None
    try:
        Path(tmp_path).write_bytes(content)
        try:
            src_conn = sqlite3.connect(tmp_path)
            integrity = src_conn.execute("PRAGMA integrity_check;").fetchone()
            if not integrity or integrity[0] != "ok":
                raise ValidationError(message="备份文件完整性校验未通过")

            tables = {
                row[0]
                for row in src_conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            required = {"import_batches", "accounts", "transactions", "categories"}
            if not required.issubset(tables):
                raise ValidationError(message="备份文件缺少账单助手核心数据表")

            # Commit any open read/write state on target before online backup restore
            db.commit()
            src_conn.backup(db)
        except ValidationError:
            raise
        except sqlite3.DatabaseError as exc:
            raise ValidationError(message=f"无法读取备份数据库: {exc}") from exc
        finally:
            if src_conn is not None:
                try:
                    src_conn.close()
                except Exception:
                    pass

        migrate_db(db)
        CategoryService.init_builtin_categories(db)
        db.commit()

        txn_count = db.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
        acc_count = db.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    return RestoreResponse(
        success=True,
        transactions_count=int(txn_count),
        accounts_count=int(acc_count),
    )


@data_router.post("/data/reset", response_model=ResetDataResponse)
def reset_data(
    payload: ResetDataRequest,
    db: sqlite3.Connection = Depends(get_db_conn),
):
    """Clear transactional bill data after explicit confirmation string check."""
    if payload.confirm_text.strip() not in {"确认清空", "RESET"}:
        raise ValidationError(message="确认口令不正确，请输入「确认清空」")

    db.execute("DELETE FROM match_links")
    db.execute("DELETE FROM match_blocklist")
    db.execute("DELETE FROM transactions")
    db.execute("DELETE FROM import_batches")
    db.execute("DELETE FROM accounts")
    db.execute("DELETE FROM fx_rates")
    db.execute("DELETE FROM llm_cache")

    if not payload.keep_rules:
        db.execute("DELETE FROM rules WHERE is_builtin = 0")
        db.execute("UPDATE rules SET hit_count = 0")
        db.execute("DELETE FROM bank_profiles WHERE is_builtin = 0")
        db.execute("DELETE FROM categories WHERE is_builtin = 0")

    if not payload.keep_settings:
        db.execute("DELETE FROM settings")

    CategoryService.init_builtin_categories(db)
    db.commit()
    return ResetDataResponse(success=True, message="账单数据已清空")
