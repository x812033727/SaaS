"""LINE auto-reply rule service.

All CRUD queries are scoped by tenant_id. Runtime matching is added separately;
this module owns persistence invariants for merchant-configured rules.
"""

from __future__ import annotations

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
