"""每日租戶營運預聚合(R3-B3)。

報表趨勢原為請求時即時聚合(analytics.trend_series / reporting.revenue_trend),
資料量大時報表頁變慢。本表由 scheduler cron(ops/aggregate_daily_stats)每日
回填近幾天(吸收事後 attended 標記/補付款),讀取端缺日或當天 fallback 即時
計算、**request path 只讀不寫**。

口徑與 analytics.py 一致:預約以 slot_start 當日、僅列常用計數;營收以
paid_at 當日、僅計 ORDER_PAID(退款個案不扣)。
"""

from __future__ import annotations

import datetime

from sqlalchemy import (
    Column,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    UniqueConstraint,
)

from saas_mvp.db import Base


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class DailyTenantStat(Base):
    __tablename__ = "daily_tenant_stats"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(
        Integer,
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    stat_date = Column(Date, nullable=False)

    bookings_total = Column(Integer, nullable=False, default=0)
    bookings_confirmed = Column(Integer, nullable=False, default=0)
    bookings_cancelled = Column(Integer, nullable=False, default=0)
    covers = Column(Integer, nullable=False, default=0)  # confirmed 人數合計
    distinct_customers = Column(Integer, nullable=False, default=0)
    attended = Column(Integer, nullable=False, default=0)
    no_show = Column(Integer, nullable=False, default=0)
    paid_orders = Column(Integer, nullable=False, default=0)
    revenue_cents = Column(Integer, nullable=False, default=0)

    computed_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)

    __table_args__ = (
        UniqueConstraint("tenant_id", "stat_date", name="uq_daily_stat_tenant_date"),
    )
