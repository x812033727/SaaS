"""PushUsage model — per-tenant 月度 LINE 推播計量器。

商業背景（vibeaico「Additional Push Notification Allowance」）：
每租戶每月有推播額度（base 200 則），可加購 +500 則（PUSH_BOOST 旗標）。
所有 LINE push 路徑（預約提醒、預約異動通知、行銷活動）共用此計量器。

結構比照 models/usage.py（ApiUsage）的 tenant_id × period 計量列，但 period
語意改為**月份**字串 'YYYYMM'（非每日 Date）。並發遞增由 services/push_quota.py
的 SELECT … FOR UPDATE upsert 序列化（比照 quota._get_or_create_usage_locked）。
"""

from __future__ import annotations

import datetime

from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import relationship

from saas_mvp.db import Base


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class PushUsage(Base):
    __tablename__ = "push_usage"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(
        Integer,
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    period = Column(String(6), nullable=False)  # 計量月份 'YYYYMM'（UTC）
    # 兩端 default 必須同時存在（缺一就壞）：
    #   default=0               → ORM INSERT 自動補 0
    #   server_default=text("0")→ DB DDL DEFAULT 0（raw SQL / ALTER 既有列回填）
    count = Column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        onupdate=_utcnow,
    )

    tenant = relationship("Tenant")

    __table_args__ = (
        UniqueConstraint("tenant_id", "period", name="uq_push_usage"),
    )
