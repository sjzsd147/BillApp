"""LLM Integration API endpoints (/api/llm/*)."""

from __future__ import annotations

import inspect
import sqlite3
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, Field

from billapp.app import get_db_conn
from billapp.classify.categories import CategoryService
from billapp.classify.rules import _get_val
from billapp.llm.client import (
    LLMClient,
    LLMConfig,
    normalize_url_origin,
    validate_llm_base_url,
)
from billapp.llm.prompts import collect_known_holder_names
from billapp.llm.tasks import classify_fallback_sync, review_classification_sync

router = APIRouter(prefix="/api/llm", tags=["llm"])


# =========================================================================
# LLM DTO Schemas
# =========================================================================

class LLMClassifyRequest(BaseModel):
    batch_id: Optional[int] = None
    transaction_ids: Optional[List[int]] = None
    limit: int = Field(40, ge=1, le=100)
    holder_name: Optional[str] = None
    update_db: bool = True


class LLMClassifyItemOut(BaseModel):
    transaction_id: int
    category_id: Optional[int] = None
    category_key: Optional[str] = None
    confidence: float
    reason: str
    updated: bool = False


class LLMClassifyResponse(BaseModel):
    success: bool
    status: str  # "ok" | "offline" | "error"
    classified_count: int
    results: List[LLMClassifyItemOut]
    reason: Optional[str] = None


class LLMReviewRequest(BaseModel):
    batch_id: Optional[int] = None
    transaction_ids: Optional[List[int]] = None
    max_confidence: float = Field(0.85, ge=0.0, le=1.0)
    limit: int = Field(50, ge=1, le=100)
    holder_name: Optional[str] = None


class LLMReviewItemOut(BaseModel):
    id: int
    agree: bool
    suggested_category_id: Optional[int] = None
    suggested_category_key: Optional[str] = None
    confidence: float
    reason: str


class LLMReviewResponse(BaseModel):
    success: bool
    status: str  # "ok" | "offline" | "error"
    reviews: List[LLMReviewItemOut]
    reason: Optional[str] = None


class LLMTestRequest(BaseModel):
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    model: Optional[str] = None
    timeout_sec: float = 10.0


class LLMTestResponse(BaseModel):
    ok: bool
    latency_ms: int = 0
    model: str = ""
    error: Optional[str] = None


class LLMModelsRequest(BaseModel):
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    timeout_sec: float = 10.0


class LLMModelInfo(BaseModel):
    id: str
    name: Optional[str] = None
    owned_by: Optional[str] = None
    description: Optional[str] = None


class LLMModelsResponse(BaseModel):
    ok: bool
    models: List[LLMModelInfo] = Field(default_factory=list)
    error: Optional[str] = None


class LLMStatusResponse(BaseModel):
    configured: bool
    enabled: bool
    base_url: str
    model: str
    cache_count: int = 0


# =========================================================================
# Endpoints
# =========================================================================

@router.post("/classify", response_model=LLMClassifyResponse)
def classify_with_llm(
    payload: LLMClassifyRequest,
    db: sqlite3.Connection = Depends(get_db_conn),
):
    """Run fallback LLM classification on unclassified transactions with offline degradation."""
    config = LLMConfig.load_from_db(db)
    client = LLMClient(config, conn=db)

    if not client.is_available():
        return LLMClassifyResponse(
            success=False,
            status="offline",
            classified_count=0,
            results=[],
            reason="AI 服务未启用或未配置 Base URL，请先在设置中启用并保存配置。",
        )

    if not config.task_classify_enabled:
        return LLMClassifyResponse(
            success=False,
            status="disabled",
            classified_count=0,
            results=[],
            reason="已在设置中关闭「未知交易兜底智能分类」任务开关。",
        )

    # Resolve target transactions
    query = """
        SELECT * FROM transactions
        WHERE category_id IS NULL
          AND COALESCE(category_source, '') != 'manual'
          AND duplicate_of IS NULL
    """
    params: List[Any] = []
    if payload.transaction_ids:
        placeholders = ",".join("?" for _ in payload.transaction_ids)
        query = f"SELECT * FROM transactions WHERE id IN ({placeholders})"
        params = list(payload.transaction_ids)
    elif payload.batch_id:
        query += " AND batch_id = ?"
        params.append(payload.batch_id)

    query += f" ORDER BY id DESC LIMIT {payload.limit}"
    txns = db.execute(query, params).fetchall()

    if not txns:
        return LLMClassifyResponse(
            success=True,
            status="ok",
            classified_count=0,
            results=[],
            reason="No pending transactions found.",
        )

    all_categories = CategoryService.list_all(db)
    key_to_id = {c.key: c.id for c in all_categories if c.key}
    name_to_id = {c.name: c.id for c in all_categories if c.name}
    cat_tree = CategoryService.get_tree(db)
    live_taxonomy = [
        {
            "key": r.key or str(r.id),
            "name": r.name,
            "direction": r.direction,
            "subcategories": [
                {"key": c.key or str(c.id), "name": c.name}
                for c in r.children
            ],
        }
        for r in cat_tree
    ]

    holder_names = collect_known_holder_names(db, payload.holder_name)
    try:
        extra_kwargs: Dict[str, Any] = {
            "key_to_id": key_to_id,
            "holder_name": holder_names or payload.holder_name,
            "taxonomy": live_taxonomy or None,
            "name_to_id": name_to_id,
            "raise_on_error": True,
        }
        sig = inspect.signature(classify_fallback_sync)
        accepts_var_kw = any(
            p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
        )
        if not accepts_var_kw:
            extra_kwargs = {
                k: v for k, v in extra_kwargs.items() if k in sig.parameters
            }
        class_results = classify_fallback_sync(txns, client, **extra_kwargs)
    except Exception as exc:
        return LLMClassifyResponse(
            success=False,
            status="error",
            classified_count=0,
            results=[],
            reason=f"LLM request error: {exc}",
        )

    out_items: List[LLMClassifyItemOut] = []
    updated_count = 0

    id_to_key = {c.id: c.key for c in all_categories if c.key}

    for txn, res in zip(txns, class_results):
        txn_id = _get_val(txn, "id")
        cat_id = res.category_id
        conf = res.confidence
        rsn = res.reason or ""

        updated = False
        if payload.update_db and cat_id is not None and conf >= 0.70 and txn_id:
            db.execute(
                """
                UPDATE transactions
                SET category_id = ?, category_source = 'llm', category_confidence = ?
                WHERE id = ? AND COALESCE(category_source, '') != 'manual'
                """,
                (cat_id, conf, txn_id),
            )
            updated = True
            updated_count += 1

        out_items.append(
            LLMClassifyItemOut(
                transaction_id=txn_id or 0,
                category_id=cat_id,
                category_key=id_to_key.get(cat_id) if cat_id else None,
                confidence=conf,
                reason=rsn,
                updated=updated,
            )
        )

    if updated_count > 0:
        db.commit()

    return LLMClassifyResponse(
        success=True,
        status="ok",
        classified_count=updated_count,
        results=out_items,
    )


@router.post("/review", response_model=LLMReviewResponse)
def review_with_llm(
    payload: LLMReviewRequest,
    db: sqlite3.Connection = Depends(get_db_conn),
):
    """Review low-confidence classifications with LLM."""
    config = LLMConfig.load_from_db(db)
    client = LLMClient(config, conn=db)

    if not client.is_available():
        return LLMReviewResponse(
            success=False,
            status="offline",
            reviews=[],
            reason="LLM is not enabled or configured.",
        )

    query = """
        SELECT t.*, c.key AS category_key, c.name AS category_name
        FROM transactions t
        LEFT JOIN categories c ON t.category_id = c.id
        WHERE t.category_id IS NOT NULL AND t.category_confidence <= ?
    """
    params: List[Any] = [payload.max_confidence]

    if payload.transaction_ids:
        placeholders = ",".join("?" for _ in payload.transaction_ids)
        query += f" AND t.id IN ({placeholders})"
        params.extend(payload.transaction_ids)
    elif payload.batch_id:
        query += " AND t.batch_id = ?"
        params.append(payload.batch_id)

    query += f" ORDER BY t.category_confidence ASC LIMIT {payload.limit}"
    txns = db.execute(query, params).fetchall()

    if not txns:
        return LLMReviewResponse(
            success=True,
            status="ok",
            reviews=[],
            reason="No low-confidence transactions found.",
        )

    all_categories = CategoryService.list_all(db)
    key_to_id = {c.key: c.id for c in all_categories if c.key}

    holder_names = collect_known_holder_names(db, payload.holder_name)
    try:
        review_results = review_classification_sync(
            txns, client, key_to_id=key_to_id, holder_name=holder_names or payload.holder_name
        )
    except Exception as exc:
        return LLMReviewResponse(
            success=False,
            status="error",
            reviews=[],
            reason=f"LLM review error: {exc}",
        )

    out_reviews = [
        LLMReviewItemOut(
            id=r.id,
            agree=r.agree,
            suggested_category_id=r.suggested_category_id,
            suggested_category_key=r.suggested_category_key,
            confidence=r.confidence,
            reason=r.reason,
        )
        for r in review_results
    ]

    return LLMReviewResponse(
        success=True,
        status="ok",
        reviews=out_reviews,
    )


@router.post("/test", response_model=LLMTestResponse)
def test_llm_connection(
    payload: LLMTestRequest,
    db: sqlite3.Connection = Depends(get_db_conn),
):
    """Test connectivity to configured LLM endpoint via lightweight probe."""
    config = LLMConfig.load_from_db(db)
    saved_base_url = config.base_url.strip()
    had_saved_key = bool(config.api_key.strip())

    if payload.base_url:
        req_url = payload.base_url.strip()
        try:
            validate_llm_base_url(req_url)
        except ValueError as exc:
            return LLMTestResponse(ok=False, error=str(exc))

        # Prevent exfiltrating persisted API key to a different base_url origin (S-02)
        if not payload.api_key and had_saved_key:
            if not saved_base_url or normalize_url_origin(req_url) != normalize_url_origin(saved_base_url):
                return LLMTestResponse(
                    ok=False,
                    error="检测到 API 服务地址 (Base URL) 与已保存地址不一致，出于安全保护禁止复用原 API Key，请重新输入 API Key 后再测试",
                )
        config.base_url = req_url

    if payload.api_key:
        config.api_key = payload.api_key
    if payload.model:
        config.model = payload.model
    if payload.timeout_sec:
        config.timeout_sec = payload.timeout_sec

    if not config.base_url.strip():
        return LLMTestResponse(ok=False, error="Base URL not configured")

    try:
        validate_llm_base_url(config.base_url)
    except ValueError as exc:
        return LLMTestResponse(ok=False, error=str(exc))

    client = LLMClient(config, conn=db)
    result = client.test_connection()
    return LLMTestResponse(
        ok=result.get("ok", False),
        latency_ms=result.get("latency_ms", 0),
        model=result.get("model", config.model),
        error=result.get("error"),
    )


@router.post("/models", response_model=LLMModelsResponse)
def list_llm_models(
    payload: LLMModelsRequest,
    db: sqlite3.Connection = Depends(get_db_conn),
):
    """Fetch available model list from the configured OpenAI-compatible endpoint."""
    config = LLMConfig.load_from_db(db)
    saved_base_url = config.base_url.strip()
    had_saved_key = bool(config.api_key.strip())

    if payload.base_url:
        req_url = payload.base_url.strip()
        try:
            validate_llm_base_url(req_url)
        except ValueError as exc:
            return LLMModelsResponse(ok=False, error=str(exc))

        # Prevent exfiltrating persisted API key to a different base_url origin (S-02)
        if not payload.api_key and had_saved_key:
            if not saved_base_url or normalize_url_origin(req_url) != normalize_url_origin(saved_base_url):
                return LLMModelsResponse(
                    ok=False,
                    error="检测到 API 服务地址 (Base URL) 与已保存地址不一致，出于安全保护禁止复用原 API Key，请重新输入 API Key 后再获取模型列表",
                )
        config.base_url = req_url

    if payload.api_key:
        config.api_key = payload.api_key
    if payload.timeout_sec:
        config.timeout_sec = payload.timeout_sec

    if not config.base_url.strip():
        return LLMModelsResponse(ok=False, error="Base URL not configured")

    try:
        validate_llm_base_url(config.base_url)
    except ValueError as exc:
        return LLMModelsResponse(ok=False, error=str(exc))

    client = LLMClient(config, conn=db)
    result = client.list_models()
    models = [
        LLMModelInfo(
            id=item["id"],
            name=item.get("name") or item["id"],
            owned_by=item.get("owned_by") or None,
            description=item.get("description") or None,
        )
        for item in result.get("models", [])
    ]
    return LLMModelsResponse(
        ok=result.get("ok", False),
        models=models,
        error=result.get("error"),
    )


@router.get("/status", response_model=LLMStatusResponse)
def get_llm_status(
    db: sqlite3.Connection = Depends(get_db_conn),
):
    """Get LLM service readiness status and cache statistics."""
    config = LLMConfig.load_from_db(db)
    cache_count = db.execute("SELECT COUNT(*) FROM llm_cache").fetchone()[0]

    return LLMStatusResponse(
        configured=bool(config.base_url.strip() and config.api_key.strip()),
        enabled=config.enabled,
        base_url=config.base_url,
        model=config.model,
        cache_count=cache_count,
    )
