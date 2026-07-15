"""平台共用 OAuth 憑證（LINE Login / Google Login + Calendar）。

client secret 以與 LINE Bot 憑證相同的 Fernet 金鑰加密；資料庫設定優先於
環境變數，讓平台管理員可在後台更新且不需重啟服務。
"""

from __future__ import annotations

import datetime

from sqlalchemy import Column, DateTime, Integer, LargeBinary, String

from saas_mvp.db import Base
from saas_mvp.models.line_channel_config import decrypt_field, encrypt_field


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class PlatformOAuthConfig(Base):
    __tablename__ = "platform_oauth_configs"

    id = Column(Integer, primary_key=True)
    provider = Column(String(16), nullable=False, unique=True, index=True)
    client_id = Column(String(255), nullable=False)
    client_secret_enc = Column(LargeBinary, nullable=False)
    updated_by_user_id = Column(Integer, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    @property
    def client_secret(self) -> str:
        return decrypt_field(self.client_secret_enc)

    @client_secret.setter
    def client_secret(self, value: str) -> None:
        self.client_secret_enc = encrypt_field(value)
