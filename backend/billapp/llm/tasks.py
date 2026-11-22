"""The 5 specialized LLM task runners with graceful offline and disabled degradation.

Tasks:
1. classify_fallback: classify unclassified transactions in batches of <=40.
2. review_classification: audit heuristic classifications with low confidence.
3. dedup_arbitration: arbitrate ambiguous 0.55-0.85 cross-source candidate pairs.
4. merchant_normalize: clean noisy counterparties into canonical brand names.
5. suggest_profile_mapping: deduce columns and amount_mode for unknown bank statements.

Invariant:
Offline/disabled LLM mode NEVER raises unhandled exceptions and always falls back safely.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Literal, Optional, Tuple

from billapp.classify.categories import BUILTIN_TAXONOMY
from billapp.classify.pipeline import ClassificationResult
from billapp.classify.rules import _get_val, clean_merchant_for_learning
from billapp.core.models import (
    AccountCreate,
    AccountKind,
    AccountRef,
    ChannelType,
    DirectionType,
    ParseResult,
    SelfCheckResult,
    TransactionCreate,
)
from billapp.core.money import to_cents
from billapp.dedup.fingerprint import assign_fingerprints
from billapp.importers.common import extract_channel_and_normalize, parse_datetime
from billapp.importers.profile_engine import (
    _normalize_currency_token,
    _normalize_date_sep,
    extract_preamble_metadata_fallback,
)
from billapp.llm.client import LLMClient
from billapp.llm.prompts import (
    build_classify_fallback_prompt,
    build_dedup_arbitration_prompt,
    build_full_statement_parse_prompt,
    build_mapping_suggest_prompt,
    build_merchant_normalize_prompt,
    build_review_classification_prompt,
    build_statement_account_prompt,
    prepare_statement_lines_for_llm,
)

logger = logging.getLogger(__name__)


# =========================================================================
# Task Result Data Transfer Objects
# =========================================================================

@dataclass
class ReviewResult:
    id: int
    agree: bool
    suggested_category_id: Optional[int]
    suggested_category_key: Optional[str]
    confidence: float
    reason: str


@dataclass
class ArbitrationResult:
    verdict: Literal["same", "different", "unsure"]
    confidence: float
    reason: str


@dataclass
class ProfileSuggestionResult:
    columns: Dict[str, str] = field(default_factory=dict)
    amount_mode: Literal["signed", "split", "flag"] = "signed"
    confidence: float = 0.0
    reason: str = ""
    bank: Optional[str] = None
    account_kind: Optional[Literal["debit", "credit"]] = None
    card_tail: Optional[str] = None
    currency: Optional[str] = None
    flag_expense_val: Optional[str] = None
    flag_income_val: Optional[str] = None
    used_llm: bool = False
    error: Optional[str] = None


@dataclass
class StatementAccountResult:
    bank: Optional[str] = None
    account_kind: Optional[AccountKind] = None
    card_tail: Optional[str] = None
    currency: str = "CNY"
    holder_name: Optional[str] = None
    period_start: Optional[str] = None
    period_end: Optional[str] = None
    confidence: float = 0.0
    used_llm: bool = False
    error: Optional[str] = None


def _parse_confidence(val: Any) -> float:
    try:
        conf = float(val)
    except (TypeError, ValueError):
        return 0.0
    if conf > 1.0 and conf <= 100.0:
        conf /= 100.0
    return max(0.0, min(1.0, conf))


def _resolve_category_key_and_id(
    cat_key: Any,
    sub_key: Any,
    key_to_id: Dict[str, int],
    name_to_id: Optional[Dict[str, int]] = None,
) -> Tuple[Optional[int], Optional[str]]:
    """Resolve LLM returned category/subcategory key cleanly with root fallback."""
    candidates: List[str] = []
    for raw in (sub_key, cat_key):
        if raw and isinstance(raw, str):
            s = raw.strip()
            if s:
                candidates.append(s)
                # Strip optional annotation like "food.dining (正餐)"
                head = s.split("(", 1)[0].split("（", 1)[0].strip()
                if head and head != s:
                    candidates.append(head)
    # Root-key fallback (e.g. "food.hallucinated_leaf" -> "food") after exact matches
    for base in list(candidates):
        if "." in base:
            root = base.split(".", 1)[0].strip()
            if root and root not in candidates:
                candidates.append(root)

    for cand in candidates:
        if cand in key_to_id:
            return key_to_id[cand], cand
        lower = cand.lower()
        for k, cid in key_to_id.items():
            if k.lower() == lower:
                return cid, k
        if name_to_id and cand in name_to_id:
            cid = name_to_id[cand]
            matched_key = next((k for k, v in key_to_id.items() if v == cid), cand)
            return cid, matched_key

    return None, None


def _parse_classify_chunk_response(
    resp: Any,
    chunk: List[Any],
    key_to_id: Dict[str, int],
    name_to_id: Optional[Dict[str, int]] = None,
) -> List[ClassificationResult]:
    if isinstance(resp, dict):
        raw_list = (
            resp.get("results")
            or resp.get("items")
            or resp.get("classifications")
            or resp.get("data")
            or []
        )
    elif isinstance(resp, list):
        raw_list = resp
    else:
        raw_list = []

    resp_by_id: Dict[str, Dict[str, Any]] = {}
    ordered_items: List[Dict[str, Any]] = []
    for item in raw_list:
        if isinstance(item, dict):
            ordered_items.append(item)
            if "id" in item and item["id"] is not None:
                resp_by_id[str(item["id"]).strip()] = item

    chunk_results: List[ClassificationResult] = []
    for idx, txn in enumerate(chunk):
        t_id = _get_val(txn, "id")
        item: Dict[str, Any] = {}
        if t_id is not None and str(t_id).strip() in resp_by_id:
            item = resp_by_id[str(t_id).strip()]
        elif idx < len(ordered_items) and len(ordered_items) == len(chunk):
            item = ordered_items[idx]

        conf = _parse_confidence(item.get("confidence", 0.0))
        sub_key = item.get("subcategory_key")
        cat_key = item.get("category_key")
        reason = str(item.get("reason") or "LLM fallback").strip()

        target_id, resolved_key = _resolve_category_key_and_id(
            cat_key=cat_key,
            sub_key=sub_key,
            key_to_id=key_to_id,
            name_to_id=name_to_id,
        )

        if target_id is not None and conf >= 0.70:
            chunk_results.append(
                ClassificationResult(
                    category_id=target_id,
                    source="llm",
                    confidence=conf,
                    rule_id=None,
                    reason=reason,
                    category_key=resolved_key,
                    subcategory_key=resolved_key if (resolved_key and "." in resolved_key) else (
                        str(sub_key).strip() if sub_key else None
                    ),
                )
            )
        else:
            chunk_results.append(
                ClassificationResult(
                    category_id=None,
                    source="none",
                    confidence=conf,
                    reason=f"LLM low confidence ({conf:.2f}): {reason}",
                )
            )
    return chunk_results


# =========================================================================
# Task 1: Fallback Classification
# =========================================================================

async def classify_fallback_async(
    transactions: List[Any],
    client: LLMClient,
    key_to_id: Dict[str, int],
    holder_name: Optional[str] = None,
    chunk_size: int = 25,
    taxonomy: Optional[List[Dict[str, Any]]] = None,
    name_to_id: Optional[Dict[str, int]] = None,
    raise_on_error: bool = False,
) -> List[ClassificationResult]:
    """Classify transactions via LLM in chunked batches."""
    if not transactions:
        return []

    if not client.is_available() or not client.config.task_classify_enabled:
        return [
            ClassificationResult(
                category_id=None,
                source="none",
                confidence=0.0,
                reason="LLM disabled or unavailable",
            )
            for _ in transactions
        ]

    results: List[ClassificationResult] = []
    active_taxonomy = taxonomy or BUILTIN_TAXONOMY

    # Process in manageable chunks
    for i in range(0, len(transactions), chunk_size):
        chunk = transactions[i : i + chunk_size]
        sys_prompt, user_prompt = build_classify_fallback_prompt(
            chunk, active_taxonomy, holder_name
        )
        messages = [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": user_prompt},
        ]

        try:
            resp = await client.chat_completion_async(
                task_type="classify",
                messages=messages,
                temperature=0.1,
                use_json_mode=True,
            )
            results.extend(_parse_classify_chunk_response(resp, chunk, key_to_id, name_to_id))

        except Exception as exc:
            logger.warning("LLM fallback classification failed: %s; falling back to none", exc)
            if raise_on_error:
                raise
            for _ in chunk:
                results.append(
                    ClassificationResult(
                        category_id=None,
                        source="none",
                        confidence=0.0,
                        reason=f"LLM execution failed: {exc}",
                    )
                )

    return results


def classify_fallback_sync(
    transactions: List[Any],
    client: LLMClient,
    key_to_id: Dict[str, int],
    holder_name: Optional[str] = None,
    chunk_size: int = 25,
    taxonomy: Optional[List[Dict[str, Any]]] = None,
    name_to_id: Optional[Dict[str, int]] = None,
    raise_on_error: bool = False,
) -> List[ClassificationResult]:
    """Classify transactions via LLM in chunked batches synchronously."""
    if not transactions:
        return []

    if not client.is_available() or not client.config.task_classify_enabled:
        return [
            ClassificationResult(
                category_id=None,
                source="none",
                confidence=0.0,
                reason="LLM disabled or unavailable",
            )
            for _ in transactions
        ]

    results: List[ClassificationResult] = []
    active_taxonomy = taxonomy or BUILTIN_TAXONOMY

    for i in range(0, len(transactions), chunk_size):
        chunk = transactions[i : i + chunk_size]
        sys_prompt, user_prompt = build_classify_fallback_prompt(
            chunk, active_taxonomy, holder_name
        )
        messages = [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": user_prompt},
        ]

        try:
            resp = client.chat_completion_sync(
                task_type="classify",
                messages=messages,
                temperature=0.1,
                use_json_mode=True,
            )
            results.extend(_parse_classify_chunk_response(resp, chunk, key_to_id, name_to_id))

        except Exception as exc:
            logger.warning("LLM fallback classification failed: %s; falling back to none", exc)
            if raise_on_error:
                raise
            for _ in chunk:
                results.append(
                    ClassificationResult(
                        category_id=None,
                        source="none",
                        confidence=0.0,
                        reason=f"LLM execution failed: {exc}",
                    )
                )

    return results


# =========================================================================
# Task 2: Classification Review
# =========================================================================

async def review_classification_async(
    transactions: List[Any],
    client: LLMClient,
    key_to_id: Dict[str, int],
    holder_name: Optional[str] = None,
) -> List[ReviewResult]:
    """Audit low-confidence classifications."""
    if not transactions or not client.is_available() or not client.config.task_review_enabled:
        return []

    sys_prompt, user_prompt = build_review_classification_prompt(
        transactions, BUILTIN_TAXONOMY, holder_name
    )
    messages = [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": user_prompt},
    ]

    try:
        resp = await client.chat_completion_async(
            task_type="review",
            messages=messages,
            temperature=0.1,
            use_json_mode=True,
        )
        raw_list = resp.get("reviews", []) if isinstance(resp, dict) else resp
        reviews: List[ReviewResult] = []

        for item in raw_list:
            if not isinstance(item, dict):
                continue
            r_id = item.get("id")
            agree = bool(item.get("agree", True))
            sub_key = item.get("suggested_subcategory_key")
            cat_key = item.get("suggested_category_key")
            target_key = sub_key or cat_key
            conf = float(item.get("confidence", 0.0))
            reason = item.get("reason", "")

            # Filter for disagreement with high confidence >= 0.80
            if not agree and conf >= 0.80 and r_id is not None:
                s_id = key_to_id.get(target_key) if target_key else None
                reviews.append(
                    ReviewResult(
                        id=r_id,
                        agree=agree,
                        suggested_category_id=s_id,
                        suggested_category_key=target_key,
                        confidence=conf,
                        reason=reason,
                    )
                )

        return reviews
    except Exception as exc:
        logger.warning("LLM classification review failed: %s", exc)
        return []


def review_classification_sync(
    transactions: List[Any],
    client: LLMClient,
    key_to_id: Dict[str, int],
    holder_name: Optional[str] = None,
) -> List[ReviewResult]:
    """Audit low-confidence classifications synchronously."""
    if not transactions or not client.is_available() or not client.config.task_review_enabled:
        return []

    sys_prompt, user_prompt = build_review_classification_prompt(
        transactions, BUILTIN_TAXONOMY, holder_name
    )
    messages = [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": user_prompt},
    ]

    try:
        resp = client.chat_completion_sync(
            task_type="review",
            messages=messages,
            temperature=0.1,
            use_json_mode=True,
        )
        raw_list = resp.get("reviews", []) if isinstance(resp, dict) else resp
        reviews: List[ReviewResult] = []

        for item in raw_list:
            if not isinstance(item, dict):
                continue
            r_id = item.get("id")
            agree = bool(item.get("agree", True))
            sub_key = item.get("suggested_subcategory_key")
            cat_key = item.get("suggested_category_key")
            target_key = sub_key or cat_key
            conf = float(item.get("confidence", 0.0))
            reason = item.get("reason", "")

            # Filter for disagreement with high confidence >= 0.80
            if not agree and conf >= 0.80 and r_id is not None:
                s_id = key_to_id.get(target_key) if target_key else None
                reviews.append(
                    ReviewResult(
                        id=r_id,
                        agree=agree,
                        suggested_category_id=s_id,
                        suggested_category_key=target_key,
                        confidence=conf,
                        reason=reason,
                    )
                )

        return reviews
    except Exception as exc:
        logger.warning("LLM classification review failed: %s", exc)
        return []


# =========================================================================
# Task 3: Dedup Arbitration
# =========================================================================

async def dedup_arbitration_async(
    primary_txn: Any,
    secondary_txn: Any,
    score: float,
    client: LLMClient,
    holder_name: Optional[str] = None,
) -> ArbitrationResult:
    """Arbitrate candidate pair in ambiguous score range 0.55 <= score < 0.85."""
    if not client.is_available() or not client.config.task_arbitrate_enabled:
        return ArbitrationResult(
            verdict="unsure",
            confidence=0.0,
            reason="LLM arbitration unavailable",
        )

    sys_prompt, user_prompt = build_dedup_arbitration_prompt(
        primary_txn, secondary_txn, score, holder_name
    )
    messages = [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": user_prompt},
    ]

    try:
        resp = await client.chat_completion_async(
            task_type="arbitrate",
            messages=messages,
            temperature=0.1,
            use_json_mode=True,
        )
        if isinstance(resp, dict):
            raw_verdict = str(resp.get("verdict", "unsure")).lower()
            verdict: Literal["same", "different", "unsure"] = "unsure"
            if raw_verdict in ("same", "different", "unsure"):
                verdict = raw_verdict  # type: ignore

            conf = float(resp.get("confidence", 0.5))
            reason = str(resp.get("reason", "LLM arbitration"))
            return ArbitrationResult(verdict=verdict, confidence=conf, reason=reason)
    except Exception as exc:
        logger.warning("LLM dedup arbitration failed: %s", exc)

    return ArbitrationResult(
        verdict="unsure",
        confidence=0.0,
        reason="LLM arbitration failed",
    )


def dedup_arbitration_sync(
    primary_txn: Any,
    secondary_txn: Any,
    score: float,
    client: LLMClient,
    holder_name: Optional[str] = None,
) -> ArbitrationResult:
    """Arbitrate candidate pair in ambiguous score range synchronously."""
    if not client.is_available() or not client.config.task_arbitrate_enabled:
        return ArbitrationResult(
            verdict="unsure",
            confidence=0.0,
            reason="LLM arbitration unavailable",
        )

    sys_prompt, user_prompt = build_dedup_arbitration_prompt(
        primary_txn, secondary_txn, score, holder_name
    )
    messages = [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": user_prompt},
    ]

    try:
        resp = client.chat_completion_sync(
            task_type="arbitrate",
            messages=messages,
            temperature=0.1,
            use_json_mode=True,
        )
        if isinstance(resp, dict):
            raw_verdict = str(resp.get("verdict", "unsure")).lower()
            verdict: Literal["same", "different", "unsure"] = "unsure"
            if raw_verdict in ("same", "different", "unsure"):
                verdict = raw_verdict  # type: ignore

            conf = float(resp.get("confidence", 0.5))
            reason = str(resp.get("reason", "LLM arbitration"))
            return ArbitrationResult(verdict=verdict, confidence=conf, reason=reason)
    except Exception as exc:
        logger.warning("LLM dedup arbitration failed: %s", exc)

    return ArbitrationResult(
        verdict="unsure",
        confidence=0.0,
        reason="LLM arbitration failed",
    )


# =========================================================================
# Task 4: Merchant Normalization
# =========================================================================

async def merchant_normalize_async(
    counterparties: List[str],
    client: LLMClient,
) -> Dict[str, str]:
    """Normalize noisy merchant strings to brand names with deterministic fallback."""
    if not counterparties:
        return {}

    # Rule-based fallback baseline
    normalized: Dict[str, str] = {
        cp: clean_merchant_for_learning(cp) for cp in counterparties
    }

    if not client.is_available() or not client.config.task_merchant_norm_enabled:
        return normalized

    sys_prompt, user_prompt = build_merchant_normalize_prompt(counterparties)
    messages = [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": user_prompt},
    ]

    try:
        resp = await client.chat_completion_async(
            task_type="merchant_norm",
            messages=messages,
            temperature=0.1,
            use_json_mode=True,
        )
        if isinstance(resp, dict) and "normalized" in resp and isinstance(resp["normalized"], dict):
            for k, v in resp["normalized"].items():
                if v and isinstance(v, str):
                    normalized[k] = v.strip()
    except Exception as exc:
        logger.debug("LLM merchant normalization fallback to rule: %s", exc)

    return normalized


def merchant_normalize_sync(
    counterparties: List[str],
    client: LLMClient,
) -> Dict[str, str]:
    """Normalize noisy merchant strings synchronously."""
    if not counterparties:
        return {}

    normalized: Dict[str, str] = {
        cp: clean_merchant_for_learning(cp) for cp in counterparties
    }

    if not client.is_available() or not client.config.task_merchant_norm_enabled:
        return normalized

    sys_prompt, user_prompt = build_merchant_normalize_prompt(counterparties)
    messages = [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": user_prompt},
    ]

    try:
        resp = client.chat_completion_sync(
            task_type="merchant_norm",
            messages=messages,
            temperature=0.1,
            use_json_mode=True,
        )
        if isinstance(resp, dict) and "normalized" in resp and isinstance(resp["normalized"], dict):
            for k, v in resp["normalized"].items():
                if v and isinstance(v, str):
                    normalized[k] = v.strip()
    except Exception as exc:
        logger.debug("LLM merchant normalization fallback to rule: %s", exc)

    return normalized


# =========================================================================
# Task 5: Bank Profile Column Mapping Suggestion
# =========================================================================

_MAPPING_KEY_ALIASES = {
    "txn_time": "date",
    "datetime": "date",
    "trade_time": "date",
    "expense": "expense_amount",
    "debit": "expense_amount",
    "income": "income_amount",
    "credit": "income_amount",
    "flag": "direction",
    "direction_flag": "direction",
    "pay_method": "channel",
    "merchant": "counterparty",
    "summary": "description",
    "remark": "description",
}


def _clean_card_tail(raw: Any) -> Optional[str]:
    if not raw:
        return None
    m = re.search(r"(\d{4})(?!\d)", str(raw).strip())
    return m.group(1) if m else None


def _normalize_account_kind(
    raw: Any, default: Optional[AccountKind] = None
) -> Optional[AccountKind]:
    s = str(raw or "").strip().lower()
    if s in ("debit", "bank_debit", "储蓄卡", "借记卡"):
        return "bank_debit"
    if s in ("credit", "bank_credit", "信用卡", "贷记卡"):
        return "bank_credit"
    if s in ("wallet", "支付宝", "微信", "零钱"):
        return "wallet"
    if s in ("platform",):
        return "platform"
    return default


def _parse_profile_suggestion_response(resp: Any, header: List[str]) -> ProfileSuggestionResult:
    if not isinstance(resp, dict):
        return ProfileSuggestionResult(error="AI 返回结果不是 JSON 对象")

    raw_cols = resp.get("columns", {})
    if not isinstance(raw_cols, dict):
        raw_cols = {}

    header_exact = {h: h for h in header}
    header_stripped = {h.strip(): h for h in header if isinstance(h, str) and h.strip()}
    header_lower = {h.strip().lower(): h for h in header if isinstance(h, str) and h.strip()}

    valid_cols: Dict[str, str] = {}
    for std_f, in_col in raw_cols.items():
        if not in_col or not isinstance(in_col, str):
            continue
        canonical_key = _MAPPING_KEY_ALIASES.get(str(std_f).strip(), str(std_f).strip())
        col_clean = in_col.strip()
        matched_h = (
            header_exact.get(in_col)
            or header_stripped.get(col_clean)
            or header_lower.get(col_clean.lower())
        )
        if matched_h:
            valid_cols[canonical_key] = matched_h

    raw_mode = str(resp.get("amount_mode", "")).strip().lower()
    if raw_mode not in ("signed", "split", "flag"):
        if "expense_amount" in valid_cols and "income_amount" in valid_cols:
            raw_mode = "split"
        elif "direction" in valid_cols:
            raw_mode = "flag"
        else:
            raw_mode = "signed"

    acct_kind = str(resp.get("account_kind", "")).strip().lower()
    if acct_kind in ("bank_debit", "debit"):
        parsed_kind: Optional[Literal["debit", "credit"]] = "debit"
    elif acct_kind in ("bank_credit", "credit"):
        parsed_kind = "credit"
    else:
        parsed_kind = None
    bank_val = str(resp.get("bank") or "").strip() or None
    card_tail_val = _clean_card_tail(resp.get("card_tail"))
    currency_val = _normalize_currency_token(str(resp.get("currency") or "").strip())
    flag_exp = str(resp.get("flag_expense_val") or "").strip() or None
    flag_inc = str(resp.get("flag_income_val") or "").strip() or None

    return ProfileSuggestionResult(
        columns=valid_cols,
        amount_mode=raw_mode,  # type: ignore[arg-type]
        confidence=_parse_confidence(resp.get("confidence", 0.0)),
        reason=str(resp.get("reason") or "AI 智能推导列映射成功").strip(),
        bank=bank_val,
        account_kind=parsed_kind,
        card_tail=card_tail_val,
        currency=currency_val,
        flag_expense_val=flag_exp,
        flag_income_val=flag_inc,
        used_llm=True,
    )


async def suggest_profile_mapping_async(
    header: List[str],
    sample_rows: List[List[Any]],
    client: LLMClient,
    preamble_rows: Optional[List[List[Any]]] = None,
    file_name: Optional[str] = None,
) -> ProfileSuggestionResult:
    """Suggest column mappings and amount_mode for unknown bank statements."""
    if not header:
        return ProfileSuggestionResult(error="未检测到有效表头列名")
    if not client.is_available():
        return ProfileSuggestionResult(error="AI 服务未启用或未配置 Base URL")
    if not client.config.task_suggest_profile_enabled:
        return ProfileSuggestionResult(error="已在设置中关闭「未知账单表头列名智能推导」任务开关")

    sys_prompt, user_prompt = build_mapping_suggest_prompt(
        header,
        sample_rows,
        preamble_rows=preamble_rows,
        file_name=file_name,
    )
    messages = [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": user_prompt},
    ]

    try:
        resp = await client.chat_completion_async(
            task_type="suggest_profile",
            messages=messages,
            temperature=0.1,
            use_json_mode=True,
        )
        return _parse_profile_suggestion_response(resp, header)
    except Exception as exc:
        logger.warning("LLM profile suggestion failed: %s", exc)
        return ProfileSuggestionResult(used_llm=True, error=str(exc))


def suggest_profile_mapping_sync(
    header: List[str],
    sample_rows: List[List[Any]],
    client: LLMClient,
    preamble_rows: Optional[List[List[Any]]] = None,
    file_name: Optional[str] = None,
) -> ProfileSuggestionResult:
    """Suggest column mappings and amount_mode synchronously."""
    if not header:
        return ProfileSuggestionResult(error="未检测到有效表头列名")
    if not client.is_available():
        return ProfileSuggestionResult(error="AI 服务未启用或未配置 Base URL")
    if not client.config.task_suggest_profile_enabled:
        return ProfileSuggestionResult(error="已在设置中关闭「未知账单表头列名智能推导」任务开关")

    sys_prompt, user_prompt = build_mapping_suggest_prompt(
        header,
        sample_rows,
        preamble_rows=preamble_rows,
        file_name=file_name,
    )
    messages = [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": user_prompt},
    ]

    try:
        resp = client.chat_completion_sync(
            task_type="suggest_profile",
            messages=messages,
            temperature=0.1,
            use_json_mode=True,
        )
        return _parse_profile_suggestion_response(resp, header)
    except Exception as exc:
        logger.warning("LLM profile suggestion failed: %s", exc)
        return ProfileSuggestionResult(used_llm=True, error=str(exc))


# =========================================================================
# Task 6: Statement Top-Account Recognition & Full Statement LLM Takeover
# =========================================================================
def recognize_statement_account_sync(
    preamble_rows: List[List[Any]],
    client: LLMClient,
    file_name: Optional[str] = None,
) -> StatementAccountResult:
    """Recognize top-of-statement account metadata via regex fallback + LLM."""
    joined_lines = [
        " ".join(str(c).strip() for c in r if c is not None and str(c).strip())
        for r in (preamble_rows or [])[:20]
    ]
    if file_name:
        joined_lines.append(str(file_name))
    fb = extract_preamble_metadata_fallback("\n".join(joined_lines))

    result = StatementAccountResult(
        bank=fb.get("bank"),
        account_kind=_normalize_account_kind(fb.get("account_kind"), default=None),
        card_tail=_clean_card_tail(fb.get("card_tail")),
        currency=_normalize_currency_token(fb.get("currency")) or "CNY",
        holder_name=fb.get("holder_name"),
        period_start=fb.get("period_start"),
        period_end=fb.get("period_end"),
    )

    if not client.is_available() or not preamble_rows:
        return result

    sys_prompt, user_prompt = build_statement_account_prompt(
        preamble_rows, file_name=file_name
    )
    messages = [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": user_prompt},
    ]
    try:
        resp = client.chat_completion_sync(
            task_type="suggest_profile",
            messages=messages,
            temperature=0.1,
            use_json_mode=True,
        )
        if isinstance(resp, dict):
            result.used_llm = True
            llm_bank = str(resp.get("bank") or "").strip() or None
            llm_kind = _normalize_account_kind(resp.get("account_kind"))
            llm_tail = _clean_card_tail(resp.get("card_tail"))
            llm_cur = _normalize_currency_token(str(resp.get("currency") or "").strip())
            if llm_bank:
                result.bank = llm_bank
            if llm_kind:
                result.account_kind = llm_kind
            if llm_tail:
                result.card_tail = llm_tail
            if llm_cur:
                result.currency = llm_cur
            result.confidence = _parse_confidence(resp.get("confidence", 0.0))
    except Exception as exc:
        logger.warning("LLM statement account recognition failed: %s", exc)
        result.used_llm = True
        result.error = str(exc)

    return result


def _parse_llm_decimal_amount(raw_val: Any) -> Optional[Decimal]:
    if raw_val is None:
        return None
    s = re.sub(r"[^\d.\-+]", "", str(raw_val).replace(",", "").strip())
    if not s or s in ("-", "+", ".", "-.", "+."):
        return None
    try:
        return Decimal(s)
    except InvalidOperation:
        return None


def _restore_masked_holder(text: Optional[str], holder_name: Optional[str]) -> Optional[str]:
    if not text:
        return None
    out = str(text).strip()
    if holder_name and "[USER]" in out:
        out = out.replace("[USER]", holder_name.strip())
    return out or None


_VALID_DIRECTIONS = {"expense", "income", "transfer", "refund", "neutral"}
_VALID_CHANNELS = {"alipay", "tenpay", "jdpay", "meituan", "unionpay"}


MAX_LLM_PARSE_CHUNKS = 20


def parse_statement_with_llm_sync(
    raw_lines: List[str],
    client: LLMClient,
    file_name: Optional[str] = None,
    chunk_size: int = 45,
    extra_holders: Optional[List[str]] = None,
) -> Tuple[ParseResult, Dict[str, Any]]:
    """Have the LLM take over full statement parsing (top account + all transaction lines)."""
    if not client.is_available():
        raise ValueError("AI 服务未启用或未配置 Base URL，无法接管解析")
    if not client.config.task_full_parse_enabled:
        raise ValueError("已在设置中关闭「复杂/未知账单 AI 整单接管识别」任务开关")

    top_context = "\n".join((raw_lines or [])[:25]) + (f"\n{file_name}" if file_name else "")
    fb = extract_preamble_metadata_fallback(top_context)
    local_holder = fb.get("holder_name")
    all_holders: List[str] = []
    if local_holder:
        all_holders.append(local_holder)
    if extra_holders:
        all_holders.extend(extra_holders)

    masked_lines = prepare_statement_lines_for_llm(raw_lines, local_holder=all_holders or None)
    if not masked_lines:
        raise ValueError("未能从文件中提取可识别的文本内容")

    header_lines = masked_lines[:22]
    if len(masked_lines) <= 55:
        chunks = [masked_lines]
    else:
        chunks = [
            masked_lines[i : i + chunk_size]
            for i in range(0, len(masked_lines), chunk_size)
        ]

    truncated_chunks = 0
    if len(chunks) > MAX_LLM_PARSE_CHUNKS:
        truncated_chunks = len(chunks) - MAX_LLM_PARSE_CHUNKS
        chunks = chunks[:MAX_LLM_PARSE_CHUNKS]

    llm_bank: Optional[str] = None
    llm_kind: Optional[AccountKind] = None
    llm_tail: Optional[str] = None
    llm_currency: Optional[str] = None
    llm_start: Optional[str] = None
    llm_end: Optional[str] = None

    raw_txn_dicts: List[Dict[str, Any]] = []
    chunks_succeeded = 0
    last_error: Optional[str] = None

    for idx, chunk in enumerate(chunks):
        sys_prompt, user_prompt = build_full_statement_parse_prompt(
            header_lines=header_lines,
            chunk_lines=chunk,
            file_name=file_name,
            include_account=(idx == 0),
        )
        messages = [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": user_prompt},
        ]
        try:
            resp = client.chat_completion_sync(
                task_type="full_parse",
                messages=messages,
                temperature=0.05,
                use_json_mode=True,
            )
            if not isinstance(resp, dict):
                last_error = "AI 返回结果不是 JSON 对象"
                continue
            chunks_succeeded += 1

            acct_obj = resp.get("account")
            if isinstance(acct_obj, dict):
                llm_bank = llm_bank or (str(acct_obj.get("bank") or "").strip() or None)
                llm_kind = llm_kind or _normalize_account_kind(acct_obj.get("account_kind"))
                llm_tail = llm_tail or _clean_card_tail(acct_obj.get("card_tail"))
                llm_currency = llm_currency or _normalize_currency_token(
                    str(acct_obj.get("currency") or "").strip()
                )

            period_obj = resp.get("period")
            if isinstance(period_obj, dict):
                s_val = str(period_obj.get("start") or "").strip()
                e_val = str(period_obj.get("end") or "").strip()
                if s_val and not llm_start:
                    llm_start = _normalize_date_sep(s_val)
                if e_val and not llm_end:
                    llm_end = _normalize_date_sep(e_val)

            txns_list = resp.get("transactions", [])
            if isinstance(txns_list, list):
                for item in txns_list:
                    if isinstance(item, dict):
                        raw_txn_dicts.append(item)
        except Exception as exc:
            logger.warning("LLM full parse chunk %d/%d failed: %s", idx + 1, len(chunks), exc)
            last_error = str(exc)

    if chunks_succeeded == 0:
        raise ValueError(f"AI 整单识别失败: {last_error or '无有效返回'}")

    eff_bank = llm_bank or fb.get("bank") or "未知银行/机构"
    eff_kind: AccountKind = (
        llm_kind or _normalize_account_kind(fb.get("account_kind"), default="bank_debit") or "bank_debit"
    )
    eff_tail = llm_tail or _clean_card_tail(fb.get("card_tail"))
    eff_currency = llm_currency or _normalize_currency_token(fb.get("currency")) or "CNY"
    p_start = llm_start or fb.get("period_start")
    p_end = llm_end or fb.get("period_end")

    source_id = "llm_statement"
    transactions: List[TransactionCreate] = []
    warnings: List[str] = []
    tails_seen: set[Optional[str]] = set()

    for item in raw_txn_dicts:
        raw_dt = item.get("txn_time") or item.get("date")
        if not raw_dt:
            continue
        iso_time, precision = parse_datetime(str(raw_dt).strip())
        if not iso_time:
            continue

        dec_amt = _parse_llm_decimal_amount(item.get("amount"))
        signed_hint: Optional[str] = None
        if dec_amt is None:
            exp_dec = _parse_llm_decimal_amount(item.get("expense_amount"))
            inc_dec = _parse_llm_decimal_amount(item.get("income_amount"))
            if exp_dec is not None and exp_dec != 0:
                dec_amt = exp_dec
                signed_hint = "expense"
            elif inc_dec is not None and inc_dec != 0:
                dec_amt = inc_dec
                signed_hint = "income"

        if dec_amt is None or dec_amt == 0:
            continue

        amount_cents = to_cents(abs(dec_amt))
        if amount_cents <= 0:
            continue

        raw_dir = str(item.get("direction") or "").strip().lower()
        if raw_dir in _VALID_DIRECTIONS:
            direction: DirectionType = raw_dir  # type: ignore[assignment]
        elif signed_hint in _VALID_DIRECTIONS:
            direction = signed_hint  # type: ignore[assignment]
        else:
            direction = "expense" if dec_amt < 0 else "income"

        counterparty = _restore_masked_holder(item.get("counterparty"), local_holder)
        description = _restore_masked_holder(item.get("description"), local_holder)

        raw_chan = str(item.get("channel") or "").strip().lower()
        if raw_chan in _VALID_CHANNELS:
            channel: Optional[ChannelType] = raw_chan  # type: ignore[assignment]
            _, merchant_norm = extract_channel_and_normalize(counterparty or description)
        else:
            channel, merchant_norm = extract_channel_and_normalize(counterparty or description)

        row_tail = _clean_card_tail(item.get("card_tail")) or eff_tail
        row_currency = _normalize_currency_token(str(item.get("currency") or "").strip()) or eff_currency
        pay_method = f"{eff_bank}({row_tail})" if row_tail else eff_bank

        tails_seen.add(row_tail)
        account_ref = AccountRef(
            kind=eff_kind,
            bank=eff_bank,
            tail=row_tail,
            holder=local_holder,
        )
        transactions.append(
            TransactionCreate(
                source=source_id,
                txn_time=iso_time,
                time_precision=precision,  # type: ignore[arg-type]
                post_date=iso_time.split(" ")[0] if " " in iso_time else iso_time,
                amount_cents=amount_cents,
                currency=row_currency,
                direction=direction,
                counterparty=counterparty,
                merchant_norm=merchant_norm or counterparty,
                description=description,
                channel=channel,
                pay_method=pay_method,
                account_ref=account_ref,
            )
        )

    if not transactions:
        raise ValueError("AI 整单识别完成，但未提取到有效交易明细")

    if not p_start or not p_end:
        dates = sorted(t.post_date for t in transactions if t.post_date)
        if dates:
            p_start = p_start or dates[0]
            p_end = p_end or dates[-1]

    accounts: List[AccountCreate] = []
    ordered_tails = sorted(tails_seen, key=lambda x: x or "")
    for tail_val in ordered_tails:
        accounts.append(
            AccountCreate(
                kind=eff_kind,
                bank=eff_bank,
                tail=tail_val,
                alias=f"{eff_bank} ({tail_val})" if tail_val else eff_bank,
                holder=local_holder,
                currency=eff_currency,
            )
        )
    if not accounts:
        accounts.append(
            AccountCreate(
                kind=eff_kind,
                bank=eff_bank,
                tail=eff_tail,
                alias=f"{eff_bank} ({eff_tail})" if eff_tail else eff_bank,
                holder=local_holder,
                currency=eff_currency,
            )
        )

    assign_fingerprints(transactions)

    self_checks = [
        SelfCheckResult(
            name="AI 整单识别笔数",
            expected=len(transactions),
            actual=len(transactions),
            passed=True,
            description=f"LLM 共提取 {len(transactions)} 笔交易记录（分 {len(chunks)} 段处理）",
        ),
        SelfCheckResult(
            name="顶部账户识别",
            expected=f"{eff_bank}({eff_tail or '无尾号'})",
            actual=f"{eff_bank}({eff_tail or '无尾号'})",
            passed=eff_bank != "未知银行/机构",
            description=(
                f"已识别账单抬头账户：{eff_bank} / 尾号 {eff_tail or '未检出'}"
                if eff_bank != "未知银行/机构"
                else "未在账单顶部检测到明确银行名称，可在下方确认并补充账户信息"
            ),
        ),
    ]

    if truncated_chunks > 0:
        warnings.append(
            f"账单内容较长，为控制 AI 调用成本已截断后续 {truncated_chunks} 个分段（上限 {MAX_LLM_PARSE_CHUNKS} 段），建议拆分为按月账单导入"
        )

    parse_result = ParseResult(
        accounts=accounts,
        transactions=transactions,
        holder_name=local_holder,
        period=(p_start, p_end) if (p_start or p_end) else None,
        self_checks=self_checks,
        warnings=warnings,
    )
    meta = {
        "bank": eff_bank,
        "account_kind": eff_kind,
        "card_tail": eff_tail,
        "holder_name": local_holder,
        "currency": eff_currency,
        "period_start": p_start,
        "period_end": p_end,
        "chunks": len(chunks),
        "used_llm": True,
    }
    return parse_result, meta
