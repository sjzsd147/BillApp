"""Bank Statement YAML Profiles API endpoints (/api/profiles/*)."""

from __future__ import annotations

from datetime import datetime
import re
import sqlite3
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from pydantic import BaseModel, Field

from billapp.app import AppException, ConflictError, NotFoundError, ValidationError, get_db_conn
from billapp.importers.base import FileProbe
from billapp.importers.profile_engine import (
    BankProfile,
    ProfileImporter,
    extract_preamble_metadata_fallback,
)
from billapp.llm.client import LLMClient, LLMConfig
from billapp.llm.tasks import suggest_profile_mapping_sync

router = APIRouter(prefix="/api/profiles", tags=["profiles"])


# =========================================================================
# Profile DTO Schemas
# =========================================================================

class ProfileSummary(BaseModel):
    id: str
    name: str
    bank: str
    account_kind: str
    file_type: str
    is_builtin: bool
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class ProfileDetail(ProfileSummary):
    yaml_content: str
    parsed_config: Dict[str, Any]


class ProfileCreateRequest(BaseModel):
    id: str
    name: str
    yaml_content: str


class ProfileUpdateRequest(BaseModel):
    name: Optional[str] = None
    yaml_content: str


class ProfileTestResponse(BaseModel):
    success: bool
    parsed_count: int
    sample_transactions: List[Dict[str, Any]]
    self_checks: List[Dict[str, Any]]
    warnings: List[str]
    errors: List[str] = Field(default_factory=list)


class SuggestMappingRequest(BaseModel):
    headers: List[str]
    sample_rows: List[List[str]] = Field(default_factory=list)
    preamble_rows: List[List[str]] = Field(default_factory=list)
    file_name: Optional[str] = None
    file_type: str = "csv"


# =========================================================================
# Endpoints
# =========================================================================

@router.get("", response_model=List[ProfileSummary])
def list_profiles(
    db: sqlite3.Connection = Depends(get_db_conn),
):
    """List all saved bank profiles."""
    rows = db.execute(
        "SELECT id, name, yaml_content, is_builtin, created_at, updated_at FROM bank_profiles ORDER BY is_builtin DESC, id ASC"
    ).fetchall()
    summaries: List[ProfileSummary] = []
    for r in rows:
        try:
            p = BankProfile.from_yaml(r["yaml_content"])
            summaries.append(
                ProfileSummary(
                    id=r["id"],
                    name=r["name"],
                    bank=p.bank,
                    account_kind=p.account_kind,
                    file_type=p.file_type,
                    is_builtin=bool(r["is_builtin"]),
                    created_at=r["created_at"],
                    updated_at=r["updated_at"],
                )
            )
        except Exception:
            continue
    return summaries


@router.get("/{profile_id}", response_model=ProfileDetail)
def get_profile(
    profile_id: str,
    db: sqlite3.Connection = Depends(get_db_conn),
):
    """Get complete YAML content and parsed configuration for a profile."""
    row = db.execute("SELECT * FROM bank_profiles WHERE id = ?", (profile_id,)).fetchone()
    if not row:
        raise NotFoundError(f"未找到银行配置: {profile_id}")
    try:
        p = BankProfile.from_yaml(row["yaml_content"])
    except Exception as exc:
        raise AppException(status_code=500, code="PROFILE_CORRUPT", message=f"配置解析失败: {exc}")

    return ProfileDetail(
        id=row["id"],
        name=row["name"],
        bank=p.bank,
        account_kind=p.account_kind,
        file_type=p.file_type,
        is_builtin=bool(row["is_builtin"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        yaml_content=row["yaml_content"],
        parsed_config=p.model_dump(),
    )


@router.post("", response_model=ProfileSummary, status_code=status.HTTP_201_CREATED)
def create_profile(
    payload: ProfileCreateRequest,
    db: sqlite3.Connection = Depends(get_db_conn),
):
    """Create a new custom bank statement profile."""
    try:
        p = BankProfile.from_yaml(payload.yaml_content)
    except Exception as exc:
        raise ValidationError(f"YAML配置解析失败: {exc}")

    existing = db.execute("SELECT id FROM bank_profiles WHERE id = ?", (payload.id,)).fetchone()
    if existing:
        raise ConflictError(f"Profile ID '{payload.id}' 已存在")

    now_iso = datetime.now().isoformat()
    db.execute(
        """
        INSERT INTO bank_profiles (id, name, yaml_content, is_builtin, created_at, updated_at)
        VALUES (?, ?, ?, 0, ?, ?)
        """,
        (payload.id, payload.name, payload.yaml_content, now_iso, now_iso),
    )

    return ProfileSummary(
        id=payload.id,
        name=payload.name,
        bank=p.bank,
        account_kind=p.account_kind,
        file_type=p.file_type,
        is_builtin=False,
        created_at=now_iso,
        updated_at=now_iso,
    )


@router.put("/{profile_id}", response_model=ProfileSummary)
def update_profile(
    profile_id: str,
    payload: ProfileUpdateRequest,
    db: sqlite3.Connection = Depends(get_db_conn),
):
    """Update a custom bank statement profile."""
    row = db.execute("SELECT is_builtin FROM bank_profiles WHERE id = ?", (profile_id,)).fetchone()
    if not row:
        raise NotFoundError(f"未找到银行配置: {profile_id}")
    if row["is_builtin"]:
        raise AppException(status_code=403, code="BUILTIN_IMMUTABLE", message="系统内置银行配置不允许修改")

    try:
        p = BankProfile.from_yaml(payload.yaml_content)
    except Exception as exc:
        raise ValidationError(f"YAML配置解析失败: {exc}")

    name = payload.name or p.name
    now_iso = datetime.now().isoformat()

    db.execute(
        """
        UPDATE bank_profiles
        SET name = ?, yaml_content = ?, updated_at = ?
        WHERE id = ?
        """,
        (name, payload.yaml_content, now_iso, profile_id),
    )

    return ProfileSummary(
        id=profile_id,
        name=name,
        bank=p.bank,
        account_kind=p.account_kind,
        file_type=p.file_type,
        is_builtin=False,
        updated_at=now_iso,
    )


@router.delete("/{profile_id}")
def delete_profile(
    profile_id: str,
    db: sqlite3.Connection = Depends(get_db_conn),
):
    """Delete a custom bank statement profile."""
    row = db.execute("SELECT is_builtin FROM bank_profiles WHERE id = ?", (profile_id,)).fetchone()
    if not row:
        raise NotFoundError(f"未找到银行配置: {profile_id}")
    if row["is_builtin"]:
        raise AppException(status_code=400, code="CANNOT_DELETE_BUILTIN", message="系统内置银行配置不允许删除")

    db.execute("DELETE FROM bank_profiles WHERE id = ?", (profile_id,))
    return {"status": "deleted", "id": profile_id}


@router.post("/test", response_model=ProfileTestResponse)
async def test_profile(
    file: UploadFile = File(...),
    yaml_content: Optional[str] = Form(None),
    profile_id: Optional[str] = Form(None),
    db: sqlite3.Connection = Depends(get_db_conn),
):
    """Dry-run test statement parsing with a bank profile."""
    if not yaml_content and not profile_id:
        raise ValidationError("必须提供 yaml_content 或 profile_id")

    if profile_id and not yaml_content:
        row = db.execute("SELECT yaml_content FROM bank_profiles WHERE id = ?", (profile_id,)).fetchone()
        if not row:
            raise NotFoundError(f"未找到配置: {profile_id}")
        yaml_content = row["yaml_content"]

    try:
        profile = BankProfile.from_yaml(yaml_content or "")
    except Exception as exc:
        return ProfileTestResponse(
            success=False,
            parsed_count=0,
            sample_transactions=[],
            self_checks=[],
            warnings=[],
            errors=[f"YAML配置错误: {exc}"],
        )

    content = await file.read()
    probe = FileProbe(content, file_name=file.filename or "")
    importer = ProfileImporter(profile)

    try:
        result = importer.parse(probe)
        return ProfileTestResponse(
            success=True,
            parsed_count=len(result.transactions),
            sample_transactions=[t.model_dump() for t in result.transactions[:20]],
            self_checks=[sc.model_dump() for sc in result.self_checks],
            warnings=result.warnings,
            errors=[],
        )
    except Exception as exc:
        return ProfileTestResponse(
            success=False,
            parsed_count=0,
            sample_transactions=[],
            self_checks=[],
            warnings=[],
            errors=[f"试解析异常: {exc}"],
        )


@router.post("/suggest")
def suggest_mapping(
    payload: SuggestMappingRequest,
    db: sqlite3.Connection = Depends(get_db_conn),
):
    """Suggest column mappings via LLM (if enabled) with heuristic fallback and explicit status."""
    headers = payload.headers
    mapping: Dict[str, Any] = {}
    mode = "signed"

    exp_col, inc_col, dir_col = None, None, None
    for h in headers:
        clean_h = h.strip()
        if re.search(r"时间|日期|交易日|记账|年月日|时刻", clean_h) and "date" not in mapping:
            mapping["date"] = h
        elif re.search(r"支出|借方|扣款", clean_h):
            exp_col = h
        elif re.search(r"收入|贷方|存入", clean_h):
            inc_col = h
        elif re.search(r"收支|方向|借贷", clean_h):
            dir_col = h
        elif re.search(r"金额|交易金额|发生额|款项", clean_h) and "amount" not in mapping:
            mapping["amount"] = h
        elif re.search(r"对方|对手|商户|户名|交易对象", clean_h) and "counterparty" not in mapping:
            mapping["counterparty"] = h
        elif re.search(r"说明|摘要|商品|备注|附言", clean_h) and "description" not in mapping:
            mapping["description"] = h
        elif re.search(r"支付方式|渠道|交易类型", clean_h) and "channel" not in mapping:
            mapping["channel"] = h
        elif re.search(r"余额|账户余额|余留", clean_h) and "balance" not in mapping:
            mapping["balance"] = h
        elif re.search(r"卡号|账号|尾号", clean_h) and "card_tail" not in mapping:
            mapping["card_tail"] = h

    if exp_col and inc_col:
        mode = "split"
        mapping["expense_amount"] = exp_col
        mapping["income_amount"] = inc_col
    elif dir_col:
        mode = "flag"
        mapping["direction"] = dir_col

    source_kind = "heuristic"
    llm_status = "offline"
    llm_error: Optional[str] = None
    confidence = 0.0
    reason = ""
    bank: Optional[str] = None
    account_kind: Optional[str] = None
    card_tail: Optional[str] = None
    currency: Optional[str] = None
    flag_expense_val: Optional[str] = None
    flag_income_val: Optional[str] = None

    preamble_lines = [
        " ".join(str(c).strip() for c in r if c is not None and str(c).strip())
        for r in (payload.preamble_rows or [])[:20]
    ]
    if payload.file_name:
        preamble_lines.append(str(payload.file_name))
    fb_meta = extract_preamble_metadata_fallback("\n".join(preamble_lines))

    llm_config = LLMConfig.load_from_db(db)
    # Interactive schema suggestion with reasoning/chat models frequently needs 20-40s;
    # enforce a safe floor so legacy 10s/20s DB defaults do not prematurely time out.
    if float(llm_config.timeout_sec or 0) < 60.0:
        llm_config.timeout_sec = 60.0
    llm_config.max_retries = min(llm_config.max_retries, 1)
    client = LLMClient(llm_config, conn=db)
    if not client.is_available():
        llm_status = "offline"
        llm_error = "AI 服务未启用或未配置 Base URL"
    elif not llm_config.task_suggest_profile_enabled:
        llm_status = "disabled"
        llm_error = "已在设置中关闭「未知账单表头列名智能推导」任务开关"
    else:
        llm_res = suggest_profile_mapping_sync(
            headers,
            payload.sample_rows,
            client,
            preamble_rows=payload.preamble_rows,
            file_name=payload.file_name,
        )
        if llm_res.error:
            llm_status = "error"
            llm_error = llm_res.error
        elif llm_res.columns:
            # Replace conflicting mode keys when LLM overrides amount_mode
            effective_mode = llm_res.amount_mode if llm_res.amount_mode in ("signed", "split", "flag") else mode
            if effective_mode == "split":
                mapping.pop("amount", None)
                mapping.pop("direction", None)
            elif effective_mode == "signed":
                mapping.pop("expense_amount", None)
                mapping.pop("income_amount", None)
                mapping.pop("direction", None)
            elif effective_mode == "flag":
                mapping.pop("expense_amount", None)
                mapping.pop("income_amount", None)
            mapping.update(llm_res.columns)
            if llm_res.amount_mode in ("signed", "split", "flag"):
                mode = llm_res.amount_mode
            source_kind = "llm"
            llm_status = "ok"
            confidence = llm_res.confidence
            reason = llm_res.reason
            bank = llm_res.bank
            account_kind = llm_res.account_kind
            card_tail = llm_res.card_tail
            currency = llm_res.currency
            flag_expense_val = llm_res.flag_expense_val
            flag_income_val = llm_res.flag_income_val
        else:
            llm_status = "error"
            llm_error = "AI 未能在当前表头中识别出可映射列"

    if not bank:
        bank = fb_meta.get("bank")
    if not account_kind:
        account_kind = fb_meta.get("account_kind")
    if not card_tail:
        card_tail = fb_meta.get("card_tail")
    if not currency:
        currency = fb_meta.get("currency")

    return {
        "amount_mode": mode,
        "column_mapping": mapping,
        "headers": headers,
        "source": source_kind,
        "llm_status": llm_status,
        "llm_error": llm_error,
        "confidence": confidence,
        "reason": reason,
        "bank": bank,
        "account_kind": account_kind,
        "card_tail": card_tail,
        "currency": currency,
        "flag_expense_val": flag_expense_val,
        "flag_income_val": flag_income_val,
    }
