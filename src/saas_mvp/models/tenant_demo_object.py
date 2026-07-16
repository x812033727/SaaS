"""示範資料追蹤(R4-B4)— 記錄「一鍵示範資料」建立了哪些物件,供精準清除。

一鍵載入示範資料會建立 service / slot / customer / reservation 各若干筆;每筆在此
留一行 (object_type, object_id)。清除時只刪本表登記的物件,絕不誤刪店家真實資料;
被真實預約引用到的示範物件會保留(避免 FK 連鎖刪真資料),並在結果回報保留數。
"""

from __future__ import annotations

import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, String

from saas_mvp.db import Base

DEMO_SERVICE = "service"
DEMO_SLOT = "slot"
DEMO_CUSTOMER = "customer"
DEMO_RESERVATION = "reservation"


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class TenantDemoObject(Base):
    __tablename__ = "tenant_demo_objects"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(
        Integer,
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # service | slot | customer | reservation
    object_type = Column(String(16), nullable=False)
    # 對應資料表的主鍵(booking_services / booking_slots / booking_customers /
    # booking_reservations)。刻意不設 FK:物件被安全刪除後此列亦一併清掉。
    object_id = Column(Integer, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)

    __table_args__ = (
        Index("ix_tenant_demo_objects_tenant_type", "tenant_id", "object_type"),
    )
