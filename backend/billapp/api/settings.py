"""Application settings and configuration API endpoints (/api/settings/*)."""

from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from typing import Any, Dict, Literal, Optional, Tuple

import anyio
from fastapi import APIRouter, Depends, File, Request, Response, UploadFile, status
from pydantic import BaseModel, Field, model_validator

from billapp.api._upload import MAX_BACKUP_UPLOAD_BYTES, read_upload_limited
from billapp.app import AppException, ValidationError, get_db_conn
from billapp.classify.categories import CategoryService
from billapp.core.db import get_default_db_path, migrate_db
from billapp.core.secrets import (
    SecretStorageError,
    encrypt_secret,
    get_secret_storage_info,
    mask_secret,
)
from billapp.llm.client import LLMConfig, normalize_url_origin, validate_llm_base_url

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
    llm_arbitrate_enabled: bool = False
    llm_auto_confirm: bool = False
    llm_auto_confirm_threshold: float = Field(0.90, ge=0.7, le=1.0)

    @model_validator(mode="after")
    def _validate_thresholds(self) -> "DedupSettings":
        if self.pending_threshold > self.auto_confirm_threshold:
            raise ValueError("pending_threshold 不能大于 auto_confirm_threshold")
        return self


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
    api_key_decryption_failed: bool = False
    secret_storage_backend: str = ""
    secret_storage_label: str = ""


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
    storage_info = get_secret_storage_info()

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
            api_key_decryption_failed=llm_conf.api_key_decryption_failed,
            secret_storage_backend=storage_info["backend"],
            secret_storage_label=storage_info["label"],
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
        raw_key = payload.llm.api_key.strip() if payload.llm.api_key is not None else None
        has_new_key = bool(
            raw_key and not raw_key.startswith("****") and not raw_key.startswith("sk-****")
        )
        explicit_clear_key = payload.llm.api_key is not None and raw_key == ""
        had_saved_key = bool(conf.api_key.strip() or conf.raw_api_key_encrypted)
        saved_base_url = conf.base_url.strip()

        if payload.llm.base_url is not None:
            cleaned_url = payload.llm.base_url.strip()
            if cleaned_url:
                try:
                    validate_llm_base_url(cleaned_url)
                except ValueError as exc:
                    raise ValidationError(message=str(exc)) from exc
                # Prevent redirecting a persisted API key to a different origin (S-02)
                if had_saved_key and not has_new_key and not explicit_clear_key:
                    if not saved_base_url or normalize_url_origin(cleaned_url) != normalize_url_origin(saved_base_url):
                        raise ValidationError(
                            message="检测到 API 服务地址 (Base URL) 与已保存地址不一致，出于安全保护禁止复用原 API Key，请重新输入 API Key 或先清空原密钥后再修改地址"
                        )
            conf.base_url = cleaned_url
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

        # Update or explicitly clear API key
        if explicit_clear_key:
            conf.api_key = ""
            conf.raw_api_key_encrypted = ""
            conf.api_key_decryption_failed = False
        elif has_new_key and raw_key is not None:
            conf.api_key = raw_key
            conf.raw_api_key_encrypted = ""
            conf.api_key_decryption_failed = False

        try:
            conf.save_to_db(db)
        except SecretStorageError as exc:
            raise AppException(
                status_code=500,
                code="SECRET_STORAGE_ERROR",
                message=str(exc),
            ) from exc

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


_REQUIRED_RESTORE_TABLES: Dict[str, set[str]] = {
    "import_batches": {"id", "file_name", "file_sha1", "source", "row_count"},
    "accounts": {"id", "kind", "bank", "currency"},
    "transactions": {"id", "batch_id", "source", "account_id", "txn_time", "amount_cents", "direction", "fingerprint"},
    "categories": {"id", "name", "direction"},
    "rules": {"id", "name", "category_id", "priority", "field", "pattern_type", "pattern_value"},
    "match_links": {"id", "kind", "primary_id", "secondary_id", "score", "status"},
    "match_blocklist": {"a_id", "b_id", "kind"},
    "bank_profiles": {"id", "name", "yaml_content"},
    "settings": {"key", "value"},
    "fx_rates": {"id", "currency", "rate", "effective_date", "source"},
    "llm_cache": {"id", "task_type", "input_hash", "response_json", "model"},
}


def _redact_backup_secrets(conn: sqlite3.Connection) -> None:
    """Strip LLM API key credentials from exported backup SQLite snapshot (S-03)."""
    try:
        row = conn.execute("SELECT value FROM settings WHERE key = 'llm_config'").fetchone()
        if row and row[0]:
            data = json.loads(row[0])
            if isinstance(data, dict):
                changed = False
                for secret_field in ("api_key_encrypted", "api_key"):
                    if data.get(secret_field):
                        data[secret_field] = ""
                        changed = True
                if changed:
                    conn.execute(
                        "UPDATE settings SET value = ?, updated_at = datetime('now', 'localtime') WHERE key = 'llm_config'",
                        (json.dumps(data, ensure_ascii=False),),
                    )
                    conn.commit()
    except Exception:
        pass


def _sanitize_restored_settings(conn: sqlite3.Connection) -> None:
    """Sanitize restored llm_config to prevent URL hijacking or plaintext key injection (S-06)."""
    try:
        row = conn.execute("SELECT value FROM settings WHERE key = 'llm_config'").fetchone()
        if not row or not row[0]:
            return
        data = json.loads(row[0])
        if not isinstance(data, dict):
            return
        changed = False
        if "api_key" in data and data["api_key"]:
            data["api_key"] = ""
            changed = True
        raw_url = str(data.get("base_url") or "").strip()
        if raw_url:
            try:
                validate_llm_base_url(raw_url)
            except ValueError:
                data["base_url"] = ""
                data["api_key_encrypted"] = ""
                data["enabled"] = False
                changed = True
        if changed:
            conn.execute(
                "UPDATE settings SET value = ?, updated_at = datetime('now', 'localtime') WHERE key = 'llm_config'",
                (json.dumps(data, ensure_ascii=False),),
            )
    except Exception:
        pass


@data_router.post("/backup")
def backup_database(db: sqlite3.Connection = Depends(get_db_conn)):
    """Create a consistent, secret-redacted SQLite snapshot using sqlite3 backup API (POST-only)."""
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    filename = f"billapp-backup-{timestamp}.db"

    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".db")
    os.close(tmp_fd)
    try:
        dst_conn = sqlite3.connect(tmp_path)
        try:
            db.backup(dst_conn)
            _redact_backup_secrets(dst_conn)
            dst_conn.commit()
            dst_conn.execute("PRAGMA journal_mode=DELETE;")
            dst_conn.execute("VACUUM;")
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


def _perform_database_restore(content: bytes, db: sqlite3.Connection) -> Tuple[int, int]:
    """Validate uploaded SQLite snapshot and restore atomically with automatic rollback on failure."""
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".db")
    os.close(tmp_fd)
    bak_fd, rollback_path = tempfile.mkstemp(suffix=".bak.db")
    os.close(bak_fd)
    src_conn: Optional[sqlite3.Connection] = None
    rollback_created = False

    try:
        Path(tmp_path).write_bytes(content)
        try:
            src_conn = sqlite3.connect(tmp_path)
            integrity = src_conn.execute("PRAGMA integrity_check;").fetchone()
            if not integrity or integrity[0] != "ok":
                raise ValidationError(message="备份文件完整性校验未通过")

            # Reject potentially malicious SQLite triggers, views, or virtual tables (S-06)
            master_rows = src_conn.execute(
                "SELECT type, name, sql FROM sqlite_master"
            ).fetchall()
            tables: set[str] = set()
            for obj_type, obj_name, obj_sql in master_rows:
                t_lower = str(obj_type or "").lower()
                sql_upper = str(obj_sql or "").upper()
                if t_lower in ("trigger", "view"):
                    raise ValidationError(
                        message=f"备份文件包含不允许的数据库对象 ({t_lower}: {obj_name})，已拒绝恢复"
                    )
                if "VIRTUAL TABLE" in sql_upper:
                    raise ValidationError(
                        message=f"备份文件包含不允许的虚拟表 ({obj_name})，已拒绝恢复"
                    )
                if t_lower == "table":
                    tables.add(str(obj_name))

            # Validate all 11 core tables and essential columns
            missing_tables = set(_REQUIRED_RESTORE_TABLES.keys()) - tables
            if missing_tables:
                raise ValidationError(
                    message=f"备份文件缺少核心数据表: {', '.join(sorted(missing_tables))}"
                )

            for tbl_name, required_cols in _REQUIRED_RESTORE_TABLES.items():
                col_rows = src_conn.execute(f"PRAGMA table_info({tbl_name})").fetchall()
                existing_cols = {str(r[1]) for r in col_rows}
                missing_cols = required_cols - existing_cols
                if missing_cols:
                    raise ValidationError(
                        message=f"备份文件表 {tbl_name} 缺少必要字段: {', '.join(sorted(missing_cols))}"
                    )

            # Create rollback snapshot of live database before overwriting
            db.commit()
            rb_conn = sqlite3.connect(rollback_path)
            try:
                db.backup(rb_conn)
                rb_conn.commit()
                rollback_created = True
            finally:
                rb_conn.close()

            # Overwrite target DB and run post-restore sanitization & migrations
            try:
                src_conn.backup(db)
                _sanitize_restored_settings(db)
                migrate_db(db)
                CategoryService.init_builtin_categories(db)
                db.commit()
            except Exception as restore_exc:
                # Roll back live DB automatically from pre-restore snapshot
                if rollback_created:
                    try:
                        rb_restore = sqlite3.connect(rollback_path)
                        try:
                            rb_restore.backup(db)
                            db.commit()
                        finally:
                            rb_restore.close()
                    except Exception:
                        pass
                raise ValidationError(message=f"数据库恢复失败并已自动回滚: {restore_exc}") from restore_exc
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

        txn_count = db.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
        acc_count = db.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
        return int(txn_count), int(acc_count)
    finally:
        for p in (tmp_path, rollback_path):
            try:
                os.unlink(p)
            except OSError:
                pass


@data_router.post("/restore", response_model=RestoreResponse)
async def restore_database(
    file: UploadFile = File(...),
    db: sqlite3.Connection = Depends(get_db_conn),
):
    """Validate and restore an uploaded SQLite backup database safely via sqlite3.backup."""
    content = await read_upload_limited(file, MAX_BACKUP_UPLOAD_BYTES)
    if len(content) < 100 or not content.startswith(b"SQLite format 3\x00"):
        raise ValidationError(message="上传文件不是有效的 SQLite 数据库备份")

    txn_count, acc_count = await anyio.to_thread.run_sync(_perform_database_restore, content, db)

    return RestoreResponse(
        success=True,
        transactions_count=txn_count,
        accounts_count=acc_count,
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
