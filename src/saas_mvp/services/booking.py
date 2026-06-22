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
from saas_mvp.services import booking_notify as booking_notify_svc
from saas_mvp.services import features as features_svc
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


class CrossTenantReferenceError(BookingError):
    """建單帶入的 staff_id / service_id 不屬於本租戶（偽造或跨租戶引用）。"""


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
    staff_id: int | None = None,
    service_id: int | None = None,
) -> Reservation:
    """原子建單：鎖時段 → 重驗容量 → 建顧客檔 → INSERT 預約 → 遞增 booked_count
    → 入列提醒 → 單一 commit。

    容量不足拋 SlotFullError；時段不存在/停用拋 SlotNotFoundError。

    staff_id（選填）：指定服務員工。若該員工 booking_mode == 'one_to_one'，
    同一時段同一員工已有 confirmed 預約時拋 SlotFullError（一對一不可重訂）。
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

    # 跨租戶/偽造引用防護：staff_id / service_id 若帶入，必須屬於本租戶，
    # 否則拒絕建單（不靜默存入 raw 值）。比照其他服務的 tenant_query 隔離。
    if service_id is not None:
        from saas_mvp.models.service import Service

        owned_service = (
            tenant_query(db, Service, tenant_id)
            .filter(Service.id == service_id)
            .first()
        )
        if owned_service is None:
            raise CrossTenantReferenceError(
                f"service {service_id} not found for tenant {tenant_id}"
            )

    # 一對一員工：鎖內檢查同時段同員工是否已被佔用（防一對一重訂）。
    if staff_id is not None:
        from saas_mvp.models.staff import STAFF_MODE_ONE_TO_ONE, Staff

        staff = (
            tenant_query(db, Staff, tenant_id)
            .filter(Staff.id == staff_id)
            .first()
        )
        if staff is None:
            raise CrossTenantReferenceError(
                f"staff {staff_id} not found for tenant {tenant_id}"
            )
        if staff.booking_mode == STAFF_MODE_ONE_TO_ONE:
            existing = (
                tenant_query(db, Reservation, tenant_id)
                .filter(
                    Reservation.slot_id == slot_id,
                    Reservation.staff_id == staff_id,
                    Reservation.status == RESERVATION_CONFIRMED,
                )
                .first()
            )
            if existing is not None:
                raise SlotFullError(
                    f"staff {staff_id} already booked for slot {slot_id}"
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
        staff_id=staff_id,
        service_id=service_id,
    )
    db.add(reservation)
    slot.booked_count = (slot.booked_count or 0) + party_size
    db.flush()  # 取得 reservation.id 供提醒入列 / 集點帳本

    # 自動提醒為進階功能：需 tenant 開通 AUTO_REMINDER 且全域 reminder_enabled。
    enqueue_reminders(
        db,
        reservation=reservation,
        slot=slot,
        day_of_lead_minutes=settings.reminder_day_of_lead_minutes,
        enabled=(
            settings.reminder_enabled
            and features_svc.is_enabled(db, tenant_id, features_svc.AUTO_REMINDER)
        ),
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

    # 預約異動通知（進階功能）：取消時 LINE 推播給顧客。同一交易內入列。
    if slot is not None:
        booking_notify_svc.enqueue_cancel(
            db,
            reservation=reservation,
            slot=slot,
            enabled=features_svc.is_enabled(
                db, tenant_id, features_svc.BOOKING_NOTIFY
            ),
        )

    db.commit()
    db.refresh(reservation)
    return reservation


def reschedule_reservation(
    db: Session,
    *,
    tenant_id: int,
    reservation_id: int,
    new_slot_id: int,
) -> Reservation:
    """原子改期：把 confirmed 預約移到新時段。

    流程（單一 commit）：
      1. 取預約（tenant_query；查無拋 ReservationNotFoundError）。
      2. 鎖舊+新時段（FOR UPDATE，依 slot id 排序避免死結）。
      3. 新時段鎖內重驗容量（不足拋 SlotFullError）。
      4. 舊時段 booked_count 回補、新時段遞增、reservation.slot_id 改為新時段。
      5. 入列 change 通知（BOOKING_NOTIFY 開通且有 line_user_id 時）。

    new_slot_id == 既有 slot_id 為 no-op（直接回傳，不入列、不動容量）。
    已取消的預約不可改期（拋 ReservationNotFoundError）。
    """
    reservation = (
        tenant_query(db, Reservation, tenant_id)
        .filter(Reservation.id == reservation_id)
        .first()
    )
    if reservation is None:
        raise ReservationNotFoundError(f"reservation {reservation_id} not found")
    if reservation.status != RESERVATION_CONFIRMED:
        raise ReservationNotFoundError(
            f"reservation {reservation_id} is not active"
        )

    old_slot_id = reservation.slot_id
    if new_slot_id == old_slot_id:
        return reservation  # no-op：同一時段

    # 依 slot id 排序鎖定（一致順序避免並發死結）。
    first_id, second_id = sorted((old_slot_id, new_slot_id))
    locked: dict[int, BookingSlot] = {}
    for sid in (first_id, second_id):
        s = db.execute(
            select(BookingSlot)
            .where(BookingSlot.id == sid, BookingSlot.tenant_id == tenant_id)
            .with_for_update()
        ).scalar_one_or_none()
        if s is not None:
            locked[sid] = s

    old_slot = locked.get(old_slot_id)
    new_slot = locked.get(new_slot_id)
    if new_slot is None or not new_slot.is_active:
        raise SlotNotFoundError(f"slot {new_slot_id} not found or inactive")

    # 新時段鎖內重驗容量。
    available = (
        (new_slot.max_capacity or 0)
        - (new_slot.walkin_reserved or 0)
        - (new_slot.booked_count or 0)
    )
    if available < reservation.party_size:
        raise SlotFullError(
            f"slot {new_slot_id} full: available={available}, "
            f"requested={reservation.party_size}"
        )

    if old_slot is not None:
        old_slot.booked_count = max(
            0, (old_slot.booked_count or 0) - reservation.party_size
        )
    new_slot.booked_count = (new_slot.booked_count or 0) + reservation.party_size
    reservation.slot_id = new_slot_id

    # 預約異動通知（進階功能）：改期時 LINE 推播給顧客。同一交易內入列。
    booking_notify_svc.enqueue_change(
        db,
        reservation=reservation,
        slot=new_slot,
        old_slot=old_slot,
        enabled=features_svc.is_enabled(
            db, tenant_id, features_svc.BOOKING_NOTIFY
        ),
    )

    db.commit()
    db.refresh(reservation)
    return reservation


def mark_attendance(
    db: Session, *, tenant_id: int, reservation_id: int, attended: bool
) -> Reservation:
    """店家標記預約到場與否（供報表算爽約率）；查無/跨租戶拋 ReservationNotFoundError。"""
    reservation = (
        tenant_query(db, Reservation, tenant_id)
        .filter(Reservation.id == reservation_id)
        .first()
    )
    if reservation is None:
        raise ReservationNotFoundError(f"reservation {reservation_id} not found")
    reservation.attended = attended
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
