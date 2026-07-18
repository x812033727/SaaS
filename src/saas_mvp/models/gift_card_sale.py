"""禮物卡線上販售(R11-A):每租戶販售設定 + 購買紀錄。

TenantGiftCardConfig — 無此列 = 未開賣。denominations 存 JSON 陣列(元)。
GiftCardPurchase — 一次購買=一張 Order(order_id 唯一);付款成功的
callback 於同一交易發卡並把明碼加密存入 code_enc,供成功頁與交付信
重覆取用(issue_card 的明碼只在首次發卡回傳一次)。
"""

from __future__ import annotations

import datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
)

from saas_mvp.db import Base
from saas_mvp.models.line_channel_config import decrypt_field, encrypt_field

PURCHASE_PENDING = "pending"
PURCHASE_ISSUED = "issued"


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class TenantGiftCardConfig(Base):
    __tablename__ = "tenant_gift_card_configs"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(
        Integer,
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    # migration 端 server_default 必用 sa.false()(R5-0050 事故:
    # sa.text("0") SQLite 容忍/PG 拒絕)
    online_sale_enabled = Column(Boolean, nullable=False, default=False)
    # JSON 陣列(元),如 [500, 1000, 2000];驗證於 service 層
    denominations = Column(Text, nullable=True)
    # 禮券履約保障揭露(法規);啟用販售時必填 10-2000 字
    fulfillment_guarantee = Column(Text, nullable=True)
    updated_by_user_id = Column(Integer, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )


class GiftCardPurchase(Base):
    __tablename__ = "gift_card_purchases"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(
        Integer,
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    order_id = Column(
        Integer,
        ForeignKey("orders.id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    )
    gift_card_id = Column(
        Integer,
        ForeignKey("gift_cards.id", ondelete="SET NULL"),
        nullable=True,
    )
    amount_cents = Column(Integer, nullable=False)
    purchaser_name = Column(String(128), nullable=True)
    purchaser_email = Column(String(256), nullable=False)
    recipient_name = Column(String(128), nullable=True)
    message = Column(String(500), nullable=True)
    status = Column(
        String(16), nullable=False, default=PURCHASE_PENDING,
        server_default=PURCHASE_PENDING,
    )
    # 發卡當下的明碼(Fernet 加密):成功頁/交付信要能重覆顯示
    code_enc = Column(LargeBinary, nullable=True)
    email_queued_at = Column(DateTime(timezone=True), nullable=True)
    issued_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)

    @property
    def plain_code(self) -> str | None:
        if not self.code_enc:
            return None
        try:
            return decrypt_field(self.code_enc)
        except Exception:  # noqa: BLE001 — 金鑰輪替等解密失敗不可 500 公開頁
            return None

    def set_plain_code(self, code: str) -> None:
        self.code_enc = encrypt_field(code)
