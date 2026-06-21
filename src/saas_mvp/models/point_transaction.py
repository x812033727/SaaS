"""PointTransaction model — 會員點數異動帳本（append-only）。

每次集點/扣點各記一列（delta 正=earn、負=redeem），Customer.points_balance 為彙總值。
帳本保留稽核軌跡，不就地改寫。
"""

from __future__ import annotations

import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String

from saas_mvp.db import Base


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class PointTransaction(Base):
    __tablename__ = "point_transactions"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(
        Integer,
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    customer_id = Column(
        Integer,
        ForeignKey("booking_customers.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    delta = Column(Integer, nullable=False)  # 正=earn / 負=redeem
    reason = Column(String(64), nullable=False)
    reservation_id = Column(
        Integer,
        ForeignKey("booking_reservations.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
