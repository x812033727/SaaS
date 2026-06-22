"""Campaign model — 行銷自動化活動（PHASE 4-1）。

一個 Campaign 定義「對哪群顧客（segment_json / type）在何時（schedule_at）以何訊息
（message_template）發送、附帶何種獎勵（reward_type / reward_value）」。

type 語意：
  - birthday：當天生日的顧客（月/日相符）。
  - welcome：新顧客建檔即一次性歡迎（period_key='once'）。
  - spend：消費/集點達門檻觸發。
  - reactivation：久未回訪者（last_booked_at 早於 dormant 天數）。
  - broadcast：時間排程的群發（schedule_at <= now 觸發）。

實際發送（claim + 獎勵 + 推播）由 services/marketing.run_campaign 執行，並以
CampaignSend 的 UniqueConstraint 做冪等與上限控管。
"""

from __future__ import annotations

import datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    text,
)

from saas_mvp.db import Base

# type 常數
CAMPAIGN_BIRTHDAY = "birthday"
CAMPAIGN_WELCOME = "welcome"
CAMPAIGN_SPEND = "spend"
CAMPAIGN_REACTIVATION = "reactivation"
CAMPAIGN_BROADCAST = "broadcast"
VALID_CAMPAIGN_TYPES = frozenset(
    {
        CAMPAIGN_BIRTHDAY,
        CAMPAIGN_WELCOME,
        CAMPAIGN_SPEND,
        CAMPAIGN_REACTIVATION,
        CAMPAIGN_BROADCAST,
    }
)

# status 常數
CAMPAIGN_ACTIVE = "active"
CAMPAIGN_PAUSED = "paused"

# reward_type 常數
REWARD_COUPON = "coupon"
REWARD_POINTS = "points"
VALID_REWARD_TYPES = frozenset({REWARD_COUPON, REWARD_POINTS})


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class Campaign(Base):
    __tablename__ = "marketing_campaigns"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(
        Integer,
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name = Column(String(128), nullable=False)
    type = Column(String(16), nullable=False)  # birthday|welcome|spend|reactivation|broadcast
    status = Column(
        String(16), nullable=False, default=CAMPAIGN_ACTIVE, server_default=CAMPAIGN_ACTIVE
    )
    schedule_at = Column(DateTime(timezone=True), nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=True)
    # 傳給 segment_customers 的 JSON 篩選條件（tag_ids/tier/min_bookings/...）。
    segment_json = Column(Text, nullable=True)
    reward_type = Column(String(8), nullable=True)  # coupon | points
    reward_value = Column(Integer, nullable=True)  # coupon_id 或 points 數量
    message_template = Column(Text, nullable=False)
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
