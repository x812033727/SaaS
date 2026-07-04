"""Auto-reply rule CRUD API.

JSON-only merchant configuration surface; no HTML admin page in this iteration.
"""

from __future__ import annotations

import datetime

from fastapi import APIRouter, Depends, Query, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from saas_mvp.deps import get_current_user, get_db, require_rate_limit
from saas_mvp.models.user import User
from saas_mvp.services import auto_reply as auto_reply_svc

router = APIRouter(
    prefix="/api/auto-reply-rules",
    tags=["auto-reply-rules"],
    dependencies=[Depends(require_rate_limit)],
)

_UPDATABLE_FIELDS = {
    "keyword",
    "match_type",
    "reply_type",
    "reply_text",
    "flex_menu_id",
    "priority",
    "is_active",
}


class AutoReplyRuleCreate(BaseModel):
    keyword: str = Field(max_length=255)
    match_type: str = Field(default="contains", max_length=16)
    reply_type: str = Field(default="text", max_length=16)
    reply_text: str | None = Field(default=None, max_length=4096)
    flex_menu_id: int | None = None
    priority: int = 0
    is_active: bool = True


class AutoReplyRuleUpdate(BaseModel):
    keyword: str | None = Field(default=None, max_length=255)
    match_type: str | None = Field(default=None, max_length=16)
    reply_type: str | None = Field(default=None, max_length=16)
    reply_text: str | None = Field(default=None, max_length=4096)
    flex_menu_id: int | None = None
    priority: int | None = None
    is_active: bool | None = None


class AutoReplyRuleResponse(BaseModel):
    id: int
    tenant_id: int
    keyword: str
    match_type: str
    reply_type: str
    reply_text: str | None
    flex_menu_id: int | None
    priority: int
    is_active: bool
    created_at: datetime.datetime
    updated_at: datetime.datetime

    model_config = {"from_attributes": True}


@router.post(
    "/", response_model=AutoReplyRuleResponse, status_code=status.HTTP_201_CREATED
)
def create_rule(
    body: AutoReplyRuleCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> AutoReplyRuleResponse:
    rule = auto_reply_svc.create_rule(
        db,
        tenant_id=current_user.tenant_id,
        keyword=body.keyword,
        match_type=body.match_type,
        reply_type=body.reply_type,
        reply_text=body.reply_text,
        flex_menu_id=body.flex_menu_id,
        priority=body.priority,
        is_active=body.is_active,
    )
    return AutoReplyRuleResponse.model_validate(rule)


@router.get("/", response_model=list[AutoReplyRuleResponse])
def list_rules(
    active_only: bool = Query(default=False),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[AutoReplyRuleResponse]:
    rows = auto_reply_svc.list_rules(
        db, tenant_id=current_user.tenant_id, active_only=active_only
    )
    return [AutoReplyRuleResponse.model_validate(row) for row in rows]


@router.get("/{rule_id}", response_model=AutoReplyRuleResponse)
def get_rule(
    rule_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> AutoReplyRuleResponse:
    rule = auto_reply_svc.get_rule(
        db, tenant_id=current_user.tenant_id, rule_id=rule_id
    )
    return AutoReplyRuleResponse.model_validate(rule)


@router.put("/{rule_id}", response_model=AutoReplyRuleResponse)
def update_rule(
    rule_id: int,
    body: AutoReplyRuleUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> AutoReplyRuleResponse:
    updates = {
        name: getattr(body, name)
        for name in body.model_fields_set
        if name in _UPDATABLE_FIELDS
    }
    rule = auto_reply_svc.update_rule(
        db,
        tenant_id=current_user.tenant_id,
        rule_id=rule_id,
        **updates,
    )
    return AutoReplyRuleResponse.model_validate(rule)


@router.delete(
    "/{rule_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response
)
def delete_rule(
    rule_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Response:
    auto_reply_svc.delete_rule(
        db, tenant_id=current_user.tenant_id, rule_id=rule_id
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
