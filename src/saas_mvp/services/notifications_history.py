"""通知與推播歷程查詢（後台 /ui/notifications 唯讀頁的薄查詢層）。

只讀既有資料表：booking_notifications（預約異動通知）、
marketing_campaign_sends（行銷發送紀錄）、push_usage（月度推播計量）。
所有查詢帶 tenant_id 強制隔離；分頁由呼叫端帶 limit/offset。
"""

from __future__ import annotations

import datetime

from sqlalchemy.orm import Session

from saas_mvp.models.booking_notification import BookingNotification
from saas_mvp.models.campaign_send import CampaignSend
from saas_mvp.models.push_usage import PushUsage
from saas_mvp.services.tenants import tenant_query


def list_booking_notifications(
    db: Session,
    *,
    tenant_id: int,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[BookingNotification], int]:
    """預約異動通知歷程（新→舊）；回傳 (rows, total)。"""
    q = tenant_query(db, BookingNotification, tenant_id)
    if status:
        q = q.filter(BookingNotification.status == status)
    total = q.count()
    rows = (
        q.order_by(BookingNotification.id.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    return rows, total


def list_campaign_sends(
    db: Session,
    *,
    tenant_id: int,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[CampaignSend], int]:
    """行銷發送紀錄（新→舊）；回傳 (rows, total)。"""
    q = tenant_query(db, CampaignSend, tenant_id)
    if status:
        q = q.filter(CampaignSend.status == status)
    total = q.count()
    rows = (
        q.order_by(CampaignSend.id.desc()).offset(offset).limit(limit).all()
    )
    return rows, total


def push_usage_history(
    db: Session,
    *,
    tenant_id: int,
    months: int = 6,
    now: datetime.datetime | None = None,
) -> list[dict]:
    """近 N 個月的推播用量（含當月；無計量列的月份補 0）。"""
    effective = now or datetime.datetime.now(datetime.timezone.utc)
    periods: list[str] = []
    year, month = effective.year, effective.month
    for _ in range(months):
        periods.append(f"{year:04d}{month:02d}")
        month -= 1
        if month == 0:
            year, month = year - 1, 12
    rows = {
        r.period: r.count or 0
        for r in tenant_query(db, PushUsage, tenant_id)
        .filter(PushUsage.period.in_(periods))
        .all()
    }
    return [
        {"period": p, "used": rows.get(p, 0)}
        for p in periods  # 新→舊
    ]
