"""報表分析服務 — 預約量、取消/爽約率、時段使用率、Top 顧客、提醒成效。

設計：
* 租戶隔離（所有查詢帶 tenant_id）。
* 聚合在單一查詢取出資料後於 Python 計算（避免 DB 方言差異如 strftime；單租戶資料量適中，
  非平台級全表，無 N+1）。
* 日期區間以 BookingSlot.slot_start（服務日期）過濾，選填。
* **誠實**：到場與否需店家標記（Reservation.attended）；未標記則 no_show_rate 回 None，
  報表以取消率 + 提醒寄送數呈現。
"""

from __future__ import annotations

import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from saas_mvp.models.booking_slot import BookingSlot
from saas_mvp.models.customer import Customer
from saas_mvp.models.reservation import (
    RESERVATION_CANCELLED,
    RESERVATION_CONFIRMED,
    Reservation,
)
from saas_mvp.models.reservation_reminder import ReservationReminder


def _reservations_in_range(
    db: Session,
    tenant_id: int,
    date_from: datetime.datetime | None,
    date_to: datetime.datetime | None,
) -> list[Reservation]:
    stmt = (
        select(Reservation)
        .join(BookingSlot, Reservation.slot_id == BookingSlot.id)
        .where(Reservation.tenant_id == tenant_id)
    )
    if date_from is not None:
        stmt = stmt.where(BookingSlot.slot_start >= date_from)
    if date_to is not None:
        stmt = stmt.where(BookingSlot.slot_start <= date_to)
    return list(db.execute(stmt).scalars())


def booking_summary(
    db: Session,
    *,
    tenant_id: int,
    date_from: datetime.datetime | None = None,
    date_to: datetime.datetime | None = None,
) -> dict:
    rows = _reservations_in_range(db, tenant_id, date_from, date_to)
    total = len(rows)
    confirmed = sum(1 for r in rows if r.status == RESERVATION_CONFIRMED)
    cancelled = sum(1 for r in rows if r.status == RESERVATION_CANCELLED)
    covers = sum(r.party_size for r in rows if r.status == RESERVATION_CONFIRMED)
    distinct_customers = len({r.line_user_id for r in rows if r.line_user_id})
    attended = sum(1 for r in rows if r.attended is True)
    no_show = sum(1 for r in rows if r.attended is False)
    marked = attended + no_show
    return {
        "total": total,
        "confirmed": confirmed,
        "cancelled": cancelled,
        "cancel_rate": round(cancelled / total, 4) if total else 0.0,
        "total_covers": covers,
        "distinct_customers": distinct_customers,
        "attended": attended,
        "no_show": no_show,
        "no_show_rate": round(no_show / marked, 4) if marked else None,
    }


def slot_utilization(
    db: Session,
    *,
    tenant_id: int,
    date_from: datetime.datetime | None = None,
    date_to: datetime.datetime | None = None,
) -> list[dict]:
    """依「小時」聚合時段使用率（sum booked / sum capacity）。"""
    stmt = select(BookingSlot).where(BookingSlot.tenant_id == tenant_id)
    if date_from is not None:
        stmt = stmt.where(BookingSlot.slot_start >= date_from)
    if date_to is not None:
        stmt = stmt.where(BookingSlot.slot_start <= date_to)
    slots = list(db.execute(stmt).scalars())

    buckets: dict[int, dict] = {}
    for s in slots:
        hour = s.slot_start.hour
        b = buckets.setdefault(hour, {"hour": hour, "booked": 0, "capacity": 0})
        b["booked"] += s.booked_count or 0
        b["capacity"] += s.max_capacity or 0
    out = []
    for hour in sorted(buckets):
        b = buckets[hour]
        b["utilization"] = (
            round(b["booked"] / b["capacity"], 4) if b["capacity"] else 0.0
        )
        out.append(b)
    return out


def top_customers(
    db: Session, *, tenant_id: int, limit: int = 10
) -> list[Customer]:
    return list(
        db.execute(
            select(Customer)
            .where(Customer.tenant_id == tenant_id)
            .order_by(Customer.booking_count.desc(), Customer.id)
            .limit(limit)
        ).scalars()
    )


def export_rows(
    db: Session,
    *,
    tenant_id: int,
    date_from: datetime.datetime | None = None,
    date_to: datetime.datetime | None = None,
) -> list[dict]:
    """預約明細扁平列（供 CSV 匯出）；含 slot_start。"""
    stmt = (
        select(Reservation, BookingSlot.slot_start)
        .join(BookingSlot, Reservation.slot_id == BookingSlot.id)
        .where(Reservation.tenant_id == tenant_id)
        .order_by(Reservation.id)
    )
    if date_from is not None:
        stmt = stmt.where(BookingSlot.slot_start >= date_from)
    if date_to is not None:
        stmt = stmt.where(BookingSlot.slot_start <= date_to)
    out = []
    for resv, slot_start in db.execute(stmt).all():
        out.append({
            "reservation_id": resv.id,
            "slot_start": slot_start.isoformat() if slot_start else "",
            "party_size": resv.party_size,
            "status": resv.status,
            "line_user_id": resv.line_user_id or "",
            "attended": "" if resv.attended is None else ("yes" if resv.attended else "no"),
            "created_at": resv.created_at.isoformat() if resv.created_at else "",
        })
    return out


def reminder_effectiveness(db: Session, *, tenant_id: int) -> dict:
    """提醒各狀態筆數（代理指標：寄送量；非精確降低爽約幅度）。"""
    rows = db.execute(
        select(ReservationReminder.status, func.count())
        .where(ReservationReminder.tenant_id == tenant_id)
        .group_by(ReservationReminder.status)
    ).all()
    return {status: count for status, count in rows}
