"""Service model — 服務項目（服務目錄 / service catalog）。

category_id / location_id 皆 nullable（NULL = 未分類 / 不限分店）。
duration_minutes、price_cents 供前台呈現與時長計算；不在此處強制扣款。
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


class Service(Base):
    __tablename__ = "booking_services"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(
        Integer,
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    category_id = Column(Integer, nullable=True, index=True)
    name = Column(String(128), nullable=False)
    duration_minutes = Column(
        Integer, nullable=False, default=60, server_default=text("60")
    )
    price_cents = Column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    is_active = Column(
        Boolean, nullable=False, default=True, server_default=text("1")
    )
    location_id = Column(Integer, nullable=True, index=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        onupdate=_utcnow,
    )
