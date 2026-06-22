"""StaffLeave model — 員工請假 / 不可約時段（含起訖時間）。

status：'approved'（預設）/ 'pending' / 'rejected'；只有 approved 會阻擋指派。
"""

from __future__ import annotations

import datetime

from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
)

from saas_mvp.db import Base


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class StaffLeave(Base):
    __tablename__ = "booking_staff_leaves"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(
        Integer,
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    staff_id = Column(
        Integer,
        ForeignKey("booking_staff.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    start_at = Column(DateTime(timezone=True), nullable=False)
    end_at = Column(DateTime(timezone=True), nullable=False)
    reason = Column(Text, nullable=True)
    status = Column(
        String(16), nullable=False, default="approved", server_default="approved"
    )
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
