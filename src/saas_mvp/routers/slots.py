"""Booking slots router — 店家端時段/容量管理（CRUD）。

受認證 + 租戶隔離 + rate limit；不掛 require_quota（quota 是翻譯計量表，
與預約不混用，見 DECISIONS）。所有操作經 services/slots.py。
"""

from __future__ import annotations

import datetime

from fastapi import APIRouter, Depends, Query, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from saas_mvp.deps import get_current_user, get_db, require_rate_limit
from saas_mvp.models.user import User
from saas_mvp.services.slots import (
    bulk_generate_slots,
    create_slot,
    deactivate_slot,
    get_slot,
    list_slots,
    update_slot,
)

router = APIRouter(
    prefix="/booking/slots",
    tags=["booking-slots"],
    dependencies=[Depends(require_rate_limit)],
)


# ─────────────────────────────── Schemas ─────────────────────────────────────

class SlotCreate(BaseModel):
    slot_start: datetime.datetime
    slot_end: datetime.datetime | None = None
    max_capacity: int = Field(ge=0)
    walkin_reserved: int = Field(default=0, ge=0)


class SlotUpdate(BaseModel):
    max_capacity: int | None = Field(default=None, ge=0)
    walkin_reserved: int | None = Field(default=None, ge=0)
    is_active: bool | None = None


class SlotResponse(BaseModel):
    id: int
    tenant_id: int
    slot_start: datetime.datetime
    slot_end: datetime.datetime | None
    max_capacity: int
    walkin_reserved: int
    booked_count: int
    is_active: bool
    online_available: int

    model_config = {"from_attributes": True}


# ─────────────────────────────── Endpoints ───────────────────────────────────

class SlotBulkCreate(BaseModel):
    """批次展開:日期區間 × 每日營業時間 × 間隔(語意同 /ui 批次表單)。"""

    date_start: datetime.date
    date_end: datetime.date
    time_start: datetime.time
    time_end: datetime.time
    interval_minutes: int = Field(gt=0)
    max_capacity: int = Field(ge=0)
    walkin_reserved: int = Field(default=0, ge=0)
    # 0=週一 … 6=週日;None/空 = 每天皆產生。
    weekdays: list[int] | None = Field(default=None)


class SlotBulkResult(BaseModel):
    created: int
    skipped: int
    total: int


@router.post("/bulk", response_model=SlotBulkResult)
def bulk_create(
    body: SlotBulkCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> SlotBulkResult:
    """批次產生時段(console 用);同 start 既存時段自動略過計入 skipped。"""
    result = bulk_generate_slots(
        db,
        tenant_id=current_user.tenant_id,
        date_start=body.date_start,
        date_end=body.date_end,
        time_start=body.time_start,
        time_end=body.time_end,
        interval_minutes=body.interval_minutes,
        max_capacity=body.max_capacity,
        walkin_reserved=body.walkin_reserved,
        weekdays=set(body.weekdays) if body.weekdays else None,
    )
    return SlotBulkResult(**result)


@router.post("/", response_model=SlotResponse, status_code=status.HTTP_201_CREATED)
def create(
    body: SlotCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> SlotResponse:
    slot = create_slot(
        db,
        tenant_id=current_user.tenant_id,
        slot_start=body.slot_start,
        slot_end=body.slot_end,
        max_capacity=body.max_capacity,
        walkin_reserved=body.walkin_reserved,
    )
    return SlotResponse.model_validate(slot)


@router.get("/", response_model=list[SlotResponse])
def list_all(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    date_from: datetime.datetime | None = Query(default=None),
    date_to: datetime.datetime | None = Query(default=None),
    active_only: bool = Query(default=False),
) -> list[SlotResponse]:
    slots = list_slots(
        db,
        tenant_id=current_user.tenant_id,
        date_from=date_from,
        date_to=date_to,
        active_only=active_only,
    )
    return [SlotResponse.model_validate(s) for s in slots]


@router.get("/{slot_id}", response_model=SlotResponse)
def get_one(
    slot_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> SlotResponse:
    slot = get_slot(db, tenant_id=current_user.tenant_id, slot_id=slot_id)
    return SlotResponse.model_validate(slot)


@router.put("/{slot_id}", response_model=SlotResponse)
def update_one(
    slot_id: int,
    body: SlotUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> SlotResponse:
    slot = update_slot(
        db,
        tenant_id=current_user.tenant_id,
        slot_id=slot_id,
        max_capacity=body.max_capacity,
        walkin_reserved=body.walkin_reserved,
        is_active=body.is_active,
    )
    return SlotResponse.model_validate(slot)


@router.delete(
    "/{slot_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response
)
def delete_one(
    slot_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Response:
    deactivate_slot(db, tenant_id=current_user.tenant_id, slot_id=slot_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
