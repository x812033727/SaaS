"""CustomerTag model — 顧客標籤（每租戶自訂分眾標籤）。

店家可建立任意數量的標籤（如「VIP」「常客」「過敏」），再透過
CustomerTagLink 多對多掛到顧客上，供 segment_customers 分眾targeting，
Phase 4 行銷自動化會重用本標籤體系。

唯一約束：(tenant_id, name) 一對一，同租戶不可重複同名標籤。
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
)

from saas_mvp.db import Base


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class CustomerTag(Base):
    __tablename__ = "customer_tags"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(
        Integer,
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name = Column(String(64), nullable=False)
    # 顯示色（hex 或 LINE 色名）；NULL = 不指定。
    color = Column(String(16), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)

    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uq_customer_tag_name"),
    )
