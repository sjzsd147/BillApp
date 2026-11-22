"""L4 Internal Transfer Pairing Engine.

Feature 16 implementation:
- Platform wallet withdrawal <-> Bank card deposit
- Bank debit card payment <-> Credit card repayment
- Inter-bank transfer between user's own accounts
- Bilateral linking: transfer_peer set mutually, directions set to 'transfer'
- Neutral expense/income impact (complete statistical neutrality)
- Auto-link (>= 0.85) vs pending review (0.55 ~ 0.85)
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple

from billapp.importers.common import parse_iso_datetime


KNOWN_BANKS = (
    "建设银行", "招商银行", "中国银行", "工商银行", "农业银行",
    "交通银行", "平安银行", "中信银行", "民生银行", "光大银行",
    "浦发银行", "广发银行", "华夏银行", "邮储银行", "北京银行", "上海银行",
)


def extract_bank_and_tail(text: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """Extract bank name and 4-digit tail from strings like '建设银行(5678)' or '招商银行储蓄卡(1234)'."""
    if not text:
        return None, None

    # Match bank name and 4-digit tail
    m = re.search(r"([\u4e00-\u9fa5]+).*?[\(（](\d{4})[\)）]", text)
    if m:
        bank_cand = m.group(1).strip()
        tail_cand = m.group(2).strip()
        for b in KNOWN_BANKS:
            if b in bank_cand or bank_cand in b:
                return b, tail_cand
        return bank_cand, tail_cand

    # Match 4-digit tail anywhere if present
    tail_match = re.search(r"[\(（]?(\d{4})[\)）]?", text)
    found_tail = tail_match.group(1) if tail_match else None

    # Match bank name only
    for b in KNOWN_BANKS:
        if b in text:
            return b, found_tail

    return None, found_tail


def compute_transfer_score(
    out_txn: Dict[str, Any],
    in_txn: Dict[str, Any],
    holder_name: Optional[str] = None,
    max_delta_sec: int = 172800,
) -> Tuple[float, Dict[str, Any]]:
    """Compute transfer matching score (0.0 - 1.0) and feature payload."""
    # Hard constraint: exact amount and currency
    if out_txn["amount_cents"] != in_txn["amount_cents"] or out_txn["amount_cents"] <= 0:
        return 0.0, {}

    if out_txn.get("currency", "CNY") != in_txn.get("currency", "CNY"):
        return 0.0, {}

    t_out = datetime.fromisoformat(out_txn["txn_time"])
    t_in = datetime.fromisoformat(in_txn["txn_time"])
    delta_sec = (t_in - t_out).total_seconds()

    o_prec = (
        out_txn.get("time_precision")
        if hasattr(out_txn, "get")
        else (out_txn["time_precision"] if "time_precision" in out_txn.keys() else "second")
    ) or "second"
    i_prec = (
        in_txn.get("time_precision")
        if hasattr(in_txn, "get")
        else (in_txn["time_precision"] if "time_precision" in in_txn.keys() else "second")
    ) or "second"

    is_date_only = (
        o_prec == "day"
        or i_prec == "day"
        or (t_out.time() == datetime.min.time() and t_in.time() == datetime.min.time())
    )

    if not is_date_only:
        if not (-600 <= delta_sec <= max_delta_sec):
            return 0.0, {}
    else:
        if abs(delta_sec) > max_delta_sec:
            return 0.0, {}

    score = 0.30  # Base score for exact amount & currency match
    time_diff = abs(delta_sec)
    features: Dict[str, Any] = {
        "amount_cents": out_txn["amount_cents"],
        "out_id": out_txn["id"],
        "in_id": in_txn["id"],
        "out_source": out_txn["source"],
        "in_source": in_txn["source"],
        "time_diff_sec": time_diff,
    }

    # Time window score
    if time_diff <= 600:
        score += 0.15
    elif time_diff <= 7200:
        score += 0.10
    elif time_diff <= 86400:
        score += 0.05
    elif time_diff <= 172800:
        score += 0.02

    out_src = out_txn["source"]
    in_src = in_txn["source"]
    out_desc = f"{out_txn.get('description') or ''} {out_txn.get('counterparty') or ''} {out_txn.get('status') or ''}"
    in_desc = f"{in_txn.get('description') or ''} {in_txn.get('counterparty') or ''}"

    out_kind = out_txn.get("acc_kind")
    in_kind = in_txn.get("acc_kind")

    # Pattern A: Platform Withdrawal -> Bank Deposit
    if out_src in ("wechat", "alipay") and in_src not in ("wechat", "alipay"):
        if any(w in out_desc for w in ("提现", "转出到银行卡", "零钱提现", "经营账户提现", "零钱通转出")):
            score += 0.25
            features["pattern"] = "wallet_withdrawal"

        target_bank, target_tail = extract_bank_and_tail(out_txn.get("counterparty"))
        in_bank = in_txn.get("acc_bank")
        in_tail = in_txn.get("acc_tail")

        if target_bank and in_bank and (target_bank in in_bank or in_bank in target_bank):
            if target_tail and in_tail and target_tail == in_tail:
                score += 0.35
            else:
                score += 0.20
        elif target_tail and in_tail and target_tail == in_tail:
            score += 0.25

    # Pattern B: Debit Card -> Credit Card Repayment
    elif (out_kind == "bank_debit" or out_src in ("wechat", "alipay")) and in_kind == "bank_credit":
        score += 0.20
        features["pattern"] = "credit_card_repay"
        if any(w in in_desc for w in ("还款", "转帐", "转账", "跨行还款", "自动还款")):
            score += 0.15
        if any(w in out_desc for w in ("信用卡还款", "还款", "转账", "转帐")):
            score += 0.15
        if holder_name and (holder_name in in_desc or holder_name in out_desc):
            score += 0.15

    # Pattern C: Inter-Bank Debit -> Debit Transfer
    elif (out_kind == "bank_debit" or out_kind is None) and (in_kind == "bank_debit" or in_kind is None):
        score += 0.20
        features["pattern"] = "interbank_self"
        if any(w in out_desc or w in in_desc for w in ("跨行转账", "行内转账", "转账", "转帐", "手机银行")):
            score += 0.15
        if holder_name and (holder_name in in_desc or holder_name in out_desc):
            score += 0.20

        # Check if card tail cross-referenced in transaction text
        if in_txn.get("acc_tail") and in_txn.get("acc_tail") in out_desc:
            score += 0.20
        elif out_txn.get("acc_tail") and out_txn.get("acc_tail") in in_desc:
            score += 0.20

    final_score = min(1.0, max(0.0, round(score, 4)))
    features["score"] = final_score
    return final_score, features


def pair_internal_transfers(
    conn: sqlite3.Connection,
    batch_id: Optional[int] = None,
    time_window_hours: int = 48,
    auto_threshold: float = 0.85,
    review_threshold: float = 0.55,
    holder_name: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Identify and pair internal transfers in SQLite database.
    
    Returns list of matched candidate pairings.
    """
    # 1. Fetch blocklisted pairs
    block_rows = conn.execute("SELECT a_id, b_id FROM match_blocklist WHERE kind='transfer'").fetchall()
    blocked_pairs: Set[Tuple[int, int]] = set()
    for br in block_rows:
        blocked_pairs.add((br["a_id"], br["b_id"]))
        blocked_pairs.add((br["b_id"], br["a_id"]))

    # 2. Query active account holder name dynamically (accounts -> settings -> None)
    if holder_name is None:
        holder_row = conn.execute(
            "SELECT holder FROM accounts WHERE holder IS NOT NULL AND trim(holder) != '' LIMIT 1"
        ).fetchone()
        if holder_row and holder_row["holder"]:
            holder_name = holder_row["holder"].strip()

        if not holder_name:
            try:
                setting_row = conn.execute(
                    "SELECT value FROM settings WHERE key IN ('cardholder_name', 'holder_name', 'user_name', 'owner_name') LIMIT 1"
                ).fetchone()
                if setting_row and setting_row["value"]:
                    raw_val = setting_row["value"]
                    try:
                        parsed = json.loads(raw_val)
                        if isinstance(parsed, str) and parsed.strip():
                            holder_name = parsed.strip()
                        elif isinstance(parsed, dict) and parsed.get("name"):
                            holder_name = str(parsed["name"]).strip()
                    except Exception:
                        if isinstance(raw_val, str) and raw_val.strip():
                            holder_name = raw_val.strip()
            except Exception:
                pass

    # 3. Query Outflow candidates (money moving out)
    out_query = """
    SELECT t.id, t.source, t.txn_time, t.amount_cents, t.currency, t.direction, t.counterparty,
           t.description, t.status, t.account_id, t.time_precision,
           a.kind AS acc_kind, a.bank AS acc_bank, a.tail AS acc_tail
    FROM transactions t
    LEFT JOIN accounts a ON t.account_id = a.id
    WHERE t.direction IN ('transfer', 'expense')
      AND t.duplicate_of IS NULL
      AND t.transfer_peer IS NULL
      AND t.refund_of IS NULL
      AND t.excluded = 0
      AND t.amount_cents > 0
    """
    out_records = conn.execute(out_query).fetchall()

    # 4. Query Inflow candidates (money moving in)
    in_query = """
    SELECT t.id, t.source, t.txn_time, t.amount_cents, t.currency, t.direction, t.counterparty,
           t.description, t.status, t.account_id, t.time_precision,
           a.kind AS acc_kind, a.bank AS acc_bank, a.tail AS acc_tail
    FROM transactions t
    LEFT JOIN accounts a ON t.account_id = a.id
    WHERE t.direction IN ('transfer', 'income')
      AND t.duplicate_of IS NULL
      AND t.transfer_peer IS NULL
      AND t.refund_of IS NULL
      AND t.excluded = 0
      AND t.amount_cents > 0
    """
    in_records = conn.execute(in_query).fetchall()

    # 5. Generate candidate pairings
    candidates = []
    max_delta_sec = time_window_hours * 3600

    for o in out_records:
        o_id = o["id"]
        o_amt = o["amount_cents"]
        o_cur = o["currency"]
        o_time = parse_iso_datetime(o["txn_time"])
        if o_time is None:
            continue

        for i in in_records:
            i_id = i["id"]
            if o_id == i_id:
                continue
            if (o_id, i_id) in blocked_pairs:
                continue
            if o_amt != i["amount_cents"] or o_cur != i["currency"]:
                continue
            if o["account_id"] and i["account_id"] and o["account_id"] == i["account_id"]:
                continue

            i_time = parse_iso_datetime(i["txn_time"])
            if i_time is None:
                continue
            delta_sec = (i_time - o_time).total_seconds()

            o_prec = o["time_precision"] if "time_precision" in o.keys() else "second"
            i_prec = i["time_precision"] if "time_precision" in i.keys() else "second"
            is_date_only = (
                o_prec == "day"
                or i_prec == "day"
                or (o_time.time() == datetime.min.time() and i_time.time() == datetime.min.time())
            )

            # Enforce temporal causality:
            # - When not date-only, allow -600s clock skew up to max_delta_sec (48h)
            # - When date-only, allow symmetric window up to max_delta_sec
            if not is_date_only:
                if not (-600 <= delta_sec <= max_delta_sec):
                    continue
            else:
                if abs(delta_sec) > max_delta_sec:
                    continue

            score, features = compute_transfer_score(
                dict(o), dict(i), holder_name=holder_name, max_delta_sec=max_delta_sec
            )
            if score >= review_threshold:
                features["orig_dir_out"] = o["direction"]
                features["orig_dir_in"] = i["direction"]
                candidates.append({
                    "out_id": o_id,
                    "in_id": i_id,
                    "score": score,
                    "time_diff": abs(delta_sec),
                    "features": features,
                })

    # 6. Greedy 1-to-1 allocation
    candidates.sort(key=lambda c: (-c["score"], c["time_diff"], c["out_id"], c["in_id"]))

    matched_out: Set[int] = set()
    matched_in: Set[int] = set()
    final_matches: List[Dict[str, Any]] = []

    for c in candidates:
        if c["out_id"] in matched_out or c["in_id"] in matched_in:
            continue

        matched_out.add(c["out_id"])
        matched_in.add(c["in_id"])

        status = "auto" if c["score"] >= auto_threshold else "pending"
        c["status"] = status
        final_matches.append(c)

        features_json = json.dumps(c["features"], ensure_ascii=False)

        existing = conn.execute(
            "SELECT id, status FROM match_links WHERE kind = 'transfer' AND primary_id = ? AND secondary_id = ?",
            (c["out_id"], c["in_id"]),
        ).fetchone()

        if existing:
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
            conn.execute(
                """
                INSERT INTO match_links (kind, primary_id, secondary_id, score, features_json, status, created_at, updated_at)
                VALUES ('transfer', ?, ?, ?, ?, ?, datetime('now', 'localtime'), datetime('now', 'localtime'))
                """,
                (c["out_id"], c["in_id"], c["score"], features_json, status),
            )

        if status == "auto":
            # Bilateral transfer peer assignment & direction update
            conn.execute(
                "UPDATE transactions SET transfer_peer = ?, direction = 'transfer' WHERE id = ?",
                (c["in_id"], c["out_id"]),
            )
            conn.execute(
                "UPDATE transactions SET transfer_peer = ?, direction = 'transfer' WHERE id = ?",
                (c["out_id"], c["in_id"]),
            )

    return final_matches
