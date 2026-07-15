"""電子禮物卡／儲值金模型。

卡號只保存 SHA-256 雜湊與末四碼，避免資料庫外洩後可直接盜用。餘額由
append-only ledger 加總，不在主表保存可被覆寫的 balance。
"""

from __future__ import annotations

import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint

from saas_mvp.db import Base


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class GiftCard(Base):
    __tablename__ = "gift_cards"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    code_hash = Column(String(64), nullable=False)
    code_last4 = Column(String(4), nullable=False)
    recipient_customer_id = Column(
        Integer, ForeignKey("booking_customers.id", ondelete="SET NULL"), nullable=True, index=True
    )
    initial_value_cents = Column(Integer, nullable=False)
    status = Column(String(16), nullable=False, default="active", server_default="active")
    purchaser_name = Column(String(128), nullable=True)
    recipient_name = Column(String(128), nullable=True)
    message = Column(String(500), nullable=True)
    # 台灣有償商品（服務）禮券應揭露履約保障；發行當下保存快照供稽核。
    fulfillment_guarantee = Column(Text, nullable=False)
    issuance_key = Column(String(64), nullable=False)
    issued_by_user_id = Column(Integer, nullable=True)
    voided_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)

    __table_args__ = (
        UniqueConstraint("tenant_id", "code_hash", name="uq_gift_card_code_hash"),
        UniqueConstraint("tenant_id", "issuance_key", name="uq_gift_card_issuance_key"),
        Index("ix_gift_card_tenant_recipient_status", "tenant_id", "recipient_customer_id", "status"),
    )


class GiftCardLedger(Base):
    __tablename__ = "gift_card_ledger"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    gift_card_id = Column(Integer, ForeignKey("gift_cards.id", ondelete="RESTRICT"), nullable=False, index=True)
    customer_id = Column(Integer, ForeignKey("booking_customers.id", ondelete="SET NULL"), nullable=True, index=True)
    order_id = Column(Integer, ForeignKey("orders.id", ondelete="SET NULL"), nullable=True, index=True)
    delta_cents = Column(Integer, nullable=False)
    kind = Column(String(16), nullable=False)  # issue|redeem|refund|adjust
    note = Column(String(255), nullable=True)
    actor_user_id = Column(Integer, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)

    __table_args__ = (
        UniqueConstraint("tenant_id", "order_id", "kind", name="uq_gift_card_order_kind"),
        Index("ix_gift_card_ledger_balance", "tenant_id", "gift_card_id"),
    )
