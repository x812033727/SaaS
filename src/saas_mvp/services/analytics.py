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
# 模組層級註冊 DailyTenantStat:trend_series 讀預聚合表;測試檔多在 import 本
# 模組後才 create_all,lazy import 會讓表缺席。
from saas_mvp.models.daily_tenant_stat import DailyTenantStat  # noqa: F401
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
    """依「小時」聚合時段使用率（sum booked / sum capacity）。

    聚合下推 SQL：extract('hour') 在 SQLite 編譯為 CAST(STRFTIME('%H',..) AS
    INTEGER)、PG 為原生 EXTRACT，雙方言可攜；比率仍在 Python 端算（除零語意）。
    """
    hour_expr = func.extract("hour", BookingSlot.slot_start)
    stmt = (
        select(
            hour_expr.label("hour"),
            func.coalesce(func.sum(BookingSlot.booked_count), 0),
            func.coalesce(func.sum(BookingSlot.max_capacity), 0),
        )
        .where(BookingSlot.tenant_id == tenant_id)
        .group_by(hour_expr)
        .order_by(hour_expr)
    )
    if date_from is not None:
        stmt = stmt.where(BookingSlot.slot_start >= date_from)
    if date_to is not None:
        stmt = stmt.where(BookingSlot.slot_start <= date_to)

    out = []
    for hour, booked, capacity in db.execute(stmt).all():
        booked = int(booked)
        capacity = int(capacity)
        out.append({
            "hour": int(hour),
            "booked": booked,
            "capacity": capacity,
            "utilization": round(booked / capacity, 4) if capacity else 0.0,
        })
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


# ── F4 報表深度:營收/趨勢/產能/回訪(即時聚合,無新表)────────────────────────

def revenue_summary(
    db: Session,
    *,
    tenant_id: int,
    date_from: datetime.datetime | None = None,
    date_to: datetime.datetime | None = None,
) -> dict:
    """POS 已付訂單營收(以 paid_at 過濾;僅計 paid,退款個案不扣)。"""
    from saas_mvp.models.order import ORDER_PAID, Order

    stmt = select(
        func.count(),
        func.coalesce(func.sum(Order.total_cents), 0),
    ).where(Order.tenant_id == tenant_id, Order.status == ORDER_PAID)
    if date_from is not None:
        stmt = stmt.where(Order.paid_at >= date_from)
    if date_to is not None:
        stmt = stmt.where(Order.paid_at <= date_to)
    count, total_cents = db.execute(stmt).one()
    count = int(count)
    total_cents = int(total_cents)
    return {
        "paid_orders": count,
        "revenue_cents": total_cents,
        "avg_order_cents": (total_cents // count) if count else 0,
    }


def _period_key(dt: datetime.datetime, period: str) -> str:
    if period == "month":
        return dt.strftime("%Y-%m")
    # week:ISO 週(YYYY-Www)
    iso = dt.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def _period_starts(now: datetime.datetime, period: str, periods: int) -> list[str]:
    """近 N 期 key(含本期),舊到新。"""
    keys: list[str] = []
    cur = now
    for _ in range(periods):
        keys.append(_period_key(cur, period))
        if period == "month":
            cur = (cur.replace(day=1) - datetime.timedelta(days=1)).replace(day=1)
        else:
            cur = cur - datetime.timedelta(days=7)
    return list(reversed(keys))


def trend_series(
    db: Session,
    *,
    tenant_id: int,
    period: str = "week",
    periods: int = 12,
    now: datetime.datetime | None = None,
) -> list[dict]:
    """近 N 期預約量 + 營收趨勢(週/月)。

    R3-B3:改讀 daily_tenant_stats 預聚合(缺日/今天由 daily_series 以單一
    範圍查詢即時補,request path 只讀不寫)。分桶鍵僅依日期,與舊即時版
    (逐 slot_start / paid_at 分桶)輸出一致 — 由 parity 測試鎖定。
    """
    from saas_mvp.services import daily_stats

    now = now or datetime.datetime.now(datetime.timezone.utc)
    keys = _period_starts(now, period, periods)
    horizon = now - datetime.timedelta(days=(periods + 1) * (31 if period == "month" else 7))

    buckets = {k: {"period": k, "bookings": 0, "revenue_cents": 0} for k in keys}
    # 上界取「本期期末」:舊即時版對 slot_start 無上限,本期內未來幾天的
    # confirmed 預約也計入本期桶;只掃到今天會漏掉它們。
    today = now.date()
    if period == "month":
        end_exclusive = (today.replace(day=1) + datetime.timedelta(days=32)).replace(day=1)
    else:
        end_exclusive = today + datetime.timedelta(days=8 - today.isoweekday())
    series = daily_stats.daily_series(
        db,
        tenant_id=tenant_id,
        date_from=horizon.date(),
        date_to_exclusive=end_exclusive,
    )
    for day, row in series.items():
        k = _period_key(datetime.datetime(day.year, day.month, day.day), period)
        if k in buckets:
            buckets[k]["bookings"] += int(row["bookings_confirmed"])
            buckets[k]["revenue_cents"] += int(row["revenue_cents"])

    return [buckets[k] for k in keys]
