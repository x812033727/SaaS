"""Google Calendar 同步 outbox；每筆預約保留一個可覆寫的最新同步意圖。"""

from __future__ import annotations

import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, String, text

from saas_mvp.db import Base

GCAL_SYNC_PENDING = "pending"
GCAL_SYNC_SYNCED = "synced"
GCAL_SYNC_FAILED = "failed"
GCAL_SYNC_CANCELED = "canceled"

GCAL_ACTION_UPSERT = "upsert"
GCAL_ACTION_DELETE = "delete"


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class GcalSyncJob(Base):
    __tablename__ = "gcal_sync_jobs"

    id = Column(Integer, primary_key=True)
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
        unique=True,
    )
    action = Column(String(16), nullable=False)
    status = Column(
        String(16),
        nullable=False,
        default=GCAL_SYNC_PENDING,
        server_default=GCAL_SYNC_PENDING,
    )
    attempt_count = Column(Integer, nullable=False, default=0, server_default=text("0"))
    next_attempt_at = Column(DateTime(timezone=True), nullable=True, default=_utcnow)
    last_error = Column(String(255), nullable=True)
    synced_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    __table_args__ = (
        Index("ix_gcal_sync_job_status_due", "status", "next_attempt_at"),
    )
