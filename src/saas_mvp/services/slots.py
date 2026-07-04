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
from saas_mvp.models.reservation import Reservation
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


# 單次批次產生的上限——防呆，避免不小心展開出上萬筆（例：90 天 × 每 5 分鐘）。
MAX_BULK_SLOTS = 1000


def bulk_generate_slots(
    db: Session,
    *,
    tenant_id: int,
    date_start: datetime.date,
    date_end: datetime.date,
    time_start: datetime.time,
    time_end: datetime.time,
    interval_minutes: int,
    max_capacity: int,
    walkin_reserved: int = 0,
    weekdays: set[int] | None = None,
) -> dict:
    """依「日期區間 × 每日營業時間 × 間隔」批次展開時段。

    * 與單筆 create_slot 同語意：slot_start 為 tz-aware（呼叫端已轉好時區）。
    * slot_end 自動填為 slot_start + interval（資訊用，不影響容量計算）。
    * 已存在的同 start 時段（唯一約束）自動**略過**，不讓整批失敗——每筆以
      savepoint 包覆，衝突僅計入 skipped。
    * weekdays：要納入的星期集合（0=週一 … 6=週日）；None/空 = 每天皆產生。

    回傳 {created, skipped, total}。參數不合理（容量、間隔、區間反向、超過上限）
    一律拋 422。
    """
    _validate_capacity(max_capacity, walkin_reserved)
    if interval_minutes <= 0:
        raise HTTPException(status_code=422, detail="interval_minutes must be > 0")
    if date_end < date_start:
        raise HTTPException(status_code=422, detail="date_end must be >= date_start")
    if time_end <= time_start:
        raise HTTPException(status_code=422, detail="time_end must be > time_start")

    tz = datetime.timezone.utc
    step = datetime.timedelta(minutes=interval_minutes)
    candidates: list[datetime.datetime] = []
    day = date_start
    while day <= date_end:
        if not weekdays or day.weekday() in weekdays:
            cursor = datetime.datetime.combine(day, time_start, tzinfo=tz)
            day_end = datetime.datetime.combine(day, time_end, tzinfo=tz)
            while cursor < day_end:
                candidates.append(cursor)
                cursor += step
        day += datetime.timedelta(days=1)

    if len(candidates) > MAX_BULK_SLOTS:
        raise HTTPException(
            status_code=422,
            detail=(
                f"一次最多產生 {MAX_BULK_SLOTS} 個時段（本次將產生 {len(candidates)} 個），"
                "請縮短區間或加大間隔"
            ),
        )

    created = 0
    skipped = 0
    for start in candidates:
        try:
            with db.begin_nested():
                db.add(
                    BookingSlot(
                        tenant_id=tenant_id,
                        slot_start=start,
                        slot_end=start + step,
                        max_capacity=max_capacity,
                        walkin_reserved=walkin_reserved,
                    )
                )
                db.flush()
            created += 1
        except IntegrityError:
            # 同 start 已存在（唯一約束）→ 略過，不中斷整批。
            skipped += 1
    db.commit()
    return {"created": created, "skipped": skipped, "total": len(candidates)}


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


def delete_slot(db: Session, *, tenant_id: int, slot_id: int) -> None:
    """硬刪時段。

    只要有任何預約紀錄引用此時段（**含已取消**）即拒絕（409）——
    Reservation.slot_id 的 FK 為 ondelete=CASCADE，硬刪會連帶消滅預約歷史。
    有紀錄者請改用 deactivate_slot。
    """
    slot = _get_or_404(db, tenant_id, slot_id)
    has_reservations = (
        tenant_query(db, Reservation, tenant_id)
        .filter(Reservation.slot_id == slot_id)
        .count()
    )
    if has_reservations:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="此時段已有預約紀錄，無法刪除，請改用停用",
        )
    db.delete(slot)
    db.commit()
