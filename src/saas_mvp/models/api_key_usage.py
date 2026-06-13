"""ApiKeyUsage model — per-API-key 每日計量（獨立表）。

設計決策：
- 獨立於 ApiUsage，不修改既有 tenant-level 計量邏輯。
- UniqueConstraint("api_key_id", "period") — api_key_id 非 nullable，
  避免 SQLite/PG NULL≠NULL 的 UNIQUE 語義問題。
"""

from __future__ import annotations

from sqlalchemy import Column, Date, ForeignKey, Integer, UniqueConstraint
from sqlalchemy.orm import relationship

from saas_mvp.db import Base


class ApiKeyUsage(Base):
    __tablename__ = "api_key_usage"

    id = Column(Integer, primary_key=True, index=True)
    api_key_id = Column(Integer, ForeignKey("api_keys.id"), nullable=False, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False, index=True)
    period = Column(Date, nullable=False)   # UTC date
    count = Column(Integer, nullable=False, default=0)

    api_key = relationship("ApiKey", back_populates="usages")
    tenant = relationship("Tenant")

    __table_args__ = (
        UniqueConstraint("api_key_id", "period", name="uq_api_key_usage_key_period"),
    )
