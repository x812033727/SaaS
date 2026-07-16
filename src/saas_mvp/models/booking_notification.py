"""BookingNotification model — 預約異動通知派送佇列（append-only）。

店家在後台修改（reschedule）或取消預約時入列一筆 pending 通知；ops 腳本
send_due_notifications 掃描 send_after <= now 的 pending 列、鎖定後 LINE
push、標 sent。結構比照 reservation_reminder.py。

冪等三層防護：
  1. UniqueConstraint(reservation_id, kind) — 同預約同類型只會有一筆。
  2. ops 腳本 SELECT … FOR UPDATE + 鎖內重驗 status=='pending'。
  3. 推播成功後才標 sent + commit。

kind：change（異動）/ cancel（取消）。
狀態：pending / sent / failed / skipped。
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
    Text,
    UniqueConstraint,
    text,
)

from saas_mvp.db import Base

# kind 常數
NOTIFY_CHANGE = "change"
NOTIFY_CANCEL = "cancel"
NOTIFY_REFUND = "deposit_refund"  # 定金退款成功通知(R3-A3)

# status 常數
NOTIFY_PENDING = "pending"
NOTIFY_SENT = "sent"
NOTIFY_FAILED = "failed"
NOTIFY_SKIPPED = "skipped"


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class BookingNotification(Base):
    __tablename__ = "booking_notifications"

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
    kind = Column(String(16), nullable=False)  # change | cancel
    status = Column(
        String(16),
        nullable=False,
        default=NOTIFY_PENDING,
        server_default=NOTIFY_PENDING,
    )
    payload_text = Column(Text, nullable=False)
    # 可延後派送（預設立即）；ops 掃描 send_after <= now。
    send_after = Column(DateTime(timezone=True), nullable=True, default=_utcnow)
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
        UniqueConstraint("reservation_id", "kind", name="uq_booking_notification"),
        Index("ix_booking_notification_status_due", "status", "send_after"),
    )
