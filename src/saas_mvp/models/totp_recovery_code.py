"""TOTP 一次性恢復碼(R5-D2)— 只存 sha256 雜湊,用過標記 used_at。"""

from __future__ import annotations

import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String

from saas_mvp.db import Base


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class TotpRecoveryCode(Base):
    __tablename__ = "totp_recovery_codes"

    id = Column(Integer, primary_key=True)
    user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    code_hash = Column(String(64), nullable=False)  # sha256 hex,不存明文
    used_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
