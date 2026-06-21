"""預約核心服務 — 容量原子控管、建單、取消、查詢。

容量競態消除（最關鍵）：book_slot 對 BookingSlot 該列 SELECT … FOR UPDATE，
**取得鎖後重驗** online_available，足夠才在同交易內遞增 booked_count、INSERT
Reservation、upsert Customer、入列提醒，單一 commit。鎖法比照
quota._get_or_create_usage_locked（SQLite 升 connection-level lock、PG 行鎖）。

跨租戶一律走 tenant_query / 帶 tenant_id 條件，查無回 404（service 拋自訂例外，
router/webhook 各自轉成 HTTP 或友善訊息）。
"""

from __future__ import annotations

import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from saas_mvp.config import settings
from saas_mvp.models.booking_slot import BookingSlot
from saas_mvp.models.customer import upsert_customer_from_line
from saas_mvp.models.reservation import (
    RESERVATION_CANCELLED,
    RESERVATION_CONFIRMED,
    Reservation,
)
from saas_mvp.services import membership as membership_svc
from saas_mvp.services.reminders import (
    cancel_reminders_for_reservation,
    enqueue_reminders,
)
from saas_mvp.services.tenants import tenant_query


# ── 自訂例外（router 轉 HTTP、webhook 轉友善訊息） ────────────────────────────
class BookingError(Exception):
    """預約相關錯誤基底。"""


class SlotNotFoundError(BookingError):
    """時段不存在、跨租戶、或已停用。"""


class SlotFullError(BookingError):
    """時段線上可用名額不足。"""


class ReservationNotFoundError(BookingError):
    """預約不存在或跨租戶。"""


class ReservationPermissionError(BookingError):
    """LINE 來源取消時 line_user_id 與建單者不符。"""


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def book_slot(
    db: Session,
    *,
    tenant_id: int,
    slot_id: int,
    party_size: int = 1,
    line_user_id: str | None = None,
    display_name: str | None = None,
    note: str | None = None,
) -> Reservation:
    """原子建單：鎖時段 → 重驗容量 → 建顧客檔 → INSERT 預約 → 遞增 booked_count
    → 入列提醒 → 單一 commit。

    容量不足拋 SlotFullError；時段不存在/停用拋 SlotNotFoundError。
    """
    if party_size < 1:
        raise ValueError(f"party_size must be >= 1, got {party_size}")

    # 鎖定時段列（序列化並發建單）
    slot = db.execute(
        select(BookingSlot)
        .where(BookingSlot.id == slot_id, BookingSlot.tenant_id == tenant_id)
        .with_for_update()
    ).scalar_one_or_none()
    if slot is None or not slot.is_active:
        raise SlotNotFoundError(f"slot {slot_id} not found or inactive")

    # 鎖內重驗容量（TOCTOU 消除）
    available = (
        (slot.max_capacity or 0)
        - (slot.walkin_reserved or 0)
        - (slot.booked_count or 0)
    )
    if available < party_size:
        raise SlotFullError(
            f"slot {slot_id} full: available={available}, requested={party_size}"
        )

    # 顧客建檔（LINE 來源才有 line_user_id）
    customer = None
    customer_id = None
    if line_user_id:
        customer = upsert_customer_from_line(
            db,
            tenant_id=tenant_id,
            line_user_id=line_user_id,
            display_name=display_name,
        )
        customer_id = customer.id

    reservation = Reservation(
        tenant_id=tenant_id,
        slot_id=slot_id,
        customer_id=customer_id,
        line_user_id=line_user_id,
        party_size=party_size,
        status=RESERVATION_CONFIRMED,
        note=note,
    )
    db.add(reservation)
    slot.booked_count = (slot.booked_count or 0) + party_size
    db.flush()  # 取得 reservation.id 供提醒入列 / 集點帳本

    enqueue_reminders(
        db,
        reservation=reservation,
        slot=slot,
        day_of_lead_minutes=settings.reminder_day_of_lead_minutes,
        enabled=settings.reminder_enabled,
    )

    # 會員集點（同一交易；customer 為 None（店家手動建單無 line_user_id）時略過）
    if customer is not None and settings.points_per_booking > 0:
        membership_svc.earn_points(
            db,
            tenant_id=tenant_id,
            customer=customer,
            delta=settings.points_per_booking,
            reason="booking",
            reservation_id=reservation.id,
        )

    db.commit()
    db.refresh(reservation)
    return reservation


def cancel_reservation(
    db: Session,
    *,
    tenant_id: int,
    reservation_id: int,
    line_user_id: str | None = None,
) -> Reservation:
    """取消預約：鎖時段 → booked_count 回補 → 狀態改 cancelled → 待發提醒標 skipped。

    line_user_id 非 None 時（LINE 來源取消）額外驗證與建單者相符，防他人取消。
    重複取消為 no-op（不重複回補）。
    """
    reservation = (
        tenant_query(db, Reservation, tenant_id)
        .filter(Reservation.id == reservation_id)
        .first()
    )
    if reservation is None:
        raise ReservationNotFoundError(f"reservation {reservation_id} not found")

    if line_user_id is not None and reservation.line_user_id != line_user_id:
        raise ReservationPermissionError("reservation belongs to another LINE user")

    if reservation.status == RESERVATION_CANCELLED:
        return reservation  # 冪等：已取消不重複回補

    # 鎖時段回補容量
    slot = db.execute(
        select(BookingSlot)
        .where(
            BookingSlot.id == reservation.slot_id,
            BookingSlot.tenant_id == tenant_id,
        )
        .with_for_update()
    ).scalar_one_or_none()
    if slot is not None:
        slot.booked_count = max(0, (slot.booked_count or 0) - reservation.party_size)

    reservation.status = RESERVATION_CANCELLED
    reservation.cancelled_at = _utcnow()
    cancel_reminders_for_reservation(db, reservation_id=reservation_id)

    db.commit()
    db.refresh(reservation)
    return reservation


def get_reservation(
    db: Session, *, tenant_id: int, reservation_id: int
) -> Reservation:
    """取得單筆預約；查無/跨租戶拋 ReservationNotFoundError。"""
    reservation = (
        tenant_query(db, Reservation, tenant_id)
        .filter(Reservation.id == reservation_id)
        .first()
    )
    if reservation is None:
        raise ReservationNotFoundError(f"reservation {reservation_id} not found")
    return reservation


def list_reservations(
    db: Session,
    *,
    tenant_id: int,
    status: str | None = None,
    line_user_id: str | None = None,
    slot_id: int | None = None,
) -> list[Reservation]:
    """列出租戶預約，可依 status / line_user_id / slot_id 篩選。"""
    q = tenant_query(db, Reservation, tenant_id)
    if status is not None:
        q = q.filter(Reservation.status == status)
    if line_user_id is not None:
        q = q.filter(Reservation.line_user_id == line_user_id)
    if slot_id is not None:
        q = q.filter(Reservation.slot_id == slot_id)
    return q.order_by(Reservation.id).all()


def list_my_reservations(
    db: Session, *, tenant_id: int, line_user_id: str
) -> list[Reservation]:
    """LINE 使用者查自己的 confirmed 預約。"""
    return list_reservations(
        db,
        tenant_id=tenant_id,
        status=RESERVATION_CONFIRMED,
        line_user_id=line_user_id,
    )
