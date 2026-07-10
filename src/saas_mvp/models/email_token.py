"""Email 用途 token（B3）— 驗證信 / 忘記密碼 / 邀請共用一張表。

安全：DB 只存 token 的 SHA-256 雜湊（token_hash），信件連結才帶明文 token；
DB 外洩不等於連結外洩（比照 password hash 慣例）。token 一次性（used_at）。
"""

from __future__ import annotations

import datetime
import hashlib
import secrets

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String

from saas_mvp.db import Base

PURPOSE_VERIFY = "verify"
PURPOSE_RESET = "reset"
PURPOSE_INVITE = "invite"  # B5 邀請成員預留
VALID_PURPOSES = frozenset({PURPOSE_VERIFY, PURPOSE_RESET, PURPOSE_INVITE})


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def generate_token() -> str:
    return secrets.token_urlsafe(32)


class EmailToken(Base):
    __tablename__ = "email_tokens"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    purpose = Column(String(16), nullable=False)
    token_hash = Column(String(64), unique=True, nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    used_at = Column(DateTime(timezone=True), nullable=True)
