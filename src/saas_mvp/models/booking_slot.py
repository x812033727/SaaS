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
    # 候補容量保留(R4-B1):目前有效 offer 為候補者保留的人數合計。可用量公式
    # 一律扣掉 held_count,讓非 offeree 訂不到被保留的名額。不變量:held_count
    # == SUM(hold_party_size) of 該 slot 上 status=notified 的 waitlist entries。
    # rev 0044。
    held_count = Column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    is_active = Column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    # 分店綁定（多分店，nullable = 不限分店）；歷史上由 _migrate_add_location_id()
    # 對舊 DB 補欄，model 現已宣告使 metadata 成為 schema 唯一真相（Alembic baseline）。
    location_id = Column(Integer, nullable=True)
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
        """線上仍可預約的名額（不低於 0）。

        R4-B1:扣掉 held_count(已為候補者保留、限時內的名額),讓公開池只顯示
        真正可搶的量。offeree 自己建單時走 booking 服務的特例把 own_hold 加回。
        """
        return max(
            0,
            (self.max_capacity or 0)
            - (self.walkin_reserved or 0)
            - (self.booked_count or 0)
            - (self.held_count or 0),
        )
