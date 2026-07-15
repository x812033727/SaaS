"""FeatureSubscription model — 進階功能訂閱（綠界信用卡定期定額）。

每筆代表一次「店家訂閱某進階功能」的定期定額授權，對應綠界一組唯一
``merchant_trade_no``。首期授權成功（ReturnURL 回調 RtnCode==1）才將功能開通；
退訂時呼叫綠界 CreditCardPeriodAction 停扣後標記 cancelled。

僅在有效金流 provider 為 ecpay 時建立；stub 模式維持即時開通、不建此列。
"""

from __future__ import annotations

import datetime

from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    text,
)

from saas_mvp.db import Base

# 狀態常數
SUB_PENDING = "pending"        # 已建立、待首期授權
SUB_ACTIVE = "active"          # 首期授權成功、定期定額生效中
SUB_FAILED = "failed"          # 首期/某期授權失敗
SUB_CANCELLED = "cancelled"    # 已退訂且綠界停扣成功
SUB_CANCEL_FAILED = "cancel_failed"  # 已退訂但綠界停扣 API 失敗（待 ops 重試）


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class FeatureSubscription(Base):
    __tablename__ = "feature_subscriptions"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(
        Integer,
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    feature = Column(String(32), nullable=False)
    # 綠界唯一交易編號；回調以此對應訂閱。
    merchant_trade_no = Column(String(20), nullable=False, unique=True, index=True)
    status = Column(
        String(16), nullable=False, default=SUB_PENDING, server_default=SUB_PENDING
    )
    period_amount_cents = Column(Integer, nullable=False, default=0, server_default=text("0"))
    period_type = Column(String(1), nullable=False, default="M", server_default="M")
    frequency = Column(Integer, nullable=False, default=1, server_default=text("1"))
    exec_times = Column(Integer, nullable=False, default=99, server_default=text("99"))
    total_success_times = Column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    gwsr = Column(String(64), nullable=True)
    auth_code = Column(String(32), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )
    activated_at = Column(DateTime(timezone=True), nullable=True)
    last_charged_at = Column(DateTime(timezone=True), nullable=True)
    cancelled_at = Column(DateTime(timezone=True), nullable=True)
