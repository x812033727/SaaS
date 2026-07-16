"""Platform-wide encrypted error-monitoring configuration."""

from __future__ import annotations

import datetime

from sqlalchemy import Column, DateTime, Integer, LargeBinary

from saas_mvp.db import Base
from saas_mvp.models.line_channel_config import decrypt_field, encrypt_field


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class PlatformObservabilityConfig(Base):
    __tablename__ = "platform_observability_configs"

    id = Column(Integer, primary_key=True)
    sentry_dsn_enc = Column(LargeBinary, nullable=False)
    updated_by_user_id = Column(Integer, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    @property
    def sentry_dsn(self) -> str:
        return decrypt_field(self.sentry_dsn_enc)

    @sentry_dsn.setter
    def sentry_dsn(self, value: str) -> None:
        self.sentry_dsn_enc = encrypt_field(value)
