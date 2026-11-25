"""Category taxonomy tree management API endpoints (/api/categories/*)."""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from billapp.app import AppException, ConflictError, NotFoundError, ValidationError, get_db_conn
from billapp.classify.categories import (
    CategoryCreate,
    CategoryInUseError,
    CategoryNode,
    CategoryService,
    CategoryUpdate,
)

router = APIRouter(prefix="/api/categories", tags=["categories"])


# =========================================================================
# Category DTO Schemas
# =========================================================================

class CategoryCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=50)
    direction: Literal["expense", "income"] = "expense"
    parent_id: Optional[int] = None
    key: Optional[str] = None
    icon: Optional[str] = None
    color: Optional[str] = None
    sort_order: int = 0


class CategoryUpdateRequest(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=50)
    icon: Optional[str] = None
    color: Optional[str] = None
    sort_order: Optional[int] = None
    budget_cents: Optional[int] = Field(None, ge=0)


class CategoryItemOut(BaseModel):
    id: int
    name: str
    direction: str
    type: str  # Frontend alias for direction
    parent_id: Optional[int] = None
    key: Optional[str] = None
    icon: Optional[str] = None
    color: Optional[str] = None
    sort_order: int = 0
    is_builtin: int = 0
    rule_count: int = 0
    transaction_count: int = 0
    budget_cents: Optional[int] = None
    path: List[str] = Field(default_factory=list)
    path_names: List[str] = Field(default_factory=list)
    children: List[CategoryItemOut] = Field(default_factory=list)


class CategoryDeleteResponse(BaseModel):
    ok: bool = True
    deleted_id: int
    reassigned_to_id: Optional[int] = None


# =========================================================================
# Endpoints
# =========================================================================

@router.get("", response_model=List[CategoryItemOut])
def list_categories_tree(
    direction: Optional[Literal["expense", "income"]] = Query(None),
    db: sqlite3.Connection = Depends(get_db_conn),
):
    """Retrieve full 2-tier category taxonomy tree with rolled-up counts and budget amounts."""
    tree = CategoryService.get_tree(db, direction=direction)

    # 1. Fetch transaction counts per category
    txn_counts: Dict[int, int] = {}
    for r in db.execute(
        """
        SELECT category_id, COUNT(*) AS cnt
        FROM transactions
        WHERE category_id IS NOT NULL AND duplicate_of IS NULL AND excluded = 0
        GROUP BY category_id
        """
    ).fetchall():
        txn_counts[r["category_id"]] = int(r["cnt"])

    # 2. Fetch rule counts per category
    rule_counts: Dict[int, int] = {}
    for r in db.execute(
        """
        SELECT category_id, COUNT(*) AS cnt
        FROM rules
        WHERE category_id IS NOT NULL
        GROUP BY category_id
        """
    ).fetchall():
        rule_counts[r["category_id"]] = int(r["cnt"])

    # 3. Fetch budgets from settings
    budget_map: Dict[str, int] = {}
    b_row = db.execute("SELECT value FROM settings WHERE key = 'category_budgets'").fetchone()
    if b_row and b_row["value"]:
        try:
            budget_map = json.loads(b_row["value"])
        except Exception:
            budget_map = {}

    # 4. Map CategoryNode tree into CategoryItemOut with roll-ups
    results: List[CategoryItemOut] = []

    for root in tree:
        child_items: List[CategoryItemOut] = []
        root_direct_txns = txn_counts.get(root.id, 0)
        root_direct_rules = rule_counts.get(root.id, 0)

        sub_txns_total = 0
        sub_rules_total = 0

        for child in root.children:
            c_txns = txn_counts.get(child.id, 0)
            c_rules = rule_counts.get(child.id, 0)
            sub_txns_total += c_txns
            sub_rules_total += c_rules

            child_items.append(
                CategoryItemOut(
                    id=child.id,
                    name=child.name,
                    direction=child.direction,
                    type=child.direction,
                    parent_id=child.parent_id,
                    key=child.key,
                    icon=child.icon,
                    color=child.color,
                    sort_order=child.sort_order,
                    is_builtin=child.is_builtin,
                    rule_count=c_rules,
                    transaction_count=c_txns,
                    budget_cents=budget_map.get(str(child.id)),
                    path=child.path,
                    path_names=child.path_names,
                    children=[],
                )
            )

        results.append(
            CategoryItemOut(
                id=root.id,
                name=root.name,
                direction=root.direction,
                type=root.direction,
                parent_id=root.parent_id,
                key=root.key,
                icon=root.icon,
                color=root.color,
                sort_order=root.sort_order,
                is_builtin=root.is_builtin,
                rule_count=root_direct_rules + sub_rules_total,
                transaction_count=root_direct_txns + sub_txns_total,
                budget_cents=budget_map.get(str(root.id)),
                path=root.path,
                path_names=root.path_names,
                children=child_items,
            )
        )

    return results


@router.post("", response_model=CategoryItemOut, status_code=status.HTTP_201_CREATED)
def create_category(
    payload: CategoryCreateRequest,
    db: sqlite3.Connection = Depends(get_db_conn),
):
    """Create a new custom category enforcing sibling uniqueness and 2-tier depth limit."""
    # 2-tier depth check & inherit parent direction before sibling uniqueness check
    effective_direction = payload.direction
    if payload.parent_id is not None:
        parent = CategoryService.get_by_id(db, payload.parent_id)
        if not parent:
            raise NotFoundError(f"父分类 #{payload.parent_id} 不存在")
        if parent.parent_id is not None:
            raise AppException(
                status_code=400,
                code="MAX_DEPTH_EXCEEDED",
                message="层级深度不能超过2级，子分类不能再创建子分类",
            )
        effective_direction = parent.direction

    # Sibling name uniqueness check
    sibling = CategoryService.get_by_name(
        db, name=payload.name, parent_id=payload.parent_id, direction=effective_direction
    )
    if sibling:
        raise ConflictError(f"同级分类中已存在同名分类: '{payload.name}'")

    try:
        cat_create = CategoryCreate(
            name=payload.name,
            direction=effective_direction,
            parent_id=payload.parent_id,
            key=payload.key,
            icon=payload.icon,
            color=payload.color,
            sort_order=payload.sort_order,
        )
        created = CategoryService.create(db, cat_create)
    except ValueError as exc:
        raise AppException(status_code=400, code="INVALID_CATEGORY", message=str(exc))

    path, path_names = CategoryService.get_path(db, created.id)
    return CategoryItemOut(
        id=created.id,
        name=created.name,
        direction=created.direction,
        type=created.direction,
        parent_id=created.parent_id,
        key=created.key,
        icon=created.icon,
        color=created.color,
        sort_order=created.sort_order,
        is_builtin=created.is_builtin,
        rule_count=0,
        transaction_count=0,
        budget_cents=None,
        path=path,
        path_names=path_names,
        children=[],
    )


@router.put("/{category_id}", response_model=CategoryItemOut)
def update_category(
    category_id: int,
    payload: CategoryUpdateRequest,
    db: sqlite3.Connection = Depends(get_db_conn),
):
    """Update category metadata and budget allocation."""
    existing = CategoryService.get_by_id(db, category_id)
    if not existing:
        raise NotFoundError(f"分类 #{category_id} 不存在")

    # Sibling uniqueness check if renaming
    if payload.name and payload.name != existing.name:
        sibling = CategoryService.get_by_name(
            db, name=payload.name, parent_id=existing.parent_id, direction=existing.direction
        )
        if sibling and sibling.id != category_id:
            raise ConflictError(f"同级分类中已存在同名分类: '{payload.name}'")

    try:
        cat_up = CategoryUpdate(
            name=payload.name,
            icon=payload.icon,
            color=payload.color,
            sort_order=payload.sort_order,
        )
        updated = CategoryService.update(db, category_id, cat_up)
    except ValueError as exc:
        raise AppException(status_code=400, code="INVALID_UPDATE", message=str(exc))

    # Persist budget if supplied
    if payload.budget_cents is not None:
        b_row = db.execute("SELECT value FROM settings WHERE key = 'category_budgets'").fetchone()
        budget_map = json.loads(b_row["value"]) if b_row and b_row["value"] else {}
        budget_map[str(category_id)] = payload.budget_cents
        db.execute(
            """
            INSERT INTO settings (key, value, updated_at)
            VALUES ('category_budgets', ?, datetime('now', 'localtime'))
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """,
            (json.dumps(budget_map),),
        )

    # Fetch budget
    b_row = db.execute("SELECT value FROM settings WHERE key = 'category_budgets'").fetchone()
    budget_map = json.loads(b_row["value"]) if b_row and b_row["value"] else {}

    # Return full rolled-up item including real counts and children
    full_tree = list_categories_tree(direction=updated.direction, db=db)
    for root in full_tree:
        if root.id == updated.id:
            return root
        for child in root.children:
            if child.id == updated.id:
                return child

    path, path_names = CategoryService.get_path(db, updated.id)
    return CategoryItemOut(
        id=updated.id,
        name=updated.name,
        direction=updated.direction,
        type=updated.direction,
        parent_id=updated.parent_id,
        key=updated.key,
        icon=updated.icon,
        color=updated.color,
        sort_order=updated.sort_order,
        is_builtin=updated.is_builtin,
        budget_cents=budget_map.get(str(category_id)),
        path=path,
        path_names=path_names,
        children=[],
    )


@router.delete("/{category_id}", response_model=CategoryDeleteResponse)
def delete_category(
    category_id: int,
    reassign_to_id: Optional[int] = Query(None),
    db: sqlite3.Connection = Depends(get_db_conn),
):
    """Delete a category with atomic transaction and rule/child reassignment protections."""
    existing = CategoryService.get_by_id(db, category_id)
    if not existing:
        raise NotFoundError(f"分类 #{category_id} 不存在")

    if existing.is_builtin:
        raise AppException(
            status_code=403,
            code="BUILTIN_CATEGORY_IMMUTABLE",
            message="系统内置基础分类不可删除",
        )

    try:
        success = CategoryService.delete(db, category_id, reassign_to_id=reassign_to_id)
        if not success:
            raise NotFoundError(f"分类 #{category_id} 不存在")
    except CategoryInUseError as err:
        raise AppException(
            status_code=409,
            code="CATEGORY_IN_USE",
            message=str(err),
            details={"category_id": category_id, "error": str(err)},
        )
    except ValueError as err:
        raise AppException(
            status_code=400,
            code="INVALID_REASSIGNMENT",
            message=str(err),
        )

    return CategoryDeleteResponse(
        ok=True,
        deleted_id=category_id,
        reassigned_to_id=reassign_to_id,
    )
