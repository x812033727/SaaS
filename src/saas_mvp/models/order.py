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
    # POS 以禮物卡實際折抵金額；total_cents 為仍需以現金／刷卡支付的餘額。
    gift_card_cents = Column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    # POS 點數折抵（與折扣分開保存，供成交淨額與抽成稽核）。
    points_cents = Column(Integer, nullable=False, default=0, server_default=text("0"))
    # POS 成交歸屬；reservation_id 至多結帳一次，避免重複收款。
    reservation_id = Column(
        Integer,
        ForeignKey("booking_reservations.id", ondelete="SET NULL"),
        nullable=True,
        unique=True,
    )
    staff_id = Column(
        Integer, ForeignKey("booking_staff.id", ondelete="SET NULL"), nullable=True, index=True
    )
    payment_method = Column(String(16), nullable=True)
    tip_cents = Column(Integer, nullable=False, default=0, server_default=text("0"))
    # 套用的優惠券代碼（紀錄用，NULL = 未套券）。
    coupon_code = Column(String(64), nullable=True)
    currency = Column(String(8), nullable=False, default="TWD", server_default="TWD")
    # 金流交易編號（綠界 MerchantTradeNo，唯一）；回調以此對應訂單。
    # 既有 DB 由 _migrate_add_order_merchant_trade_no() 補欄。
    merchant_trade_no = Column(String(20), nullable=True, unique=True)
    # LINE Pay Request API 回傳的 transactionId；confirm 時必須與 query string
    # 比對一致(txid↔order 綁定),防 txid/order 錯配或重放。
    payment_txn_id = Column(String(32), nullable=True, index=True)
    # 分店綁定（多分店，nullable = 不限分店）；歷史上由 _migrate_add_location_id()
    # 對舊 DB 補欄，model 現已宣告使 metadata 成為 schema 唯一真相（Alembic baseline）。
    location_id = Column(Integer, nullable=True)
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
        Index("ix_order_tenant_staff_paid", "tenant_id", "staff_id", "paid_at"),
    )
