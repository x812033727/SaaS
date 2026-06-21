"""Coupon model — 店家優惠券/票券。

核銷（redeem）採原子流程：services/coupons.redeem_coupon 對本列 SELECT … FOR UPDATE，
鎖內重驗有效性與 redeemed_count < max_redemptions 後遞增，消除超發競態。
一人一券由 CouponRedemption 的 UniqueConstraint(coupon_id, line_user_id) 於 DB 層擋下。
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
    UniqueConstraint,
    text,
)

from saas_mvp.db import Base

# discount_type 常數
DISCOUNT_PERCENT = "percent"
DISCOUNT_AMOUNT = "amount"
VALID_DISCOUNT_TYPES = frozenset({DISCOUNT_PERCENT, DISCOUNT_AMOUNT})


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class Coupon(Base):
    __tablename__ = "coupons"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(
        Integer,
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    code = Column(String(64), nullable=False)
    name = Column(String(128), nullable=False)
    discount_type = Column(String(16), nullable=False)  # percent | amount
    discount_value = Column(Integer, nullable=False)  # percent: 0-100；amount: cents
    max_redemptions = Column(Integer, nullable=True)  # NULL = 不限
    redeemed_count = Column(Integer, nullable=False, default=0, server_default=text("0"))
    active_from = Column(DateTime(timezone=True), nullable=True)
    active_until = Column(DateTime(timezone=True), nullable=True)
    is_active = Column(Boolean, nullable=False, default=True, server_default=text("1"))
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        onupdate=_utcnow,
    )

    __table_args__ = (
        UniqueConstraint("tenant_id", "code", name="uq_coupon_tenant_code"),
    )
