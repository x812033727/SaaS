"""Pure matching logic for LINE auto-reply rules."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from saas_mvp.models.auto_reply_rule import (
    MATCH_TYPE_CONTAINS,
    MATCH_TYPE_EXACT,
    MATCH_TYPE_PREFIX,
)

_MATCH_TYPE_RANK = {
    MATCH_TYPE_EXACT: 0,
    MATCH_TYPE_PREFIX: 1,
    MATCH_TYPE_CONTAINS: 2,
}


def _attr(rule: Any, name: str, default: Any = None) -> Any:
    return getattr(rule, name, default)


def _priority(rule: Any) -> int:
    value = _attr(rule, "priority", 0)
    return 0 if value is None else int(value)


def _rule_id(rule: Any) -> int:
    value = _attr(rule, "id", None)
    return 2**63 - 1 if value is None else int(value)


def _is_match(rule: Any, text: str) -> bool:
    if _attr(rule, "is_active", True) is False:
        return False

    keyword = _attr(rule, "keyword", None)
    if keyword is None:
        return False

    keyword = str(keyword)
    if not keyword.strip():
        return False

    match_type = _attr(rule, "match_type", MATCH_TYPE_CONTAINS)
    if match_type is None:
        match_type = MATCH_TYPE_CONTAINS

    if match_type == MATCH_TYPE_EXACT:
        return text == keyword

    text_folded = text.lower()
    keyword_folded = keyword.lower()
    if match_type == MATCH_TYPE_PREFIX:
        return text_folded.startswith(keyword_folded)
    if match_type == MATCH_TYPE_CONTAINS:
        return keyword_folded in text_folded
    return False


def _sort_key(rule: Any) -> tuple[int, int, int]:
    match_type = _attr(rule, "match_type", MATCH_TYPE_CONTAINS)
    if match_type is None:
        match_type = MATCH_TYPE_CONTAINS
    return (_MATCH_TYPE_RANK[match_type], _priority(rule), _rule_id(rule))


def match(rules: Iterable[Any], text: str) -> Any | None:
    """Return the single winning auto-reply rule, or None when nothing matches.

    Ordering is deterministic: exact > prefix > contains, then lower priority,
    then lower id. Exact matching is case-sensitive; prefix and contains are
    case-insensitive.
    """

    if text is None:
        return None

    candidates = [rule for rule in rules if _is_match(rule, str(text))]
    if not candidates:
        return None
    return min(candidates, key=_sort_key)
