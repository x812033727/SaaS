"""ReservationReminder model — 預約提醒派送佇列。

建單時入列 day_before / day_of 兩筆 pending；ops 腳本 send_due_reminders 掃描
remind_at <= now 的 pending 列、鎖定後推播、標 sent。

冪等三層防護：
  1. UniqueConstraint(reservation_id, kind) — 同預約同類型只會有一筆，無法重複入列。
  2. ops 腳本 SELECT … FOR UPDATE + 鎖內重驗 status=='pending' — 並發掃描不重送。
  3. 推播成功後才標 sent + commit。

狀態：pending / sent / failed / skipped（取消預約時對應 reminder 標 skipped）。
"""

from __future__ import annotations

import datetime

from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)

from saas_mvp.db import Base

# kind 常數
REMINDER_DAY_BEFORE = "day_before"
REMINDER_DAY_OF = "day_of"

# status 常數
REMINDER_PENDING = "pending"
REMINDER_SENT = "sent"
REMINDER_FAILED = "failed"
REMINDER_SKIPPED = "skipped"


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class ReservationReminder(Base):
    __tablename__ = "booking_reservation_reminders"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(
        Integer,
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    reservation_id = Column(
        Integer,
        ForeignKey("booking_reservations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    line_user_id = Column(String(64), nullable=False)
    kind = Column(String(16), nullable=False)  # day_before | day_of
    remind_at = Column(DateTime(timezone=True), nullable=False)
    status = Column(
        String(16),
        nullable=False,
        default=REMINDER_PENDING,
        server_default=REMINDER_PENDING,
    )
    sent_at = Column(DateTime(timezone=True), nullable=True)
    attempt_count = Column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    last_error = Column(String(255), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        onupdate=_utcnow,
    )

    __table_args__ = (
        UniqueConstraint("reservation_id", "kind", name="uq_reminder_resv_kind"),
        Index("ix_reminder_status_due", "status", "remind_at"),
    )
