"""Campaigns router — 行銷自動化活動管理 + 手動觸發（PHASE 4-1）。

受認證 + 租戶隔離 + rate limit + require_feature(MARKETING_AUTO)。
"""

from __future__ import annotations

import datetime
import json

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from saas_mvp.deps import get_current_user, get_db, require_rate_limit
from saas_mvp.line_client import LinePushClient, get_push_client
from saas_mvp.models.campaign import VALID_CAMPAIGN_TYPES, VALID_REWARD_TYPES, Campaign
from saas_mvp.models.user import User
from saas_mvp.services import marketing as marketing_svc
from saas_mvp.services.features import MARKETING_AUTO, require_feature
from saas_mvp.services.tenants import tenant_query

router = APIRouter(
    prefix="/booking/campaigns",
    tags=["booking-campaigns"],
    dependencies=[Depends(require_rate_limit), Depends(require_feature(MARKETING_AUTO))],
)


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class CampaignCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    type: str = Field(pattern="^(birthday|welcome|spend|reactivation|broadcast)$")
    message_template: str = Field(min_length=1)
    schedule_at: datetime.datetime | None = None
    expires_at: datetime.datetime | None = None
    segment_json: dict | None = None
    reward_type: str | None = Field(default=None, pattern="^(coupon|points)$")
    reward_value: int | None = Field(default=None, ge=0)


class CampaignUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    message_template: str | None = Field(default=None, min_length=1)
    schedule_at: datetime.datetime | None = None
    expires_at: datetime.datetime | None = None
    segment_json: dict | None = None
    reward_type: str | None = Field(default=None, pattern="^(coupon|points)$")
    reward_value: int | None = Field(default=None, ge=0)
    is_active: bool | None = None


class CampaignResponse(BaseModel):
    id: int
    tenant_id: int
    name: str
    type: str
    status: str
    schedule_at: datetime.datetime | None
    expires_at: datetime.datetime | None
    segment_json: str | None
    reward_type: str | None
    reward_value: int | None
    message_template: str
    is_active: bool

    model_config = {"from_attributes": True}


class RunResponse(BaseModel):
    sent: int
    skipped: int


def _get_or_404(db: Session, tenant_id: int, campaign_id: int) -> Campaign:
    from fastapi import HTTPException

    c = (
        tenant_query(db, Campaign, tenant_id)
        .filter(Campaign.id == campaign_id)
        .first()
    )
    if c is None:
        raise HTTPException(status_code=404, detail="Campaign not found")
    return c


@router.post("/", response_model=CampaignResponse, status_code=status.HTTP_201_CREATED)
def create(
    body: CampaignCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> CampaignResponse:
    campaign = Campaign(
        tenant_id=current_user.tenant_id,
        name=body.name,
        type=body.type,
        message_template=body.message_template,
        schedule_at=body.schedule_at,
        expires_at=body.expires_at,
        segment_json=json.dumps(body.segment_json) if body.segment_json else None,
        reward_type=body.reward_type,
        reward_value=body.reward_value,
    )
    db.add(campaign)
    db.commit()
    db.refresh(campaign)
    return CampaignResponse.model_validate(campaign)


@router.get("/", response_model=list[CampaignResponse])
def list_all(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[CampaignResponse]:
    rows = (
        tenant_query(db, Campaign, current_user.tenant_id)
        .order_by(Campaign.id.desc())
        .all()
    )
    return [CampaignResponse.model_validate(c) for c in rows]


@router.get("/{campaign_id}", response_model=CampaignResponse)
def get_one(
    campaign_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> CampaignResponse:
    return CampaignResponse.model_validate(
        _get_or_404(db, current_user.tenant_id, campaign_id)
    )


@router.put("/{campaign_id}", response_model=CampaignResponse)
def update_one(
    campaign_id: int,
    body: CampaignUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> CampaignResponse:
    campaign = _get_or_404(db, current_user.tenant_id, campaign_id)
    if body.name is not None:
        campaign.name = body.name
    if body.message_template is not None:
        campaign.message_template = body.message_template
    if body.schedule_at is not None:
        campaign.schedule_at = body.schedule_at
    if body.expires_at is not None:
        campaign.expires_at = body.expires_at
    if body.segment_json is not None:
        campaign.segment_json = json.dumps(body.segment_json)
    if body.reward_type is not None:
        campaign.reward_type = body.reward_type
    if body.reward_value is not None:
        campaign.reward_value = body.reward_value
    if body.is_active is not None:
        campaign.is_active = body.is_active
    db.commit()
    db.refresh(campaign)
    return CampaignResponse.model_validate(campaign)


@router.delete(
    "/{campaign_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response
)
def delete_one(
    campaign_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Response:
    campaign = _get_or_404(db, current_user.tenant_id, campaign_id)
    campaign.is_active = False
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{campaign_id}/run", response_model=RunResponse)
def run(
    campaign_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    push_client: LinePushClient = Depends(get_push_client),
) -> RunResponse:
    """手動觸發一個活動（立即執行 claim + 獎勵 + 推播）。"""
    from saas_mvp.config import settings

    campaign = _get_or_404(db, current_user.tenant_id, campaign_id)
    result = marketing_svc.run_campaign(
        db,
        campaign=campaign,
        now=_utcnow(),
        cap=settings.marketing_max_per_run,
        push_client=push_client,
    )
    return RunResponse(**result)
