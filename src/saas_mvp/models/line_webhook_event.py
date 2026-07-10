"""LINE webhook event metadata for idempotent processing."""

from __future__ import annotations

import datetime
import enum

from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)

from saas_mvp.db import Base


class LineWebhookEventStatus(str, enum.Enum):
    PENDING = "pending"
    PROCESSED = "processed"
    FAILED = "failed"


class LineWebhookEventStage(str, enum.Enum):
    CLAIMED = "claimed"
    QUOTA_CHECKED = "quota_checked"
    TRANSLATED = "translated"
    REPLY_SENT = "reply_sent"
    USAGE_INCREMENTED = "usage_incremented"


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class LineWebhookEvent(Base):
    __tablename__ = "line_webhook_events"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(
        Integer,
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    webhook_event_id = Column(String(64), nullable=False, index=True)
    status = Column(
        String(16),
        nullable=False,
        default=LineWebhookEventStatus.PENDING.value,
        server_default=LineWebhookEventStatus.PENDING.value,
    )
    attempt_count = Column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    last_error = Column(String(255), nullable=True)
    # 原始 event JSON（A0.2 outbox）：claim 時落盤，worker 中途死掉可由
    # ops/retry_stuck_webhook_events 重放。Alembic rev 0011 補欄。
    payload_json = Column(Text, nullable=True)
    last_stage = Column(String(32), nullable=True)
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        onupdate=_utcnow,
    )
    processed_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "webhook_event_id",
            name="uq_line_webhook_events_tenant_event",
        ),
    )
