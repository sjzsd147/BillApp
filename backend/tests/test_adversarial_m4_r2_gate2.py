"""Milestone 4 Round 2 Adversarial Gate 2 Verification Suite:
Empirical Stress Testing for Categories & Rule Engine Remediation.

Specific Verification Focus:
1. Category deletion self-loop prevention (reassign_to_id == category_id, invalid target, child target).
2. Category deletion depth 3 prevention (cannot reassign children to subcategories).
3. Category deletion active rules protection (cannot delete category with active rules unless reassigned).
4. ReDoS nested quantifier rejection (^(a+)+$, (\\w+)+, (x+x+)+y, etc.).
5. apply_rule_to_existing with regex rules correctly updates matching transactions and respects manual protection.
"""

from __future__ import annotations

import re
import sqlite3
import time
from typing import Any, Dict, List, Optional

import pytest

from billapp.core.db import init_db
from billapp.classify.categories import (
    Category,
    CategoryCreate,
    CategoryInUseError,
    CategoryService,
    CategoryUpdate,
)
from billapp.classify.rules import (
    Rule,
    apply_rule_to_existing,
    compile_safe_regex,
    match_pattern,
    rule_matches,
)


@pytest.fixture
def db() -> sqlite3.Connection:
    """Provide an isolated in-memory SQLite database initialized with schema.sql."""
    conn = init_db(":memory:")
    conn.execute(
        "INSERT INTO accounts (id, kind, bank, alias) VALUES (1, 'wallet', '支付宝', '支付宝余额')"
    )
    conn.execute(
        "INSERT INTO import_batches (id, file_name, file_sha1, file_size, source) "
        "VALUES (1, 'r2_gate2_test.csv', 'sha1_gate2_batch', 1024, 'alipay')"
    )
    CategoryService.init_builtin_categories(conn)
    conn.commit()
    yield conn
    conn.close()


def insert_txn(
    conn: sqlite3.Connection,
    txn_id: int,
    amount_cents: int = 3500,
    counterparty: str = "星巴克咖啡上海店",
    description: str = "下午茶",
    platform_category: Optional[str] = None,
    pay_method: Optional[str] = "花呗",
    category_id: Optional[int] = None,
    category_source: Optional[str] = None,
    batch_id: int = 1,
) -> None:
    """Helper to insert test transaction."""
    conn.execute(
        """
        INSERT INTO transactions (
            id, account_id, batch_id, source, txn_time, time_precision,
            amount_cents, direction, counterparty, description,
            platform_category, pay_method, fingerprint,
            category_id, category_source, category_confidence
        ) VALUES (
            ?, 1, ?, 'alipay', '2026-11-01 14:00:00', 'second',
            ?, 'expense', ?, ?,
            ?, ?, ?,
            ?, ?, 1.0
        )
        """,
        (
            txn_id,
            batch_id,
            amount_cents,
            counterparty,
            description,
            platform_category,
            pay_method,
            f"fp_gate2_{txn_id}",
            category_id,
            category_source,
        ),
    )
    conn.commit()


# ==============================================================================
# Vector 1: Category Deletion Self-Loop & Invalid Target Prevention
# ==============================================================================

class TestCategoryDeletionSelfLoopPrevention:
    """Adversarial testing against self-referencing loops and invalid targets."""

    def test_delete_reassign_to_self_strictly_blocked(self, db: sqlite3.Connection):
        """Passing reassign_to_id == category_id must raise ValueError immediately."""
        food = CategoryService.get_by_key(db, "food")
        assert food is not None

        with pytest.raises(ValueError, match="Cannot reassign to the category being deleted"):
            CategoryService.delete(db, food.id, reassign_to_id=food.id)

        # Confirm category was NOT deleted
        assert CategoryService.get_by_id(db, food.id) is not None

    def test_delete_subcat_reassign_to_self_strictly_blocked(self, db: sqlite3.Connection):
        """Self-reassignment must be blocked on subcategories as well."""
        dining = CategoryService.get_by_key(db, "food.dining")
        assert dining is not None

        with pytest.raises(ValueError, match="Cannot reassign to the category being deleted"):
            CategoryService.delete(db, dining.id, reassign_to_id=dining.id)

        assert CategoryService.get_by_id(db, dining.id) is not None

    def test_delete_reassign_to_nonexistent_category_blocked(self, db: sqlite3.Connection):
        """Passing a non-existent reassign_to_id must raise ValueError."""
        food = CategoryService.get_by_key(db, "food")
        assert food is not None

        with pytest.raises(ValueError, match="Reassignment target category #999999 does not exist"):
            CategoryService.delete(db, food.id, reassign_to_id=999999)

        assert CategoryService.get_by_id(db, food.id) is not None

    def test_delete_parent_reassign_children_to_own_child_blocked(self, db: sqlite3.Connection):
        """Deleting a root category cannot reassign its children to one of its own children (cycle)."""
        food = CategoryService.get_by_key(db, "food")
        dining = CategoryService.get_by_key(db, "food.dining")
        assert food is not None and dining is not None

        with pytest.raises(ValueError, match="Cannot reassign to a child category"):
            CategoryService.delete(db, food.id, reassign_to_id=dining.id)

        # Confirm food and its subcategories are untouched
        assert CategoryService.get_by_id(db, food.id) is not None
        assert CategoryService.get_by_id(db, dining.id) is not None


# ==============================================================================
# Vector 2: Category Deletion Depth-3 Prevention
# ==============================================================================

class TestCategoryDeletionDepth3Prevention:
    """Verify that deleting a root category cannot reassign children to a subcategory (depth <= 2)."""

    def test_reassign_children_to_subcategory_raises_depth_violation(self, db: sqlite3.Connection):
        """Reassigning child categories to a subcategory creates depth 3; must raise ValueError."""
        # Create a custom root category with children
        custom_root = CategoryService.create(
            db, CategoryCreate(name="自定义根类", direction="expense")
        )
        custom_sub1 = CategoryService.create(
            db, CategoryCreate(name="自定义子类1", parent_id=custom_root.id, direction="expense")
        )
        custom_sub2 = CategoryService.create(
            db, CategoryCreate(name="自定义子类2", parent_id=custom_root.id, direction="expense")
        )

        # Existing subcategory (depth 2)
        dining = CategoryService.get_by_key(db, "food.dining")
        assert dining is not None and dining.parent_id is not None

        # Attempt to delete custom_root and reassign children to dining (which is a subcategory)
        with pytest.raises(ValueError, match="depth <= 2 violation"):
            CategoryService.delete(db, custom_root.id, reassign_to_id=dining.id)

        # Verify nothing was deleted or reparented
        assert CategoryService.get_by_id(db, custom_root.id) is not None
        assert CategoryService.get_by_id(db, custom_sub1.id).parent_id == custom_root.id
        assert CategoryService.get_by_id(db, custom_sub2.id).parent_id == custom_root.id

    def test_reassign_children_to_valid_root_succeeds_and_preserves_2_tier_tree(
        self, db: sqlite3.Connection
    ):
        """Reassigning child categories to another root category is valid and preserves 2 tiers."""
        # Create custom root A with child A1
        root_a = CategoryService.create(
            db, CategoryCreate(name="待删大类", direction="expense")
        )
        sub_a1 = CategoryService.create(
            db, CategoryCreate(name="待迁子类", parent_id=root_a.id, direction="expense")
        )

        # Target root category: 'shopping'
        shopping = CategoryService.get_by_key(db, "shopping")
        assert shopping is not None and shopping.parent_id is None

        # Delete root_a and reassign children to shopping
        success = CategoryService.delete(db, root_a.id, reassign_to_id=shopping.id)
        assert success is True

        # Verify root_a is deleted
        assert CategoryService.get_by_id(db, root_a.id) is None

        # Verify sub_a1 now belongs to shopping
        reparented_sub = CategoryService.get_by_id(db, sub_a1.id)
        assert reparented_sub is not None
        assert reparented_sub.parent_id == shopping.id

        # Verify cascader tree still strictly adheres to 2 tiers
        tree = CategoryService.get_tree(db)
        for root_node in tree:
            for child_node in root_node.children:
                assert child_node.children == [], "No 3rd tier allowed"
                assert len(child_node.path) == 2


# ==============================================================================
# Vector 3: Category Deletion Active Rules Protection
# ==============================================================================

class TestCategoryDeletionActiveRulesProtection:
    """Verify that deleting a category referenced by active rules is protected."""

    def test_delete_category_with_rules_and_no_reassign_raises_in_use(
        self, db: sqlite3.Connection
    ):
        """Deleting a category with 0 transactions but >=1 active rules must raise CategoryInUseError."""
        # Create a category with NO transactions
        custom_cat = CategoryService.create(
            db, CategoryCreate(name="专有规则类别", direction="expense")
        )

        # Create active rule pointing to this category
        db.execute(
            """
            INSERT INTO rules (name, category_id, priority, field, pattern_type, pattern_value, is_builtin)
            VALUES ('专有规则', ?, 10, 'counterparty', 'contains', '专有测试', 0)
            """,
            (custom_cat.id,),
        )
        db.commit()

        # Deletion without reassign_to_id MUST raise CategoryInUseError
        with pytest.raises(CategoryInUseError, match="referenced by 1 active rules"):
            CategoryService.delete(db, custom_cat.id, reassign_to_id=None)

        # Verify category still exists and rule is NOT cascade-deleted
        assert CategoryService.get_by_id(db, custom_cat.id) is not None
        rule_row = db.execute(
            "SELECT category_id FROM rules WHERE name = '专有规则'"
        ).fetchone()
        assert rule_row is not None
        assert rule_row["category_id"] == custom_cat.id

    def test_delete_category_with_rules_and_valid_reassign_migrates_rules(
        self, db: sqlite3.Connection
    ):
        """Deleting a category with active rules and valid reassign_to_id migrates all rules."""
        custom_cat = CategoryService.create(
            db, CategoryCreate(name="待废弃类别", direction="expense")
        )
        target_cat = CategoryService.get_by_key(db, "food.dining")
        assert target_cat is not None

        # Insert 2 rules pointing to custom_cat
        db.execute(
            """
            INSERT INTO rules (name, category_id, priority, field, pattern_type, pattern_value, is_builtin)
            VALUES ('规则A', ?, 10, 'counterparty', 'contains', '商户A', 0),
                   ('规则B', ?, 20, 'counterparty', 'contains', '商户B', 0)
            """,
            (custom_cat.id, custom_cat.id),
        )
        db.commit()

        # Delete with reassignment
        success = CategoryService.delete(db, custom_cat.id, reassign_to_id=target_cat.id)
        assert success is True

        # Category deleted
        assert CategoryService.get_by_id(db, custom_cat.id) is None

        # Both rules migrated to target_cat.id
        rules = db.execute(
            "SELECT category_id FROM rules WHERE name IN ('规则A', '规则B')"
        ).fetchall()
        assert len(rules) == 2
        for r in rules:
            assert r["category_id"] == target_cat.id

    def test_delete_category_with_both_transactions_and_rules_migrates_both(
        self, db: sqlite3.Connection
    ):
        """When both transactions and rules exist, reassign_to_id migrates both atomically."""
        custom_cat = CategoryService.create(
            db, CategoryCreate(name="综合类别", direction="expense")
        )
        target_cat = CategoryService.get_by_key(db, "food.takeout")
        assert target_cat is not None

        # 2 transactions
        insert_txn(db, txn_id=101, category_id=custom_cat.id, category_source="user_rule")
        insert_txn(db, txn_id=102, category_id=custom_cat.id, category_source="user_rule")

        # 1 rule
        db.execute(
            """
            INSERT INTO rules (name, category_id, priority, field, pattern_type, pattern_value, is_builtin)
            VALUES ('综合规则', ?, 10, 'counterparty', 'contains', '外卖测试', 0)
            """,
            (custom_cat.id,),
        )
        db.commit()

        # Delete without reassign -> raises CategoryInUseError
        with pytest.raises(CategoryInUseError):
            CategoryService.delete(db, custom_cat.id, reassign_to_id=None)

        # Delete with reassign -> succeeds
        success = CategoryService.delete(db, custom_cat.id, reassign_to_id=target_cat.id)
        assert success is True

        # Verify transactions migrated
        t1 = db.execute("SELECT category_id FROM transactions WHERE id = 101").fetchone()[0]
        t2 = db.execute("SELECT category_id FROM transactions WHERE id = 102").fetchone()[0]
        assert t1 == target_cat.id
        assert t2 == target_cat.id

        # Verify rule migrated
        r = db.execute("SELECT category_id FROM rules WHERE name = '综合规则'").fetchone()[0]
        assert r == target_cat.id


# ==============================================================================
# Vector 4: ReDoS Nested Quantifier Rejection
# ==============================================================================

class TestReDoSNestedQuantifierRejection:
    """Stress test rejection of ReDoS nested quantifiers and catastrophic backtracking patterns."""

    @pytest.mark.parametrize(
        "catastrophic_pattern",
        [
            r"^(a+)+$",
            r"(\w+)+",
            r"(x+x+)+y",
            r"(a*)*",
            r"([0-9]+)+",
            r"(a{1,5})+",
            r"(a+){2,}",
            r"((a+)+)",
            r"^([a-z0-9_-]+)+$",
            r"([a-zA-Z]+)*",
        ],
    )
    def test_nested_quantifiers_are_strictly_rejected(self, catastrophic_pattern: str):
        """Nested quantifier regexes must be rejected and return None without hanging."""
        t0 = time.time()
        compiled = compile_safe_regex(catastrophic_pattern)
        elapsed = time.time() - t0

        assert compiled is None, f"Pattern {catastrophic_pattern} should be rejected!"
        assert elapsed < 0.05, f"Check took {elapsed:.4f}s, too slow!"

        # When raise_on_error=True, raises ValueError
        with pytest.raises(ValueError, match="ReDoS|catastrophic"):
            compile_safe_regex(catastrophic_pattern, raise_on_error=True)

        # match_pattern returns False immediately without freeze
        assert match_pattern("regex", catastrophic_pattern, "a" * 30 + "!") is False

    @pytest.mark.parametrize(
        "safe_pattern",
        [
            r"(?i)mcdonald|麦当劳",
            r"^星巴克.*",
            r"^\d{4}-\d{2}-\d{2}$",
            r"美团[外卖|优选]",
            r"[a-zA-Z0-9._%+-]+",
            r"全家|罗森|7-eleven",
        ],
    )
    def test_safe_patterns_compile_and_match_successfully(self, safe_pattern: str):
        """Standard valid production regex patterns must compile and match correctly."""
        import regex as regex_lib
        compiled = compile_safe_regex(safe_pattern)
        assert compiled is not None
        assert isinstance(compiled, (re.Pattern, regex_lib.Pattern))

    def test_rule_with_redos_pattern_fails_safe_without_hanging(self):
        """A Rule with ReDoS regex safely returns False on rule_matches without hanging."""
        redos_rule = Rule(
            id=99,
            name="恶意ReDoS规则",
            category_id=1,
            field="counterparty",
            pattern_type="regex",
            pattern_value=r"^(a+)+$",
        )

        txn = {"counterparty": "a" * 28 + "!", "amount_cents": 1000}

        t0 = time.time()
        matched = rule_matches(redos_rule, txn)
        elapsed = time.time() - t0

        assert matched is False
        assert elapsed < 0.01, f"Evaluation hung for {elapsed:.4f}s"


# ==============================================================================
# Vector 5: apply_rule_to_existing with Regex Rules
# ==============================================================================

class TestApplyRuleToExistingWithRegexRules:
    """Verify that regex rules work correctly in apply_rule_to_existing and protect manual edits."""

    def test_apply_regex_rule_to_matching_historical_transactions(self, db: sqlite3.Connection):
        """Regex rules must find and update matching transactions via Python evaluation."""
        dining = CategoryService.get_by_key(db, "food.dining")
        assert dining is not None

        takeout = CategoryService.get_by_key(db, "food.takeout")
        assert takeout is not None

        # Insert 10 transactions
        # 3 match Starbucks regex
        insert_txn(db, txn_id=1, counterparty="星巴克咖啡上海港汇店")
        insert_txn(db, txn_id=2, counterparty="STARBUCKS RESERVE SHANGHAI")
        insert_txn(db, txn_id=3, counterparty="星巴克企业管理有限公司")
        # 1 matches Starbucks regex but is MANUAL override pointing to takeout
        insert_txn(
            db,
            txn_id=4,
            counterparty="星巴克臻选店",
            category_id=takeout.id,
            category_source="manual",
        )
        # 2 match KFC
        insert_txn(db, txn_id=5, counterparty="肯德基五角场店")
        insert_txn(db, txn_id=6, counterparty="KFC 宅急送")
        # 4 others
        insert_txn(db, txn_id=7, counterparty="中国联通交费")
        insert_txn(db, txn_id=8, counterparty="国家电网缴费")
        insert_txn(db, txn_id=9, counterparty="全家便利店")
        insert_txn(db, txn_id=10, counterparty="罗森便利店")

        # Create regex rule for Starbucks
        db.execute(
            """
            INSERT INTO rules (id, name, category_id, priority, field, pattern_type, pattern_value, is_builtin, hit_count)
            VALUES (10, '星巴克正则规则', ?, 10, 'counterparty', 'regex', '(?i)starbucks|星巴克', 0, 0)
            """,
            (dining.id,),
        )
        db.commit()

        rule = Rule(
            id=10,
            name="星巴克正则规则",
            category_id=dining.id,
            priority=10,
            field="counterparty",
            pattern_type="regex",
            pattern_value=r"(?i)starbucks|星巴克",
            hit_count=0,
        )

        # Apply rule with overwrite_manual=False
        result = apply_rule_to_existing(db, rule, overwrite_manual=False)

        # Exactly 4 counterparties match the regex (1, 2, 3, 4)
        # Txn 4 is manual -> skipped
        # Txn 1, 2, 3 -> updated
        assert result["matched_count"] == 4
        assert result["updated_count"] == 3
        assert result["skipped_manual_count"] == 1

        # Check DB updates for txns 1, 2, 3
        for tid in (1, 2, 3):
            row = db.execute(
                "SELECT category_id, category_source, category_confidence FROM transactions WHERE id = ?",
                (tid,),
            ).fetchone()
            assert row["category_id"] == dining.id
            assert row["category_source"] == "user_rule"
            assert row["category_confidence"] == 1.0

        # Check Txn 4 (manual) remains intact pointing to takeout
        manual_row = db.execute(
            "SELECT category_id, category_source FROM transactions WHERE id = 4"
        ).fetchone()
        assert manual_row["category_id"] == takeout.id
        assert manual_row["category_source"] == "manual"

        # Check non-matching txns (5 to 10) remain unclassified
        for tid in (5, 6, 7, 8, 9, 10):
            row = db.execute(
                "SELECT category_id, category_source FROM transactions WHERE id = ?", (tid,)
            ).fetchone()
            assert row["category_id"] is None
            assert row["category_source"] is None

        # Check rule hit_count was updated in DB
        rule_db = db.execute("SELECT hit_count FROM rules WHERE id = 10").fetchone()
        assert rule_db["hit_count"] == 3

    def test_apply_regex_rule_with_force_reclassify_overwrites_manual(
        self, db: sqlite3.Connection
    ):
        """When force_reclassify / overwrite_manual=True, manual records are reclassified."""
        dining = CategoryService.get_by_key(db, "food.dining")
        takeout = CategoryService.get_by_key(db, "food.takeout")
        assert dining is not None and takeout is not None

        insert_txn(
            db,
            txn_id=20,
            counterparty="星巴克咖啡上海店",
            category_id=takeout.id,
            category_source="manual",
        )

        rule = Rule(
            id=11,
            name="星巴克覆盖规则",
            category_id=dining.id,
            priority=10,
            field="counterparty",
            pattern_type="regex",
            pattern_value=r"(?i)starbucks|星巴克",
            hit_count=0,
        )

        result = apply_rule_to_existing(db, rule, overwrite_manual=True)

        assert result["matched_count"] == 1
        assert result["updated_count"] == 1
        assert result["skipped_manual_count"] == 0

        row = db.execute(
            "SELECT category_id, category_source FROM transactions WHERE id = 20"
        ).fetchone()
        assert row["category_id"] == dining.id
        assert row["category_source"] == "user_rule"

    def test_apply_regex_rule_targeting_description_and_batch_filter(
        self, db: sqlite3.Connection
    ):
        """Regex rule targeting description and filtered by batch_id works as expected."""
        transport = CategoryService.get_by_key(db, "transport")
        assert transport is not None

        # Create batch 2
        db.execute(
            "INSERT INTO import_batches (id, file_name, file_sha1, file_size, source) "
            "VALUES (2, 'batch2.csv', 'sha1_batch2', 2048, 'wechat')"
        )
        db.commit()

        # Batch 1 txns
        insert_txn(db, txn_id=31, counterparty="滴滴出行", description="快车行程单费用", batch_id=1)
        insert_txn(db, txn_id=32, counterparty="美团单车", description="骑行月卡扣款", batch_id=1)

        # Batch 2 txn
        insert_txn(db, txn_id=33, counterparty="花小猪", description="快车打车费", batch_id=2)

        rule = Rule(
            id=12,
            name="打车描述正则",
            category_id=transport.id,
            priority=10,
            field="description",
            pattern_type="regex",
            pattern_value=r"快车|骑行",
            hit_count=0,
        )

        # Restrict to batch_id=1
        result = apply_rule_to_existing(db, rule, batch_id=1, overwrite_manual=False)

        # Txn 31 and 32 match; txn 33 in batch 2 is excluded by batch_id
        assert result["matched_count"] == 2
        assert result["updated_count"] == 2

        # Verify txn 33 is untouched
        row33 = db.execute("SELECT category_id FROM transactions WHERE id = 33").fetchone()
        assert row33["category_id"] is None

    def test_apply_regex_rule_with_malformed_regex_fails_safe(self, db: sqlite3.Connection):
        """Malformed regex rule in apply_rule_to_existing fails safe with 0 matches."""
        dining = CategoryService.get_by_key(db, "food.dining")
        assert dining is not None

        insert_txn(db, txn_id=40, counterparty="测试商户")

        rule = Rule(
            id=13,
            name="畸形正则规则",
            category_id=dining.id,
            field="counterparty",
            pattern_type="regex",
            pattern_value="[a-z",  # unclosed class
            hit_count=0,
        )

        result = apply_rule_to_existing(db, rule, overwrite_manual=False)
        assert result["matched_count"] == 0
        assert result["updated_count"] == 0

    def test_delete_root_with_children_and_no_reassign_raises_value_error(
        self, db: sqlite3.Connection
    ):
        """Deleting root category with children without reassign_to_id raises ValueError."""
        food = CategoryService.get_by_key(db, "food")
        assert food is not None

        with pytest.raises(ValueError, match="has .* subcategories. Reassign children first"):
            CategoryService.delete(db, food.id, reassign_to_id=None)

    def test_delete_subcategory_reassign_to_sibling_succeeds(self, db: sqlite3.Connection):
        """Deleting a subcategory and reassigning its txns and rules to sibling subcategory succeeds."""
        dining = CategoryService.get_by_key(db, "food.dining")
        takeout = CategoryService.get_by_key(db, "food.takeout")
        assert dining is not None and takeout is not None

        # Insert 1 txn and 1 rule for dining
        insert_txn(db, txn_id=50, category_id=dining.id, category_source="user_rule")
        db.execute(
            """
            INSERT INTO rules (name, category_id, priority, field, pattern_type, pattern_value, is_builtin)
            VALUES ('临时正餐规则', ?, 10, 'counterparty', 'contains', '临时', 0)
            """,
            (dining.id,),
        )
        db.commit()

        # Delete dining reassign to takeout
        success = CategoryService.delete(db, dining.id, reassign_to_id=takeout.id)
        assert success is True

        assert CategoryService.get_by_id(db, dining.id) is None
        t50 = db.execute("SELECT category_id FROM transactions WHERE id = 50").fetchone()[0]
        assert t50 == takeout.id
        r = db.execute("SELECT category_id FROM rules WHERE name = '临时正餐规则'").fetchone()[0]
        assert r == takeout.id
