"""ApiKey model — per-user API key with SHA-256 hash storage."""

from datetime import datetime, timezone

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import relationship

from saas_mvp.db import Base


class ApiKey(Base):
    __tablename__ = "api_keys"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False, index=True)
    name = Column(String(128), nullable=False)
    # 隨機部分前 8 字元（key[6:14]，跳過固定 prefix "myapp_"），用於縮小查詢候選集
    key_prefix = Column(String(8), nullable=False, index=True)
    # SHA-256 hexdigest，64 字元，唯一索引
    key_hash = Column(String(64), nullable=False, unique=True)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    user = relationship("User", back_populates="api_keys")
    tenant = relationship("Tenant")
