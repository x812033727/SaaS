"""LINE auto-reply rule service."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from saas_mvp.models.auto_reply_rule import (
    MATCH_TYPE_CONTAINS,
    MATCH_TYPE_EXACT,
    MATCH_TYPE_PREFIX,
    REPLY_TYPE_FLEX,
    REPLY_TYPE_TEXT,
    AutoReplyRule,
)
from saas_mvp.models.flex_menu import FlexMenu
from saas_mvp.services.tenants import tenant_query

_VALID_MATCH_TYPES = frozenset(
    {MATCH_TYPE_EXACT, MATCH_TYPE_PREFIX, MATCH_TYPE_CONTAINS}
)
_VALID_REPLY_TYPES = frozenset({REPLY_TYPE_TEXT, REPLY_TYPE_FLEX})
_MATCH_TYPE_RANK = {
    MATCH_TYPE_EXACT: 0,
    MATCH_TYPE_PREFIX: 1,
    MATCH_TYPE_CONTAINS: 2,
}
_UNSET = object()


def _rule_or_404(db: Session, tenant_id: int, rule_id: int) -> AutoReplyRule:
    rule = (
        tenant_query(db, AutoReplyRule, tenant_id)
        .filter(AutoReplyRule.id == rule_id)
        .first()
    )
    if rule is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Auto-reply rule not found",
        )
    return rule


def _owned_flex_menu_or_404(
    db: Session, tenant_id: int, flex_menu_id: int | None
) -> None:
    if flex_menu_id is None:
        return
    exists = (
        tenant_query(db, FlexMenu, tenant_id)
        .filter(FlexMenu.id == flex_menu_id)
        .first()
    )
    if exists is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Flex menu not found",
        )


def _clean_keyword(keyword: str) -> str:
    cleaned = keyword.strip()
    if not cleaned:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="keyword must not be blank",
        )
    return cleaned


def _validate_match_type(match_type: str) -> str:
    if match_type not in _VALID_MATCH_TYPES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid match_type: {match_type!r}",
        )
    return match_type


def _validate_reply_type(reply_type: str) -> str:
    if reply_type not in _VALID_REPLY_TYPES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid reply_type: {reply_type!r}",
        )
    return reply_type


def _normalize_payload(
    db: Session,
    *,
    tenant_id: int,
    reply_type: str,
    reply_text: str | None,
    flex_menu_id: int | None,
) -> tuple[str | None, int | None]:
    _validate_reply_type(reply_type)
    if reply_type == REPLY_TYPE_TEXT:
        text = (reply_text or "").strip()
        if not text:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="reply_text is required when reply_type is text",
            )
        return text, None

    if flex_menu_id is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="flex_menu_id is required when reply_type is flex",
        )
    _owned_flex_menu_or_404(db, tenant_id, flex_menu_id)
    return None, flex_menu_id


def create_rule(
    db: Session,
    *,
    tenant_id: int,
    keyword: str,
    match_type: str = MATCH_TYPE_CONTAINS,
    reply_type: str = REPLY_TYPE_TEXT,
    reply_text: str | None = None,
    flex_menu_id: int | None = None,
    priority: int = 0,
    is_active: bool = True,
) -> AutoReplyRule:
    cleaned_keyword = _clean_keyword(keyword)
    match_type = _validate_match_type(match_type)
    reply_text, flex_menu_id = _normalize_payload(
        db,
        tenant_id=tenant_id,
        reply_type=reply_type,
        reply_text=reply_text,
        flex_menu_id=flex_menu_id,
    )
    rule = AutoReplyRule(
        tenant_id=tenant_id,
        keyword=cleaned_keyword,
        match_type=match_type,
        reply_type=reply_type,
        reply_text=reply_text,
        flex_menu_id=flex_menu_id,
        priority=priority,
        is_active=is_active,
    )
    db.add(rule)
    db.commit()
    db.refresh(rule)
    return rule


def list_rules(
    db: Session, *, tenant_id: int, active_only: bool = False
) -> list[AutoReplyRule]:
    q = tenant_query(db, AutoReplyRule, tenant_id)
    if active_only:
        q = q.filter(AutoReplyRule.is_active.is_(True))
    return q.order_by(AutoReplyRule.priority, AutoReplyRule.id).all()


def get_rule(db: Session, *, tenant_id: int, rule_id: int) -> AutoReplyRule:
    return _rule_or_404(db, tenant_id, rule_id)


def update_rule(
    db: Session,
    *,
    tenant_id: int,
    rule_id: int,
    keyword: str | None = None,
    match_type: str | None = None,
    reply_type: str | None = None,
    reply_text: str | None | object = _UNSET,
    flex_menu_id: int | None | object = _UNSET,
    priority: int | None = None,
    is_active: bool | None = None,
) -> AutoReplyRule:
    rule = _rule_or_404(db, tenant_id, rule_id)

    next_reply_type = reply_type if reply_type is not None else rule.reply_type
    next_reply_text = rule.reply_text if reply_text is _UNSET else reply_text
    next_flex_menu_id = rule.flex_menu_id if flex_menu_id is _UNSET else flex_menu_id
    next_reply_text, next_flex_menu_id = _normalize_payload(
        db,
        tenant_id=tenant_id,
        reply_type=next_reply_type,
        reply_text=next_reply_text if isinstance(next_reply_text, str) else None,
        flex_menu_id=next_flex_menu_id if isinstance(next_flex_menu_id, int) else None,
    )

    if keyword is not None:
        rule.keyword = _clean_keyword(keyword)
    if match_type is not None:
        rule.match_type = _validate_match_type(match_type)
    rule.reply_type = next_reply_type
    rule.reply_text = next_reply_text
    rule.flex_menu_id = next_flex_menu_id
    if priority is not None:
        rule.priority = priority
    if is_active is not None:
        rule.is_active = is_active
    db.commit()
    db.refresh(rule)
    return rule


def delete_rule(db: Session, *, tenant_id: int, rule_id: int) -> None:
    rule = _rule_or_404(db, tenant_id, rule_id)
    db.delete(rule)
    db.commit()


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
    """Return the winning rule, or None when nothing matches."""

    if text is None:
        return None

    candidates = [rule for rule in rules if _is_match(rule, str(text))]
    if not candidates:
        return None
    return min(candidates, key=_sort_key)
