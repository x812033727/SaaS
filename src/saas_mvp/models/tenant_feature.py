"""TenantFeature model — per-tenant 進階功能權限（entitlement）。

列存在＝該租戶對該功能已「明確設定」；enabled 欄存實際開關值。
無列時的預設由 settings.features_default_enabled 決定（向後相容＝True）。

唯一約束：(tenant_id, feature) 一對一，upsert 更新即可。
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
    UniqueConstraint,
)

from saas_mvp.db import Base


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class TenantFeature(Base):
    __tablename__ = "tenant_features"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(
        Integer,
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    feature = Column(String(32), nullable=False)
    enabled = Column(Boolean, nullable=False)
    updated_by_user_id = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        onupdate=_utcnow,
    )

    __table_args__ = (
        UniqueConstraint("tenant_id", "feature", name="uq_tenant_feature"),
    )
