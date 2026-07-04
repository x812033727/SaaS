"""Booking customers router — 店家端顧客 CRM（唯讀 + 補欄位）。

顧客檔由 LINE 預約流程自動建立；此處提供 list/get/PATCH(phone,note)。
受認證 + 租戶隔離 + rate limit；不掛 require_quota。
"""

from __future__ import annotations

import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from saas_mvp.deps import get_current_user, get_db, require_rate_limit
from saas_mvp.models.point_transaction import PointTransaction
from saas_mvp.models.user import User
from saas_mvp.services import membership as membership_svc
from saas_mvp.services import segments as segments_svc
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


class TagCreate(BaseModel):
    name: str = Field(max_length=64)
    color: str | None = Field(default=None, max_length=16)


class TagUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=64)
    color: str | None = Field(default=None, max_length=16)


class TagResponse(BaseModel):
    id: int
    tenant_id: int
    name: str
    color: str | None

    model_config = {"from_attributes": True}


# ─────────────────────────────── Endpoints ───────────────────────────────────

@router.get("/", response_model=list[CustomerResponse])
def list_all(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[CustomerResponse]:
    rows = list_customers(db, tenant_id=current_user.tenant_id)
    return [CustomerResponse.model_validate(c) for c in rows]


# ── 標籤 CRUD（須在 /{customer_id} 之前宣告，否則 "tags" 會被當成 customer_id） ──

@router.post("/tags", response_model=TagResponse, status_code=status.HTTP_201_CREATED)
def create_tag(
    body: TagCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> TagResponse:
    tag = segments_svc.create_tag(
        db, tenant_id=current_user.tenant_id, name=body.name, color=body.color
    )
    return TagResponse.model_validate(tag)


@router.get("/tags", response_model=list[TagResponse])
def list_tags(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[TagResponse]:
    rows = segments_svc.list_tags(db, tenant_id=current_user.tenant_id)
    return [TagResponse.model_validate(t) for t in rows]


@router.get("/tags/{tag_id}", response_model=TagResponse)
def get_tag(
    tag_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> TagResponse:
    tag = segments_svc.get_tag(
        db, tenant_id=current_user.tenant_id, tag_id=tag_id
    )
    return TagResponse.model_validate(tag)


@router.put("/tags/{tag_id}", response_model=TagResponse)
def update_tag(
    tag_id: int,
    body: TagUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> TagResponse:
    tag = segments_svc.update_tag(
        db,
        tenant_id=current_user.tenant_id,
        tag_id=tag_id,
        name=body.name,
        color=body.color,
    )
    return TagResponse.model_validate(tag)


@router.delete(
    "/tags/{tag_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
def delete_tag(
    tag_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Response:
    segments_svc.delete_tag(db, tenant_id=current_user.tenant_id, tag_id=tag_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ── 分眾查詢 ──────────────────────────────────────────────────────────────────

@router.get("/segment", response_model=list[CustomerResponse])
def segment(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    tag_ids: str | None = Query(default=None, description="逗號分隔的 tag id"),
    tier: str | None = Query(default=None),
    min_bookings: int | None = Query(default=None, ge=0),
    last_booked_before: datetime.date | None = Query(default=None),
    location_id: int | None = Query(default=None),
) -> list[CustomerResponse]:
    parsed_tag_ids: list[int] | None = None
    if tag_ids:
        try:
            parsed_tag_ids = [int(x) for x in tag_ids.split(",") if x.strip()]
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="tag_ids must be comma-separated integers",
            )
    before_dt: datetime.datetime | None = None
    if last_booked_before is not None:
        before_dt = datetime.datetime.combine(
            last_booked_before, datetime.time.min, tzinfo=datetime.timezone.utc
        )
    rows = segments_svc.segment_customers(
        db,
        tenant_id=current_user.tenant_id,
        tag_ids=parsed_tag_ids,
        tier=tier,
        min_bookings=min_bookings,
        last_booked_before=before_dt,
        location_id=location_id,
    )
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


# ── 顧客 ⇄ 標籤 掛載 ──────────────────────────────────────────────────────────

@router.get("/{customer_id}/tags", response_model=list[TagResponse])
def list_customer_tags(
    customer_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[TagResponse]:
    rows = segments_svc.list_tags_for_customer(
        db, tenant_id=current_user.tenant_id, customer_id=customer_id
    )
    return [TagResponse.model_validate(t) for t in rows]


@router.post(
    "/{customer_id}/tags/{tag_id}",
    response_model=TagResponse,
    status_code=status.HTTP_201_CREATED,
)
def attach_tag(
    customer_id: int,
    tag_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> TagResponse:
    segments_svc.attach_tag(
        db,
        tenant_id=current_user.tenant_id,
        customer_id=customer_id,
        tag_id=tag_id,
    )
    tag = segments_svc._get_tag_or_404(db, current_user.tenant_id, tag_id)
    return TagResponse.model_validate(tag)


@router.delete(
    "/{customer_id}/tags/{tag_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
def detach_tag(
    customer_id: int,
    tag_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Response:
    segments_svc.detach_tag(
        db,
        tenant_id=current_user.tenant_id,
        customer_id=customer_id,
        tag_id=tag_id,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
