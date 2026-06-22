"""CampaignSend model — 行銷活動發送紀錄（append-only，PHASE 4-1）。

每筆代表「某活動對某顧客在某 period_key 期間的一次發送」。

冪等與上限控管核心：UniqueConstraint(campaign_id, customer_id, period_key)。
  - period_key 隨 type 語意決定：
      birthday/spend → 'YYYY'（每年一次）
      welcome        → 'once'（一輩子一次）
      reactivation   → 'YYYYMMDD'（每天一次）
      broadcast      → str(campaign_id) 或排程日（每活動一次）
  - run_campaign 先 INSERT claim，catch IntegrityError 即跳過（重跑/並發不重送），
    比照 reminders.enqueue_reminders 的去重手法。

status：pending（已 claim 未送）→ sent / failed。
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

# status 常數
CAMPAIGN_SEND_PENDING = "pending"
CAMPAIGN_SEND_SENT = "sent"
CAMPAIGN_SEND_FAILED = "failed"


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class CampaignSend(Base):
    __tablename__ = "marketing_campaign_sends"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(
        Integer,
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    campaign_id = Column(
        Integer,
        ForeignKey("marketing_campaigns.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    customer_id = Column(
        Integer,
        ForeignKey("booking_customers.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    line_user_id = Column(String(64), nullable=True)
    period_key = Column(String(32), nullable=False)
    status = Column(
        String(16),
        nullable=False,
        default=CAMPAIGN_SEND_PENDING,
        server_default=CAMPAIGN_SEND_PENDING,
    )
    reward_ref = Column(String(64), nullable=True)  # coupon redemption id / points tx ref
    sent_at = Column(DateTime(timezone=True), nullable=True)
    attempt_count = Column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    last_error = Column(String(255), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)

    __table_args__ = (
        UniqueConstraint(
            "campaign_id", "customer_id", "period_key", name="uq_campaign_send"
        ),
        Index("ix_campaign_send_campaign", "campaign_id", "period_key"),
    )
