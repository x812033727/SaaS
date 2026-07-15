"""平台共用 AI 供應商設定；API key 以 Fernet 加密保存。"""

from __future__ import annotations

import datetime

from sqlalchemy import Column, DateTime, Integer, LargeBinary, String

from saas_mvp.db import Base
from saas_mvp.models.line_channel_config import decrypt_field, encrypt_field


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class PlatformAIConfig(Base):
    __tablename__ = "platform_ai_configs"

    id = Column(Integer, primary_key=True)
    provider = Column(String(32), nullable=False, unique=True, default="anthropic")
    api_key_enc = Column(LargeBinary, nullable=False)
    model = Column(String(128), nullable=False)
    updated_by_user_id = Column(Integer, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    @property
    def api_key(self) -> str:
        return decrypt_field(self.api_key_enc)

    @api_key.setter
    def api_key(self, value: str) -> None:
        self.api_key_enc = encrypt_field(value)
