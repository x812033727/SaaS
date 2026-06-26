"""Order model — 顧客訂單（含狀態流轉）。

狀態：pending（待付）→ paid（已付）/ cancelled（取消，回補庫存）/ fulfilled（已完成）。
金額一律整數 cents。明細見 OrderItem（下單時快照單價）。
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
    text,
)

from saas_mvp.db import Base

ORDER_PENDING = "pending"
ORDER_PAID = "paid"
ORDER_CANCELLED = "cancelled"
ORDER_FULFILLED = "fulfilled"


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class Order(Base):
    __tablename__ = "orders"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(
        Integer,
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    customer_id = Column(
        Integer,
        ForeignKey("booking_customers.id", ondelete="SET NULL"),
        nullable=True,
    )
    line_user_id = Column(String(64), nullable=True, index=True)
    status = Column(
        String(16), nullable=False, default=ORDER_PENDING, server_default=ORDER_PENDING
    )
    total_cents = Column(Integer, nullable=False, default=0, server_default=text("0"))
    # 套用優惠券後折抵的金額（cents）；total_cents 為折抵後實付。
    discount_cents = Column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    # 套用的優惠券代碼（紀錄用，NULL = 未套券）。
    coupon_code = Column(String(64), nullable=True)
    currency = Column(String(8), nullable=False, default="TWD", server_default="TWD")
    # 金流交易編號（綠界 MerchantTradeNo，唯一）；回調以此對應訂單。
    # 既有 DB 由 _migrate_add_order_merchant_trade_no() 補欄。
    merchant_trade_no = Column(String(20), nullable=True, unique=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        onupdate=_utcnow,
    )
    paid_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_order_tenant_status", "tenant_id", "status"),
    )
