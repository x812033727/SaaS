"""BookingSlot model — 可預約時段，同時是容量單位。

容量計數（booked_count）刻意 denormalize 在本列上，使容量檢查只需鎖**單一列**
（SELECT … FOR UPDATE），比照 quota.ApiUsage 的計數列鎖法消除超賣競態。

線上可用名額 = max_capacity - walkin_reserved - booked_count。
walkin_reserved：保留給現場客、不開放線上預約的名額（例：20 桌保留 5 → 線上最多 15）。

唯一約束：(tenant_id, slot_start) 一對一，避免同租戶同時段重複建立。
"""

from __future__ import annotations

import datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    UniqueConstraint,
    text,
)

from saas_mvp.db import Base


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class BookingSlot(Base):
    __tablename__ = "booking_slots"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(
        Integer,
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    slot_start = Column(DateTime(timezone=True), nullable=False, index=True)
    slot_end = Column(DateTime(timezone=True), nullable=True)
    max_capacity = Column(Integer, nullable=False)
    walkin_reserved = Column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    booked_count = Column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    is_active = Column(
        Boolean, nullable=False, default=True, server_default=text("1")
    )
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        onupdate=_utcnow,
    )

    __table_args__ = (
        UniqueConstraint("tenant_id", "slot_start", name="uq_booking_slot_start"),
    )

    @property
    def online_available(self) -> int:
        """線上仍可預約的名額（不低於 0）。"""
        return max(
            0,
            (self.max_capacity or 0)
            - (self.walkin_reserved or 0)
            - (self.booked_count or 0),
        )
