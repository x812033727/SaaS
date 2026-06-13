"""ApiKey model — per-tenant API key，SHA-256 雜湊儲存。

Key 格式: myapp_ + token_urlsafe(32)，總長約 49 字元。
key_prefix: key[len(_KEY_PREFIX):len(_KEY_PREFIX)+8]（跳過固定 prefix，取隨機部分前 8 字元）。
key_hash: sha256(key).hexdigest()，unique index，無 salt（高熵 key 不需要）。
"""

from __future__ import annotations

import datetime
import hashlib
import secrets

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import relationship

from saas_mvp.db import Base

_KEY_PREFIX = "myapp_"


def generate_api_key() -> str:
    """生成新 API key: myapp_ + token_urlsafe(32)。"""
    return _KEY_PREFIX + secrets.token_urlsafe(32)


def hash_api_key(key: str) -> str:
    """SHA-256 雜湊（無 salt）。"""
    return hashlib.sha256(key.encode()).hexdigest()


def get_key_prefix(key: str) -> str:
    """取隨機部分前 8 字元（跳過固定 prefix，長度動態計算）。"""
    start = len(_KEY_PREFIX)
    return key[start:start + 8]


class ApiKey(Base):
    __tablename__ = "api_keys"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False, index=True)
    name = Column(String(128), nullable=False)
    key_prefix = Column(String(8), nullable=False, index=True)   # 隨機部分前 8 字元
    key_hash = Column(String(64), nullable=False, unique=True)   # sha256 hex
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.datetime.now(datetime.timezone.utc),
    )

    user = relationship("User", back_populates="api_keys")
    tenant = relationship("Tenant")
    usages = relationship("ApiKeyUsage", back_populates="api_key",
                          cascade="all, delete-orphan")
