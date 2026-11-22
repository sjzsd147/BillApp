"""Cross-Source L3 Deduplication Engine (Platform vs Bank Statement Matching).

Features 14 & 15:
- Gatekeeper hard constraints (amount, currency, direction, tail consistency, time window, blocklist)
- Authoritative composite scoring formula from implementation_plan.md §4.3
- Frontend breakdown contract {amount, tail, time, channel, name, penalty}
- Status thresholds (>=0.85 auto, 0.55~0.85 pending)
- Greedy 1-to-1 bipartite allocation
- Robust merchant normalization and multi-strategy similarity
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple
import sqlite3

from billapp.importers.common import parse_iso_datetime

CHANNEL_PREFIXES = (
    "支付宝-", "支付宝—", "支付宝_",
    "财付通-", "财付通—", "财付通_",
    "网银在线-", "网银在线—",
    "京东支付-", "京东支付—",
    "美团支付-", "美团-",
    "银联-", "银联转账-",
    "云闪付-",
)

CORPORATE_SUFFIXES = (
    "（中国）有限公司", "(中国)有限公司",
    "股份有限公司上海市分公司", "股份有限公司分公司",
    "股份有限公司", "有限责任公司", "有限公司",
    "分公司", "上海分公司", "上海分",
    "商行", "便利店", "分店",
    "REDEMPTION",
)

PARENTHETICAL_ANNOTATIONS = re.compile(
    r"[（\(](?:特约商户|特惠|中国|上海市分公司|分店|[\u4e00-\u9fa5A-Za-z0-9\s]+?店)[）\)]"
)

SPECIAL_CHARS = re.compile(r"^[\s*#\-—_]+|[\s*#\-—_]+$")


def normalize_merchant(name: Optional[str]) -> str:
    """Normalize merchant names by stripping payment channel prefixes and corporate suffixes."""
    if not name:
        return ""

    text = name.strip().replace("\x00", "")
    if not text:
        return ""

    # Stage 1: Strip payment channel prefixes
    for p in CHANNEL_PREFIXES:
        if text.startswith(p):
            text = text[len(p):].strip()
            break

    # Stage 2: Strip specific and generic parenthetical notes
    text = PARENTHETICAL_ANNOTATIONS.sub("", text).strip()
    text = re.sub(r"[（\(].*?[）\)]", "", text).strip()

    # Stage 3: Strip corporate entity suffixes
    for s in CORPORATE_SUFFIXES:
        if text.endswith(s):
            text = text[:-len(s)].strip()
            break

    # Stage 4: Strip leading/trailing noise symbols
    text = SPECIAL_CHARS.sub("", text).strip()

    # Core brand protection: ensure non-empty fallback
    return text if text else name.strip()


def compute_name_sim(
    b_merchant_norm: str,
    p_counterparty: Optional[str],
    p_description: Optional[str] = None,
) -> float:
    """Compute similarity (0.0 to 1.0) between bank normalized merchant and platform records."""
    if not b_merchant_norm:
        return 0.0

    b_norm = b_merchant_norm.lower().strip()
    p_norm = normalize_merchant(p_counterparty).lower().strip() if p_counterparty else ""
    p_raw_party = (p_counterparty or "").lower().strip()
    p_desc = (p_description or "").lower().strip()

    if not b_norm:
        return 0.0

    # 1. Exact match after normalization
    if p_norm and b_norm == p_norm:
        return 1.0

    # 2. Substring containment in normalized counterparty
    if p_norm and (b_norm in p_norm or p_norm in b_norm):
        return 1.0

    # 3. Substring containment in raw counterparty
    if p_raw_party and (b_norm in p_raw_party or p_raw_party in b_norm):
        return 1.0

    # 4. Substring containment in description (e.g. 金拱门 in 金拱门套餐 / 麦当劳)
    if p_desc and len(b_norm) >= 2 and b_norm in p_desc:
        return 1.0
    if p_norm and len(p_norm) >= 2 and p_norm in p_desc:
        return 1.0

    # 5. Character-level Jaccard similarity for multi-character entities
    if p_norm and len(b_norm) >= 2 and len(p_norm) >= 2:
        set_b = set(b_norm)
        set_p = set(p_norm)
        intersection = len(set_b & set_p)
        union = len(set_b | set_p)
        if union > 0:
            jaccard = intersection / union
            if jaccard >= 0.50:
                return jaccard

    return 0.0


def compute_match_score_with_breakdown(
    p_amount_cents: int,
    b_amount_cents: int,
    p_tail: Optional[str],
    b_tail: Optional[str],
    p_time: datetime,
    b_time: datetime,
    b_channel: Optional[str],
    p_source: str,
    b_merchant_norm: str,
    p_counterparty: str,
    p_description: Optional[str] = None,
    p_bank: Optional[str] = None,
    b_bank: Optional[str] = None,
    p_precision: str = "second",
    b_precision: str = "second",
    p_currency: str = "CNY",
    b_currency: str = "CNY",
    p_direction: str = "expense",
    b_direction: str = "expense",
    b_card_kind: Optional[str] = None,
    is_blocked: bool = False,
    second_window_minutes: int = 30,
    day_window_debit_days: int = 3,
    day_window_credit_days: int = 5,
) -> Tuple[float, Dict[str, float]]:
    """Compute cross-source L3 candidate matching score and frontend breakdown.
    
    Conforms to frontend breakdown contract:
    {amount: number, tail: number, time: number, channel: number, name: number, penalty: number}
    """
    zero_breakdown = {
        "amount": 0.0,
        "tail": 0.0,
        "time": 0.0,
        "channel": 0.0,
        "name": 0.0,
        "penalty": 0.0,
    }

    # 1. Blocklist check
    if is_blocked:
        return 0.0, zero_breakdown

    # 2. Hard constraint: exact amount
    if p_amount_cents != b_amount_cents or p_amount_cents <= 0:
        return 0.0, zero_breakdown

    # 3. Hard constraint: exact currency
    if p_currency != b_currency:
        return 0.0, zero_breakdown

    # 4. Hard constraint: direction correspondence
    valid_dir = (p_direction == b_direction) or (
        p_direction in ("expense", "neutral") and b_direction in ("expense", "neutral")
    )
    if not valid_dir:
        return 0.0, zero_breakdown

    # 5. Hard constraint: card tail consistency
    # If both tails are known and distinct -> hard reject
    if p_tail and b_tail and p_tail != b_tail:
        return 0.0, zero_breakdown
    # If neither tail is known, check bank institution conflict
    if not p_tail and not b_tail and p_bank and b_bank:
        if p_bank != b_bank and p_bank not in b_bank and b_bank not in p_bank:
            return 0.0, zero_breakdown

    # 6. Time feasibility boundaries
    is_date_only = (
        p_precision == "day"
        or b_precision == "day"
        or (p_time.time() == datetime.min.time() and b_time.time() == datetime.min.time())
    )

    max_sec_window = max(60, int(second_window_minutes) * 60)
    if not is_date_only:
        sec_diff = abs((p_time - b_time).total_seconds())
        if sec_diff > max_sec_window:
            return 0.0, zero_breakdown
    else:
        day_diff = abs((p_time.date() - b_time.date()).days)
        max_days = int(day_window_credit_days) if b_card_kind == "bank_credit" else int(day_window_debit_days)
        if day_diff > max_days:
            return 0.0, zero_breakdown

    # Passed hard constraints -> Base score
    amount_score = 0.30

    # Account / Card Tail scoring
    tail_score = 0.0
    if p_tail and b_tail and p_tail == b_tail:
        tail_score = 0.25
    elif (p_tail and not b_tail) or (b_tail and not p_tail):
        tail_score = 0.10
    elif p_bank and b_bank and (p_bank == b_bank or p_bank in b_bank or b_bank in p_bank):
        tail_score = 0.10

    # Time scoring
    time_score = 0.0
    if not is_date_only:
        sec_diff = abs((p_time - b_time).total_seconds())
        if sec_diff <= 120:
            time_score = 0.30
        elif sec_diff <= 600:
            time_score = 0.20
        elif sec_diff <= max_sec_window:
            time_score = 0.10
    else:
        day_diff = abs((p_time.date() - b_time.date()).days)
        if day_diff == 0:
            time_score = 0.15
        elif day_diff == 1:
            time_score = 0.10
        elif day_diff <= max_days:
            time_score = 0.05

    # Channel match scoring
    channel_score = 0.0
    penalty_score = 0.0
    norm_p = "wechat" if p_source in ("wechat", "tenpay") else ("alipay" if p_source == "alipay" else p_source)
    norm_b = "wechat" if b_channel in ("wechat", "tenpay") else ("alipay" if b_channel == "alipay" else b_channel)

    if norm_b is not None:
        if norm_b == norm_p:
            channel_score = 0.10
        else:
            penalty_score = -0.25

    # Merchant name similarity scoring
    sim = compute_name_sim(b_merchant_norm, p_counterparty, p_description)
    name_score = round(0.10 * sim, 4)

    total = amount_score + tail_score + time_score + channel_score + name_score + penalty_score
    final_score = max(0.0, min(1.0, round(total, 4)))

    breakdown = {
        "amount": amount_score,
        "tail": tail_score,
        "time": time_score,
        "channel": channel_score,
        "name": name_score,
        "penalty": penalty_score,
    }

    return final_score, breakdown


def compute_match_score(
    p_amount_cents: int,
    b_amount_cents: int,
    p_tail: Optional[str],
    b_tail: Optional[str],
    p_time: datetime,
    b_time: datetime,
    b_channel: Optional[str],
    p_source: str,
    b_merchant_norm: str,
    p_counterparty: str,
    p_description: Optional[str] = None,
    p_bank: Optional[str] = None,
    b_bank: Optional[str] = None,
    p_precision: str = "second",
    b_precision: str = "second",
    p_currency: str = "CNY",
    b_currency: str = "CNY",
    p_direction: str = "expense",
    b_direction: str = "expense",
    b_card_kind: Optional[str] = None,
    is_blocked: bool = False,
) -> float:
    """Compute cross-source L3 candidate matching score (0.0 - 1.0)."""
    score, _ = compute_match_score_with_breakdown(
        p_amount_cents=p_amount_cents,
        b_amount_cents=b_amount_cents,
        p_tail=p_tail,
        b_tail=b_tail,
        p_time=p_time,
        b_time=b_time,
        b_channel=b_channel,
        p_source=p_source,
        b_merchant_norm=b_merchant_norm,
        p_counterparty=p_counterparty,
        p_description=p_description,
        p_bank=p_bank,
        b_bank=b_bank,
        p_precision=p_precision,
        b_precision=b_precision,
        p_currency=p_currency,
        b_currency=b_currency,
        p_direction=p_direction,
        b_direction=b_direction,
        b_card_kind=b_card_kind,
        is_blocked=is_blocked,
    )
    return score


def match_cross_source(
    conn: sqlite3.Connection,
    time_window_days: int = 7,
    auto_threshold: float = 0.85,
    review_threshold: float = 0.55,
    second_window_minutes: int = 30,
    day_window_debit_days: int = 3,
    day_window_credit_days: int = 5,
) -> List[Dict[str, Any]]:
    """Match platform transactions (Alipay, WeChat) against bank statements.
    
    Greedy 1-to-1 allocation:
    - score >= auto_threshold (0.85): status='auto', transactions.duplicate_of set immediately.
    - review_threshold (0.55) <= score < auto_threshold: status='pending' for manual review.
    """
    # 1. Fetch blocklisted pairs
    block_rows = conn.execute("SELECT a_id, b_id FROM match_blocklist WHERE kind='duplicate'").fetchall()
    blocked_pairs: Set[Tuple[int, int]] = set()
    for br in block_rows:
        blocked_pairs.add((br["a_id"], br["b_id"]))
        blocked_pairs.add((br["b_id"], br["a_id"]))

    # 2. Query platform transactions (primary candidates)
    p_query = """
    SELECT t.id, t.source, t.txn_time, t.amount_cents, t.currency, t.direction,
           t.counterparty, t.description, t.merchant_norm, t.pay_method,
           t.fund_account_id, t.time_precision,
           a.tail AS p_tail, a.bank AS p_bank
    FROM transactions t
    LEFT JOIN accounts a ON t.fund_account_id = a.id
    WHERE t.source IN ('alipay', 'wechat')
      AND t.direction IN ('expense', 'refund')
      AND t.duplicate_of IS NULL
      AND t.excluded = 0
      AND t.amount_cents > 0
    """
    p_records = conn.execute(p_query).fetchall()

    # 3. Query bank statement transactions (secondary candidates)
    b_query = """
    SELECT t.id, t.source, t.txn_time, t.amount_cents, t.currency, t.direction,
           t.counterparty, t.description, t.merchant_norm, t.channel,
           t.account_id, t.time_precision,
           a.tail AS b_tail, a.bank AS b_bank, a.kind AS b_card_kind
    FROM transactions t
    LEFT JOIN accounts a ON t.account_id = a.id
    WHERE t.source NOT IN ('alipay', 'wechat')
      AND t.direction IN ('expense', 'refund')
      AND t.duplicate_of IS NULL
      AND t.excluded = 0
      AND t.amount_cents > 0
    """
    b_records = conn.execute(b_query).fetchall()

    # 4. Generate candidate pairs
    candidates = []
    for p in p_records:
        p_id = p["id"]
        p_amt = p["amount_cents"]
        p_cur = p["currency"]
        p_dir = p["direction"]
        p_time = parse_iso_datetime(p["txn_time"])
        if p_time is None:
            continue
        p_tail = p["p_tail"]
        p_bank = p["p_bank"]
        p_party = p["counterparty"] or ""
        p_desc = p["description"] or ""
        p_src = p["source"]
        p_prec = p["time_precision"] or "second"

        for b in b_records:
            b_id = b["id"]
            if (p_id, b_id) in blocked_pairs:
                continue

            # Hard constraints: amount and currency must match
            if p_amt != b["amount_cents"] or p_cur != b["currency"]:
                continue

            b_dir = b["direction"]
            if p_dir != b_dir and not (p_dir in ("expense", "neutral") and b_dir in ("expense", "neutral")):
                continue

            b_time = parse_iso_datetime(b["txn_time"])
            if b_time is None:
                continue
            b_tail = b["b_tail"]
            b_bank = b["b_bank"]
            b_card_kind = b["b_card_kind"]
            b_chan = b["channel"]
            b_prec = b["time_precision"] or "second"
            b_norm = b["merchant_norm"] or normalize_merchant(b["counterparty"] or b["description"] or "")

            score, breakdown = compute_match_score_with_breakdown(
                p_amount_cents=p_amt,
                b_amount_cents=b["amount_cents"],
                p_tail=p_tail,
                b_tail=b_tail,
                p_time=p_time,
                b_time=b_time,
                b_channel=b_chan,
                p_source=p_src,
                b_merchant_norm=b_norm,
                p_counterparty=p_party,
                p_description=p_desc,
                p_bank=p_bank,
                b_bank=b_bank,
                p_precision=p_prec,
                b_precision=b_prec,
                p_currency=p_cur,
                b_currency=b["currency"],
                p_direction=p_dir,
                b_direction=b_dir,
                b_card_kind=b_card_kind,
                is_blocked=False,
                second_window_minutes=second_window_minutes,
                day_window_debit_days=day_window_debit_days,
                day_window_credit_days=day_window_credit_days,
            )

            if score >= review_threshold:
                time_diff = abs((p_time - b_time).total_seconds())
                candidates.append({
                    "p_id": p_id,
                    "b_id": b_id,
                    "score": score,
                    "time_diff": time_diff,
                    "features": {
                        "amount_cents": p_amt,
                        "time_diff_sec": time_diff,
                        "p_tail": p_tail,
                        "b_tail": b_tail,
                        "b_channel": b_chan,
                        "p_source": p_src,
                        "breakdown": breakdown,
                    },
                })

    # 5. Greedy 1-to-1 allocation
    # Sort by score descending, then time diff ascending, then IDs for determinism
    candidates.sort(key=lambda c: (-c["score"], c["time_diff"], c["p_id"], c["b_id"]))

    matched_p: Set[int] = set()
    matched_b: Set[int] = set()
    final_matches: List[Dict[str, Any]] = []

    for c in candidates:
        if c["p_id"] in matched_p or c["b_id"] in matched_b:
            continue

        matched_p.add(c["p_id"])
        matched_b.add(c["b_id"])

        status = "auto" if c["score"] >= auto_threshold else "pending"
        c["status"] = status
        final_matches.append(c)

        features_json = json.dumps(c["features"], ensure_ascii=False)
        # Check if match_link already exists
        existing = conn.execute(
            "SELECT id, status FROM match_links WHERE kind = 'duplicate' AND primary_id = ? AND secondary_id = ?",
            (c["p_id"], c["b_id"]),
        ).fetchone()

        if existing:
            c["link_id"] = existing["id"]
            if existing["status"] not in ("confirmed", "rejected"):
                conn.execute(
                    """
                    UPDATE match_links
                    SET score = ?, features_json = ?, status = ?, updated_at = datetime('now', 'localtime')
                    WHERE id = ?
                    """,
                    (c["score"], features_json, status, existing["id"]),
                )
        else:
            cur = conn.execute(
                """
                INSERT INTO match_links (kind, primary_id, secondary_id, score, features_json, status, created_at, updated_at)
                VALUES ('duplicate', ?, ?, ?, ?, ?, datetime('now', 'localtime'), datetime('now', 'localtime'))
                """,
                (c["p_id"], c["b_id"], c["score"], features_json, status),
            )
            c["link_id"] = cur.lastrowid

        if status == "auto":
            conn.execute(
                "UPDATE transactions SET duplicate_of = ? WHERE id = ?",
                (c["p_id"], c["b_id"]),
            )

    return final_matches
