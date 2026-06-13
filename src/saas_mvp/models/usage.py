"""ApiUsage model — per-tenant daily API call counter."""

from sqlalchemy import Column, Date, ForeignKey, Integer, UniqueConstraint
from sqlalchemy.orm import relationship

from saas_mvp.db import Base


class ApiUsage(Base):
    __tablename__ = "api_usage"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False, index=True)
    period = Column(Date, nullable=False)        # 計量日期（UTC date）
    count = Column(Integer, nullable=False, default=0)

    tenant = relationship("Tenant")

    __table_args__ = (
        UniqueConstraint("tenant_id", "period", name="uq_usage_tenant_period"),
    )
