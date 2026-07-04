"""SubscriptionCharge model — 進階功能訂閱的逐期扣款明細（append-only）。

FeatureSubscription 只有 total_success_times / last_charged_at 聚合值,
無法呈現「哪一期、何時、成敗、金額」或對帳;本表由
services/subscriptions.py 的 activate / record_period / mark_failed
在同一交易內落列。以 (subscription_id, period_no, success) 查重,
防金流回調重放產生重複列。
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
)

from saas_mvp.db import Base


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class SubscriptionCharge(Base):
    __tablename__ = "subscription_charges"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(
        Integer,
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    subscription_id = Column(
        Integer,
        ForeignKey("feature_subscriptions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # 期數（首期 = 1;失敗期記「嘗試的期數」= 目前成功數 + 1）
    period_no = Column(Integer, nullable=False)
    success = Column(Boolean, nullable=False)
    amount_cents = Column(Integer, nullable=False)
    # 綠界授權單號 / 回應訊息（診斷用,可空）
    gwsr = Column(String(64), nullable=True)
    rtn_msg = Column(String(255), nullable=True)
    charged_at = Column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
