"""WaitlistEntry model — 額滿時段的候補登記。

顧客在 LINE 端遇到時段額滿時可登記候補；取消/改期回補容量時，
服務層（services/waitlist.py）在同一交易鎖內挑出第一位符合人數的
waiting 候補標為 notified，commit 後 best-effort 推播「立即預約」按鈕。

狀態：waiting（等候中）/ notified（已通知，等顧客自行預約）/
booked（已完成補位）/ expired（通知逾時）/ cancelled（取消候補）。
同一 (slot, line_user) 僅一列
（UniqueConstraint；重複登記走 reactivate 冪等）。
"""

from __future__ import annotations

import datetime

from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)

from saas_mvp.db import Base

# 狀態常數（避免散落字串硬碼）
WAITLIST_WAITING = "waiting"
WAITLIST_NOTIFIED = "notified"
WAITLIST_BOOKED = "booked"
WAITLIST_EXPIRED = "expired"
WAITLIST_CANCELLED = "cancelled"


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class WaitlistEntry(Base):
    __tablename__ = "booking_waitlist_entries"

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
    line_user_id = Column(String(64), nullable=False, index=True)
    display_name = Column(String(255), nullable=True)
    party_size = Column(Integer, nullable=False, default=1)
    status = Column(
        String(16),
        nullable=False,
        default=WAITLIST_WAITING,
        server_default=WAITLIST_WAITING,
        index=True,
    )
    created_at = Column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at = Column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )
    notified_at = Column(DateTime(timezone=True), nullable=True)
    # 候補名額提醒有限時，逾時後排程會轉 expired 並遞補下一位。
    offer_expires_at = Column(DateTime(timezone=True), nullable=True)
    notification_attempts = Column(Integer, nullable=False, default=0, server_default="0")
    # 保留顧客原本選擇，從候補通知完成預約時不會遺失服務／員工。
    service_id = Column(Integer, nullable=True)
    staff_id = Column(Integer, nullable=True)
    reservation_id = Column(
        Integer,
        ForeignKey("booking_reservations.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    __table_args__ = (
        UniqueConstraint(
            "slot_id",
            "line_user_id",
            name="uq_waitlist_slot_line_user",
        ),
    )
