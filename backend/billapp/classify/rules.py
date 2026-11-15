"""Rule matching engine, safe regex evaluation, merchant cleaner, and rule learning.

Implements:
- Pattern operators: exact, contains, prefix, regex (safely bounded)
- Multi-field matching (counterparty, merchant_norm, description, platform_category, etc.)
- clean_merchant_for_learning() for extracting crisp brand keywords
- learn_rule_from_edit() candidate generation protecting manual classifications
- apply_rule_to_existing() batch execution
- Builtin YAML rule loader with taxonomy category key resolution
"""

from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import yaml

from billapp.classify.categories import CategoryService

logger = logging.getLogger(__name__)

DEFAULT_BUILTIN_RULES_PATH = Path(__file__).parent / "builtin_rules.yaml"


# =========================================================================
# Rule Data Models
# =========================================================================

@dataclass
class Rule:
    id: Optional[int]
    name: str
    category_id: int
    priority: int = 100
    field: str = "counterparty"
    pattern_type: str = "contains"
    pattern_value: str = ""
    amount_min_cents: Optional[int] = None
    amount_max_cents: Optional[int] = None
    is_builtin: bool = False
    hit_count: int = 0
    category_key: Optional[str] = None
    subcategory_key: Optional[str] = None

    def matches(self, txn: Any) -> bool:
        """Evaluate whether this rule matches a transaction record."""
        return rule_matches(self, txn)


@dataclass
class RuleCandidate:
    name: str
    category_id: int
    field: str
    pattern_type: str
    pattern_value: str
    priority: int
    sample_txn_id: int
    matched_total: int
    already_same_category: int
    manual_protected: int
    applicable_count: int
    sample_applicable_txn_ids: List[int] = field(default_factory=list)


# =========================================================================
# Safe Regex & Pattern Matching Engine
# =========================================================================

_REGEX_CACHE: Dict[str, Optional[re.Pattern]] = {}


def compile_safe_regex(pattern: str, raise_on_error: bool = False) -> Optional[re.Pattern]:
    """Compile regex with size bounds, ReDoS heuristics, and error suppression."""
    if not pattern:
        return None
    if pattern in _REGEX_CACHE:
        cached = _REGEX_CACHE[pattern]
        if cached is None and raise_on_error:
            raise ValueError("Potentially catastrophic regex pattern (ReDoS) or invalid regex")
        return cached

    # Guard against ReDoS via oversized patterns
    if len(pattern) > 200:
        logger.warning("Regex pattern exceeds 200 characters; rejecting pattern: %s", pattern[:30])
        _REGEX_CACHE[pattern] = None
        if raise_on_error:
            raise ValueError("Regex pattern exceeds 200 characters")
        return None

    # Guard against ReDoS via nested quantifiers (e.g. (a+)+, (\w+)+, (x+x+)+)
    if re.search(r"\([^)]*(?:\+|\*|\{\d*,?\d*\})[^)]*\)\s*(?:\+|\*|\{\d*,?\d*\})", pattern):
        logger.warning("Potentially catastrophic regex pattern (ReDoS) rejected: %s", pattern)
        _REGEX_CACHE[pattern] = None
        if raise_on_error:
            raise ValueError("Potentially catastrophic regex pattern (ReDoS)")
        return None

    # Guard against ReDoS via repeated alternations (e.g. (a|aa)+, (a|aa)+$, (x|y)*)
    if re.search(r"\([^)]*\|[^)]*\)\s*(?:\+|\*|\{\d*,?\d*\})", pattern):
        logger.warning("Potentially catastrophic regex pattern (ReDoS alternation) rejected: %s", pattern)
        _REGEX_CACHE[pattern] = None
        if raise_on_error:
            raise ValueError("Potentially catastrophic regex pattern (ReDoS alternation)")
        return None

    try:
        compiled = re.compile(pattern, re.IGNORECASE)
        _REGEX_CACHE[pattern] = compiled
        return compiled
    except re.error as err:
        logger.debug("Invalid regex pattern '%s': %s", pattern, err)
        _REGEX_CACHE[pattern] = None
        if raise_on_error:
            raise ValueError(f"Invalid regex pattern: {err}")
        return None


def match_pattern(pattern_type: str, pattern: str, text: str) -> bool:
    """Evaluate pattern against candidate text string."""
    if not pattern or not text:
        return False

    ptype = pattern_type.lower()
    p_clean = pattern.strip().lower()
    t_clean = text.strip().lower()

    if ptype == "contains":
        return p_clean in t_clean
    elif ptype == "exact":
        return p_clean == t_clean
    elif ptype == "prefix":
        return t_clean.startswith(p_clean)
    elif ptype == "regex":
        regex = compile_safe_regex(pattern)
        return bool(regex.search(text)) if regex is not None else False

    return False


def _get_val(obj: Any, key: str, default: Any = None) -> Any:
    """Safely extract attribute or dictionary/sqlite3.Row item."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    if isinstance(obj, sqlite3.Row) or hasattr(obj, "keys"):
        return obj[key] if key in obj.keys() else default
    return getattr(obj, key, default)


def rule_matches(rule: Rule, txn: Any) -> bool:
    """Check if a rule matches a given transaction object or dict."""
    # 1. Amount range check
    amt = _get_val(txn, "amount_cents", 0) or 0
    if rule.amount_min_cents is not None and amt < rule.amount_min_cents:
        return False
    if rule.amount_max_cents is not None and amt > rule.amount_max_cents:
        return False

    # 2. Extract target field
    field_name = rule.field
    val = _get_val(txn, field_name)

    # 3. Fallback heuristic for counterparty <-> merchant_norm
    if not val:
        if field_name == "merchant_norm":
            val = _get_val(txn, "counterparty")
        elif field_name == "counterparty":
            val = _get_val(txn, "merchant_norm")
            if not val:
                val = _get_val(txn, "description")

    if not val:
        return False

    return match_pattern(rule.pattern_type, rule.pattern_value, str(val))


# =========================================================================
# Builtin YAML Rules Loader
# =========================================================================

def load_builtin_rules_from_yaml(
    yaml_path: Optional[Union[str, Path]] = None,
    category_key_to_id: Optional[Dict[str, int]] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> List[Rule]:
    """Parse builtin_rules.yaml and resolve category IDs via taxonomy keys."""
    target_path = Path(yaml_path or DEFAULT_BUILTIN_RULES_PATH)
    if not target_path.exists():
        logger.warning("Builtin rules YAML not found at %s", target_path)
        return []

    data = yaml.safe_load(target_path.read_text(encoding="utf-8"))
    raw_rules = data.get("rules", [])

    # Build key -> id map if not provided
    key_map = dict(category_key_to_id or {})
    if conn and not key_map:
        rows = conn.execute("SELECT id, key FROM categories WHERE key IS NOT NULL").fetchall()
        for r in rows:
            key_map[r[1]] = r[0]

    rules: List[Rule] = []
    for idx, r in enumerate(raw_rules, start=1):
        cat_key = r.get("subcategory_key") or r.get("category_key")
        cat_id = key_map.get(cat_key, 0) if cat_key else 0

        rule = Rule(
            id=idx,
            name=r.get("name", f"Builtin #{idx}"),
            category_id=cat_id,
            priority=r.get("priority", 100),
            field=r.get("field", "counterparty"),
            pattern_type=r.get("pattern_type", "contains"),
            pattern_value=str(r.get("pattern_value", "")),
            amount_min_cents=r.get("amount_min_cents"),
            amount_max_cents=r.get("amount_max_cents"),
            is_builtin=True,
            hit_count=0,
            category_key=r.get("category_key"),
            subcategory_key=r.get("subcategory_key"),
        )
        rules.append(rule)

    rules.sort(key=lambda r: (r.priority, r.id or 0))
    return rules


# =========================================================================
# Merchant Brand Keyword Cleaner
# =========================================================================

def clean_merchant_for_learning(counterparty: Optional[str], merchant_norm: Optional[str] = None) -> str:
    """Extract clean merchant brand keyword from transaction counterparty."""
    if not counterparty and not merchant_norm:
        return ""
    text = (merchant_norm.strip() if merchant_norm else (counterparty or "").strip())
    if not text:
        return ""

    # 1. Strip payment channel prefixes
    text = re.sub(r"^(支付宝|财付通|网银在线|美团支付|微信支付|云闪付|银联)[-_/]", "", text)

    # 2. Strip brackets and parentheses
    text = re.sub(r"（.*?）|\(.*?\)|【.*?】|\[.*?\]", "", text)

    # 3. Strip corporate entity suffixes and any subsequent branch details
    text = re.sub(
        r"(（中国）)?(有限责任公司|股份有限公司|网络技术有限公司|信息技术有限公司|技术有限公司|科技有限公司|食品有限公司|餐饮管理有限公司|管理有限公司|商贸有限公司|实业有限公司|连锁有限公司|发展有限公司|有限公司).*",
        "",
        text,
    )

    # 4. Strip specific brand qualifiers (e.g. 中国汉堡)
    text = re.sub(r"[·•\s]?(中国汉堡|汉堡)", "", text)

    # 5. Strip branch and store suffixes (e.g. 麦当劳五角场店 -> 麦当劳)
    m = re.search(r"^(.{2,10}?)([\u4e00-\u9fa5A-Za-z0-9]{2,3}(?:分店|支店|总店|专卖店|便利店|旗舰店|店|档口|餐厅))$", text)
    if m:
        candidate_brand = m.group(1).strip()
        if len(candidate_brand) >= 2:
            text = candidate_brand
    else:
        text = re.sub(r"(总店|分店|支店|专卖店|便利店|旗舰店|店)$", "", text)

    # 6. Clean punctuation and outer whitespace
    text = text.strip("-—_ ·/ ")

    # If reduced to less than 2 characters, fallback to original counterparty or first 8 chars
    if len(text) < 2:
        clean_cp = counterparty.strip()
        clean_cp = re.sub(r"^(支付宝|财付通|网银在线|美团支付|微信支付|云闪付|银联)[-_/]", "", clean_cp)
        return clean_cp[:8] if len(clean_cp) > 8 else clean_cp

    return text


# =========================================================================
# Rule Learning & Candidate Generation
# =========================================================================

def learn_rule_from_edit(
    conn: sqlite3.Connection,
    txn_id: int,
    new_category_id: int,
) -> Optional[RuleCandidate]:
    """Generate a candidate rule from a manual category modification."""
    row = conn.execute(
        "SELECT id, counterparty, merchant_norm, amount_cents FROM transactions WHERE id = ?",
        (txn_id,),
    ).fetchone()
    if not row:
        return None

    cp = row["counterparty"] or ""
    mn = row["merchant_norm"] or ""
    brand_keyword = clean_merchant_for_learning(cp, mn)
    if not brand_keyword:
        return None

    target_cat = CategoryService.get_by_id(conn, new_category_id)
    cat_name = target_cat.name if target_cat else "分类"

    # Search for matching transactions
    query = """
        SELECT id, category_id, category_source
        FROM transactions
        WHERE (counterparty LIKE '%' || ? || '%' OR merchant_norm LIKE '%' || ? || '%')
    """
    matching_rows = conn.execute(query, (brand_keyword, brand_keyword)).fetchall()

    matched_total = len(matching_rows)
    already_same_category = 0
    manual_protected = 0
    sample_applicable: List[int] = []

    for r in matching_rows:
        r_id = r["id"]
        c_id = r["category_id"]
        c_src = r["category_source"]

        if c_id == new_category_id:
            already_same_category += 1
        elif c_src == "manual" and r_id != txn_id:
            # Protect manual records
            manual_protected += 1
        else:
            if len(sample_applicable) < 10:
                sample_applicable.append(r_id)

    applicable_count = max(0, matched_total - already_same_category - manual_protected)

    return RuleCandidate(
        name=f"{brand_keyword} -> {cat_name}",
        category_id=new_category_id,
        field="counterparty",
        pattern_type="contains",
        pattern_value=brand_keyword,
        priority=100,
        sample_txn_id=txn_id,
        matched_total=matched_total,
        already_same_category=already_same_category,
        manual_protected=manual_protected,
        applicable_count=applicable_count,
        sample_applicable_txn_ids=sample_applicable,
    )


def apply_rule_to_existing(
    conn: sqlite3.Connection,
    rule: Rule,
    overwrite_manual: bool = False,
    batch_id: Optional[int] = None,
    force_reclassify: Optional[bool] = None,
) -> Dict[str, int]:
    """Apply a rule to all matching historical transactions."""
    if force_reclassify is not None:
        overwrite_manual = force_reclassify

    pat = rule.pattern_value
    base_query = """
        SELECT id, amount_cents, counterparty, merchant_norm, description,
               platform_category, pay_method, direction, category_id, category_source
        FROM transactions
        WHERE 1=1
    """
    params: List[Any] = []
    if batch_id is not None:
        base_query += " AND batch_id = ?"
        params.append(batch_id)

    # For regex patterns, do NOT apply SQL LIKE since SQL LIKE treats regex literally.
    # For non-regex patterns, apply field-specific LIKE to narrow candidates.
    if rule.pattern_type.lower() != "regex" and pat:
        target_field = rule.field.lower() if rule.field else "counterparty"
        if target_field in ("platform_category", "pay_method"):
            base_query += f" AND {target_field} LIKE '%' || ? || '%'"
            params.append(pat)
        elif target_field in ("counterparty", "merchant_norm"):
            base_query += " AND (counterparty LIKE '%' || ? || '%' OR merchant_norm LIKE '%' || ? || '%' OR description LIKE '%' || ? || '%')"
            params.extend([pat, pat, pat])
        elif target_field == "description":
            base_query += " AND description LIKE '%' || ? || '%'"
            params.append(pat)
        else:
            base_query += " AND (counterparty LIKE '%' || ? || '%' OR merchant_norm LIKE '%' || ? || '%' OR description LIKE '%' || ? || '%')"
            params.extend([pat, pat, pat])

    candidates = conn.execute(base_query, params).fetchall()

    matched_count = 0
    updated_ids: List[int] = []
    skipped_manual_count = 0

    for cand in candidates:
        if rule.matches(cand):
            matched_count += 1
            is_manual = cand["category_source"] == "manual"
            if is_manual and not overwrite_manual:
                skipped_manual_count += 1
            else:
                updated_ids.append(cand["id"])

    if updated_ids:
        placeholders = ", ".join("?" for _ in updated_ids)
        conn.execute(
            f"""
            UPDATE transactions
            SET category_id = ?, category_source = 'user_rule', category_confidence = 1.0
            WHERE id IN ({placeholders})
            """,
            [rule.category_id, *updated_ids],
        )

        # Update hit_count
        rule.hit_count += len(updated_ids)
        if rule.id:
            conn.execute(
                "UPDATE rules SET hit_count = hit_count + ? WHERE id = ?",
                (len(updated_ids), rule.id),
            )
        conn.commit()

    return {
        "matched_count": matched_count,
        "updated_count": len(updated_ids),
        "skipped_manual_count": skipped_manual_count,
    }


def get_learning_suggestions(
    conn: sqlite3.Connection,
    min_count: int = 3,
) -> List[Dict[str, Any]]:
    """Query accumulated manual edits that suggest new rules."""
    query = """
        SELECT counterparty, merchant_norm, category_id, COUNT(*) as cnt
        FROM transactions
        WHERE category_source = 'manual' AND category_id IS NOT NULL
        GROUP BY counterparty, merchant_norm, category_id
        HAVING cnt >= ?
        ORDER BY cnt DESC
    """
    rows = conn.execute(query, (min_count,)).fetchall()
    suggestions: List[Dict[str, Any]] = []

    for r in rows:
        cp = r["counterparty"] or ""
        mn = r["merchant_norm"] or ""
        cat_id = r["category_id"]
        cnt = r["cnt"]

        brand = clean_merchant_for_learning(cp, mn)
        if not brand:
            continue

        # Check if user already has an active rule for this pattern
        has_rule = conn.execute(
            "SELECT id FROM rules WHERE pattern_value = ? AND category_id = ?",
            (brand, cat_id),
        ).fetchone()
        if has_rule:
            continue

        cat = CategoryService.get_by_id(conn, cat_id)
        suggestions.append({
            "brand": brand,
            "category_id": cat_id,
            "category_name": cat.name if cat else "未分类",
            "count": cnt,
            "sample_counterparty": cp,
        })

    return suggestions
