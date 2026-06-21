"""預約時段（容量）服務層 — 店家端 CRUD。

純 REST 用途，比照 services/notes.py 直接拋 HTTPException（404 查無、409 衝突）。
所有查詢走 tenant_query 強制隔離；查無/跨租戶一律 404，不洩漏 ID 存在性。
"""

from __future__ import annotations

import datetime

from fastapi import HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from saas_mvp.models.booking_slot import BookingSlot
from saas_mvp.services.tenants import tenant_query


def _get_or_404(db: Session, tenant_id: int, slot_id: int) -> BookingSlot:
    slot = (
        tenant_query(db, BookingSlot, tenant_id)
        .filter(BookingSlot.id == slot_id)
        .first()
    )
    if slot is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Slot not found"
        )
    return slot


def _validate_capacity(max_capacity: int, walkin_reserved: int) -> None:
    if max_capacity < 0:
        raise HTTPException(status_code=422, detail="max_capacity must be >= 0")
    if walkin_reserved < 0:
        raise HTTPException(status_code=422, detail="walkin_reserved must be >= 0")
    if walkin_reserved > max_capacity:
        raise HTTPException(
            status_code=422, detail="walkin_reserved must be <= max_capacity"
        )


def create_slot(
    db: Session,
    *,
    tenant_id: int,
    slot_start: datetime.datetime,
    max_capacity: int,
    slot_end: datetime.datetime | None = None,
    walkin_reserved: int = 0,
) -> BookingSlot:
    _validate_capacity(max_capacity, walkin_reserved)
    slot = BookingSlot(
        tenant_id=tenant_id,
        slot_start=slot_start,
        slot_end=slot_end,
        max_capacity=max_capacity,
        walkin_reserved=walkin_reserved,
    )
    db.add(slot)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A slot already exists at this start time",
        )
    db.refresh(slot)
    return slot


def list_slots(
    db: Session,
    *,
    tenant_id: int,
    date_from: datetime.datetime | None = None,
    date_to: datetime.datetime | None = None,
    active_only: bool = False,
) -> list[BookingSlot]:
    q = tenant_query(db, BookingSlot, tenant_id)
    if date_from is not None:
        q = q.filter(BookingSlot.slot_start >= date_from)
    if date_to is not None:
        q = q.filter(BookingSlot.slot_start <= date_to)
    if active_only:
        q = q.filter(BookingSlot.is_active.is_(True))
    return q.order_by(BookingSlot.slot_start).all()


def get_slot(db: Session, *, tenant_id: int, slot_id: int) -> BookingSlot:
    return _get_or_404(db, tenant_id, slot_id)


def update_slot(
    db: Session,
    *,
    tenant_id: int,
    slot_id: int,
    max_capacity: int | None = None,
    walkin_reserved: int | None = None,
    is_active: bool | None = None,
) -> BookingSlot:
    slot = _get_or_404(db, tenant_id, slot_id)
    new_max = max_capacity if max_capacity is not None else slot.max_capacity
    new_walkin = (
        walkin_reserved if walkin_reserved is not None else slot.walkin_reserved
    )
    _validate_capacity(new_max, new_walkin)

    # 不可把容量下修到低於已訂量（會造成超賣的負可用名額）。
    if new_max - new_walkin < (slot.booked_count or 0):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Cannot shrink capacity below current bookings "
                f"(booked={slot.booked_count})"
            ),
        )

    if max_capacity is not None:
        slot.max_capacity = max_capacity
    if walkin_reserved is not None:
        slot.walkin_reserved = walkin_reserved
    if is_active is not None:
        slot.is_active = is_active
    db.commit()
    db.refresh(slot)
    return slot


def deactivate_slot(db: Session, *, tenant_id: int, slot_id: int) -> None:
    """軟刪：停用時段（保留既有預約與容量計數）。"""
    slot = _get_or_404(db, tenant_id, slot_id)
    slot.is_active = False
    db.commit()
