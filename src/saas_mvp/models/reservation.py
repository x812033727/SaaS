"""Reservation model — 單筆預約。

容量計數維護在 BookingSlot.booked_count（建單 +party_size、取消 -party_size），
本表只記預約本身的狀態。line_user_id denormalize 在此，供 LINE「我的預約」查詢與
提醒推播直接取用，免再 join customer。

狀態：confirmed / cancelled（軟取消，保留歷史）。
"""

from __future__ import annotations

import datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)

from saas_mvp.db import Base

# 狀態常數（避免散落字串硬碼）
RESERVATION_CONFIRMED = "confirmed"
RESERVATION_CANCELLED = "cancelled"


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class Reservation(Base):
    __tablename__ = "booking_reservations"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(
        Integer,
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    slot_id = Column(
        Integer,
        ForeignKey("booking_slots.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # LINE 來源建單時回填；店家端手動建單可為 NULL。
    customer_id = Column(
        Integer,
        ForeignKey("booking_customers.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    line_user_id = Column(String(64), nullable=True, index=True)
    party_size = Column(
        Integer, nullable=False, default=1, server_default=text("1")
    )
    status = Column(
        String(16),
        nullable=False,
        default=RESERVATION_CONFIRMED,
        server_default=RESERVATION_CONFIRMED,
    )
    note = Column(Text, nullable=True)
    # 到場與否（P5 報表算爽約率用）；NULL=未標記，既有 DB 由 _migrate_add_reservation_attended() 補欄。
    attended = Column(Boolean, nullable=True)
    # PHASE 1：指派員工 / 服務項目（皆 nullable = 未指派 / 任意）；
    # 既有 DB 由 _migrate_add_reservation_staff_id() / _migrate_add_reservation_service_id() 補欄。
    staff_id = Column(Integer, nullable=True, index=True)
    service_id = Column(Integer, nullable=True, index=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        onupdate=_utcnow,
    )
    cancelled_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_reservation_tenant_status", "tenant_id", "status"),
    )
