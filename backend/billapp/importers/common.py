"""Common utilities for date/time parsing, channel prefix extraction, and encoding detection."""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Optional, Tuple, Union

CHANNEL_PREFIX_MAP = [
    (re.compile(r"^支付宝[-_—]"), "alipay"),
    (re.compile(r"^财付通[-_—]"), "tenpay"),
    (re.compile(r"^(网银在线|京东支付)[-_—]"), "jdpay"),
    (re.compile(r"^美团支付[-_—]"), "meituan"),
    (re.compile(r"^云闪付[-_—]"), "unionpay"),
]

FUNDING_CHANNEL_REGEX = re.compile(
    r"^(?P<bank>[\u4e00-\u9fa5A-Za-z]+?)\s*(?P<kind>信用卡|贷记卡|储蓄卡|借记卡)?\s*[（\(](?P<tail>\d{4})[）\)]"
)

MERCHANT_SUFFIX_REGEX = re.compile(
    r"([（\(]中国[）\)]|有限公司|有限责任公司|股份有限公司|分公司|REDEMPTION|\s+分店)$"
)


def extract_channel_and_normalize(
    raw_name: Optional[str],
) -> Tuple[Optional[str], Optional[str]]:
    """Extract payment channel (alipay/tenpay/etc.) from remark/name and normalize merchant.

    Returns:
        (channel, normalized_merchant_name)
    """
    if not raw_name:
        return None, None

    cleaned = raw_name.strip()
    detected_channel: Optional[str] = None

    for pattern, channel in CHANNEL_PREFIX_MAP:
        if pattern.search(cleaned):
            detected_channel = channel
            cleaned = pattern.sub("", cleaned).strip()
            break

    # Strip company suffixes
    normalized = MERCHANT_SUFFIX_REGEX.sub("", cleaned).strip()
    return detected_channel, normalized or cleaned


def parse_datetime(raw_dt: Union[str, datetime, date, None]) -> Tuple[str, str]:
    """Parse various datetime formats to ISO string 'YYYY-MM-DD HH:MM:SS' or 'YYYY-MM-DD'.

    Returns:
        (iso_string, time_precision) where precision is 'second', 'minute', or 'day'.
    """
    if raw_dt is None:
        return "", "day"

    if isinstance(raw_dt, datetime):
        return raw_dt.strftime("%Y-%m-%d %H:%M:%S"), "second"

    if isinstance(raw_dt, date):
        return raw_dt.strftime("%Y-%m-%d"), "day"

    dt_str = str(raw_dt).strip().replace("/", "-")
    # Clean possible tab / brackets
    dt_str = re.sub(r"[\[\]\t]", "", dt_str).strip()

    formats = [
        ("%Y-%m-%d %H:%M:%S", "second"),
        ("%Y-%m-%d %H:%M", "minute"),
        ("%Y-%m-%d", "day"),
        ("%Y%m%d %H:%M:%S", "second"),
        ("%Y%m%d%H%M%S", "second"),
        ("%Y%m%d", "day"),
    ]

    for fmt, precision in formats:
        try:
            dt = datetime.strptime(dt_str, fmt)
            if precision in ("second", "minute"):
                return dt.strftime("%Y-%m-%d %H:%M:%S"), precision
            return dt.strftime("%Y-%m-%d"), "day"
        except ValueError:
            continue

    return dt_str, "day"


def parse_iso_datetime(value: Optional[str]) -> Optional[datetime]:
    """Safely parse ISO datetime string without raising on malformed dates."""
    if not value:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        pass
    # Fallback for common bank date formats if raw string slipped through
    normalized, _ = parse_datetime(raw)
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


_CURRENCY_ALIAS_MAP: dict[str, str] = {
    "人民币": "CNY",
    "人民币元": "CNY",
    "RMB": "CNY",
    "CNY": "CNY",
    "CNH": "CNY",
    "美元": "USD",
    "美金": "USD",
    "美圆": "USD",
    "USD": "USD",
    "欧元": "EUR",
    "EUR": "EUR",
    "港币": "HKD",
    "港元": "HKD",
    "HKD": "HKD",
    "日元": "JPY",
    "日圆": "JPY",
    "JPY": "JPY",
    "英镑": "GBP",
    "GBP": "GBP",
    "澳大利亚元": "AUD",
    "澳元": "AUD",
    "AUD": "AUD",
    "加拿大元": "CAD",
    "加元": "CAD",
    "CAD": "CAD",
    "新加坡元": "SGD",
    "新币": "SGD",
    "SGD": "SGD",
    "瑞士法郎": "CHF",
    "瑞郎": "CHF",
    "CHF": "CHF",
    "韩元": "KRW",
    "韩国元": "KRW",
    "KRW": "KRW",
    "泰铢": "THB",
    "THB": "THB",
    "新台币": "TWD",
    "台币": "TWD",
    "TWD": "TWD",
    "新西兰元": "NZD",
    "纽元": "NZD",
    "NZD": "NZD",
    "澳门元": "MOP",
    "MOP": "MOP",
    "林吉特": "MYR",
    "MYR": "MYR",
    "卢布": "RUB",
    "RUB": "RUB",
}

_ISO_CURRENCY_RE = re.compile(r"\b([A-Z]{3})\b")


def normalize_currency(raw: Optional[str], default: str = "CNY") -> str:
    """Normalize Chinese or ISO currency names into standard 3-letter ISO-4217 codes."""
    if not raw:
        return default
    cleaned = str(raw).strip()
    if not cleaned:
        return default

    upper = cleaned.upper()
    if upper in _CURRENCY_ALIAS_MAP:
        return _CURRENCY_ALIAS_MAP[upper]
    if cleaned in _CURRENCY_ALIAS_MAP:
        return _CURRENCY_ALIAS_MAP[cleaned]

    for alias, iso in _CURRENCY_ALIAS_MAP.items():
        if alias in cleaned or alias in upper:
            return iso

    m = _ISO_CURRENCY_RE.search(upper)
    if m:
        return m.group(1)

    return default


def parse_funding_channel(cell: Optional[str]) -> Optional[Tuple[str, str, str]]:
    """Parse funding channel from strings like '工商银行信用卡(8801)&工商银行天天减'
    or '余额宝&工商银行储蓄卡(8801)'.

    Returns:
        (bank, tail, kind) where kind is 'bank_credit' or 'bank_debit', or None if not matched.
    """
    if not cell:
        return None

    parts = [p.strip() for p in str(cell).split("&") if p.strip()]
    for part in parts:
        m = FUNDING_CHANNEL_REGEX.match(part)
        if m:
            bank = m.group("bank")
            tail = m.group("tail")
            kind_str = m.group("kind")
            kind = "bank_credit" if (kind_str in ("信用卡", "贷记卡") or bank == "Visa") else "bank_debit"
            return bank, tail, kind

    return None


def sanitize_order_id(raw_order_id: Optional[str]) -> Optional[str]:
    """Strip tabs/whitespace and return None if empty or matches placeholder tokens."""
    if not raw_order_id:
        return None
    cleaned = str(raw_order_id).replace("\t", "").strip()
    if not cleaned or cleaned.lower() in ("/", "-", "--", "---", "——", "－", "无", "null", "none", "nan", "nil"):
        return None
    return cleaned


def detect_encoding(raw_bytes: bytes) -> str:
    """Detect encoding for Chinese CSV/text files (UTF-8, UTF-8-SIG, GBK, CP936)."""
    if raw_bytes.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"

    try:
        raw_bytes.decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError:
        pass

    try:
        raw_bytes.decode("gbk")
        return "gbk"
    except UnicodeDecodeError:
        return "utf-8"
