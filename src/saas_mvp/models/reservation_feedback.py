"""預約後滿意度調查（A3.3）。

服務結束後 cron 推 1–5 分 quick-reply；顧客點分數 → webhook `rate` action 寫回。
一預約一筆（reservation_id unique）：requested_at = 已發問卷、responded_at = 已回。
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


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class ReservationFeedback(Base):
    __tablename__ = "reservation_feedback"

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
    )
    line_user_id = Column(String(64), nullable=False)
    # 1–5;NULL = 已發問卷未回。
    score = Column(Integer, nullable=True)
    comment = Column(String(500), nullable=True)
    requested_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    responded_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("reservation_id", name="uq_reservation_feedback"),
    )
