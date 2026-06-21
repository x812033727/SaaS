"""Booking customers router — 店家端顧客 CRM（唯讀 + 補欄位）。

顧客檔由 LINE 預約流程自動建立；此處提供 list/get/PATCH(phone,note)。
受認證 + 租戶隔離 + rate limit；不掛 require_quota。
"""

from __future__ import annotations

import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from saas_mvp.deps import get_current_user, get_db, require_rate_limit
from saas_mvp.models.point_transaction import PointTransaction
from saas_mvp.models.user import User
from saas_mvp.services import membership as membership_svc
from saas_mvp.services.customers import (
    get_customer,
    list_customers,
    update_customer,
)
from saas_mvp.services.tenants import tenant_query

router = APIRouter(
    prefix="/booking/customers",
    tags=["booking-customers"],
    dependencies=[Depends(require_rate_limit)],
)


# ─────────────────────────────── Schemas ─────────────────────────────────────

class CustomerUpdate(BaseModel):
    phone: str | None = Field(default=None, max_length=32)
    note: str | None = Field(default=None, max_length=2048)


class CustomerResponse(BaseModel):
    id: int
    tenant_id: int
    line_user_id: str
    display_name: str | None
    phone: str | None
    booking_count: int
    last_booked_at: datetime.datetime | None
    note: str | None
    points_balance: int
    tier: str

    model_config = {"from_attributes": True}


class PointsAdjust(BaseModel):
    delta: int = Field(description="正=加點、負=扣點")
    reason: str = Field(default="manual", max_length=64)


class PointTxResponse(BaseModel):
    id: int
    delta: int
    reason: str
    reservation_id: int | None
    created_at: datetime.datetime

    model_config = {"from_attributes": True}


# ─────────────────────────────── Endpoints ───────────────────────────────────

@router.get("/", response_model=list[CustomerResponse])
def list_all(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[CustomerResponse]:
    rows = list_customers(db, tenant_id=current_user.tenant_id)
    return [CustomerResponse.model_validate(c) for c in rows]


@router.get("/{customer_id}", response_model=CustomerResponse)
def get_one(
    customer_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> CustomerResponse:
    customer = get_customer(
        db, tenant_id=current_user.tenant_id, customer_id=customer_id
    )
    return CustomerResponse.model_validate(customer)


@router.patch("/{customer_id}", response_model=CustomerResponse)
def patch_one(
    customer_id: int,
    body: CustomerUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> CustomerResponse:
    customer = update_customer(
        db,
        tenant_id=current_user.tenant_id,
        customer_id=customer_id,
        phone=body.phone,
        note=body.note,
    )
    return CustomerResponse.model_validate(customer)


@router.get("/{customer_id}/points", response_model=list[PointTxResponse])
def points_ledger(
    customer_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[PointTxResponse]:
    get_customer(db, tenant_id=current_user.tenant_id, customer_id=customer_id)
    rows = (
        tenant_query(db, PointTransaction, current_user.tenant_id)
        .filter(PointTransaction.customer_id == customer_id)
        .order_by(PointTransaction.id.desc())
        .all()
    )
    return [PointTxResponse.model_validate(r) for r in rows]


@router.post("/{customer_id}/points", response_model=CustomerResponse)
def adjust_points(
    customer_id: int,
    body: PointsAdjust,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> CustomerResponse:
    """店家手動調整點數（正加負扣）；扣點不足回 409。"""
    customer = get_customer(
        db, tenant_id=current_user.tenant_id, customer_id=customer_id
    )
    if body.delta == 0:
        return CustomerResponse.model_validate(customer)
    if body.delta > 0:
        membership_svc.earn_points(
            db, tenant_id=current_user.tenant_id, customer=customer,
            delta=body.delta, reason=body.reason,
        )
    else:
        try:
            membership_svc.redeem_points(
                db, tenant_id=current_user.tenant_id, customer=customer,
                amount=-body.delta, reason=body.reason,
            )
        except membership_svc.InsufficientPoints:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="Insufficient points"
            )
    db.commit()
    db.refresh(customer)
    return CustomerResponse.model_validate(customer)
