"""Coupons router — 店家端優惠券管理（CRUD + 核銷紀錄）。

受認證 + 租戶隔離 + rate limit；不掛 require_quota（沿用 booking 決策）。
"""

from __future__ import annotations

import datetime

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from saas_mvp.deps import get_current_user, get_db, require_rate_limit
from saas_mvp.models.user import User
from saas_mvp.services.features import COUPON_SYSTEM, require_feature
from saas_mvp.services.coupons import (
    create_coupon,
    deactivate_coupon,
    get_coupon,
    list_coupons,
    list_redemptions,
    update_coupon,
)

router = APIRouter(
    prefix="/booking/coupons",
    tags=["booking-coupons"],
    dependencies=[Depends(require_rate_limit), Depends(require_feature(COUPON_SYSTEM))],
)


class CouponCreate(BaseModel):
    code: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=128)
    discount_type: str = Field(pattern="^(percent|amount|gift|upsell)$")
    discount_value: int = Field(ge=0)
    min_spend_cents: int = Field(default=0, ge=0)
    max_redemptions: int | None = Field(default=None, ge=1)
    active_from: datetime.datetime | None = None
    active_until: datetime.datetime | None = None


class CouponUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    max_redemptions: int | None = Field(default=None, ge=1)
    active_until: datetime.datetime | None = None
    is_active: bool | None = None


class CouponResponse(BaseModel):
    id: int
    tenant_id: int
    code: str
    name: str
    discount_type: str
    discount_value: int
    min_spend_cents: int
    max_redemptions: int | None
    redeemed_count: int
    active_from: datetime.datetime | None
    active_until: datetime.datetime | None
    is_active: bool

    model_config = {"from_attributes": True}


class RedemptionResponse(BaseModel):
    id: int
    coupon_id: int
    line_user_id: str
    customer_id: int | None
    reservation_id: int | None
    order_id: int | None = None
    redeemed_at: datetime.datetime

    model_config = {"from_attributes": True}


@router.post("/", response_model=CouponResponse, status_code=status.HTTP_201_CREATED)
def create(
    body: CouponCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> CouponResponse:
    coupon = create_coupon(
        db,
        tenant_id=current_user.tenant_id,
        code=body.code,
        name=body.name,
        discount_type=body.discount_type,
        discount_value=body.discount_value,
        min_spend_cents=body.min_spend_cents,
        max_redemptions=body.max_redemptions,
        active_from=body.active_from,
        active_until=body.active_until,
    )
    return CouponResponse.model_validate(coupon)


@router.get("/", response_model=list[CouponResponse])
def list_all(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[CouponResponse]:
    return [
        CouponResponse.model_validate(c)
        for c in list_coupons(db, tenant_id=current_user.tenant_id)
    ]


@router.get("/{coupon_id}", response_model=CouponResponse)
def get_one(
    coupon_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> CouponResponse:
    return CouponResponse.model_validate(
        get_coupon(db, tenant_id=current_user.tenant_id, coupon_id=coupon_id)
    )


@router.put("/{coupon_id}", response_model=CouponResponse)
def update_one(
    coupon_id: int,
    body: CouponUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> CouponResponse:
    coupon = update_coupon(
        db,
        tenant_id=current_user.tenant_id,
        coupon_id=coupon_id,
        name=body.name,
        max_redemptions=body.max_redemptions,
        active_until=body.active_until,
        is_active=body.is_active,
    )
    return CouponResponse.model_validate(coupon)


@router.delete(
    "/{coupon_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response
)
def delete_one(
    coupon_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Response:
    deactivate_coupon(db, tenant_id=current_user.tenant_id, coupon_id=coupon_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{coupon_id}/redemptions", response_model=list[RedemptionResponse])
def redemptions(
    coupon_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[RedemptionResponse]:
    return [
        RedemptionResponse.model_validate(r)
        for r in list_redemptions(
            db, tenant_id=current_user.tenant_id, coupon_id=coupon_id
        )
    ]
