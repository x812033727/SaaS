"""平台共用金流設定；ECPay HashKey/HashIV 以 Fernet 加密保存。"""

from __future__ import annotations

import datetime

from sqlalchemy import Column, DateTime, Integer, LargeBinary, String

from saas_mvp.db import Base
from saas_mvp.models.line_channel_config import decrypt_field, encrypt_field


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class PlatformPaymentConfig(Base):
    __tablename__ = "platform_payment_configs"

    id = Column(Integer, primary_key=True)
    provider = Column(String(32), nullable=False, default="stub")
    environment = Column(String(16), nullable=False, default="stage")
    merchant_id = Column(String(64), nullable=False, default="")
    hash_key_enc = Column(LargeBinary, nullable=False)
    hash_iv_enc = Column(LargeBinary, nullable=False)
    updated_by_user_id = Column(Integer, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    @property
    def hash_key(self) -> str:
        return decrypt_field(self.hash_key_enc)

    @hash_key.setter
    def hash_key(self, value: str) -> None:
        self.hash_key_enc = encrypt_field(value)

    @property
    def hash_iv(self) -> str:
        return decrypt_field(self.hash_iv_enc)

    @hash_iv.setter
    def hash_iv(self, value: str) -> None:
        self.hash_iv_enc = encrypt_field(value)
