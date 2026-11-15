"""Classification rules engine API endpoints (/api/rules/*)."""

from __future__ import annotations

import re
import sqlite3
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from billapp.app import AppException, NotFoundError, ValidationError, get_db_conn
from billapp.classify.categories import CategoryService
from billapp.classify.rules import (
    Rule,
    apply_rule_to_existing,
    compile_safe_regex,
    get_learning_suggestions,
    load_builtin_rules_from_yaml,
    rule_matches,
)

router = APIRouter(prefix="/api/rules", tags=["rules"])


# =========================================================================
# Rule DTO Schemas
# =========================================================================

class RuleOut(BaseModel):
    id: int
    name: str
    category_id: int
    category_name: Optional[str] = None
    category_key: Optional[str] = None
    priority: int = 100
    field: str
    pattern_type: str
    pattern_value: str
    amount_min_cents: Optional[int] = None
    amount_max_cents: Optional[int] = None
    is_builtin: bool = False
    hit_count: int = 0
    created_at: Optional[str] = None


class RuleCreateRequest(BaseModel):
    name: Optional[str] = None
    category_id: int
    priority: int = Field(100, ge=0, le=999)
    field: Literal["counterparty", "merchant_norm", "description", "platform_category", "pay_method"] = "counterparty"
    pattern_type: Literal["contains", "exact", "prefix", "regex"] = "contains"
    pattern_value: str = Field(..., min_length=1, max_length=200)
    amount_min_cents: Optional[int] = Field(None, ge=0)
    amount_max_cents: Optional[int] = Field(None, ge=0)


class RuleUpdateRequest(BaseModel):
    name: Optional[str] = None
    category_id: Optional[int] = None
    priority: Optional[int] = Field(None, ge=0, le=999)
    field: Optional[Literal["counterparty", "merchant_norm", "description", "platform_category", "pay_method"]] = None
    pattern_type: Optional[Literal["contains", "exact", "prefix", "regex"]] = None
    pattern_value: Optional[str] = Field(None, min_length=1, max_length=200)
    amount_min_cents: Optional[int] = Field(None, ge=0)
    amount_max_cents: Optional[int] = Field(None, ge=0)


class MatchedTxnPreview(BaseModel):
    id: int
    txn_time: str
    counterparty: Optional[str] = None
    merchant_norm: Optional[str] = None
    description: Optional[str] = None
    amount_cents: int
    current_category_id: Optional[int] = None
    current_category_name: Optional[str] = None
    category_source: Optional[str] = None


class RuleTestRequest(BaseModel):
    field: Literal["counterparty", "merchant_norm", "description", "platform_category", "pay_method"] = "counterparty"
    pattern_type: Literal["contains", "exact", "prefix", "regex"] = "contains"
    pattern_value: str = Field(..., min_length=1, max_length=200)
    amount_min_cents: Optional[int] = None
    amount_max_cents: Optional[int] = None
    category_id: Optional[int] = None
    limit: int = Field(20, ge=1, le=100)
    exclude_manual: bool = True


class RuleTestResponse(BaseModel):
    matched_count: int
    manual_skipped_count: int
    preview: List[MatchedTxnPreview]


class RuleApplyProposalRequest(BaseModel):
    name: Optional[str] = None
    category_id: int
    field: Literal["counterparty", "merchant_norm", "description", "platform_category", "pay_method"] = "counterparty"
    pattern_type: Literal["contains", "exact", "prefix", "regex"] = "contains"
    pattern_value: str = Field(..., min_length=1, max_length=200)
    priority: int = 100
    apply_to_existing: bool = True
    overwrite_manual: bool = False


class RuleApplyProposalResponse(BaseModel):
    rule: RuleOut
    matched_count: int = 0
    updated_count: int = 0
    skipped_manual_count: int = 0


class RuleSuggestionOut(BaseModel):
    brand: str
    category_id: int
    category_name: str
    count: int
    sample_counterparty: str


# =========================================================================
# Endpoints
# =========================================================================

@router.get("", response_model=List[RuleOut])
def list_rules(
    category_id: Optional[int] = Query(None),
    include_builtin: bool = Query(True),
    db: sqlite3.Connection = Depends(get_db_conn),
):
    """List classification rules (user rules + optionally builtin rules)."""
    where_sql = "1=1"
    params: List[Any] = []
    if category_id is not None:
        where_sql += " AND r.category_id = ?"
        params.append(category_id)

    query = f"""
        SELECT r.*, c.name AS category_name, c.key AS category_key
        FROM rules r
        LEFT JOIN categories c ON r.category_id = c.id
        WHERE {where_sql}
        ORDER BY r.priority ASC, r.id ASC
    """
    rows = db.execute(query, params).fetchall()
    user_rules = [
        RuleOut(
            id=r["id"],
            name=r["name"],
            category_id=r["category_id"],
            category_name=r["category_name"],
            category_key=r["category_key"],
            priority=r["priority"],
            field=r["field"],
            pattern_type=r["pattern_type"],
            pattern_value=r["pattern_value"],
            amount_min_cents=r["amount_min_cents"],
            amount_max_cents=r["amount_max_cents"],
            is_builtin=bool(r["is_builtin"]),
            hit_count=r["hit_count"],
            created_at=r["created_at"],
        )
        for r in rows
    ]

    if not include_builtin:
        return user_rules

    # Load builtin rules from yaml
    builtin_rules = load_builtin_rules_from_yaml(conn=db)
    builtin_outs: List[RuleOut] = []

    cat_lookup = {
        c.id: c.name for c in CategoryService.list_all(db)
    }

    for b in builtin_rules:
        if category_id is not None and b.category_id != category_id:
            continue
        builtin_outs.append(
            RuleOut(
                id=100000 + (b.id or 0),
                name=b.name,
                category_id=b.category_id,
                category_name=cat_lookup.get(b.category_id),
                category_key=b.subcategory_key or b.category_key,
                priority=b.priority,
                field=b.field,
                pattern_type=b.pattern_type,
                pattern_value=b.pattern_value,
                amount_min_cents=b.amount_min_cents,
                amount_max_cents=b.amount_max_cents,
                is_builtin=True,
                hit_count=b.hit_count,
                created_at="builtin",
            )
        )

    all_rules = user_rules + builtin_outs
    all_rules.sort(key=lambda x: (x.priority, x.id))
    return all_rules


def _validate_regex_pattern(pattern_val: str) -> None:
    """Validate regex pattern, converting ReDoS and syntax errors into ValidationError (422)."""
    try:
        rx = compile_safe_regex(pattern_val, raise_on_error=True)
        if rx is None:
            raise ValidationError("正则表达式不合法或存在潜在灾难性回溯 (ReDoS)")
    except (ValueError, re.error) as err:
        raise ValidationError(f"正则表达式不合法或存在潜在灾难性回溯 (ReDoS): {err}")


@router.post("", response_model=RuleOut, status_code=status.HTTP_201_CREATED)
def create_rule(
    payload: RuleCreateRequest,
    db: sqlite3.Connection = Depends(get_db_conn),
):
    """Create a new user rule with safe regex validation."""
    cat = CategoryService.get_by_id(db, payload.category_id)
    if not cat:
        raise NotFoundError(f"分类 #{payload.category_id} 不存在")

    if payload.pattern_type == "regex":
        _validate_regex_pattern(payload.pattern_value)

    rule_name = payload.name
    if not rule_name:
        rule_name = f"{payload.pattern_value} -> {cat.name}"

    cur = db.execute(
        """
        INSERT INTO rules (name, category_id, priority, field, pattern_type, pattern_value,
                           amount_min_cents, amount_max_cents, is_builtin, hit_count, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 0, datetime('now', 'localtime'))
        """,
        (
            rule_name,
            payload.category_id,
            payload.priority,
            payload.field,
            payload.pattern_type,
            payload.pattern_value,
            payload.amount_min_cents,
            payload.amount_max_cents,
        ),
    )
    new_id = cur.lastrowid
    row = db.execute("SELECT * FROM rules WHERE id = ?", (new_id,)).fetchone()

    return RuleOut(
        id=row["id"],
        name=row["name"],
        category_id=row["category_id"],
        category_name=cat.name,
        category_key=cat.key,
        priority=row["priority"],
        field=row["field"],
        pattern_type=row["pattern_type"],
        pattern_value=row["pattern_value"],
        amount_min_cents=row["amount_min_cents"],
        amount_max_cents=row["amount_max_cents"],
        is_builtin=False,
        hit_count=0,
        created_at=row["created_at"],
    )


@router.put("/{rule_id}", response_model=RuleOut)
def update_rule(
    rule_id: int,
    payload: RuleUpdateRequest,
    db: sqlite3.Connection = Depends(get_db_conn),
):
    """Update user rule metadata and pattern."""
    existing = db.execute("SELECT * FROM rules WHERE id = ?", (rule_id,)).fetchone()
    if not existing:
        raise NotFoundError(f"规则 #{rule_id} 不存在")
    if existing["is_builtin"]:
        raise AppException(
            status_code=403,
            code="BUILTIN_RULE_IMMUTABLE",
            message="内置规则不允许直接修改，请新建用户规则覆盖",
        )

    if payload.category_id is not None:
        cat = CategoryService.get_by_id(db, payload.category_id)
        if not cat:
            raise NotFoundError(f"分类 #{payload.category_id} 不存在")

    pattern_type = payload.pattern_type or existing["pattern_type"]
    pattern_val = payload.pattern_value if payload.pattern_value is not None else existing["pattern_value"]

    if pattern_type == "regex":
        _validate_regex_pattern(pattern_val)

    update_fields = []
    update_vals: List[Any] = []

    if payload.name is not None:
        update_fields.append("name = ?")
        update_vals.append(payload.name)
    if payload.category_id is not None:
        update_fields.append("category_id = ?")
        update_vals.append(payload.category_id)
    if payload.priority is not None:
        update_fields.append("priority = ?")
        update_vals.append(payload.priority)
    if payload.field is not None:
        update_fields.append("field = ?")
        update_vals.append(payload.field)
    if payload.pattern_type is not None:
        update_fields.append("pattern_type = ?")
        update_vals.append(payload.pattern_type)
    if payload.pattern_value is not None:
        update_fields.append("pattern_value = ?")
        update_vals.append(payload.pattern_value)
    if payload.amount_min_cents is not None:
        update_fields.append("amount_min_cents = ?")
        update_vals.append(payload.amount_min_cents)
    if payload.amount_max_cents is not None:
        update_fields.append("amount_max_cents = ?")
        update_vals.append(payload.amount_max_cents)

    if update_fields:
        update_vals.append(rule_id)
        db.execute(f"UPDATE rules SET {', '.join(update_fields)} WHERE id = ?", update_vals)

    row = db.execute(
        """
        SELECT r.*, c.name AS category_name, c.key AS category_key
        FROM rules r
        LEFT JOIN categories c ON r.category_id = c.id
        WHERE r.id = ?
        """,
        (rule_id,),
    ).fetchone()

    return RuleOut(
        id=row["id"],
        name=row["name"],
        category_id=row["category_id"],
        category_name=row["category_name"],
        category_key=row["category_key"],
        priority=row["priority"],
        field=row["field"],
        pattern_type=row["pattern_type"],
        pattern_value=row["pattern_value"],
        amount_min_cents=row["amount_min_cents"],
        amount_max_cents=row["amount_max_cents"],
        is_builtin=False,
        hit_count=row["hit_count"],
        created_at=row["created_at"],
    )


@router.delete("/{rule_id}")
def delete_rule(
    rule_id: int,
    db: sqlite3.Connection = Depends(get_db_conn),
):
    """Delete a user custom rule."""
    existing = db.execute("SELECT is_builtin FROM rules WHERE id = ?", (rule_id,)).fetchone()
    if not existing:
        raise NotFoundError(f"规则 #{rule_id} 不存在")
    if existing["is_builtin"]:
        raise AppException(
            status_code=403,
            code="BUILTIN_RULE_IMMUTABLE",
            message="内置规则不允许删除",
        )

    db.execute("DELETE FROM rules WHERE id = ?", (rule_id,))
    return {"ok": True, "deleted_id": rule_id}


@router.post("/test", response_model=RuleTestResponse)
def test_rule(
    payload: RuleTestRequest,
    db: sqlite3.Connection = Depends(get_db_conn),
):
    """Test rule against existing transaction database without writing any mutations."""
    if payload.pattern_type == "regex":
        _validate_regex_pattern(payload.pattern_value)

    rule = Rule(
        id=None,
        name="Transient Test Rule",
        category_id=payload.category_id or 1,
        priority=100,
        field=payload.field,
        pattern_type=payload.pattern_type,
        pattern_value=payload.pattern_value,
        amount_min_cents=payload.amount_min_cents,
        amount_max_cents=payload.amount_max_cents,
    )

    query = """
        SELECT t.id, t.txn_time, t.counterparty, t.merchant_norm, t.description,
               t.amount_cents, t.category_id, t.category_source, t.pay_method, t.platform_category,
               c.name AS category_name
        FROM transactions t
        LEFT JOIN categories c ON t.category_id = c.id
        ORDER BY t.txn_time DESC
    """
    candidates = db.execute(query).fetchall()

    matched_count = 0
    manual_skipped_count = 0
    preview: List[MatchedTxnPreview] = []

    for row in candidates:
        if rule_matches(rule, row):
            is_manual = row["category_source"] == "manual"
            if is_manual and payload.exclude_manual:
                manual_skipped_count += 1
                continue

            matched_count += 1
            if len(preview) < payload.limit:
                preview.append(
                    MatchedTxnPreview(
                        id=row["id"],
                        txn_time=row["txn_time"],
                        counterparty=row["counterparty"],
                        merchant_norm=row["merchant_norm"],
                        description=row["description"],
                        amount_cents=row["amount_cents"],
                        current_category_id=row["category_id"],
                        current_category_name=row["category_name"],
                        category_source=row["category_source"],
                    )
                )

    return RuleTestResponse(
        matched_count=matched_count,
        manual_skipped_count=manual_skipped_count,
        preview=preview,
    )


@router.post("/apply-proposal", response_model=RuleApplyProposalResponse)
def apply_rule_proposal(
    payload: RuleApplyProposalRequest,
    db: sqlite3.Connection = Depends(get_db_conn),
):
    """Create a new rule and immediately batch apply to historical transactions."""
    cat = CategoryService.get_by_id(db, payload.category_id)
    if not cat:
        raise NotFoundError(f"分类 #{payload.category_id} 不存在")

    if payload.pattern_type == "regex":
        _validate_regex_pattern(payload.pattern_value)

    rule_name = payload.name or f"{payload.pattern_value} -> {cat.name}"

    cur = db.execute(
        """
        INSERT INTO rules (name, category_id, priority, field, pattern_type, pattern_value,
                           is_builtin, hit_count, created_at)
        VALUES (?, ?, ?, ?, ?, ?, 0, 0, datetime('now', 'localtime'))
        """,
        (
            rule_name,
            payload.category_id,
            payload.priority,
            payload.field,
            payload.pattern_type,
            payload.pattern_value,
        ),
    )
    rule_id = cur.lastrowid
    created_rule = Rule(
        id=rule_id,
        name=rule_name,
        category_id=payload.category_id,
        priority=payload.priority,
        field=payload.field,
        pattern_type=payload.pattern_type,
        pattern_value=payload.pattern_value,
    )

    matched = 0
    updated = 0
    skipped_manual = 0

    if payload.apply_to_existing:
        apply_res = apply_rule_to_existing(
            db, created_rule, overwrite_manual=payload.overwrite_manual
        )
        matched = apply_res.get("matched_count", 0)
        updated = apply_res.get("updated_count", 0)
        skipped_manual = apply_res.get("skipped_manual_count", 0)

    row = db.execute(
        """
        SELECT r.*, c.name AS category_name, c.key AS category_key
        FROM rules r
        LEFT JOIN categories c ON r.category_id = c.id
        WHERE r.id = ?
        """,
        (rule_id,),
    ).fetchone()

    rule_out = RuleOut(
        id=row["id"],
        name=row["name"],
        category_id=row["category_id"],
        category_name=row["category_name"],
        category_key=row["category_key"],
        priority=row["priority"],
        field=row["field"],
        pattern_type=row["pattern_type"],
        pattern_value=row["pattern_value"],
        amount_min_cents=row["amount_min_cents"],
        amount_max_cents=row["amount_max_cents"],
        is_builtin=False,
        hit_count=row["hit_count"],
        created_at=row["created_at"],
    )

    return RuleApplyProposalResponse(
        rule=rule_out,
        matched_count=matched,
        updated_count=updated,
        skipped_manual_count=skipped_manual,
    )


@router.get("/suggestions", response_model=List[RuleSuggestionOut])
def get_rule_suggestions(
    min_count: int = Query(3, ge=1, le=50),
    db: sqlite3.Connection = Depends(get_db_conn),
):
    """Retrieve rule learning proposals derived from frequent manual classifications."""
    suggestions = get_learning_suggestions(db, min_count=min_count)
    cat_lookup = {c.id: c.name for c in CategoryService.list_all(db)}

    results = []
    for s in suggestions:
        results.append(
            RuleSuggestionOut(
                brand=s["brand"],
                category_id=s["category_id"],
                category_name=cat_lookup.get(s["category_id"], "未知"),
                count=s["count"],
                sample_counterparty=s["sample_counterparty"],
            )
        )
    return results
