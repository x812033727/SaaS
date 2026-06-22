"""Location model — 分店（多分店 / multi-location）。

每租戶可開多家分店；slots / reservations / customers / orders 之 location_id
為 nullable（NULL = 未指定分店 / 任意，向後相容既有單店資料）。

啟用中分店數受 settings.max_locations_per_tenant 上限控管（service 層強制）。
"""

from __future__ import annotations

import datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    text,
)

from saas_mvp.db import Base


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class Location(Base):
    __tablename__ = "booking_locations"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(
        Integer,
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name = Column(String(128), nullable=False)
    address = Column(String(255), nullable=True)
    phone = Column(String(32), nullable=True)
    timezone = Column(
        String(64), nullable=True, default="Asia/Taipei", server_default="Asia/Taipei"
    )
    is_active = Column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        onupdate=_utcnow,
    )
