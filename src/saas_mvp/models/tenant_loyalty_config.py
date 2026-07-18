"""Tenant loyalty 設定(R6-B3)— 每租戶會員分級門檻/折扣/集點率。

無此列 = 沿用全域 settings 預設(向後相容)。
"""

from __future__ import annotations

import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer

from saas_mvp.db import Base


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class TenantLoyaltyConfig(Base):
    __tablename__ = "tenant_loyalty_configs"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(
        Integer,
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    silver_threshold = Column(Integer, nullable=False, default=100, server_default="100")
    gold_threshold = Column(Integer, nullable=False, default=500, server_default="500")
    regular_discount_pct = Column(Integer, nullable=False, default=0, server_default="0")
    silver_discount_pct = Column(Integer, nullable=False, default=5, server_default="5")
    gold_discount_pct = Column(Integer, nullable=False, default=10, server_default="10")
    points_per_booking = Column(Integer, nullable=False, default=10, server_default="10")
    # R11-B:被推薦客首次到訪後,推薦人獲得的點數
    referral_points = Column(Integer, nullable=False, default=50, server_default="50")
    updated_by_user_id = Column(Integer, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )
