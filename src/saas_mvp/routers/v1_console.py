"""Console JSON API(R3-C1)— Next.js saas-console 的日常營運端點。

薄 pydantic 層包既有 service:
* GET /api/v1/reservations       — enriched 預約列(join 時段/顧客/員工/服務)
* GET /api/v1/customers/{id}/reservations — 單一顧客的預約歷史
* GET /api/v1/calendar/month|week — 直接包 services/calendar_view
* GET /api/v1/dashboard/today    — 今日營運快照(預約+摘要+營收+待辦)

皆為 Bearer JWT + 租戶隔離 + rate limit;時間語意沿用 /ui(naive 顯示值),
console 與 Jinja UI 顯示一致。
"""

from __future__ import annotations

import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from saas_mvp.deps import get_current_user, get_db, require_rate_limit
from saas_mvp.models.booking_waitlist import WAITLIST_WAITING, WaitlistEntry
from saas_mvp.models.reservation import RESERVATION_CONFIRMED, Reservation
from saas_mvp.models.user import User
from saas_mvp.services import analytics as analytics_svc
from saas_mvp.services import booking as booking_svc
from saas_mvp.services import calendar_view as calendar_svc
from saas_mvp.services import customers as customers_svc
from saas_mvp.services.tenants import tenant_query

router = APIRouter(
    prefix="/api/v1",
    tags=["v1-console"],
    dependencies=[Depends(require_rate_limit)],
)


class ReservationRow(BaseModel):
    id: int
    status: str
    party_size: int
    attended: bool | None
    line_user_id: str | None
    deposit_status: str | None
    deposit_cents: int | None
    slot_id: int
    slot_start: datetime.datetime
    slot_end: datetime.datetime | None
    customer_id: int | None
    customer_name: str | None
    customer_phone: str | None
    staff_id: int | None
    staff_name: str | None
    service_id: int | None
    service_name: str | None


def _parse_date(value: str | None, field: str) -> datetime.date | None:
    if not value:
        return None
    try:
        return datetime.date.fromisoformat(value)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"{field} 需為 YYYY-MM-DD")


def _day_start(d: datetime.date) -> datetime.datetime:
    # 沿用 calendar_view 的 naive 日界線語意,與 /ui 顯示一致。
    return datetime.datetime(d.year, d.month, d.day)


@router.get("/reservations", response_model=list[ReservationRow])
def list_reservations_enriched(
    response: Response,
    date_from: str | None = Query(default=None, description="YYYY-MM-DD(含)"),
    date_to: str | None = Query(default=None, description="YYYY-MM-DD(不含)"),
    status: str | None = Query(default=None, alias="status"),
    customer_id: int | None = Query(default=None),
    staff_id: int | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict]:
    df = _parse_date(date_from, "date_from")
    dt = _parse_date(date_to, "date_to")
    kwargs = dict(
        tenant_id=current_user.tenant_id,
        date_from=_day_start(df) if df else None,
        date_to=_day_start(dt) if dt else None,
        status=status,
        customer_id=customer_id,
        staff_id=staff_id,
    )
    rows = booking_svc.list_reservations_enriched(db, **kwargs, limit=limit, offset=offset)
    response.headers["X-Total-Count"] = str(
        booking_svc.count_reservations_enriched(db, **kwargs)
    )
    return rows


@router.get("/customers/{customer_id}/reservations", response_model=list[ReservationRow])
def customer_reservations(
    customer_id: int,
    response: Response,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict]:
    # 顧客必須屬於本租戶(查無即 404,防跨租戶枚舉)。
    customers_svc.get_customer(
        db, tenant_id=current_user.tenant_id, customer_id=customer_id
    )
    kwargs = dict(tenant_id=current_user.tenant_id, customer_id=customer_id)
    rows = booking_svc.list_reservations_enriched(db, **kwargs, limit=limit, offset=offset)
    response.headers["X-Total-Count"] = str(
        booking_svc.count_reservations_enriched(db, **kwargs)
    )
    return rows


@router.get("/calendar/month")
def calendar_month(
    year: int = Query(ge=2000, le=2100),
    month: int = Query(ge=1, le=12),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    return calendar_svc.build_month(
        db, tenant_id=current_user.tenant_id, year=year, month=month
    )


@router.get("/calendar/week")
def calendar_week(
    anchor: str = Query(description="YYYY-MM-DD;回該日所在週(週一起)"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    d = _parse_date(anchor, "anchor")
    return calendar_svc.build_week(db, tenant_id=current_user.tenant_id, anchor=d)


@router.get("/dashboard/today")
def dashboard_today(
    date: str | None = Query(default=None, description="YYYY-MM-DD;預設今天"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    tenant_id = current_user.tenant_id
    day = _parse_date(date, "date") or datetime.date.today()
    start, end = _day_start(day), _day_start(day + datetime.timedelta(days=1))

    reservations = booking_svc.list_reservations_enriched(
        db, tenant_id=tenant_id, date_from=start, date_to=end, limit=200
    )
    reservations.reverse()  # 當日由早到晚

    now = datetime.datetime.now()
    attendance_unmarked = sum(
        1 for r in reservations
        if r["status"] == RESERVATION_CONFIRMED
        and r["attended"] is None
        and r["slot_start"].replace(tzinfo=None) < now
    )
    waitlist_waiting = (
        tenant_query(db, WaitlistEntry, tenant_id)
        .filter(WaitlistEntry.status == WAITLIST_WAITING)
        .count()
    )
    deposits_pending = (
        tenant_query(db, Reservation, tenant_id)
        .filter(Reservation.deposit_status == "pending")
        .count()
    )

    return {
        "date": day.isoformat(),
        "reservations": [ReservationRow(**r).model_dump() for r in reservations],
        "summary": analytics_svc.booking_summary(
            db, tenant_id=tenant_id, date_from=start, date_to=end
        ),
        "revenue": analytics_svc.revenue_summary(
            db, tenant_id=tenant_id, date_from=start, date_to=end
        ),
        "pending": {
            "waitlist_waiting": waitlist_waiting,
            "deposits_pending": deposits_pending,
            "attendance_unmarked": attendance_unmarked,
        },
    }
