"""TenantEinvoiceConfig — 店家自有綠界電子發票憑證(R5-C2,opt-in)。

店家對「自己的顧客」開發票(訂單/定金),用的是**店家自己的發票商店憑證**——
與平台對店家開月費發票的平台級憑證(platform_invoice_config)完全分離,
不可混用。HashKey/HashIV 以 Fernet 加密存放(比照 line_config)。

enabled=False 或憑證不齊 → 完全不開票(現狀行為;純 opt-in)。
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
)

from saas_mvp.db import Base
from saas_mvp.models.line_channel_config import decrypt_field, encrypt_field


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class TenantEinvoiceConfig(Base):
    __tablename__ = "tenant_einvoice_configs"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(
        Integer,
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    merchant_id = Column(String(20), nullable=False, default="")
    hash_key_enc = Column(LargeBinary, nullable=True)
    hash_iv_enc = Column(LargeBinary, nullable=True)
    environment = Column(String(8), nullable=False, default="stage")  # stage|prod
    enabled = Column(Boolean, nullable=False, default=False)
    updated_by_user_id = Column(Integer, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    @property
    def hash_key(self) -> str:
        return decrypt_field(self.hash_key_enc) if self.hash_key_enc else ""

    @hash_key.setter
    def hash_key(self, value: str) -> None:
        self.hash_key_enc = encrypt_field(value) if value else None

    @property
    def hash_iv(self) -> str:
        return decrypt_field(self.hash_iv_enc) if self.hash_iv_enc else ""

    @hash_iv.setter
    def hash_iv(self, value: str) -> None:
        self.hash_iv_enc = encrypt_field(value) if value else None

    @property
    def is_complete(self) -> bool:
        """憑證三要素齊備(可真開票)。"""
        return bool(self.merchant_id and self.hash_key_enc and self.hash_iv_enc)
