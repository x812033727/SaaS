"""CouponRedemption model — 一筆優惠券核銷紀錄。

UniqueConstraint(coupon_id, line_user_id) 於 DB 層強制「一人一券」，
重複核銷會撞 IntegrityError，由 services/coupons 轉成 AlreadyRedeemedError。
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


class CouponRedemption(Base):
    __tablename__ = "coupon_redemptions"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(
        Integer,
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    coupon_id = Column(
        Integer,
        ForeignKey("coupons.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    customer_id = Column(
        Integer,
        ForeignKey("booking_customers.id", ondelete="SET NULL"),
        nullable=True,
    )
    line_user_id = Column(String(64), nullable=False)
    reservation_id = Column(
        Integer,
        ForeignKey("booking_reservations.id", ondelete="SET NULL"),
        nullable=True,
    )
    redeemed_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)

    __table_args__ = (
        UniqueConstraint("coupon_id", "line_user_id", name="uq_redemption_coupon_user"),
    )
