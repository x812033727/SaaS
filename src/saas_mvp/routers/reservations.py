"""Booking reservations router — 店家端預約管理。

受認證 + 租戶隔離 + rate limit；不掛 require_quota（見 DECISIONS）。
建單共用 services/booking.book_slot（原子容量控管），與 LINE 預約同一條路徑。
"""

from __future__ import annotations

import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from saas_mvp.deps import get_current_user, get_db, require_rate_limit
from saas_mvp.models.user import User
from saas_mvp.services.booking import (
    ReservationNotFoundError,
    SlotFullError,
    SlotNotFoundError,
    book_slot,
    cancel_reservation,
    get_reservation,
    list_reservations,
    mark_attendance,
    reschedule_reservation,
)

router = APIRouter(
    prefix="/booking/reservations",
    tags=["booking-reservations"],
    dependencies=[Depends(require_rate_limit)],
)


# ─────────────────────────────── Schemas ─────────────────────────────────────

class ReservationCreate(BaseModel):
    slot_id: int
    party_size: int = Field(default=1, ge=1)
    line_user_id: str | None = Field(default=None, max_length=64)
    display_name: str | None = Field(default=None, max_length=128)
    note: str | None = Field(default=None, max_length=1024)
    staff_id: int | None = Field(default=None)
    service_id: int | None = Field(default=None)


class ReservationResponse(BaseModel):
    id: int
    tenant_id: int
    slot_id: int
    customer_id: int | None
    line_user_id: str | None
    party_size: int
    status: str
    note: str | None
    attended: bool | None
    staff_id: int | None
    service_id: int | None
    created_at: datetime.datetime
    cancelled_at: datetime.datetime | None

    model_config = {"from_attributes": True}


class AttendanceBody(BaseModel):
    attended: bool


class RescheduleBody(BaseModel):
    new_slot_id: int


# ─────────────────────────────── Endpoints ───────────────────────────────────

@router.post("/", response_model=ReservationResponse, status_code=status.HTTP_201_CREATED)
def create(
    body: ReservationCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ReservationResponse:
    try:
        reservation = book_slot(
            db,
            tenant_id=current_user.tenant_id,
            slot_id=body.slot_id,
            party_size=body.party_size,
            line_user_id=body.line_user_id,
            display_name=body.display_name,
            note=body.note,
            staff_id=body.staff_id,
            service_id=body.service_id,
        )
    except SlotNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Slot not found"
        )
    except SlotFullError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Slot is full"
        )
    return ReservationResponse.model_validate(reservation)


@router.get("/", response_model=list[ReservationResponse])
def list_all(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    status_filter: str | None = Query(default=None, alias="status"),
    slot_id: int | None = Query(default=None),
) -> list[ReservationResponse]:
    rows = list_reservations(
        db,
        tenant_id=current_user.tenant_id,
        status=status_filter,
        slot_id=slot_id,
    )
    return [ReservationResponse.model_validate(r) for r in rows]


@router.get("/{reservation_id}", response_model=ReservationResponse)
def get_one(
    reservation_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ReservationResponse:
    try:
        reservation = get_reservation(
            db, tenant_id=current_user.tenant_id, reservation_id=reservation_id
        )
    except ReservationNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Reservation not found"
        )
    return ReservationResponse.model_validate(reservation)


@router.post("/{reservation_id}/attendance", response_model=ReservationResponse)
def set_attendance(
    reservation_id: int,
    body: AttendanceBody,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ReservationResponse:
    try:
        reservation = mark_attendance(
            db,
            tenant_id=current_user.tenant_id,
            reservation_id=reservation_id,
            attended=body.attended,
        )
    except ReservationNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Reservation not found"
        )
    return ReservationResponse.model_validate(reservation)


@router.post("/{reservation_id}/reschedule", response_model=ReservationResponse)
def reschedule_one(
    reservation_id: int,
    body: RescheduleBody,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ReservationResponse:
    try:
        reservation = reschedule_reservation(
            db,
            tenant_id=current_user.tenant_id,
            reservation_id=reservation_id,
            new_slot_id=body.new_slot_id,
        )
    except ReservationNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Reservation not found"
        )
    except SlotNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Slot not found"
        )
    except SlotFullError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Slot is full"
        )
    return ReservationResponse.model_validate(reservation)


@router.post("/{reservation_id}/cancel", response_model=ReservationResponse)
def cancel_one(
    reservation_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ReservationResponse:
    try:
        reservation = cancel_reservation(
            db, tenant_id=current_user.tenant_id, reservation_id=reservation_id
        )
    except ReservationNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Reservation not found"
        )
    return ReservationResponse.model_validate(reservation)
