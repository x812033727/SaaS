"""ApiUsage model — per-tenant daily API call counter."""

from sqlalchemy import Column, Date, ForeignKey, Integer, UniqueConstraint, text
from sqlalchemy.orm import relationship

from saas_mvp.db import Base


class ApiUsage(Base):
    __tablename__ = "api_usage"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False, index=True)
    period = Column(Date, nullable=False)        # 計量日期（UTC date）
    count = Column(Integer, nullable=False, default=0)
    # 翻譯字數累計（與 count 獨立計量，獨立超額擋下）。
    # 雙保險：``default=0`` 給 ORM INSERT、``server_default=text("0")`` 給
    # DDL DEFAULT；後者確保 raw SQL INSERT（如測試塞配額）也會自動補 0，
    # 不撞 NOT NULL。讀取端 ``(row.char_count or 0)`` 為第三層防線，
    # 對接相容性 row。
    char_count = Column(
        Integer, nullable=False, default=0, server_default=text("0"),
    )

    tenant = relationship("Tenant")

    __table_args__ = (
        UniqueConstraint("tenant_id", "period", name="uq_usage_tenant_period"),
    )
