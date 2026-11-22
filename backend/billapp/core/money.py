"""Core money and currency utilities for integer-cents financial precision."""

from __future__ import annotations

import math
import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Union

CURRENCY_SYMBOLS: dict[str, str] = {
    "CNY": "¥",
    "USD": "$",
    "EUR": "€",
    "HKD": "HK$",
    "GBP": "£",
    "JPY": "JP¥",
}

_CLEAN_AMOUNT_PATTERN = re.compile(r"[^\d.\-+]")
_CURRENCY_STRIP_PATTERN = re.compile(r"[\s¥￥$€£,，元块]")
_SCI_DECIMAL_PATTERN = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$")


def to_cents(value: Union[str, int, float, Decimal, None]) -> int:
    """Convert arbitrary currency representation to exact integer cents.

    Handles strings with currency symbols ('¥123.45', '$14.97'), commas ('1,029.80'),
    scientific notation ('1e3' -> 100000), signed floats, and Decimals.
    Rejects booleans and non-finite floats (NaN/Infinity). Guarantees no
    floating-point rounding errors by using Decimal with ROUND_HALF_UP.

    Examples:
        to_cents("123.45") -> 12345
        to_cents("-29.80") -> -2980
        to_cents("1,029.80") -> 102980
        to_cents(None) -> 0
    """
    if value is None:
        return 0
    if isinstance(value, bool):
        raise ValueError(f"Invalid monetary value (boolean not allowed): {value!r}")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"Invalid non-finite monetary value: {value!r}")
        dec = Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return int(dec * 100)
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError(f"Invalid non-finite monetary value: {value!r}")
        d = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return int(d * 100)

    val_str = str(value).strip()
    if not val_str:
        return 0

    # First check direct numeric / scientific notation after stripping currency markers & commas
    sci_candidate = _CURRENCY_STRIP_PATTERN.sub("", val_str)
    if sci_candidate and _SCI_DECIMAL_PATTERN.match(sci_candidate):
        try:
            dec = Decimal(sci_candidate)
            if not dec.is_finite():
                raise ValueError(f"Invalid non-finite monetary value: {value!r}")
            dec = dec.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            return int(dec * 100)
        except InvalidOperation as e:
            raise ValueError(f"Invalid monetary value: {value!r}") from e

    # Remove currency symbols, commas, spaces, '元', labels
    cleaned = _CLEAN_AMOUNT_PATTERN.sub("", val_str)
    if not cleaned or cleaned in ("-", "+", "."):
        return 0

    try:
        dec = Decimal(cleaned)
        if not dec.is_finite():
            raise ValueError(f"Invalid non-finite monetary value: {value!r}")
        dec = dec.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return int(dec * 100)
    except Exception as e:
        raise ValueError(f"Invalid monetary value: {value!r}") from e


def to_display(cents: int) -> str:
    """Format integer cents as standard 2-decimal string.

    Examples:
        to_display(12345) -> "123.45"
        to_display(-2980) -> "-29.80"
        to_display(0) -> "0.00"
    """
    sign = "-" if cents < 0 else ""
    abs_cents = abs(cents)
    dollars = abs_cents // 100
    rem = abs_cents % 100
    return f"{sign}{dollars}.{rem:02d}"


def format_money(cents: int, currency: str = "CNY", symbol: bool = True) -> str:
    """Format integer cents with optional currency symbol and standard 2-decimal places.

    Examples:
        format_money(12345, "CNY") -> "¥123.45"
        format_money(1497, "USD") -> "$14.97"
        format_money(12345, "CNY", symbol=False) -> "123.45 CNY"
    """
    display = to_display(cents)
    curr_upper = currency.upper()
    if symbol:
        sym = CURRENCY_SYMBOLS.get(curr_upper, curr_upper + " ")
        if display.startswith("-"):
            return f"-{sym}{display[1:]}"
        return f"{sym}{display}"
    return f"{display} {curr_upper}"


def parse_signed_amount(raw_val: Any) -> tuple[int, str]:
    """Parse a signed amount value into non-negative cents and direction.

    Returns:
        (abs_cents, direction) where direction is 'expense' (negative) or 'income' (positive).
    """
    cents = to_cents(raw_val)
    if cents < 0:
        return abs(cents), "expense"
    return cents, "income"
