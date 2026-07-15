"""平台共用 SMTP 設定；密碼以 Fernet 加密保存。"""

from __future__ import annotations

import datetime

from sqlalchemy import Column, DateTime, Integer, LargeBinary, String

from saas_mvp.db import Base
from saas_mvp.models.line_channel_config import decrypt_field, encrypt_field


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class PlatformEmailConfig(Base):
    __tablename__ = "platform_email_configs"

    id = Column(Integer, primary_key=True)
    smtp_host = Column(String(255), nullable=False)
    smtp_port = Column(Integer, nullable=False, default=587)
    smtp_user = Column(String(255), nullable=False, default="")
    smtp_password_enc = Column(LargeBinary, nullable=False)
    smtp_from = Column(String(255), nullable=False)
    updated_by_user_id = Column(Integer, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    @property
    def smtp_password(self) -> str:
        return decrypt_field(self.smtp_password_enc)

    @smtp_password.setter
    def smtp_password(self, value: str) -> None:
        self.smtp_password_enc = encrypt_field(value)
