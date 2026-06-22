"""ServiceCategory model — 服務項目分類（服務目錄 / service catalog）。"""

from __future__ import annotations

from sqlalchemy import (
    Boolean,
    Column,
    ForeignKey,
    Integer,
    String,
    text,
)

from saas_mvp.db import Base


class ServiceCategory(Base):
    __tablename__ = "booking_service_categories"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(
        Integer,
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name = Column(String(128), nullable=False)
    sort_order = Column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    is_active = Column(
        Boolean, nullable=False, default=True, server_default=text("1")
    )
