"""Analytics router — 店家端報表（摘要/時段使用率/Top 顧客/CSV 匯出）。

受認證 + 租戶隔離 + rate limit；不掛 require_quota。
"""

from __future__ import annotations

import csv
import datetime
import io

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from saas_mvp.deps import get_current_user, get_db, require_rate_limit
from saas_mvp.models.user import User
from saas_mvp.services import analytics as analytics_svc
from saas_mvp.services import reporting as reporting_svc
from saas_mvp.services.features import ADVANCED_REPORTING, require_feature

router = APIRouter(
    prefix="/booking/analytics",
    tags=["booking-analytics"],
    dependencies=[Depends(require_rate_limit)],
)


class SummaryResponse(BaseModel):
    total: int
    confirmed: int
    cancelled: int
    cancel_rate: float
    total_covers: int
    distinct_customers: int
    attended: int
    no_show: int
    no_show_rate: float | None


@router.get("/summary", response_model=SummaryResponse)
def summary(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    date_from: datetime.datetime | None = Query(default=None),
    date_to: datetime.datetime | None = Query(default=None),
) -> SummaryResponse:
    return SummaryResponse(
        **analytics_svc.booking_summary(
            db, tenant_id=current_user.tenant_id, date_from=date_from, date_to=date_to
        )
    )


@router.get("/utilization")
def utilization(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    date_from: datetime.datetime | None = Query(default=None),
    date_to: datetime.datetime | None = Query(default=None),
) -> list[dict]:
    return analytics_svc.slot_utilization(
        db, tenant_id=current_user.tenant_id, date_from=date_from, date_to=date_to
    )


@router.get("/customers")
def customers(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    limit: int = Query(default=10, ge=1, le=100),
) -> list[dict]:
    rows = analytics_svc.top_customers(
        db, tenant_id=current_user.tenant_id, limit=limit
    )
    return [
        {
            "id": c.id,
            "display_name": c.display_name,
            "line_user_id": c.line_user_id,
            "booking_count": c.booking_count,
            "points_balance": c.points_balance,
            "tier": c.tier,
        }
        for c in rows
    ]


_CSV_FIELDS = [
    "reservation_id", "slot_start", "party_size", "status",
    "line_user_id", "attended", "created_at",
]


@router.get("/export.csv")
def export_csv(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    date_from: datetime.datetime | None = Query(default=None),
    date_to: datetime.datetime | None = Query(default=None),
) -> Response:
    rows = analytics_svc.export_rows(
        db, tenant_id=current_user.tenant_id, date_from=date_from, date_to=date_to
    )
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=_CSV_FIELDS)
    writer.writeheader()
    for r in rows:
        writer.writerow(r)
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=reservations.csv"},
    )


# ── 進階報表（PHASE 4-2，ADVANCED_REPORTING 閘門） ────────────────────────────

_XLSX_MEDIA = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@router.get(
    "/report/popular-services",
    dependencies=[Depends(require_feature(ADVANCED_REPORTING))],
)
def report_popular_services(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    date_from: datetime.datetime | None = Query(default=None),
    date_to: datetime.datetime | None = Query(default=None),
    location_id: int | None = Query(default=None),
) -> list[dict]:
    return reporting_svc.popular_services(
        db, tenant_id=current_user.tenant_id,
        date_from=date_from, date_to=date_to, location_id=location_id,
    )


@router.get(
    "/report/staff-performance",
    dependencies=[Depends(require_feature(ADVANCED_REPORTING))],
)
def report_staff_performance(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    date_from: datetime.datetime | None = Query(default=None),
    date_to: datetime.datetime | None = Query(default=None),
    location_id: int | None = Query(default=None),
) -> list[dict]:
    return reporting_svc.staff_performance(
        db, tenant_id=current_user.tenant_id,
        date_from=date_from, date_to=date_to, location_id=location_id,
    )


@router.get(
    "/report/revenue-trend",
    dependencies=[Depends(require_feature(ADVANCED_REPORTING))],
)
def report_revenue_trend(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    date_from: datetime.datetime | None = Query(default=None),
    date_to: datetime.datetime | None = Query(default=None),
    location_id: int | None = Query(default=None),
) -> list[dict]:
    return reporting_svc.revenue_trend(
        db, tenant_id=current_user.tenant_id,
        date_from=date_from, date_to=date_to, location_id=location_id,
    )


@router.get(
    "/report/return-rate",
    dependencies=[Depends(require_feature(ADVANCED_REPORTING))],
)
def report_return_rate(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    date_from: datetime.datetime | None = Query(default=None),
    date_to: datetime.datetime | None = Query(default=None),
    location_id: int | None = Query(default=None),
) -> dict:
    return reporting_svc.return_rate(
        db, tenant_id=current_user.tenant_id,
        date_from=date_from, date_to=date_to, location_id=location_id,
    )


def _report_rows(
    db: Session, tenant_id: int,
    date_from: datetime.datetime | None,
    date_to: datetime.datetime | None,
    location_id: int | None,
) -> list[dict]:
    """匯出用扁平列：熱門服務排名（穩定、可讀的表格內容）。"""
    return reporting_svc.popular_services(
        db, tenant_id=tenant_id,
        date_from=date_from, date_to=date_to, location_id=location_id,
    )


@router.get(
    "/report.xlsx",
    dependencies=[Depends(require_feature(ADVANCED_REPORTING))],
)
def report_xlsx(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    date_from: datetime.datetime | None = Query(default=None),
    date_to: datetime.datetime | None = Query(default=None),
    location_id: int | None = Query(default=None),
) -> Response:
    rows = _report_rows(db, current_user.tenant_id, date_from, date_to, location_id)
    content = reporting_svc.to_xlsx(rows, sheet_title="PopularServices")
    return Response(
        content=content,
        media_type=_XLSX_MEDIA,
        headers={"Content-Disposition": "attachment; filename=report.xlsx"},
    )


@router.get(
    "/report.pdf",
    dependencies=[Depends(require_feature(ADVANCED_REPORTING))],
)
def report_pdf(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    date_from: datetime.datetime | None = Query(default=None),
    date_to: datetime.datetime | None = Query(default=None),
    location_id: int | None = Query(default=None),
) -> Response:
    rows = _report_rows(db, current_user.tenant_id, date_from, date_to, location_id)
    content = reporting_svc.to_pdf(rows, title="Popular Services")
    return Response(
        content=content,
        media_type="application/pdf",
        headers={"Content-Disposition": "attachment; filename=report.pdf"},
    )
