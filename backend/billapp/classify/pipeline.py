"""6-stage classification priority pipeline.

Priority sequence:
manual > user_rule > builtin_rule > platform > llm > none

Key Invariants:
- Never overwrite `category_source == 'manual'` unless `force_reclassify=True`.
- Builtin rules guarantee >=85% classification on real samples.
- Graceful offline fallback: if LLM is unavailable or fails, fallback to `none` without crashing.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional, Tuple, Union

from billapp.classify.categories import CategoryService
from billapp.classify.rules import (
    Rule,
    _get_val,
    load_builtin_rules_from_yaml,
    rule_matches,
)

logger = logging.getLogger(__name__)

CategorySource = Literal["manual", "user_rule", "builtin_rule", "platform", "llm", "none"]


@dataclass
class ClassificationResult:
    category_id: Optional[int]
    source: CategorySource
    confidence: float
    rule_id: Optional[int] = None
    reason: Optional[str] = None
    category_key: Optional[str] = None
    subcategory_key: Optional[str] = None


@dataclass
class ClassificationSummary:
    batch_id: Optional[int] = None
    total_evaluated: int = 0
    manual_preserved: int = 0
    user_rule_matched: int = 0
    builtin_rule_matched: int = 0
    platform_mapped: int = 0
    llm_classified: int = 0
    unclassified: int = 0


# =========================================================================
# Platform Category Mapping Matrix (Alipay & WeChat)
# =========================================================================

# Format: (platform_source, platform_category_str) -> (target_category_key, confidence)
PLATFORM_CATEGORY_MAP: Dict[Tuple[str, str], Tuple[str, float]] = {
    # Alipay mappings
    ("alipay", "餐饮美食"): ("food.dining", 0.85),
    ("alipay", "日用百货"): ("shopping.daily", 0.85),
    ("alipay", "交通出行"): ("transport.public", 0.85),
    ("alipay", "充值缴费"): ("utilities.telecom", 0.85),
    ("alipay", "服饰装扮"): ("shopping.apparel", 0.85),
    ("alipay", "数码电器"): ("shopping.electronics", 0.85),
    ("alipay", "文化休闲"): ("entertainment.gaming", 0.80),
    ("alipay", "生活服务"): ("shopping.daily", 0.75),
    ("alipay", "爱车养车"): ("transport.fuel_parking", 0.85),
    ("alipay", "转账充值"): ("financial.internal_transfer", 0.85),
    ("alipay", "借还款"): ("financial.credit_repay", 0.85),
    ("alipay", "运动健身"): ("entertainment.fitness", 0.85),
    ("alipay", "医疗健康"): ("medical.pharmacy", 0.85),

    # WeChat mappings
    ("wechat", "微信红包"): ("family_social.red_packet", 0.90),
    ("wechat", "群收款"): ("family_social.aa", 0.95),
    ("wechat", "转账"): ("financial.internal_transfer", 0.80),
    ("wechat", "信用卡还款"): ("financial.credit_repay", 0.95),
    ("wechat", "零钱充值"): ("financial.internal_transfer", 0.90),
    ("wechat", "零钱提现"): ("financial.internal_transfer", 0.90),
    ("wechat", "扫二维码付款"): ("shopping.daily", 0.70),
    ("wechat", "商户消费"): ("food.dining", 0.70),
}


# =========================================================================
# Classification Pipeline Engine
# =========================================================================

class ClassificationPipeline:
    """Orchestrates multi-stage transaction classification."""

    def __init__(
        self,
        conn: Optional[sqlite3.Connection] = None,
        builtin_rules: Optional[List[Rule]] = None,
        user_rules: Optional[List[Rule]] = None,
        llm_client: Optional[Any] = None,
        auto_init_builtins: bool = True,
    ):
        self.conn = conn
        self.llm_client = llm_client
        self.key_to_id: Dict[str, int] = {}
        self.id_to_key: Dict[int, str] = {}

        # 1. Build category key mapping
        if self.conn:
            if auto_init_builtins:
                CategoryService.init_builtin_categories(self.conn)
            rows = self.conn.execute("SELECT id, key FROM categories WHERE key IS NOT NULL").fetchall()
            for r in rows:
                self.key_to_id[r[1]] = r[0]
                self.id_to_key[r[0]] = r[1]

        # 2. Load builtin rules
        if builtin_rules is not None:
            self.builtin_rules = builtin_rules
        else:
            self.builtin_rules = load_builtin_rules_from_yaml(
                category_key_to_id=self.key_to_id,
                conn=self.conn,
            )

        # 3. Load user rules
        if user_rules is not None:
            self.user_rules = user_rules
        elif self.conn:
            self.reload_user_rules()
        else:
            self.user_rules = []

    def reload_user_rules(self) -> None:
        """Fetch active user rules from SQLite."""
        if not self.conn:
            return
        query = """
            SELECT id, name, category_id, priority, field, pattern_type,
                   pattern_value, amount_min_cents, amount_max_cents, hit_count
            FROM rules
            WHERE is_builtin = 0
            ORDER BY priority ASC, id ASC
        """
        rows = self.conn.execute(query).fetchall()
        rules: List[Rule] = []
        for r in rows:
            rules.append(
                Rule(
                    id=r["id"],
                    name=r["name"],
                    category_id=r["category_id"],
                    priority=r["priority"],
                    field=r["field"],
                    pattern_type=r["pattern_type"],
                    pattern_value=r["pattern_value"],
                    amount_min_cents=r["amount_min_cents"],
                    amount_max_cents=r["amount_max_cents"],
                    is_builtin=False,
                    hit_count=r["hit_count"],
                    category_key=self.id_to_key.get(r["category_id"]),
                )
            )
        self.user_rules = rules

    def classify_transaction(
        self,
        txn: Any,
        force_reclassify: bool = False,
    ) -> ClassificationResult:
        """Run a single transaction through the 6-stage pipeline."""
        # -------------------------------------------------------------
        # Stage 1: manual
        # -------------------------------------------------------------
        cat_src = _get_val(txn, "category_source")
        cat_id = _get_val(txn, "category_id")

        if cat_src == "manual" and not force_reclassify and cat_id is not None:
            return ClassificationResult(
                category_id=cat_id,
                source="manual",
                confidence=1.0,
                rule_id=None,
                reason="Preserved manual classification",
                category_key=self.id_to_key.get(cat_id),
            )

        # -------------------------------------------------------------
        # Stage 2: user_rule
        # -------------------------------------------------------------
        for rule in self.user_rules:
            if rule_matches(rule, txn):
                rule.hit_count += 1
                if self.conn and rule.id:
                    self.conn.execute("UPDATE rules SET hit_count = hit_count + 1 WHERE id = ?", (rule.id,))
                    self.conn.commit()
                return ClassificationResult(
                    category_id=rule.category_id,
                    source="user_rule",
                    confidence=1.0,
                    rule_id=rule.id,
                    reason=f"Matched user rule #{rule.id}: {rule.name}",
                    category_key=self.id_to_key.get(rule.category_id),
                )

        # -------------------------------------------------------------
        # Stage 3: builtin_rule
        # -------------------------------------------------------------
        for rule in self.builtin_rules:
            if rule_matches(rule, txn):
                rule.hit_count += 1
                return ClassificationResult(
                    category_id=rule.category_id,
                    source="builtin_rule",
                    confidence=1.0,
                    rule_id=rule.id,
                    reason=f"Matched builtin rule #{rule.id}: {rule.name}",
                    category_key=rule.subcategory_key or rule.category_key or self.id_to_key.get(rule.category_id),
                    subcategory_key=rule.subcategory_key,
                )

        # -------------------------------------------------------------
        # Stage 4: platform mapping
        # -------------------------------------------------------------
        src = _get_val(txn, "source")
        plat_cat = _get_val(txn, "platform_category")

        if src and plat_cat:
            key_pair = (str(src).lower(), str(plat_cat).strip())
            mapping = PLATFORM_CATEGORY_MAP.get(key_pair)
            if mapping:
                target_key, conf = mapping
                target_id = self.key_to_id.get(target_key)
                if target_id is not None and conf >= 0.70:
                    return ClassificationResult(
                        category_id=target_id,
                        source="platform",
                        confidence=conf,
                        rule_id=None,
                        reason=f"Platform mapping: {src}:{plat_cat}",
                        category_key=target_key,
                    )

        # -------------------------------------------------------------
        # Stage 5: llm fallback (if available)
        # -------------------------------------------------------------
        if self.llm_client and hasattr(self.llm_client, "is_available") and self.llm_client.is_available():
            try:
                from billapp.llm.tasks import classify_fallback_sync
                llm_res = classify_fallback_sync([txn], self.llm_client, self.key_to_id)
                if llm_res and llm_res[0].category_id is not None and llm_res[0].confidence >= 0.70:
                    res = llm_res[0]
                    res.category_key = self.id_to_key.get(res.category_id)
                    return res
            except Exception as exc:
                logger.debug("LLM classification error in pipeline (falling back to none): %s", exc)

        # -------------------------------------------------------------
        # Stage 6: unclassified fallback
        # -------------------------------------------------------------
        return ClassificationResult(
            category_id=None,
            source="none",
            confidence=0.0,
            rule_id=None,
            reason="Unclassified",
            category_key=None,
        )

    def classify_batch(
        self,
        transactions: List[Any],
        force_reclassify: bool = False,
        update_db: bool = False,
    ) -> Tuple[List[ClassificationResult], ClassificationSummary]:
        """Classify a batch of transactions and optionally update DB."""
        results: List[ClassificationResult] = []
        summary = ClassificationSummary(total_evaluated=len(transactions))

        # Check if batch optimization for LLM is beneficial
        pending_llm_indices: List[int] = []

        for idx, txn in enumerate(transactions):
            # Evaluate Stages 1-4 first
            res = self._classify_stages_1_to_4(txn, force_reclassify)
            if res is not None:
                results.append(res)
                self._tally_summary(summary, res.source)
            else:
                # Needs stage 5 (LLM) or stage 6
                results.append(
                    ClassificationResult(
                        category_id=None,
                        source="none",
                        confidence=0.0,
                        reason="Pending LLM",
                    )
                )
                pending_llm_indices.append(idx)

        # Run chunked LLM classification for pending items if LLM available
        if pending_llm_indices and self.llm_client and hasattr(self.llm_client, "is_available") and self.llm_client.is_available():
            try:
                from billapp.llm.tasks import classify_fallback_sync
                pending_txns = [transactions[i] for i in pending_llm_indices]
                llm_results = classify_fallback_sync(pending_txns, self.llm_client, self.key_to_id)

                for sub_idx, orig_idx in enumerate(pending_llm_indices):
                    if sub_idx < len(llm_results):
                        l_res = llm_results[sub_idx]
                        if l_res.category_id is not None and l_res.confidence >= 0.70:
                            l_res.category_key = self.id_to_key.get(l_res.category_id)
                            results[orig_idx] = l_res
                            self._tally_summary(summary, "llm")
                            continue
                    # Otherwise remain unclassified
                    results[orig_idx] = ClassificationResult(
                        category_id=None,
                        source="none",
                        confidence=0.0,
                        reason="Unclassified",
                    )
                    self._tally_summary(summary, "none")
            except Exception as exc:
                logger.debug("Batch LLM error: %s; falling back to none", exc)
                for orig_idx in pending_llm_indices:
                    results[orig_idx] = ClassificationResult(
                        category_id=None,
                        source="none",
                        confidence=0.0,
                        reason="Unclassified",
                    )
                    self._tally_summary(summary, "none")
        else:
            for orig_idx in pending_llm_indices:
                results[orig_idx] = ClassificationResult(
                    category_id=None,
                    source="none",
                    confidence=0.0,
                    reason="Unclassified",
                )
                self._tally_summary(summary, "none")

        # Optionally persist to database
        if update_db and self.conn:
            for txn, res in zip(transactions, results):
                txn_id = _get_val(txn, "id")
                if txn_id is not None:
                    self.conn.execute(
                        """
                        UPDATE transactions
                        SET category_id = ?, category_source = ?, category_confidence = ?
                        WHERE id = ?
                        """,
                        (res.category_id, res.source, res.confidence, txn_id),
                    )
            self.conn.commit()

        return results, summary

    def _classify_stages_1_to_4(
        self,
        txn: Any,
        force_reclassify: bool,
    ) -> Optional[ClassificationResult]:
        """Evaluate Stages 1 to 4; return None if candidate needs Stage 5/6."""
        # Stage 1: manual
        cat_src = _get_val(txn, "category_source")
        cat_id = _get_val(txn, "category_id")
        if cat_src == "manual" and not force_reclassify and cat_id is not None:
            return ClassificationResult(
                category_id=cat_id,
                source="manual",
                confidence=1.0,
                rule_id=None,
                reason="Preserved manual classification",
                category_key=self.id_to_key.get(cat_id),
            )

        # Stage 2: user_rule
        for rule in self.user_rules:
            if rule_matches(rule, txn):
                rule.hit_count += 1
                if self.conn and rule.id:
                    self.conn.execute("UPDATE rules SET hit_count = hit_count + 1 WHERE id = ?", (rule.id,))
                return ClassificationResult(
                    category_id=rule.category_id,
                    source="user_rule",
                    confidence=1.0,
                    rule_id=rule.id,
                    reason=f"Matched user rule #{rule.id}: {rule.name}",
                    category_key=self.id_to_key.get(rule.category_id),
                )

        # Stage 3: builtin_rule
        for rule in self.builtin_rules:
            if rule_matches(rule, txn):
                rule.hit_count += 1
                return ClassificationResult(
                    category_id=rule.category_id,
                    source="builtin_rule",
                    confidence=1.0,
                    rule_id=rule.id,
                    reason=f"Matched builtin rule #{rule.id}: {rule.name}",
                    category_key=rule.subcategory_key or rule.category_key or self.id_to_key.get(rule.category_id),
                    subcategory_key=rule.subcategory_key,
                )

        # Stage 4: platform
        src = _get_val(txn, "source")
        plat_cat = _get_val(txn, "platform_category")
        if src and plat_cat:
            key_pair = (str(src).lower(), str(plat_cat).strip())
            mapping = PLATFORM_CATEGORY_MAP.get(key_pair)
            if mapping:
                target_key, conf = mapping
                target_id = self.key_to_id.get(target_key)
                if target_id is not None and conf >= 0.70:
                    return ClassificationResult(
                        category_id=target_id,
                        source="platform",
                        confidence=conf,
                        rule_id=None,
                        reason=f"Platform mapping: {src}:{plat_cat}",
                        category_key=target_key,
                    )

        return None

    def trace_pipeline(self, txn: Any) -> List[Dict[str, Any]]:
        """Trace pipeline step-by-step for testing drawer (POST /api/rules/test)."""
        trace: List[Dict[str, Any]] = []

        # Step 1: manual
        cat_src = _get_val(txn, "category_source")
        cat_id = _get_val(txn, "category_id")
        if cat_src == "manual" and cat_id is not None:
            trace.append({"step": "manual", "hit": True, "category_id": cat_id, "desc": "Manual ground truth"})
            return trace
        else:
            trace.append({"step": "manual", "hit": False, "desc": "Not manually classified"})

        # Step 2: user_rule
        matched_user = None
        for rule in self.user_rules:
            if rule_matches(rule, txn):
                matched_user = rule
                break
        if matched_user:
            trace.append({"step": "user_rule", "hit": True, "rule_id": matched_user.id, "desc": f"Matched {matched_user.name}"})
            return trace
        else:
            trace.append({"step": "user_rule", "hit": False, "desc": "No matching user rule"})

        # Step 3: builtin_rule
        matched_builtin = None
        for rule in self.builtin_rules:
            if rule_matches(rule, txn):
                matched_builtin = rule
                break
        if matched_builtin:
            trace.append({"step": "builtin_rule", "hit": True, "rule_id": matched_builtin.id, "desc": f"Matched {matched_builtin.name}"})
            return trace
        else:
            trace.append({"step": "builtin_rule", "hit": False, "desc": "No matching builtin rule"})

        # Step 4: platform
        src = _get_val(txn, "source")
        plat_cat = _get_val(txn, "platform_category")
        if src and plat_cat and (str(src).lower(), str(plat_cat).strip()) in PLATFORM_CATEGORY_MAP:
            target_key, conf = PLATFORM_CATEGORY_MAP[(str(src).lower(), str(plat_cat).strip())]
            trace.append({"step": "platform", "hit": True, "desc": f"Mapped {plat_cat} -> {target_key}"})
            return trace
        else:
            trace.append({"step": "platform", "hit": False, "desc": "No platform category mapping"})

        # Step 5: llm
        trace.append({"step": "llm", "hit": False, "desc": "LLM offline/skipped"})

        # Step 6: unclassified
        trace.append({"step": "unclassified", "hit": True, "desc": "Unclassified"})
        return trace

    @staticmethod
    def _tally_summary(summary: ClassificationSummary, source: str) -> None:
        if source == "manual":
            summary.manual_preserved += 1
        elif source == "user_rule":
            summary.user_rule_matched += 1
        elif source == "builtin_rule":
            summary.builtin_rule_matched += 1
        elif source == "platform":
            summary.platform_mapped += 1
        elif source == "llm":
            summary.llm_classified += 1
        else:
            summary.unclassified += 1
