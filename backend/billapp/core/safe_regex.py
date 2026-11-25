"""ReDoS-hardened regular expression compilation and bounded-timeout execution.

Uses the third-party ``regex`` engine with strict per-match execution timeouts,
combined with static AST-like nesting analysis and bounded LRU caching so
user-supplied rules and bank profile YAML patterns cannot cause catastrophic
backtracking or unbounded memory growth.
"""

from __future__ import annotations

from functools import lru_cache
import re
from typing import Optional

import regex as _regex

DEFAULT_MAX_PATTERN_LEN = 200
PROFILE_MAX_PATTERN_LEN = 300
DEFAULT_REGEX_TIMEOUT = 0.1

# Backreferences enable NP-complete matching regardless of quantifier structure.
_BACKREF_RE = re.compile(r"(?<!\\)(?:\\\\)*(?:\\[1-9]|\\g<|\\k<|\(\?P=)")

# Immediately adjacent overlapping unbounded wildcards/classes without separator,
# e.g. \w+\w+, \d+\d+, .*.*, .+.+
_ADJACENT_OVERLAP_RE = re.compile(
    r"(?<!\\)(?:\\\\)*(?:\\w|\\d|\\s|\.|\[[^\]]+\])[+*](?:\\w|\\d|\\s|\.|\[[^\]]+\])[+*]"
)


def _read_quantifier_repeat_bound(pattern: str, pos: int) -> tuple[bool, int]:
    """Inspect a quantifier starting at ``pos`` and return (is_multi_repeat, next_pos).

    ``is_multi_repeat`` is True for ``*``, ``+``, and ``{m,n}`` where upper bound
    is unbounded or > 1.
    """
    n = len(pattern)
    if pos >= n:
        return False, pos

    ch = pattern[pos]
    if ch in ("*", "+"):
        nxt = pos + 1
        if nxt < n and pattern[nxt] in ("?", "+"):
            nxt += 1
        return True, nxt

    if ch == "?":
        nxt = pos + 1
        if nxt < n and pattern[nxt] in ("?", "+"):
            nxt += 1
        return False, nxt

    if ch == "{":
        close = pattern.find("}", pos + 1)
        if close != -1:
            body = pattern[pos + 1 : close]
            m = re.fullmatch(r"(\d*)(?:,(\d*))?", body)
            if m and (m.group(1) or m.group(2)):
                nxt = close + 1
                if nxt < n and pattern[nxt] in ("?", "+"):
                    nxt += 1
                lo = int(m.group(1)) if m.group(1) else 0
                if "," in body:
                    hi_str = m.group(2)
                    # Unbounded {m,} or {m,n} with n > 1
                    is_multi = (hi_str == "" or hi_str is None) or int(hi_str) > 1
                else:
                    is_multi = lo > 1
                return is_multi, nxt

    return False, pos


def _has_nested_quantifiers(pattern: str) -> bool:
    """Scan arbitrary group nesting depth to detect nested quantifiers or repeated alternations."""
    # Each stack frame: [has_inner_quantifier, has_alternation]
    stack: list[list[bool]] = [[False, False]]
    in_char_class = False
    i = 0
    n = len(pattern)

    while i < n:
        ch = pattern[i]

        if ch == "\\":
            i += 2
            continue

        if in_char_class:
            if ch == "]":
                in_char_class = False
            i += 1
            continue

        if ch == "[":
            in_char_class = True
            # Handle leading ^ and ] inside character class
            if i + 1 < n and pattern[i + 1] == "^":
                i += 1
            if i + 1 < n and pattern[i + 1] == "]":
                i += 1
            i += 1
            continue

        if ch == "(":
            # Push a new group frame; skip group header tokens like (?:, (?P<name>, (?i)
            stack.append([False, False])
            if i + 1 < n and pattern[i + 1] == "?":
                # Skip inline flag group (?imsxauL) completely if closed immediately
                j = i + 2
                if j < n and pattern[j] == "P" and j + 1 < n and pattern[j + 1] == "<":
                    end_name = pattern.find(">", j + 2)
                    if end_name != -1:
                        i = end_name + 1
                        continue
            i += 1
            continue

        if ch == "|":
            stack[-1][1] = True
            i += 1
            continue

        if ch == ")":
            if len(stack) > 1:
                inner_has_q, inner_has_alt = stack.pop()
                is_multi, next_pos = _read_quantifier_repeat_bound(pattern, i + 1)
                if is_multi and (inner_has_q or inner_has_alt):
                    return True
                # Propagate quantifier and alternation existence upward so ((a)+)+ and ((a|aa))+ are caught
                if inner_has_q or is_multi:
                    stack[-1][0] = True
                if inner_has_alt:
                    stack[-1][1] = True
                i = next_pos
                continue
            i += 1
            continue

        # Detect quantifiers applied to atoms inside the current group
        if ch in ("*", "+", "{"):
            is_multi, next_pos = _read_quantifier_repeat_bound(pattern, i)
            if is_multi:
                stack[-1][0] = True
                i = next_pos
                continue

        i += 1

    return False


def validate_regex_pattern(
    pattern: str,
    max_len: int = DEFAULT_MAX_PATTERN_LEN,
) -> str:
    """Validate that a user-supplied regex pattern is bounded and ReDoS-safe."""
    if not isinstance(pattern, str) or not pattern:
        raise ValueError("Regex pattern cannot be empty")
    if len(pattern) > max_len:
        raise ValueError(f"Regex pattern exceeds maximum length of {max_len} characters")
    if _BACKREF_RE.search(pattern):
        raise ValueError("Regex pattern contains disallowed backreferences (ReDoS risk)")
    if _has_nested_quantifiers(pattern):
        raise ValueError(
            "Regex pattern contains nested quantifiers or repeated alternations (ReDoS risk)"
        )
    if _ADJACENT_OVERLAP_RE.search(pattern):
        raise ValueError(
            "Regex pattern contains adjacent overlapping unbounded quantifiers (ReDoS risk)"
        )
    try:
        _regex.compile(pattern)
    except _regex.error as exc:
        raise ValueError(f"Invalid regex syntax: {exc}") from exc
    return pattern


@lru_cache(maxsize=512)
def compile_safe_regex(
    pattern: str,
    flags: int = 0,
    max_len: int = DEFAULT_MAX_PATTERN_LEN,
) -> _regex.Pattern:
    """Validate and compile a regex with bounded LRU cache (maxsize=512)."""
    validate_regex_pattern(pattern, max_len=max_len)
    try:
        return _regex.compile(pattern, flags)
    except _regex.error as exc:
        raise ValueError(f"Invalid regex syntax: {exc}") from exc


def safe_search(
    pattern: str | _regex.Pattern,
    text: str,
    flags: int = 0,
    timeout: float = DEFAULT_REGEX_TIMEOUT,
    max_len: int = DEFAULT_MAX_PATTERN_LEN,
) -> Optional[_regex.Match]:
    """Execute regex search with strict timeout protection; returns None on timeout."""
    compiled = (
        pattern
        if isinstance(pattern, _regex.Pattern)
        else compile_safe_regex(pattern, flags=flags, max_len=max_len)
    )
    try:
        return compiled.search(text, timeout=timeout)
    except TimeoutError:
        return None


def safe_match(
    pattern: str | _regex.Pattern,
    text: str,
    flags: int = 0,
    timeout: float = DEFAULT_REGEX_TIMEOUT,
    max_len: int = DEFAULT_MAX_PATTERN_LEN,
) -> Optional[_regex.Match]:
    """Execute regex match with strict timeout protection; returns None on timeout."""
    compiled = (
        pattern
        if isinstance(pattern, _regex.Pattern)
        else compile_safe_regex(pattern, flags=flags, max_len=max_len)
    )
    try:
        return compiled.match(text, timeout=timeout)
    except TimeoutError:
        return None
