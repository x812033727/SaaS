"""ServiceStaff model — 服務項目 ↔ 員工 多對多指派。

唯一約束：(service_id, staff_id) 避免重複指派。
"""

from __future__ import annotations

from sqlalchemy import (
    Column,
    ForeignKey,
    Integer,
    UniqueConstraint,
)

from saas_mvp.db import Base


class ServiceStaff(Base):
    __tablename__ = "booking_service_staff"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(
        Integer,
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    service_id = Column(
        Integer,
        ForeignKey("booking_services.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    staff_id = Column(
        Integer,
        ForeignKey("booking_staff.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    __table_args__ = (
        UniqueConstraint("service_id", "staff_id", name="uq_service_staff"),
    )
