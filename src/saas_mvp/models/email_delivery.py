"""可靠 Email 派送佇列；本文加密保存，避免驗證 token 明文落地。"""

from __future__ import annotations

import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, LargeBinary, String, text

from saas_mvp.db import Base
from saas_mvp.models.line_channel_config import decrypt_field, encrypt_field

EMAIL_PENDING = "pending"
EMAIL_SENT = "sent"
EMAIL_FAILED = "failed"
EMAIL_CANCELED = "canceled"


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class EmailDelivery(Base):
    __tablename__ = "email_deliveries"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    category = Column(String(32), nullable=False)
    recipient = Column(String(255), nullable=False)
    subject = Column(String(255), nullable=False)
    body_enc = Column(LargeBinary, nullable=False)
    status = Column(String(16), nullable=False, default=EMAIL_PENDING, server_default=EMAIL_PENDING)
    attempt_count = Column(Integer, nullable=False, default=0, server_default=text("0"))
    next_attempt_at = Column(DateTime(timezone=True), nullable=True, default=_utcnow)
    last_error = Column(String(255), nullable=True)
    sent_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow)

    __table_args__ = (Index("ix_email_delivery_status_due", "status", "next_attempt_at"),)

    @property
    def body(self) -> str:
        return decrypt_field(self.body_enc)

    @body.setter
    def body(self, value: str) -> None:
        self.body_enc = encrypt_field(value)
