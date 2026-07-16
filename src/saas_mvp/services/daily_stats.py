"""每日租戶營運預聚合服務(R3-B3)。

寫入:scheduler cron(ops/aggregate_daily_stats)每日跑 ``rollup``(近幾天,
吸收事後 attended 標記/晚到付款);**request path 只讀不寫**。
讀取:``daily_series`` 讀表,缺日與「今天以後」以**單一 GROUP BY date 範圍查詢**
即時補算(不逐日打查詢,表未回填前報表頁也不會退化成 N 次查詢)。

口徑鎖定 analytics.py:預約歸屬 slot_start 當日(僅 confirmed 計 covers)、
營收歸屬 paid_at 當日且僅 ORDER_PAID(退款個案不扣)。
"""

from __future__ import annotations

import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from saas_mvp.models.booking_slot import BookingSlot
from saas_mvp.models.daily_tenant_stat import DailyTenantStat
from saas_mvp.models.order import ORDER_PAID, Order
from saas_mvp.models.reservation import (
    RESERVATION_CANCELLED,
    RESERVATION_CONFIRMED,
    Reservation,
)

STAT_FIELDS = (
    "bookings_total",
    "bookings_confirmed",
    "bookings_cancelled",
    "covers",
    "distinct_customers",
    "attended",
    "no_show",
    "paid_orders",
    "revenue_cents",
)


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _empty() -> dict:
    return {k: 0 for k in STAT_FIELDS}


def _as_date(value) -> datetime.date:
    """func.date() 在 SQLite 回字串、PG 回 date;統一成 date。"""
    if isinstance(value, datetime.date):
        return value
    return datetime.date.fromisoformat(str(value))


def compute_range(
    db: Session,
    *,
    tenant_id: int,
    date_from: datetime.date,
    date_to_exclusive: datetime.date,
) -> dict[datetime.date, dict]:
    """[date_from, date_to_exclusive) 逐日即時聚合;單次範圍查詢、Python 分日。

    只回有資料的日子(空日由呼叫端視為 0)。
    """
    start = datetime.datetime(date_from.year, date_from.month, date_from.day)
    end = datetime.datetime(
        date_to_exclusive.year, date_to_exclusive.month, date_to_exclusive.day
    )
    out: dict[datetime.date, dict] = {}

    def day_row(d: datetime.date) -> dict:
        return out.setdefault(d, _empty())

    resv_rows = db.execute(
        select(
            BookingSlot.slot_start,
            Reservation.status,
            Reservation.party_size,
            Reservation.attended,
            Reservation.line_user_id,
        )
        .join(BookingSlot, Reservation.slot_id == BookingSlot.id)
        .where(
            Reservation.tenant_id == tenant_id,
            BookingSlot.slot_start >= start,
            BookingSlot.slot_start < end,
        )
    ).all()
    customers_by_day: dict[datetime.date, set] = {}
    for slot_start, status, party_size, attended, line_user_id in resv_rows:
        d = slot_start.date()
        row = day_row(d)
        row["bookings_total"] += 1
        if status == RESERVATION_CONFIRMED:
            row["bookings_confirmed"] += 1
            row["covers"] += int(party_size or 0)
        elif status == RESERVATION_CANCELLED:
            row["bookings_cancelled"] += 1
        if attended is True:
            row["attended"] += 1
        elif attended is False:
            row["no_show"] += 1
        if line_user_id:
            customers_by_day.setdefault(d, set()).add(line_user_id)
    for d, users in customers_by_day.items():
        out[d]["distinct_customers"] = len(users)

    day_expr = func.date(Order.paid_at).label("day")
    order_rows = db.execute(
        select(day_expr, func.count(Order.id), func.coalesce(func.sum(Order.total_cents), 0))
        .where(
            Order.tenant_id == tenant_id,
            Order.status == ORDER_PAID,
            Order.paid_at.is_not(None),
            Order.paid_at >= start,
            Order.paid_at < end,
        )
        .group_by(day_expr)
    ).all()
    for day_value, cnt, total in order_rows:
        row = day_row(_as_date(day_value))
        row["paid_orders"] += int(cnt)
        row["revenue_cents"] += int(total or 0)

    return out


def compute_day(db: Session, *, tenant_id: int, day: datetime.date) -> dict:
    """單日即時聚合(cron 與測試用)。"""
    return compute_range(
        db, tenant_id=tenant_id, date_from=day,
        date_to_exclusive=day + datetime.timedelta(days=1),
    ).get(day, _empty())


def upsert_day(
    db: Session,
    *,
    tenant_id: int,
    day: datetime.date,
    values: dict | None = None,
) -> DailyTenantStat:
    """冪等 upsert 一日(不 commit,呼叫端批次提交)。"""
    values = values if values is not None else compute_day(db, tenant_id=tenant_id, day=day)
    row = db.execute(
        select(DailyTenantStat).where(
            DailyTenantStat.tenant_id == tenant_id,
            DailyTenantStat.stat_date == day,
        )
    ).scalar_one_or_none()
    if row is None:
        row = DailyTenantStat(tenant_id=tenant_id, stat_date=day)
        db.add(row)
    for k in STAT_FIELDS:
        setattr(row, k, int(values.get(k, 0)))
    row.computed_at = _utcnow()
    return row


def rollup(
    db: Session,
    *,
    tenant_id: int,
    days_back: int = 3,
    today: datetime.date | None = None,
) -> int:
    """回填 [today-days_back, today) 共 days_back 天(不含今天;今天仍在變動,
    讀取端 fallback 即時)。回傳 upsert 天數。呼叫端 commit。"""
    today = today or _utcnow().date()
    start = today - datetime.timedelta(days=days_back)
    computed = compute_range(
        db, tenant_id=tenant_id, date_from=start, date_to_exclusive=today
    )
    n = 0
    for i in range(days_back):
        day = start + datetime.timedelta(days=i)
        upsert_day(db, tenant_id=tenant_id, day=day, values=computed.get(day, _empty()))
        n += 1
    return n


def daily_series(
    db: Session,
    *,
    tenant_id: int,
    date_from: datetime.date,
    date_to_exclusive: datetime.date,
    today: datetime.date | None = None,
) -> dict[datetime.date, dict]:
    """讀表回逐日 dict;缺日與 >= today 的日子 fallback 即時計算(**只讀不寫**)。

    回傳鍵含範圍內每一天(無資料=全 0),呼叫端可直接分桶。
    """
    today = today or _utcnow().date()
    stored: dict[datetime.date, dict] = {}
    for row in db.execute(
        select(DailyTenantStat).where(
            DailyTenantStat.tenant_id == tenant_id,
            DailyTenantStat.stat_date >= date_from,
            DailyTenantStat.stat_date < date_to_exclusive,
        )
    ).scalars():
        stored[row.stat_date] = {k: int(getattr(row, k) or 0) for k in STAT_FIELDS}

    all_days = [
        date_from + datetime.timedelta(days=i)
        for i in range((date_to_exclusive - date_from).days)
    ]
    # 今天(含以後)永遠即時:cron 只回填到昨天,今天資料持續變動。
    missing = [d for d in all_days if d >= today or d not in stored]
    live: dict[datetime.date, dict] = {}
    if missing:
        live = compute_range(
            db, tenant_id=tenant_id,
            date_from=min(missing),
            date_to_exclusive=max(missing) + datetime.timedelta(days=1),
        )

    out: dict[datetime.date, dict] = {}
    for d in all_days:
        if d in missing:
            out[d] = live.get(d, _empty())
        else:
            out[d] = stored[d]
    return out
