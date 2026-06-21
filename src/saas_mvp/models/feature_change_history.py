"""FeatureChangeHistory model — 進階功能開關的稽核軌跡（append-only，仿 PlanChangeHistory）。

每次訂閱/退訂/admin 覆寫各記一列，保留 who/when/source，不就地改寫。
"""

from __future__ import annotations

import datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
)

from saas_mvp.db import Base


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class FeatureChangeHistory(Base):
    __tablename__ = "feature_change_history"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(
        Integer,
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    feature = Column(String(32), nullable=False)
    enabled = Column(Boolean, nullable=False)
    changed_by_user_id = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    changed_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    source = Column(String(16), nullable=False)  # subscribe | unsubscribe | admin
    reason = Column(String(256), nullable=True)
