"""Comprehensive unit tests for Milestone 4 Classification Engine:
- Category Taxonomy Tree & CRUD (F18)
- 6-Stage Priority Classification Pipeline (F19)
- Builtin Rules Coverage >=85% (F20)
- Rule Engine & Learning with Manual Record Protection (F21)
"""

import re
import sqlite3
import pytest
from pathlib import Path

from billapp.core.db import init_db
from billapp.classify.categories import (
    CategoryService,
    CategoryCreate,
    CategoryUpdate,
    CategoryInUseError,
    BUILTIN_TAXONOMY,
)
from billapp.classify.rules import (
    Rule,
    compile_safe_regex,
    match_pattern,
    rule_matches,
    clean_merchant_for_learning,
    learn_rule_from_edit,
    apply_rule_to_existing,
    get_learning_suggestions,
    load_builtin_rules_from_yaml,
)
from billapp.classify.pipeline import (
    ClassificationPipeline,
    ClassificationResult,
)


@pytest.fixture
def db_conn() -> sqlite3.Connection:
    """Provide an in-memory SQLite database initialized with schema.sql."""
    conn = init_db(":memory:")
    conn.execute(
        "INSERT INTO accounts (id, kind, bank, alias) VALUES (1, 'wallet', '支付宝', '支付宝余额')"
    )
    conn.execute(
        "INSERT INTO import_batches (id, file_name, file_sha1, file_size, source) "
        "VALUES (1, 'test.csv', 'sha1_test_batch', 100, 'alipay')"
    )
    conn.commit()
    yield conn
    conn.close()


# =========================================================================
# 1. Category Taxonomy Tree & CRUD Tests (Feature 18)
# =========================================================================

class TestCategoryTaxonomyTree:
    """Tests for two-tier category tree, CRUD, and hierarchy enforcement."""

    def test_init_builtin_categories(self, db_conn: sqlite3.Connection):
        count = CategoryService.init_builtin_categories(db_conn)
        assert count >= 59  # 16 roots + 43 subcategories

        # Re-running init must be idempotent
        recount = CategoryService.init_builtin_categories(db_conn)
        assert recount == count

        # Verify root count
        tree = CategoryService.get_tree(db_conn)
        assert len(tree) == 16
        expense_roots = [n for n in tree if n.direction == "expense"]
        income_roots = [n for n in tree if n.direction == "income"]
        assert len(expense_roots) == 11
        assert len(income_roots) == 5

    def test_two_tier_cascader_hierarchy(self, db_conn: sqlite3.Connection):
        CategoryService.init_builtin_categories(db_conn)
        tree = CategoryService.get_tree(db_conn)

        food_root = next(n for n in tree if n.key == "food")
        assert food_root.name == "餐饮美食"
        assert len(food_root.children) == 4
        sub_keys = {c.key for c in food_root.children}
        assert {"food.dining", "food.takeout", "food.drinks", "food.canteen"}.issubset(sub_keys)

        # Check path breadcrumbs
        dining = next(c for c in food_root.children if c.key == "food.dining")
        assert dining.path == ["food", "food.dining"]
        assert dining.path_names == ["餐饮美食", "正餐"]

    def test_depth_limit_enforcement(self, db_conn: sqlite3.Connection):
        CategoryService.init_builtin_categories(db_conn)
        tree = CategoryService.get_tree(db_conn)
        food_root = next(n for n in tree if n.key == "food")
        dining_sub = food_root.children[0]

        # Attempting to add a 3rd tier under dining_sub must raise ValueError
        with pytest.raises(ValueError, match="Hierarchy depth cannot exceed 2 tiers"):
            CategoryService.create(
                db_conn,
                CategoryCreate(name="三级分类", parent_id=dining_sub.id),
            )

    def test_sibling_uniqueness_enforcement(self, db_conn: sqlite3.Connection):
        CategoryService.init_builtin_categories(db_conn)
        tree = CategoryService.get_tree(db_conn)
        food_root = next(n for n in tree if n.key == "food")

        # Attempting to create duplicate sibling "正餐" under food_root
        with pytest.raises(ValueError, match="already exists"):
            CategoryService.create(
                db_conn,
                CategoryCreate(name="正餐", parent_id=food_root.id),
            )

    def test_update_category(self, db_conn: sqlite3.Connection):
        CategoryService.init_builtin_categories(db_conn)
        cat = CategoryService.get_by_key(db_conn, "food.dining")
        assert cat is not None

        updated = CategoryService.update(db_conn, cat.id, CategoryUpdate(name="大餐与堂食"))
        assert updated.name == "大餐与堂食"
        assert updated.key == "food.dining"  # Stable key preserved

    def test_delete_protection_with_transactions(self, db_conn: sqlite3.Connection):
        CategoryService.init_builtin_categories(db_conn)
        dining = CategoryService.get_by_key(db_conn, "food.dining")
        takeout = CategoryService.get_by_key(db_conn, "food.takeout")
        assert dining is not None and takeout is not None

        # Insert transaction referencing dining
        db_conn.execute(
            """
            INSERT INTO transactions (batch_id, account_id, source, txn_time, time_precision, amount_cents, direction, fingerprint, category_id)
            VALUES (1, 1, 'alipay', '2026-11-09 10:00:00', 'second', 2500, 'expense', 'fp_cat_test', ?)
            """,
            (dining.id,),
        )
        db_conn.commit()

        # Deleting without reassign must raise CategoryInUseError
        with pytest.raises(CategoryInUseError):
            CategoryService.delete(db_conn, dining.id, reassign_to_id=None)

        # Deleting with reassign moves transactions to takeout
        success = CategoryService.delete(db_conn, dining.id, reassign_to_id=takeout.id)
        assert success is True

        txn_cat = db_conn.execute("SELECT category_id FROM transactions WHERE fingerprint = 'fp_cat_test'").fetchone()[0]
        assert txn_cat == takeout.id


# =========================================================================
# 2. Rule Matching Engine Tests (Feature 21)
# =========================================================================

class TestRuleMatchingEngine:
    """Tests for exact, contains, prefix, safe regex, and multi-field matching."""

    def test_pattern_types(self):
        assert match_pattern("contains", "金拱门", "上海金拱门食品有限公司")
        assert not match_pattern("contains", "肯德基", "上海金拱门食品有限公司")

        assert match_pattern("exact", "transfer", "transfer")
        assert not match_pattern("exact", "transfer", "transfer_income")

        assert match_pattern("prefix", "滴滴", "滴滴出行科技有限公司")
        assert not match_pattern("prefix", "出行", "滴滴出行科技有限公司")

        assert match_pattern("regex", r"(?i)mcdonald|麦当劳", "McDonald's Store")

    def test_safe_regex_error_suppression(self):
        # Invalid regex syntax must not raise an unhandled exception
        bad_pattern = "["
        assert compile_safe_regex(bad_pattern) is None
        assert not match_pattern("regex", bad_pattern, "sample text")

        # Oversized regex (>200 chars) rejected to guard against ReDoS
        long_pattern = "a" * 250
        assert compile_safe_regex(long_pattern) is None

    def test_amount_bounds(self):
        rule = Rule(
            id=1,
            name="大额餐饮",
            category_id=10,
            field="counterparty",
            pattern_type="contains",
            pattern_value="海鲜",
            amount_min_cents=5000,
            amount_max_cents=20000,
        )

        # Matched within bounds
        assert rule_matches(rule, {"counterparty": "东海海鲜", "amount_cents": 12000})

        # Rejected below min
        assert not rule_matches(rule, {"counterparty": "东海海鲜", "amount_cents": 3000})

        # Rejected above max
        assert not rule_matches(rule, {"counterparty": "东海海鲜", "amount_cents": 25000})

    def test_field_fallback_heuristics(self):
        rule = Rule(
            id=2,
            name="星巴克",
            category_id=11,
            field="merchant_norm",
            pattern_type="contains",
            pattern_value="星巴克",
        )

        # When merchant_norm is missing, falls back to counterparty
        assert rule_matches(rule, {"counterparty": "星巴克咖啡上海店", "merchant_norm": None})


# =========================================================================
# 3. Builtin Rules & Coverage Tests (Feature 20)
# =========================================================================

class TestBuiltinRulesCoverage:
    """Verifies that builtin_rules.yaml achieves >=85% coverage on real sample counterparties."""

    def test_load_builtin_rules_yaml(self, db_conn: sqlite3.Connection):
        CategoryService.init_builtin_categories(db_conn)
        rules = load_builtin_rules_from_yaml(conn=db_conn)
        assert len(rules) >= 80

        # Confirm all rules have resolved non-zero category_ids
        for r in rules:
            assert r.category_id > 0, f"Rule '{r.name}' failed to resolve category_id"

    def test_sample_merchants_coverage_exceeds_85_percent(self, db_conn: sqlite3.Connection):
        CategoryService.init_builtin_categories(db_conn)
        pipeline = ClassificationPipeline(db_conn)

        # 25 Diverse sample transactions extracted directly from real statement data
        sample_txns = [
            {"counterparty": "麦当劳", "amount_cents": 2747, "direction": "expense"},
            {"counterparty": "上海金拱门食品有限公司", "amount_cents": 2747, "direction": "expense"},
            {"counterparty": "肯德基", "amount_cents": 3500, "direction": "expense"},
            {"counterparty": "塔斯汀·中国汉堡", "amount_cents": 1800, "direction": "expense"},
            {"counterparty": "食其家", "amount_cents": 2200, "direction": "expense"},
            {"counterparty": "老盛兴汤包馆", "amount_cents": 1500, "direction": "expense"},
            {"counterparty": "寿司郎", "amount_cents": 8900, "direction": "expense"},
            {"counterparty": "美团外卖", "amount_cents": 3200, "direction": "expense"},
            {"counterparty": "瑞幸咖啡", "amount_cents": 990, "direction": "expense"},
            {"counterparty": "霸王茶姬", "amount_cents": 1600, "direction": "expense"},
            {"counterparty": "星辰大学", "description": "第一食堂学生餐厅", "amount_cents": 1400, "direction": "expense"},
            {"counterparty": "上海罗森便利", "amount_cents": 1150, "direction": "expense"},
            {"counterparty": "统一超商", "amount_cents": 800, "direction": "expense"},
            {"counterparty": "UP售货机", "amount_cents": 350, "direction": "expense"},
            {"counterparty": "滴滴出行", "amount_cents": 2340, "direction": "expense"},
            {"counterparty": "上海公共交通卡", "amount_cents": 5000, "direction": "expense"},
            {"counterparty": "中国铁路12306", "amount_cents": 15450, "direction": "expense"},
            {"counterparty": "京东", "amount_cents": 6900, "direction": "expense"},
            {"counterparty": "某数码专营店", "description": "数码配件采购", "amount_cents": 8500, "direction": "expense"},
            {"counterparty": "优衣库", "amount_cents": 29900, "direction": "expense"},
            {"counterparty": "上海米哈游影铁科技", "amount_cents": 3000, "direction": "expense"},
            {"counterparty": "腾讯计算机", "description": "Q币充值", "amount_cents": 5000, "direction": "expense"},
            {"counterparty": "中国人寿保险", "amount_cents": 10000, "direction": "expense"},
            {"counterparty": "张三", "amount_cents": 100000, "direction": "transfer"},
            {"counterparty": "VisaMerchantRebate", "amount_cents": 1500, "direction": "income"},
        ]

        hits = 0
        for txn in sample_txns:
            res = pipeline.classify_transaction(txn)
            if res.source in ("builtin_rule", "user_rule"):
                hits += 1

        coverage = hits / len(sample_txns)
        assert coverage >= 0.85, f"Coverage {coverage:.2%} is below required 85%"
        assert coverage == 1.0, f"Expected 100% on sample set, got {coverage:.2%}"


# =========================================================================
# 4. Rule Learning & Protection Tests (Feature 21)
# =========================================================================

class TestRuleLearningAndProtection:
    """Tests merchant keyword cleaning, proposal extraction, and batch application."""

    def test_clean_merchant_for_learning(self):
        assert clean_merchant_for_learning("支付宝-金拱门（中国）有限公司人民广场分店") == "金拱门"
        assert clean_merchant_for_learning("财付通-美团外卖") == "美团外卖"
        assert clean_merchant_for_learning("上海罗森便利有限公司漕宝路分店") == "上海罗森便利"
        assert clean_merchant_for_learning("微信支付-霸王茶姬【四川茶姬企业管理有限公司】") == "霸王茶姬"

    def test_learn_rule_candidate_generation(self, db_conn: sqlite3.Connection):
        CategoryService.init_builtin_categories(db_conn)
        dining = CategoryService.get_by_key(db_conn, "food.dining")
        assert dining is not None

        # Insert sample transactions
        db_conn.execute(
            """
            INSERT INTO transactions (id, batch_id, account_id, source, txn_time, time_precision, amount_cents, direction, fingerprint, counterparty, category_source)
            VALUES (1, 1, 1, 'alipay', '2026-11-09 10:00:00', 'second', 2500, 'expense', 'fp1', '麦当劳五角场店', 'manual'),
                   (2, 1, 1, 'alipay', '2026-11-08 10:00:00', 'second', 3000, 'expense', 'fp2', '麦当劳人民广场店', 'none'),
                   (3, 1, 1, 'alipay', '2026-11-07 10:00:00', 'second', 2000, 'expense', 'fp3', '麦当劳徐家汇店', 'manual')
            """
        )
        db_conn.commit()

        # Learn from txn #1 edited to dining
        candidate = learn_rule_from_edit(db_conn, txn_id=1, new_category_id=dining.id)
        assert candidate is not None
        assert candidate.pattern_value == "麦当劳"
        assert candidate.matched_total == 3
        # txn #3 is manual so it must be protected
        assert candidate.manual_protected == 1
        assert candidate.applicable_count == 2  # txn #1 (the edited one) + txn #2

    def test_apply_rule_strictly_protects_manual_records(self, db_conn: sqlite3.Connection):
        CategoryService.init_builtin_categories(db_conn)
        dining = CategoryService.get_by_key(db_conn, "food.dining")
        other = CategoryService.get_by_key(db_conn, "other_expense.misc")
        assert dining is not None and other is not None

        db_conn.execute(
            """
            INSERT INTO transactions (id, batch_id, account_id, source, txn_time, time_precision, amount_cents, direction, fingerprint, counterparty, category_id, category_source)
            VALUES (10, 1, 1, 'alipay', '2026-11-09 10:00:00', 'second', 2000, 'expense', 'fp10', '星巴克烘焙坊', NULL, 'none'),
                   (11, 1, 1, 'alipay', '2026-11-08 10:00:00', 'second', 3500, 'expense', 'fp11', '星巴克臻选店', ?, 'manual'),
                   (12, 1, 1, 'alipay', '2026-11-07 10:00:00', 'second', 4000, 'expense', 'fp12', '星巴克咖啡', NULL, 'builtin_rule')
            """,
            (other.id,),
        )
        db_conn.commit()

        rule = Rule(
            id=1,
            name="星巴克->正餐",
            category_id=dining.id,
            field="counterparty",
            pattern_type="contains",
            pattern_value="星巴克",
        )

        res = apply_rule_to_existing(db_conn, rule, overwrite_manual=False)
        assert res["matched_count"] == 3
        assert res["updated_count"] == 2
        assert res["skipped_manual_count"] == 1

        # Check that manual record #11 was NOT modified
        row11 = db_conn.execute("SELECT category_id, category_source FROM transactions WHERE id = 11").fetchone()
        assert row11["category_id"] == other.id
        assert row11["category_source"] == "manual"

        # Check that record #10 was updated
        row10 = db_conn.execute("SELECT category_id, category_source FROM transactions WHERE id = 10").fetchone()
        assert row10["category_id"] == dining.id
        assert row10["category_source"] == "user_rule"

    def test_accumulated_learning_suggestions(self, db_conn: sqlite3.Connection):
        CategoryService.init_builtin_categories(db_conn)
        dining = CategoryService.get_by_key(db_conn, "food.dining")
        assert dining is not None

        # Insert 3 manual edits for '塔斯汀'
        for i in range(3):
            db_conn.execute(
                f"""
                INSERT INTO transactions (batch_id, account_id, source, txn_time, time_precision, amount_cents, direction, fingerprint, counterparty, category_id, category_source)
                VALUES (1, 1, 'alipay', '2026-11-0{i+1} 10:00:00', 'second', 1500, 'expense', 'fp_tastin_{i}', '塔斯汀中国汉堡', {dining.id}, 'manual')
                """
            )
        db_conn.commit()

        suggestions = get_learning_suggestions(db_conn, min_count=3)
        assert len(suggestions) >= 1
        assert any(s["brand"] == "塔斯汀" for s in suggestions)


# =========================================================================
# 5. 6-Stage Priority Classification Pipeline Tests (Feature 19)
# =========================================================================

class TestSixStageClassificationPipeline:
    """Verifies priority ordering: manual > user_rule > builtin_rule > platform > llm > none."""

    def test_stage_1_manual_beats_all(self, db_conn: sqlite3.Connection):
        CategoryService.init_builtin_categories(db_conn)
        pipeline = ClassificationPipeline(db_conn)

        # Transaction for '麦当劳' (matches builtin rule) but marked manual with category_id=999
        txn = {
            "counterparty": "麦当劳",
            "amount_cents": 2747,
            "category_id": 999,
            "category_source": "manual",
        }

        res = pipeline.classify_transaction(txn, force_reclassify=False)
        assert res.source == "manual"
        assert res.category_id == 999

        # When force_reclassify=True, re-evaluates through rules
        forced_res = pipeline.classify_transaction(txn, force_reclassify=True)
        assert forced_res.source == "builtin_rule"
        assert forced_res.category_id != 999

    def test_stage_2_user_rule_beats_builtin_rule(self, db_conn: sqlite3.Connection):
        CategoryService.init_builtin_categories(db_conn)
        other_cat = CategoryService.get_by_key(db_conn, "other_expense.misc")
        assert other_cat is not None

        # Insert user rule for '麦当劳' with high priority
        db_conn.execute(
            """
            INSERT INTO rules (name, category_id, priority, field, pattern_type, pattern_value, is_builtin)
            VALUES ('麦当劳特例', ?, 5, 'counterparty', 'contains', '麦当劳', 0)
            """,
            (other_cat.id,),
        )
        db_conn.commit()

        pipeline = ClassificationPipeline(db_conn)
        res = pipeline.classify_transaction({"counterparty": "麦当劳", "amount_cents": 2747})

        assert res.source == "user_rule"
        assert res.category_id == other_cat.id

    def test_stage_3_builtin_rule_beats_platform(self, db_conn: sqlite3.Connection):
        CategoryService.init_builtin_categories(db_conn)
        pipeline = ClassificationPipeline(db_conn)

        # Transaction has platform_category '日用百货' but counterparty is '麦当劳'
        txn = {
            "source": "alipay",
            "platform_category": "日用百货",
            "counterparty": "麦当劳",
            "amount_cents": 2747,
        }
        res = pipeline.classify_transaction(txn)
        assert res.source == "builtin_rule"
        assert res.category_key == "food.dining"

    def test_stage_4_platform_mapping(self, db_conn: sqlite3.Connection):
        CategoryService.init_builtin_categories(db_conn)
        pipeline = ClassificationPipeline(db_conn)

        # Unrecognized merchant but valid platform category
        txn = {
            "source": "alipay",
            "platform_category": "餐饮美食",
            "counterparty": "无名偏僻小吃摊",
            "amount_cents": 1500,
        }
        res = pipeline.classify_transaction(txn)
        assert res.source == "platform"
        assert res.category_key == "food.dining"
        assert res.confidence == 0.85

    def test_stage_6_unclassified_fallback(self, db_conn: sqlite3.Connection):
        CategoryService.init_builtin_categories(db_conn)
        pipeline = ClassificationPipeline(db_conn)

        # Obscure transaction with no matching rule or platform mapping
        txn = {
            "source": "bank",
            "counterparty": "XYZ987654321",
            "amount_cents": 1000,
        }
        res = pipeline.classify_transaction(txn)
        assert res.source == "none"
        assert res.category_id is None
        assert res.confidence == 0.0

    def test_pipeline_trace(self, db_conn: sqlite3.Connection):
        CategoryService.init_builtin_categories(db_conn)
        pipeline = ClassificationPipeline(db_conn)

        trace = pipeline.trace_pipeline({"counterparty": "麦当劳", "amount_cents": 2747})
        step_names = [s["step"] for s in trace]
        assert "manual" in step_names
        assert "user_rule" in step_names
        assert "builtin_rule" in step_names
        assert trace[-1]["hit"] is True
