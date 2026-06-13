"""PlanChangeHistory model — append-only 帳單方案異動歷程表。

每次 Tenant.plan 變更時 insert 一筆，不覆蓋，方便稽核與回溯。
"""

from __future__ import annotations

import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import relationship

from saas_mvp.db import Base


class PlanChangeHistory(Base):
    __tablename__ = "plan_change_history"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False, index=True)
    from_plan = Column(String(32), nullable=False)
    to_plan = Column(String(32), nullable=False)
    # changed_by_user_id: API key 認證時填入 actor.user.id；nullable 保留彈性
    changed_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    changed_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.datetime.now(datetime.timezone.utc),
    )
    reason = Column(String(256), nullable=True)

    tenant = relationship("Tenant")
    changed_by = relationship("User", foreign_keys=[changed_by_user_id])
