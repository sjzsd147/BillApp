"""Two-tier category taxonomy tree, CRUD interfaces, and hierarchy builder.

Implements:
- 11 Expense root categories + 5 Income root categories + 43 Subcategories
- Stable English keys (e.g. 'food', 'food.dining') per Frontend contract §8.1
- Two-tier tree constraints (depth <= 2)
- CategoryService for query, tree construction, mutation, and cascading protection
"""

from __future__ import annotations

import logging
import sqlite3
import uuid
from typing import Any, Dict, List, Literal, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)

DirectionLiteral = Literal["expense", "income"]


class CategoryInUseError(sqlite3.IntegrityError):
    """Raised when attempting to delete a category that has transactions."""
    pass


# =========================================================================
# Category Data Transfer Objects & Pydantic Models
# =========================================================================

class CategoryBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=50)
    direction: DirectionLiteral = "expense"
    parent_id: Optional[int] = None
    key: Optional[str] = None
    icon: Optional[str] = None
    color: Optional[str] = None
    sort_order: int = 0
    is_builtin: int = 0


class CategoryCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=50)
    direction: DirectionLiteral = "expense"
    parent_id: Optional[int] = None
    key: Optional[str] = None
    icon: Optional[str] = None
    color: Optional[str] = None
    sort_order: int = 0


class CategoryUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=50)
    icon: Optional[str] = None
    color: Optional[str] = None
    sort_order: Optional[int] = None


class Category(CategoryBase):
    model_config = ConfigDict(from_attributes=True)
    id: int


class CategoryNode(Category):
    children: List[CategoryNode] = Field(default_factory=list)
    path: List[str] = Field(default_factory=list)        # ["food", "food.dining"]
    path_names: List[str] = Field(default_factory=list)  # ["餐饮美食", "正餐"]


# =========================================================================
# Builtin Category Taxonomy Matrix (11 Expense + 5 Income + 43 Subcategories)
# =========================================================================

BUILTIN_TAXONOMY: List[Dict[str, Any]] = [
    # --- 11 Expense Roots ---
    {
        "key": "food",
        "name": "餐饮美食",
        "direction": "expense",
        "icon": "Food",
        "color": "#F97316",
        "sort_order": 10,
        "subcategories": [
            {"key": "food.dining", "name": "正餐", "icon": "Bowl", "color": "#F97316", "sort_order": 11},
            {"key": "food.takeout", "name": "外卖", "icon": "Bicycle", "color": "#FB923C", "sort_order": 12},
            {"key": "food.drinks", "name": "饮品零食", "icon": "CoffeeCup", "color": "#FDBA74", "sort_order": 13},
            {"key": "food.canteen", "name": "食堂", "icon": "School", "color": "#EA580C", "sort_order": 14},
        ],
    },
    {
        "key": "shopping",
        "name": "日用百货",
        "direction": "expense",
        "icon": "ShoppingCart",
        "color": "#EC4899",
        "sort_order": 20,
        "subcategories": [
            {"key": "shopping.daily", "name": "日用便利", "icon": "Goods", "color": "#EC4899", "sort_order": 21},
            {"key": "shopping.apparel", "name": "服饰装扮", "icon": "Handbag", "color": "#F472B6", "sort_order": 22},
            {"key": "shopping.electronics", "name": "数码电器", "icon": "Monitor", "color": "#DB2777", "sort_order": 23},
            {"key": "shopping.home", "name": "家居家装", "icon": "House", "color": "#BE185D", "sort_order": 24},
        ],
    },
    {
        "key": "transport",
        "name": "交通出行",
        "direction": "expense",
        "icon": "Van",
        "color": "#3B82F6",
        "sort_order": 30,
        "subcategories": [
            {"key": "transport.public", "name": "公共交通", "icon": "Ship", "color": "#3B82F6", "sort_order": 31},
            {"key": "transport.taxi", "name": "打车出行", "icon": "Van", "color": "#60A5FA", "sort_order": 32},
            {"key": "transport.flight_train", "name": "火车飞机", "icon": "Plane", "color": "#2563EB", "sort_order": 33},
            {"key": "transport.fuel_parking", "name": "加油停车", "icon": "LocationInformation", "color": "#1D4ED8", "sort_order": 34},
        ],
    },
    {
        "key": "housing",
        "name": "住房物业",
        "direction": "expense",
        "icon": "House",
        "color": "#14B8A6",
        "sort_order": 40,
        "subcategories": [
            {"key": "housing.rent_mortgage", "name": "房租房贷", "icon": "Key", "color": "#14B8A6", "sort_order": 41},
            {"key": "housing.property_fee", "name": "物业维修", "icon": "Tools", "color": "#2DD4BF", "sort_order": 42},
            {"key": "housing.cleaning", "name": "家政保洁", "icon": "Brush", "color": "#0F766E", "sort_order": 43},
        ],
    },
    {
        "key": "utilities",
        "name": "生活缴费",
        "direction": "expense",
        "icon": "Lightning",
        "color": "#06B6D4",
        "sort_order": 50,
        "subcategories": [
            {"key": "utilities.water_electricity", "name": "水电气煤", "icon": "Sunny", "color": "#06B6D4", "sort_order": 51},
            {"key": "utilities.telecom", "name": "话费宽带", "icon": "Phone", "color": "#22D3EE", "sort_order": 52},
            {"key": "utilities.subscription", "name": "软件服务", "icon": "Cloudy", "color": "#0891B2", "sort_order": 53},
            {"key": "utilities.campus_facilities", "name": "校园生活", "icon": "OfficeBuilding", "color": "#0E7490", "sort_order": 54},
        ],
    },
    {
        "key": "entertainment",
        "name": "休闲娱乐",
        "direction": "expense",
        "icon": "Film",
        "color": "#8B5CF6",
        "sort_order": 60,
        "subcategories": [
            {"key": "entertainment.gaming", "name": "网络游戏", "icon": "Coordinate", "color": "#8B5CF6", "sort_order": 61},
            {"key": "entertainment.media", "name": "影音会员", "icon": "Headset", "color": "#A78BFA", "sort_order": 62},
            {"key": "entertainment.fitness", "name": "运动健身", "icon": "Basketball", "color": "#7C3AED", "sort_order": 63},
            {"key": "entertainment.travel", "name": "旅游度假", "icon": "Guide", "color": "#6D28D9", "sort_order": 64},
        ],
    },
    {
        "key": "medical",
        "name": "医疗健康",
        "direction": "expense",
        "icon": "FirstAidKit",
        "color": "#10B981",
        "sort_order": 70,
        "subcategories": [
            {"key": "medical.pharmacy", "name": "药品器械", "icon": "Medicine", "color": "#10B981", "sort_order": 71},
            {"key": "medical.treatment", "name": "就诊门诊", "icon": "Hospital", "color": "#34D399", "sort_order": 72},
            {"key": "medical.insurance", "name": "商业保险", "icon": "Lock", "color": "#059669", "sort_order": 73},
        ],
    },
    {
        "key": "education",
        "name": "文教培训",
        "direction": "expense",
        "icon": "Reading",
        "color": "#F59E0B",
        "sort_order": 80,
        "subcategories": [
            {"key": "education.books", "name": "图书文具", "icon": "Notebook", "color": "#F59E0B", "sort_order": 81},
            {"key": "education.training", "name": "培训考证", "icon": "Memo", "color": "#FBBF24", "sort_order": 82},
            {"key": "education.tuition", "name": "学杂学费", "icon": "CollectionTag", "color": "#D97706", "sort_order": 83},
        ],
    },
    {
        "key": "financial",
        "name": "金融转账",
        "direction": "expense",
        "icon": "Money",
        "color": "#6366F1",
        "sort_order": 90,
        "subcategories": [
            {"key": "financial.credit_repay", "name": "信用卡还款", "icon": "CreditCard", "color": "#6366F1", "sort_order": 91},
            {"key": "financial.loan", "name": "贷款借还", "icon": "Coin", "color": "#818CF8", "sort_order": 92},
            {"key": "financial.fee", "name": "手续利息", "icon": "PriceTag", "color": "#4F46E5", "sort_order": 93},
            {"key": "financial.internal_transfer", "name": "资金互转", "icon": "Switch", "color": "#4338CA", "sort_order": 94},
        ],
    },
    {
        "key": "family_social",
        "name": "人情往来",
        "direction": "expense",
        "icon": "UserFilled",
        "color": "#E11D48",
        "sort_order": 100,
        "subcategories": [
            {"key": "family_social.aa", "name": "群收款AA", "icon": "Share", "color": "#E11D48", "sort_order": 101},
            {"key": "family_social.red_packet", "name": "红包礼金", "icon": "Present", "color": "#F43F5E", "sort_order": 102},
            {"key": "family_social.gift", "name": "请客送礼", "icon": "Box", "color": "#BE123C", "sort_order": 103},
        ],
    },
    {
        "key": "other_expense",
        "name": "其他支出",
        "direction": "expense",
        "icon": "MoreFilled",
        "color": "#64748B",
        "sort_order": 110,
        "subcategories": [
            {"key": "other_expense.misc", "name": "其他杂项", "icon": "Files", "color": "#64748B", "sort_order": 111},
            {"key": "other_expense.loss", "name": "意外支出", "icon": "Warning", "color": "#94A3B8", "sort_order": 112},
        ],
    },

    # --- 5 Income Roots ---
    {
        "key": "salary",
        "name": "职业工资",
        "direction": "income",
        "icon": "Briefcase",
        "color": "#059669",
        "sort_order": 200,
        "subcategories": [
            {"key": "salary.payroll", "name": "全职薪资", "icon": "Briefcase", "color": "#059669", "sort_order": 201},
            {"key": "salary.bonus", "name": "奖金补贴", "icon": "Trophy", "color": "#10B981", "sort_order": 202},
            {"key": "salary.part_time", "name": "兼职劳务", "icon": "Suitcase", "color": "#047857", "sort_order": 203},
        ],
    },
    {
        "key": "investment",
        "name": "理财收益",
        "direction": "income",
        "icon": "TrendCharts",
        "color": "#D97706",
        "sort_order": 210,
        "subcategories": [
            {"key": "investment.profit", "name": "基金股票", "icon": "TrendCharts", "color": "#D97706", "sort_order": 211},
            {"key": "investment.rebate", "name": "消费返现", "icon": "Discount", "color": "#F59E0B", "sort_order": 212},
            {"key": "investment.interest", "name": "利息收益", "icon": "PieChart", "color": "#B45309", "sort_order": 213},
        ],
    },
    {
        "key": "transfer_income",
        "name": "转账收款",
        "direction": "income",
        "icon": "Wallet",
        "color": "#0284C7",
        "sort_order": 220,
        "subcategories": [
            {"key": "transfer_income.peer", "name": "个人转账", "icon": "User", "color": "#0284C7", "sort_order": 221},
            {"key": "transfer_income.aa", "name": "群收款到账", "icon": "Connection", "color": "#38BDF8", "sort_order": 222},
        ],
    },
    {
        "key": "reimbursement",
        "name": "报销退款",
        "direction": "income",
        "icon": "Tickets",
        "color": "#65A30D",
        "sort_order": 230,
        "subcategories": [
            {"key": "reimbursement.expense_claim", "name": "差旅报销", "icon": "Tickets", "color": "#65A30D", "sort_order": 231},
            {"key": "reimbursement.refund", "name": "退款入账", "icon": "Refresh", "color": "#84CC16", "sort_order": 232},
        ],
    },
    {
        "key": "other_income",
        "name": "其他收入",
        "direction": "income",
        "icon": "Coin",
        "color": "#475569",
        "sort_order": 240,
        "subcategories": [
            {"key": "other_income.misc", "name": "其他杂项", "icon": "FolderAdd", "color": "#475569", "sort_order": 241},
        ],
    },
]


def _get_table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    """Inspect columns of a table in SQLite."""
    cursor = conn.execute(f"PRAGMA table_info({table_name})")
    return {row[1] for row in cursor.fetchall()}


def _row_to_category_dict(row: Any) -> Dict[str, Any]:
    """Convert an sqlite3.Row or tuple/dict to a Category model dictionary."""
    if isinstance(row, dict):
        d = dict(row)
    else:
        # sqlite3.Row
        d = {k: row[k] for k in row.keys()}
    
    # Handle schema variance: 'direction' vs 'kind'
    if "direction" not in d and "kind" in d:
        d["direction"] = d["kind"]
    elif "direction" in d and "kind" not in d:
        d["kind"] = d["direction"]
    return d


# =========================================================================
# Category Service Implementation
# =========================================================================

class CategoryService:
    """Provides complete CRUD, initialization, and tree traversal for categories."""

    @staticmethod
    def init_builtin_categories(conn: sqlite3.Connection) -> int:
        """Seed all 16 root categories and their subcategories idempotently.
        
        Returns the total number of seeded or existing builtin categories.
        """
        cols = _get_table_columns(conn, "categories")
        has_key = "key" in cols
        has_color = "color" in cols
        has_direction = "direction" in cols
        has_kind = "kind" in cols

        total_seeded = 0

        for root_data in BUILTIN_TAXONOMY:
            root_key = root_data["key"]
            root_name = root_data["name"]
            root_dir = root_data["direction"]
            root_icon = root_data.get("icon")
            root_color = root_data.get("color")
            root_order = root_data.get("sort_order", 0)

            # Check if root exists by key or name
            existing_root = None
            if has_key:
                existing_root = conn.execute(
                    "SELECT id FROM categories WHERE key = ?", (root_key,)
                ).fetchone()
            if not existing_root:
                dir_col = "direction" if has_direction else "kind"
                existing_root = conn.execute(
                    f"SELECT id FROM categories WHERE parent_id IS NULL AND {dir_col} = ? AND name = ?",
                    (root_dir, root_name),
                ).fetchone()

            if existing_root:
                root_id = existing_root[0]
                # Update existing root attributes if needed
                update_fields = []
                update_vals = []
                if has_key:
                    update_fields.append("key = coalesce(key, ?)")
                    update_vals.append(root_key)
                if has_color:
                    update_fields.append("color = coalesce(color, ?)")
                    update_vals.append(root_color)
                if root_icon:
                    update_fields.append("icon = coalesce(icon, ?)")
                    update_vals.append(root_icon)
                if update_fields:
                    update_vals.append(root_id)
                    conn.execute(
                        f"UPDATE categories SET {', '.join(update_fields)} WHERE id = ?",
                        update_vals,
                    )
            else:
                # Insert root
                insert_cols = ["name", "parent_id", "sort_order", "is_builtin"]
                insert_vals: List[Any] = [root_name, None, root_order, 1]

                if has_direction:
                    insert_cols.append("direction")
                    insert_vals.append(root_dir)
                if has_kind:
                    insert_cols.append("kind")
                    insert_vals.append(root_dir)
                if has_key:
                    insert_cols.append("key")
                    insert_vals.append(root_key)
                if has_color:
                    insert_cols.append("color")
                    insert_vals.append(root_color)
                if root_icon and "icon" in cols:
                    insert_cols.append("icon")
                    insert_vals.append(root_icon)

                placeholders = ", ".join("?" for _ in insert_cols)
                cursor = conn.execute(
                    f"INSERT INTO categories ({', '.join(insert_cols)}) VALUES ({placeholders})",
                    insert_vals,
                )
                root_id = cursor.lastrowid
            total_seeded += 1

            # Seed subcategories
            for sub_data in root_data.get("subcategories", []):
                sub_key = sub_data["key"]
                sub_name = sub_data["name"]
                sub_icon = sub_data.get("icon")
                sub_color = sub_data.get("color")
                sub_order = sub_data.get("sort_order", 0)

                existing_sub = None
                if has_key:
                    existing_sub = conn.execute(
                        "SELECT id FROM categories WHERE key = ?", (sub_key,)
                    ).fetchone()
                if not existing_sub:
                    existing_sub = conn.execute(
                        "SELECT id FROM categories WHERE parent_id = ? AND name = ?",
                        (root_id, sub_name),
                    ).fetchone()

                if existing_sub:
                    sub_id = existing_sub[0]
                    update_fields = []
                    update_vals = []
                    if has_key:
                        update_fields.append("key = coalesce(key, ?)")
                        update_vals.append(sub_key)
                    if has_color:
                        update_fields.append("color = coalesce(color, ?)")
                        update_vals.append(sub_color)
                    if sub_icon:
                        update_fields.append("icon = coalesce(icon, ?)")
                        update_vals.append(sub_icon)
                    if update_fields:
                        update_vals.append(sub_id)
                        conn.execute(
                            f"UPDATE categories SET {', '.join(update_fields)} WHERE id = ?",
                            update_vals,
                        )
                else:
                    insert_cols = ["name", "parent_id", "sort_order", "is_builtin"]
                    insert_vals = [sub_name, root_id, sub_order, 1]

                    if has_direction:
                        insert_cols.append("direction")
                        insert_vals.append(root_dir)
                    if has_kind:
                        insert_cols.append("kind")
                        insert_vals.append(root_dir)
                    if has_key:
                        insert_cols.append("key")
                        insert_vals.append(sub_key)
                    if has_color:
                        insert_cols.append("color")
                        insert_vals.append(sub_color)
                    if sub_icon and "icon" in cols:
                        insert_cols.append("icon")
                        insert_vals.append(sub_icon)

                    placeholders = ", ".join("?" for _ in insert_cols)
                    conn.execute(
                        f"INSERT INTO categories ({', '.join(insert_cols)}) VALUES ({placeholders})",
                        insert_vals,
                    )
                total_seeded += 1

        conn.commit()
        return total_seeded

    @staticmethod
    def get_by_id(conn: sqlite3.Connection, category_id: int) -> Optional[Category]:
        """Fetch a single category by primary key ID."""
        row = conn.execute("SELECT * FROM categories WHERE id = ?", (category_id,)).fetchone()
        if not row:
            return None
        return Category(**_row_to_category_dict(row))

    @staticmethod
    def get_by_key(conn: sqlite3.Connection, key: str) -> Optional[Category]:
        """Fetch category by stable English key."""
        cols = _get_table_columns(conn, "categories")
        if "key" not in cols:
            return None
        row = conn.execute("SELECT * FROM categories WHERE key = ?", (key,)).fetchone()
        if not row:
            return None
        return Category(**_row_to_category_dict(row))

    @staticmethod
    def get_by_name(
        conn: sqlite3.Connection,
        name: str,
        parent_id: Optional[int] = None,
        direction: Optional[str] = None,
    ) -> Optional[Category]:
        """Fetch category by display name."""
        cols = _get_table_columns(conn, "categories")
        dir_col = "direction" if "direction" in cols else "kind"

        query = f"SELECT * FROM categories WHERE name = ?"
        params: List[Any] = [name]

        if parent_id is not None:
            query += " AND parent_id = ?"
            params.append(parent_id)
        else:
            query += " AND parent_id IS NULL"
        if direction is not None:
            query += f" AND {dir_col} = ?"
            params.append(direction)

        row = conn.execute(query, params).fetchone()
        if not row:
            return None
        return Category(**_row_to_category_dict(row))

    @staticmethod
    def list_all(
        conn: sqlite3.Connection,
        direction: Optional[str] = None,
    ) -> List[Category]:
        """List all categories, ordered by sort_order and id."""
        cols = _get_table_columns(conn, "categories")
        dir_col = "direction" if "direction" in cols else "kind"

        if direction:
            rows = conn.execute(
                f"SELECT * FROM categories WHERE {dir_col} = ? ORDER BY sort_order ASC, id ASC",
                (direction,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM categories ORDER BY sort_order ASC, id ASC"
            ).fetchall()

        return [Category(**_row_to_category_dict(r)) for r in rows]

    @staticmethod
    def get_tree(
        conn: sqlite3.Connection,
        direction: Optional[DirectionLiteral] = None,
    ) -> List[CategoryNode]:
        """Construct full 2-tier cascader tree with nested subcategories and path breadcrumbs."""
        all_cats = CategoryService.list_all(conn, direction=direction)

        # Separate roots and subcategories
        roots: List[CategoryNode] = []
        children_map: Dict[int, List[CategoryNode]] = {}

        for cat in all_cats:
            cat_dict = cat.model_dump()
            key_val = cat.key or str(cat.id)
            node = CategoryNode(
                **cat_dict,
                children=[],
                path=[key_val],
                path_names=[cat.name],
            )

            if cat.parent_id is None:
                roots.append(node)
            else:
                children_map.setdefault(cat.parent_id, []).append(node)

        # Assemble children into roots and augment path breadcrumbs
        for root in roots:
            root_children = children_map.get(root.id, [])
            for child in root_children:
                child.path = [root.key or str(root.id), child.key or str(child.id)]
                child.path_names = [root.name, child.name]
            root.children = root_children

        return roots

    @staticmethod
    def get_path(conn: sqlite3.Connection, category_id: int) -> Tuple[List[str], List[str]]:
        """Retrieve ([key_path], [name_path]) breadcrumbs for a category."""
        cat = CategoryService.get_by_id(conn, category_id)
        if not cat:
            return [], []

        if cat.parent_id is None:
            return [cat.key or str(cat.id)], [cat.name]

        parent = CategoryService.get_by_id(conn, cat.parent_id)
        if parent:
            return [parent.key or str(parent.id), cat.key or str(cat.id)], [parent.name, cat.name]

        return [cat.key or str(cat.id)], [cat.name]

    @staticmethod
    def create(conn: sqlite3.Connection, data: CategoryCreate) -> Category:
        """Create a user custom category under strict 2-tier constraints."""
        cols = _get_table_columns(conn, "categories")
        has_key = "key" in cols
        has_color = "color" in cols
        has_direction = "direction" in cols
        has_kind = "kind" in cols

        # 1. Enforce max 2 tiers: If parent_id specified, parent must exist and must be root
        if data.parent_id is not None:
            parent = CategoryService.get_by_id(conn, data.parent_id)
            if not parent:
                raise ValueError(f"Parent category #{data.parent_id} does not exist.")
            if parent.parent_id is not None:
                raise ValueError("Hierarchy depth cannot exceed 2 tiers. Subcategories cannot have child categories.")
            # Inherit direction from parent
            data.direction = parent.direction

        # 2. Check sibling name uniqueness
        existing_sibling = CategoryService.get_by_name(
            conn, name=data.name, parent_id=data.parent_id, direction=data.direction
        )
        if existing_sibling:
            raise ValueError(f"Category with name '{data.name}' already exists in this level.")

        # 3. Assign stable key if omitted
        key_val = data.key
        if not key_val and has_key:
            suffix = uuid.uuid4().hex[:8]
            key_val = f"custom_{suffix}"

        # 4. Insert category
        insert_cols = ["name", "parent_id", "sort_order", "is_builtin"]
        insert_vals: List[Any] = [data.name, data.parent_id, data.sort_order, 0]

        if has_direction:
            insert_cols.append("direction")
            insert_vals.append(data.direction)
        if has_kind:
            insert_cols.append("kind")
            insert_vals.append(data.direction)
        if has_key:
            insert_cols.append("key")
            insert_vals.append(key_val)
        if has_color and data.color:
            insert_cols.append("color")
            insert_vals.append(data.color)
        if data.icon and "icon" in cols:
            insert_cols.append("icon")
            insert_vals.append(data.icon)

        placeholders = ", ".join("?" for _ in insert_cols)
        cursor = conn.execute(
            f"INSERT INTO categories ({', '.join(insert_cols)}) VALUES ({placeholders})",
            insert_vals,
        )
        conn.commit()
        new_id = cursor.lastrowid
        created = CategoryService.get_by_id(conn, new_id)
        assert created is not None
        return created

    @staticmethod
    def update(conn: sqlite3.Connection, category_id: int, data: CategoryUpdate) -> Category:
        """Update category name, icon, color, or sort_order."""
        existing = CategoryService.get_by_id(conn, category_id)
        if not existing:
            raise ValueError(f"Category #{category_id} not found.")

        cols = _get_table_columns(conn, "categories")
        update_fields = []
        update_vals: List[Any] = []

        if data.name is not None:
            # Check sibling uniqueness
            sibling = CategoryService.get_by_name(
                conn, name=data.name, parent_id=existing.parent_id, direction=existing.direction
            )
            if sibling and sibling.id != category_id:
                raise ValueError(f"Category name '{data.name}' already taken by sibling category.")
            update_fields.append("name = ?")
            update_vals.append(data.name)

        if data.icon is not None and "icon" in cols:
            update_fields.append("icon = ?")
            update_vals.append(data.icon)

        if data.color is not None and "color" in cols:
            update_fields.append("color = ?")
            update_vals.append(data.color)

        if data.sort_order is not None:
            update_fields.append("sort_order = ?")
            update_vals.append(data.sort_order)

        if update_fields:
            update_vals.append(category_id)
            conn.execute(
                f"UPDATE categories SET {', '.join(update_fields)} WHERE id = ?",
                update_vals,
            )
            conn.commit()

        updated = CategoryService.get_by_id(conn, category_id)
        assert updated is not None
        return updated

    @staticmethod
    def delete(
        conn: sqlite3.Connection,
        category_id: int,
        reassign_to_id: Optional[int] = None,
    ) -> bool:
        """Delete a category.
        
        Enforces:
        - If category has child subcategories, reassign or error.
        - If transactions reference category_id:
            - If reassign_to_id provided: bulk updates transactions and rules to reassign_to_id.
            - If reassign_to_id is None: raises CategoryInUseError.
        """
        existing = CategoryService.get_by_id(conn, category_id)
        if not existing:
            return False

        target_cat: Optional[Category] = None
        if reassign_to_id is not None:
            if reassign_to_id == category_id:
                raise ValueError("Cannot reassign to the category being deleted")
            target_cat = CategoryService.get_by_id(conn, reassign_to_id)
            if not target_cat:
                raise ValueError(f"Reassignment target category #{reassign_to_id} does not exist.")
            if target_cat.direction != existing.direction:
                raise ValueError(
                    f"Cannot reassign category #{category_id} ({existing.direction}) "
                    f"to target #{reassign_to_id} ({target_cat.direction}): direction mismatch."
                )

        # 1. Check child categories
        children = conn.execute(
            "SELECT id FROM categories WHERE parent_id = ?", (category_id,)
        ).fetchall()
        if children:
            if reassign_to_id is None:
                raise ValueError(f"Category #{category_id} has {len(children)} subcategories. Reassign children first.")
            child_ids = [c[0] for c in children]
            if reassign_to_id in child_ids:
                raise ValueError(f"Cannot reassign to a child category of #{category_id}")
            if target_cat is not None and target_cat.parent_id is not None:
                raise ValueError("Cannot reassign child categories to a subcategory (depth <= 2 violation)")
            # Reassign children to new parent
            conn.execute(
                "UPDATE categories SET parent_id = ? WHERE parent_id = ?",
                (reassign_to_id, category_id),
            )

        # 2. Check transaction usage
        txn_count = conn.execute(
            "SELECT COUNT(*) FROM transactions WHERE category_id = ?", (category_id,)
        ).fetchone()[0]

        if txn_count > 0:
            if reassign_to_id is None:
                raise CategoryInUseError(
                    f"Cannot delete category #{category_id}: {txn_count} transactions are associated with it."
                )
            # Bulk reassign transactions
            conn.execute(
                "UPDATE transactions SET category_id = ? WHERE category_id = ?",
                (reassign_to_id, category_id),
            )

        # 3. Check rule usage
        rule_count = conn.execute(
            "SELECT COUNT(*) FROM rules WHERE category_id = ?", (category_id,)
        ).fetchone()[0]
        if rule_count > 0:
            if reassign_to_id is None:
                raise CategoryInUseError(
                    f"Category #{category_id} is referenced by {rule_count} active rules. Reassign rules or supply reassign_to_id."
                )
            conn.execute(
                "UPDATE rules SET category_id = ? WHERE category_id = ?",
                (reassign_to_id, category_id),
            )

        # 4. Perform deletion
        conn.execute("DELETE FROM categories WHERE id = ?", (category_id,))
        conn.commit()
        return True
