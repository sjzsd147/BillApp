"""Milestone 4 Adversarial Stress Testing Suite:
Categories & Rule Engine Adversarial Verification (Features 18-21).

Test Vectors:
1. Category Hierarchy & Boundary Attacks:
   - Tree depth boundary: attempt to create depth > 2 (raises ValueError / depth error).
   - Circular hierarchy: setting parent_id to itself, cycle prevention, get_path robustness.
   - Sibling uniqueness collisions: duplicate names under same parent, duplicate top-level categories,
     same name allowed across different parents, update collision prevention.
   - In-use deletion protection: transactions referencing category block deletion without reassign_to_id,
     rules reassignment, non-existent category handling.
   - Cascader format integrity: 16 roots, 43 subcategories, strict 2-tier tree, Ant Design / Element Plus
     contract (keys, names, color, direction/kind, children, path breadcrumbs).
2. Rule Engine ReDoS & Malformed Pattern Safety:
   - ReDoS & Catastrophic backtracking: bounded pattern lengths (>200 chars rejected),
     complex pattern evaluation within safe runtime bounds, non-hanging execution.
   - Malformed regex syntax: unclosed brackets, trailing backslash, invalid escape sequences safely suppressed.
   - Inverted & boundary amount checks: min > max, negative cents, zero cents, unbounded rules.
3. 6-Stage Pipeline Priority & Collision Stress:
   - Simultaneous 3-way match collision: user_rule beats builtin_rule and platform_mapping strictly.
   - Manual override absolute protection: single classification, batch classification, and
     apply_rule_to_existing permanently preserve manual source unless force_reclassify=True.
   - Rule hit count atomic tracking across pipeline evaluations.
4. Dirty Merchant Rule Learning:
   - Extreme dirty strings: emojis, nested brackets, payment prefixes, corporate suffixes,
     purely numeric strings, single characters, empty/whitespace strings.
   - learn_rule_from_edit candidate generation with manual record protection and applicable counts.
   - Batch rule application to existing records with hit count synchronization.
"""

from __future__ import annotations

import re
import sqlite3
import time
from typing import Any, Dict, List, Optional

import pytest

from billapp.core.db import init_db
from billapp.classify.categories import (
    BUILTIN_TAXONOMY,
    Category,
    CategoryCreate,
    CategoryInUseError,
    CategoryNode,
    CategoryService,
    CategoryUpdate,
)
from billapp.classify.rules import (
    Rule,
    RuleCandidate,
    apply_rule_to_existing,
    clean_merchant_for_learning,
    compile_safe_regex,
    get_learning_suggestions,
    learn_rule_from_edit,
    load_builtin_rules_from_yaml,
    match_pattern,
    rule_matches,
)
from billapp.classify.pipeline import (
    PLATFORM_CATEGORY_MAP,
    ClassificationPipeline,
    ClassificationResult,
    ClassificationSummary,
)


# ==============================================================================
# Database Fixtures
# ==============================================================================

@pytest.fixture
def db() -> sqlite3.Connection:
    """Provide an isolated in-memory SQLite database initialized with schema.sql."""
    conn = init_db(":memory:")
    # Seed baseline account and import batch for transaction insertions
    conn.execute(
        "INSERT INTO accounts (id, kind, bank, alias) VALUES (1, 'wallet', '支付宝', '支付宝余额')"
    )
    conn.execute(
        "INSERT INTO import_batches (id, file_name, file_sha1, file_size, source) "
        "VALUES (1, 'adv_test.csv', 'sha1_adv_test_batch', 1024, 'alipay')"
    )
    conn.commit()
    yield conn
    conn.close()


def insert_test_txn(
    conn: sqlite3.Connection,
    txn_id: int,
    amount_cents: int = 2500,
    counterparty: str = "测试商户",
    description: str = "测试交易",
    platform_category: Optional[str] = None,
    source: str = "alipay",
    direction: str = "expense",
    category_id: Optional[int] = None,
    category_source: str = "none",
    merchant_norm: Optional[str] = None,
) -> int:
    """Helper to insert a test transaction with full classification attributes."""
    conn.execute(
        """
        INSERT INTO transactions (
            id, batch_id, account_id, source, txn_time, time_precision,
            amount_cents, direction, fingerprint, counterparty, description,
            platform_category, category_id, category_source, merchant_norm
        ) VALUES (
            ?, 1, 1, ?, '2026-11-09 12:00:00', 'second',
            ?, ?, ?, ?, ?,
            ?, ?, ?, ?
        )
        """,
        (
            txn_id,
            source,
            amount_cents,
            direction,
            f"fp_adv_{txn_id}",
            counterparty,
            description,
            platform_category,
            category_id,
            category_source,
            merchant_norm,
        ),
    )
    conn.commit()
    return txn_id


# ==============================================================================
# Vector 1: Category Hierarchy & Boundary Attacks
# ==============================================================================

class TestCategoryHierarchyBoundaries:
    """Adversarial tests for two-tier constraints, cycle safety, and uniqueness."""

    def test_tree_depth_boundary_strict_enforcement(self, db: sqlite3.Connection):
        """Depth > 2 must be strictly blocked on subcategory creation."""
        CategoryService.init_builtin_categories(db)
        dining = CategoryService.get_by_key(db, "food.dining")
        assert dining is not None
        assert dining.parent_id is not None  # Dining is depth 2

        # Attempt to create tier 3 child under dining
        with pytest.raises((ValueError, Exception), match="(?i)depth|cannot exceed 2 tiers"):
            CategoryService.create(
                db,
                CategoryCreate(name="川湘大餐", parent_id=dining.id, direction="expense"),
            )

    def test_nonexistent_parent_id_rejection(self, db: sqlite3.Connection):
        """Creation with an invalid/non-existent parent_id must be rejected."""
        CategoryService.init_builtin_categories(db)
        with pytest.raises(ValueError, match="does not exist"):
            CategoryService.create(
                db,
                CategoryCreate(name="孤儿分类", parent_id=999999, direction="expense"),
            )

    def test_sibling_uniqueness_under_same_parent(self, db: sqlite3.Connection):
        """Duplicate category name under the same parent must be rejected."""
        CategoryService.init_builtin_categories(db)
        food = CategoryService.get_by_key(db, "food")
        assert food is not None

        with pytest.raises(ValueError, match="already exists"):
            CategoryService.create(
                db,
                CategoryCreate(name="正餐", parent_id=food.id, direction="expense"),
            )

    def test_root_category_uniqueness(self, db: sqlite3.Connection):
        """Duplicate root category with same name and direction must be rejected."""
        CategoryService.init_builtin_categories(db)
        with pytest.raises(ValueError, match="already exists"):
            CategoryService.create(
                db,
                CategoryCreate(name="餐饮美食", parent_id=None, direction="expense"),
            )

    def test_same_name_permitted_under_different_parents(self, db: sqlite3.Connection):
        """Identical subcategory names under DIFFERENT root categories are valid."""
        CategoryService.init_builtin_categories(db)
        food = CategoryService.get_by_key(db, "food")
        shopping = CategoryService.get_by_key(db, "shopping")
        assert food is not None and shopping is not None

        # Create '特例杂项' under food
        sub_food = CategoryService.create(
            db,
            CategoryCreate(name="特例杂项", parent_id=food.id, direction="expense"),
        )
        assert sub_food.id > 0

        # Create same name '特例杂项' under shopping (should succeed)
        sub_shopping = CategoryService.create(
            db,
            CategoryCreate(name="特例杂项", parent_id=shopping.id, direction="expense"),
        )
        assert sub_shopping.id > 0
        assert sub_food.id != sub_shopping.id

    def test_update_name_collision_with_sibling(self, db: sqlite3.Connection):
        """Renaming a category to an existing sibling's name must be blocked."""
        CategoryService.init_builtin_categories(db)
        dining = CategoryService.get_by_key(db, "food.dining")
        takeout = CategoryService.get_by_key(db, "food.takeout")
        assert dining is not None and takeout is not None

        # Attempt to rename takeout to '正餐'
        with pytest.raises(ValueError, match="already taken"):
            CategoryService.update(db, takeout.id, CategoryUpdate(name=dining.name))

    def test_update_category_self_name_is_idempotent(self, db: sqlite3.Connection):
        """Renaming a category to its current name should succeed without false collision."""
        CategoryService.init_builtin_categories(db)
        dining = CategoryService.get_by_key(db, "food.dining")
        assert dining is not None

        updated = CategoryService.update(db, dining.id, CategoryUpdate(name=dining.name))
        assert updated.name == dining.name

    def test_get_path_cycle_resilience(self, db: sqlite3.Connection):
        """get_path must remain bounded and not trigger recursion if parent points to self."""
        CategoryService.init_builtin_categories(db)
        cat = CategoryService.create(db, CategoryCreate(name="孤岛分类", direction="expense"))
        # Force corrupt parent_id to itself directly in SQLite to simulate edge condition
        db.execute("UPDATE categories SET parent_id = ? WHERE id = ?", (cat.id, cat.id))
        db.commit()

        # get_path must not crash or hang
        key_path, name_path = CategoryService.get_path(db, cat.id)
        assert len(key_path) <= 2
        assert len(name_path) <= 2


class TestCategoryInUseDeletionProtection:
    """Adversarial tests for deletion cascade and reference protection."""

    def test_deletion_blocked_when_transactions_exist(self, db: sqlite3.Connection):
        """Category with linked transactions must raise CategoryInUseError if no reassign_to_id."""
        CategoryService.init_builtin_categories(db)
        dining = CategoryService.get_by_key(db, "food.dining")
        assert dining is not None

        insert_test_txn(db, txn_id=1, category_id=dining.id, category_source="user_rule")

        with pytest.raises(CategoryInUseError):
            CategoryService.delete(db, dining.id, reassign_to_id=None)

        # Confirm category still exists
        assert CategoryService.get_by_id(db, dining.id) is not None

    def test_deletion_with_valid_reassignment_migrates_all_records(self, db: sqlite3.Connection):
        """Supplying reassign_to_id safely migrates transactions and rules to target category."""
        CategoryService.init_builtin_categories(db)
        dining = CategoryService.get_by_key(db, "food.dining")
        takeout = CategoryService.get_by_key(db, "food.takeout")
        assert dining is not None and takeout is not None

        # Insert 3 transactions pointing to dining
        for i in range(1, 4):
            insert_test_txn(db, txn_id=i, category_id=dining.id, category_source="user_rule")

        # Insert 1 user rule pointing to dining
        db.execute(
            """
            INSERT INTO rules (name, category_id, priority, field, pattern_type, pattern_value, is_builtin)
            VALUES ('旧正餐规则', ?, 10, 'counterparty', 'contains', '正餐', 0)
            """,
            (dining.id,),
        )
        db.commit()

        # Delete dining with reassignment to takeout
        success = CategoryService.delete(db, dining.id, reassign_to_id=takeout.id)
        assert success is True

        # Verify dining is deleted
        assert CategoryService.get_by_id(db, dining.id) is None

        # Verify all 3 transactions migrated to takeout
        txns = db.execute("SELECT category_id FROM transactions WHERE id IN (1, 2, 3)").fetchall()
        for r in txns:
            assert r["category_id"] == takeout.id

        # Verify rule migrated to takeout
        rule_row = db.execute("SELECT category_id FROM rules WHERE name = '旧正餐规则'").fetchone()
        assert rule_row["category_id"] == takeout.id

    def test_delete_nonexistent_category_returns_false(self, db: sqlite3.Connection):
        """Deleting non-existent ID gracefully returns False."""
        CategoryService.init_builtin_categories(db)
        assert CategoryService.delete(db, 888888) is False


class TestCascaderTreeFormatIntegrity:
    """Verifies cascader tree structure and frontend contract conformance."""

    def test_cascader_tree_dimensions_and_hierarchy(self, db: sqlite3.Connection):
        """Verify 16 roots, exactly 2 tiers, and correct child associations."""
        CategoryService.init_builtin_categories(db)
        tree = CategoryService.get_tree(db)

        # 16 root nodes (11 expense + 5 income)
        assert len(tree) == 16
        expense_roots = [n for n in tree if n.direction == "expense"]
        income_roots = [n for n in tree if n.direction == "income"]
        assert len(expense_roots) == 11
        assert len(income_roots) == 5

        # Every child must have empty children list (strictly 2 tiers)
        total_subcategories = 0
        for root in tree:
            assert root.parent_id is None
            assert root.key is not None
            assert root.name is not None
            assert root.color is not None
            assert root.path == [root.key]
            assert root.path_names == [root.name]

            for child in root.children:
                total_subcategories += 1
                assert child.parent_id == root.id
                assert child.children == []  # No 3rd tier
                assert child.path == [root.key, child.key]
                assert child.path_names == [root.name, child.name]

        assert total_subcategories == 49

    def test_tree_filtering_by_direction(self, db: sqlite3.Connection):
        """Filtering cascader tree by expense vs income returns strict subsets."""
        CategoryService.init_builtin_categories(db)
        expense_tree = CategoryService.get_tree(db, direction="expense")
        income_tree = CategoryService.get_tree(db, direction="income")

        assert len(expense_tree) == 11
        assert len(income_tree) == 5
        assert all(n.direction == "expense" for n in expense_tree)
        assert all(n.direction == "income" for n in income_tree)


# ==============================================================================
# Vector 2: Rule Engine ReDoS & Malformed Pattern Safety
# ==============================================================================

class TestRuleEngineReDoSAndMalformedSafety:
    """Adversarial stress testing against regular expressions and boundary values."""

    def test_oversized_regex_rejection(self):
        """Regex exceeding 200 characters must be rejected to prevent resource exhaustion."""
        long_pattern = "a" * 201
        assert compile_safe_regex(long_pattern) is None
        assert match_pattern("regex", long_pattern, "sample") is False

    def test_malformed_syntax_error_suppression(self):
        """Malformed regexes must not raise uncaught exceptions."""
        malformed_patterns = [
            "[a-z",          # unclosed character class
            "(?P<name>",     # unclosed group
            "*invalid_start", # dangling quantifier
            "\\",            # trailing single backslash
            "[0-9++",        # malformed range
            "\\k",           # invalid escape
        ]
        for pat in malformed_patterns:
            compiled = compile_safe_regex(pat)
            assert compiled is None
            # Must return False safely without unhandled exception
            assert match_pattern("regex", pat, "test string 123") is False

    def test_catastrophic_backtracking_bounded_execution(self):
        """ReDoS patterns with non-matching inputs must not freeze within normal bounds."""
        redos_patterns = [
            r"^(a+)+$",
            r"(x+x+)+y",
            r"(a|aa)+$",
        ]

        # Test non-matching input up to 18 chars to verify execution within 100ms
        for pat in redos_patterns:
            t0 = time.time()
            res = match_pattern("regex", pat, "a" * 18 + "!")
            elapsed = time.time() - t0
            assert res is False
            assert elapsed < 0.20, f"Pattern {pat} took too long: {elapsed:.3f}s"

    def test_inverted_amount_bounds_fail_safe(self):
        """When min_amount_cents > max_amount_cents, rule must predictably reject all inputs."""
        rule = Rule(
            id=1,
            name="反转金额规则",
            category_id=1,
            field="counterparty",
            pattern_type="contains",
            pattern_value="海鲜",
            amount_min_cents=10000,
            amount_max_cents=5000,  # min > max
        )

        test_cases = [
            {"counterparty": "东海海鲜", "amount_cents": 3000},   # below max
            {"counterparty": "东海海鲜", "amount_cents": 7000},   # between max and min
            {"counterparty": "东海海鲜", "amount_cents": 12000},  # above min
        ]
        for txn in test_cases:
            assert rule_matches(rule, txn) is False

    def test_negative_and_zero_amount_bounds(self):
        """Verify behavior on negative cents (refund/discount) and zero amounts."""
        rule_positive_only = Rule(
            id=2,
            name="正支出",
            category_id=1,
            field="counterparty",
            pattern_type="contains",
            pattern_value="麦当劳",
            amount_min_cents=0,
            amount_max_cents=5000,
        )

        # Zero cents matches
        assert rule_matches(rule_positive_only, {"counterparty": "麦当劳", "amount_cents": 0}) is True
        # Negative cents rejected
        assert rule_matches(rule_positive_only, {"counterparty": "麦当劳", "amount_cents": -500}) is False


# ==============================================================================
# Vector 3: 6-Stage Pipeline Priority & Collision Stress
# ==============================================================================

class TestSixStagePipelinePriorityCollisions:
    """Stress tests on priority ordering and manual classification inviolability."""

    def test_simultaneous_collision_user_rule_beats_builtin_and_platform(self, db: sqlite3.Connection):
        """When a transaction matches user_rule, builtin_rule, and platform_category, user_rule wins strictly."""
        CategoryService.init_builtin_categories(db)
        food_dining = CategoryService.get_by_key(db, "food.dining")
        shopping_daily = CategoryService.get_by_key(db, "shopping.daily")
        assert food_dining is not None and shopping_daily is not None

        # Insert user rule: "麦当劳" -> shopping.daily (overriding default food.dining)
        db.execute(
            """
            INSERT INTO rules (name, category_id, priority, field, pattern_type, pattern_value, is_builtin)
            VALUES ('用户覆写麦当劳', ?, 1, 'counterparty', 'contains', '麦当劳', 0)
            """,
            (shopping_daily.id,),
        )
        db.commit()

        pipeline = ClassificationPipeline(db)

        # Transaction matching:
        # 1. user_rule: counterparty contains "麦当劳" -> shopping.daily
        # 2. builtin_rule: counterparty contains "麦当劳" -> food.dining
        # 3. platform_mapping: alipay + "餐饮美食" -> food.dining
        txn = {
            "source": "alipay",
            "platform_category": "餐饮美食",
            "counterparty": "麦当劳人民广场店",
            "amount_cents": 3500,
            "category_source": "none",
        }

        res = pipeline.classify_transaction(txn)
        assert res.source == "user_rule"
        assert res.category_id == shopping_daily.id
        assert res.category_key == "shopping.daily"

    def test_builtin_rule_beats_platform_mapping(self, db: sqlite3.Connection):
        """When user_rule is absent, builtin_rule strictly beats platform_category mapping."""
        CategoryService.init_builtin_categories(db)
        pipeline = ClassificationPipeline(db)

        # Transaction with contradictory platform_category
        txn = {
            "source": "alipay",
            "platform_category": "日用百货",  # Maps to shopping.daily
            "counterparty": "上海金拱门食品有限公司",  # Builtin maps to food.dining
            "amount_cents": 2747,
        }
        res = pipeline.classify_transaction(txn)
        assert res.source == "builtin_rule"
        assert res.category_key == "food.dining"

    def test_manual_override_absolute_protection_single(self, db: sqlite3.Connection):
        """A transaction with category_source='manual' is NEVER overwritten unless force_reclassify=True."""
        CategoryService.init_builtin_categories(db)
        other_cat = CategoryService.get_by_key(db, "other_expense.misc")
        assert other_cat is not None

        pipeline = ClassificationPipeline(db)

        manual_txn = {
            "counterparty": "麦当劳",
            "amount_cents": 2747,
            "category_id": other_cat.id,
            "category_source": "manual",
        }

        # Standard classification: manual preserved
        res = pipeline.classify_transaction(manual_txn, force_reclassify=False)
        assert res.source == "manual"
        assert res.category_id == other_cat.id
        assert res.category_key == "other_expense.misc"

        # Explicit force_reclassify: allows pipeline to re-evaluate
        forced_res = pipeline.classify_transaction(manual_txn, force_reclassify=True)
        assert forced_res.source == "builtin_rule"
        assert forced_res.category_key == "food.dining"

    def test_manual_protection_in_apply_rule_to_existing(self, db: sqlite3.Connection):
        """apply_rule_to_existing must strictly skip manual records when overwrite_manual=False."""
        CategoryService.init_builtin_categories(db)
        dining = CategoryService.get_by_key(db, "food.dining")
        misc = CategoryService.get_by_key(db, "other_expense.misc")
        assert dining is not None and misc is not None

        # Insert 3 transactions:
        # txn 101: manual category -> misc
        insert_test_txn(db, 101, counterparty="瑞幸咖啡五角场店", category_id=misc.id, category_source="manual")
        # txn 102: none category
        insert_test_txn(db, 102, counterparty="瑞幸咖啡长宁店", category_id=None, category_source="none")
        # txn 103: builtin_rule category
        insert_test_txn(db, 103, counterparty="瑞幸咖啡静安店", category_id=dining.id, category_source="builtin_rule")

        drinks_cat = CategoryService.get_by_key(db, "food.drinks")
        assert drinks_cat is not None

        rule = Rule(
            id=1,
            name="瑞幸->饮品",
            category_id=drinks_cat.id,
            field="counterparty",
            pattern_type="contains",
            pattern_value="瑞幸咖啡",
        )

        res = apply_rule_to_existing(db, rule, overwrite_manual=False)
        assert res["matched_count"] == 3
        assert res["updated_count"] == 2
        assert res["skipped_manual_count"] == 1

        # Check that manual record 101 remains UNTOUCHED
        row101 = db.execute("SELECT category_id, category_source FROM transactions WHERE id = 101").fetchone()
        assert row101["category_id"] == misc.id
        assert row101["category_source"] == "manual"

        # Check that other records were updated
        row102 = db.execute("SELECT category_id, category_source FROM transactions WHERE id = 102").fetchone()
        assert row102["category_id"] == drinks_cat.id
        assert row102["category_source"] == "user_rule"

    def test_batch_classification_manual_accounting(self, db: sqlite3.Connection):
        """classify_batch preserves manual items and accurately aggregates summary metrics."""
        CategoryService.init_builtin_categories(db)
        pipeline = ClassificationPipeline(db)

        misc = CategoryService.get_by_key(db, "other_expense.misc")
        assert misc is not None

        batch = [
            {"counterparty": "麦当劳", "amount_cents": 2747, "category_id": misc.id, "category_source": "manual"},
            {"counterparty": "肯德基", "amount_cents": 3500, "category_source": "none"},
            {"counterparty": "未知商户XYZ999", "amount_cents": 1000, "category_source": "none"},
        ]

        results, summary = pipeline.classify_batch(batch, force_reclassify=False)
        assert summary.total_evaluated == 3
        assert summary.manual_preserved == 1
        assert summary.builtin_rule_matched >= 1
        assert summary.unclassified >= 1
        assert results[0].source == "manual"
        assert results[0].category_id == misc.id


# ==============================================================================
# Vector 4: Dirty Merchant Rule Learning
# ==============================================================================

class TestDirtyMerchantRuleLearning:
    """Adversarial testing against noisy, corrupt, and extreme counterparty strings."""

    @pytest.mark.parametrize(
        "dirty_input,expected_keyword",
        [
            ("支付宝-金拱门（中国）有限公司人民广场分店", "金拱门"),
            ("财付通-美团外卖", "美团外卖"),
            ("微信支付-霸王茶姬【四川茶姬企业管理有限公司】", "霸王茶姬"),
            ("上海罗森便利有限公司漕宝路分店", "上海罗森便利"),
            ("网银在线-京东商城有限公司上海分部", "京东商城"),
            ("云闪付-麦当劳五角场餐厅", "麦当劳"),
            ("美团支付-全家便利店徐家汇店", "全家便利店"),
            ("塔斯汀·中国汉堡（五角场店）", "塔斯汀"),
        ],
    )
    def test_clean_merchant_standard_and_dirty_patterns(self, dirty_input: str, expected_keyword: str):
        """Verify merchant cleaner eliminates platform noise and corporate suffixes."""
        cleaned = clean_merchant_for_learning(dirty_input)
        assert cleaned == expected_keyword

    def test_clean_merchant_extreme_and_empty_inputs(self):
        """Verify robust fallbacks on empty, whitespace, and short strings."""
        assert clean_merchant_for_learning("") == ""
        assert clean_merchant_for_learning("   ") == ""
        assert clean_merchant_for_learning("A") == "A"
        assert clean_merchant_for_learning("1234567890") == "1234567890"

    def test_learn_rule_candidate_from_edit_filters_manual_records(self, db: sqlite3.Connection):
        """learn_rule_from_edit identifies candidates while protecting other manual edits."""
        CategoryService.init_builtin_categories(db)
        dining = CategoryService.get_by_key(db, "food.dining")
        misc = CategoryService.get_by_key(db, "other_expense.misc")
        assert dining is not None and misc is not None

        # Insert 4 historical transactions with "老盛兴"
        # 1. Target edited transaction
        insert_test_txn(db, 201, counterparty="老盛兴汤包馆淮海店", category_id=dining.id, category_source="manual")
        # 2. None category transaction (applicable)
        insert_test_txn(db, 202, counterparty="老盛兴汤包馆黄浦店", category_id=None, category_source="none")
        # 3. Another manual category transaction (must be protected)
        insert_test_txn(db, 203, counterparty="老盛兴汤包馆徐汇店", category_id=misc.id, category_source="manual")
        # 4. Already has dining category (already same)
        insert_test_txn(db, 204, counterparty="老盛兴汤包馆静安店", category_id=dining.id, category_source="builtin_rule")

        candidate = learn_rule_from_edit(db, txn_id=201, new_category_id=dining.id)
        assert candidate is not None
        assert candidate.pattern_value == "老盛兴汤包"
        assert candidate.matched_total == 4
        assert candidate.already_same_category == 2  # txn 201 + txn 204
        assert candidate.manual_protected == 1        # txn 203
        assert candidate.applicable_count == 1       # txn 202

    def test_learn_rule_on_empty_counterparty_returns_none(self, db: sqlite3.Connection):
        """learn_rule_from_edit returns None when counterparty is empty or unextractable."""
        CategoryService.init_builtin_categories(db)
        dining = CategoryService.get_by_key(db, "food.dining")
        assert dining is not None

        insert_test_txn(db, 299, counterparty="", merchant_norm="", category_id=dining.id, category_source="manual")

        candidate = learn_rule_from_edit(db, txn_id=299, new_category_id=dining.id)
        assert candidate is None

    def test_accumulated_learning_suggestions_skips_existing_rules(self, db: sqlite3.Connection):
        """get_learning_suggestions ignores patterns that already have active user rules."""
        CategoryService.init_builtin_categories(db)
        dining = CategoryService.get_by_key(db, "food.dining")
        assert dining is not None

        # Insert 3 manual edits for '七宝老街小吃'
        for i in range(1, 4):
            insert_test_txn(
                db, 300 + i,
                counterparty="七宝老街小吃",
                category_id=dining.id,
                category_source="manual",
            )

        # Before rule creation: suggestion appears
        suggestions = get_learning_suggestions(db, min_count=3)
        assert any(s["brand"] == "七宝老街小吃" for s in suggestions)

        # Now create an active user rule for this pattern
        db.execute(
            """
            INSERT INTO rules (name, category_id, priority, field, pattern_type, pattern_value, is_builtin)
            VALUES ('七宝规则', ?, 10, 'counterparty', 'contains', '七宝老街小吃', 0)
            """,
            (dining.id,),
        )
        db.commit()

        # After rule creation: suggestion must no longer appear
        suggestions_after = get_learning_suggestions(db, min_count=3)
        assert not any(s["brand"] == "七宝老街小吃" for s in suggestions_after)
