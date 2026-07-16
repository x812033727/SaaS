"""Google Calendar 授權憑證（E1 Step B）— refresh_token Fernet 加密落庫。"""

from __future__ import annotations

import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, LargeBinary, String

from saas_mvp.db import Base
from saas_mvp.models.line_channel_config import _get_fernet

GCAL_CONNECTED = "connected"
GCAL_ERROR = "error"


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class TenantGcalCredential(Base):
    __tablename__ = "tenant_gcal_credentials"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(
        Integer,
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    refresh_token_enc = Column(LargeBinary, nullable=False)
    calendar_id = Column(String(256), nullable=False, default="primary")
    google_email = Column(String(256), nullable=True)
    status = Column(String(16), nullable=False, default=GCAL_CONNECTED)
    last_error = Column(String(255), nullable=True)
    # 漂移偵測游標(R4-B3):上次 events.list 的 updatedMin;增量掃描用。rev 0046。
    last_drift_check_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    @property
    def refresh_token(self) -> str:
        return _get_fernet().decrypt(bytes(self.refresh_token_enc)).decode()

    @refresh_token.setter
    def refresh_token(self, value: str) -> None:
        self.refresh_token_enc = _get_fernet().encrypt(value.encode())
