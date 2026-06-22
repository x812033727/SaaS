"""CustomerTagLink model — 顧客 ⇄ 標籤 多對多關聯。

每筆 = 一個顧客掛上一個標籤。tenant_id denormalize 在此，使 tenant_query
能直接套租戶 filter（不必 join customer / tag）。

唯一約束：(customer_id, tag_id) 一對一，同顧客同標籤只會有一筆（attach 冪等）。
"""

from __future__ import annotations

import datetime

from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Integer,
    UniqueConstraint,
)

from saas_mvp.db import Base


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class CustomerTagLink(Base):
    __tablename__ = "customer_tag_links"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(
        Integer,
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    customer_id = Column(
        Integer,
        ForeignKey("booking_customers.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    tag_id = Column(
        Integer,
        ForeignKey("customer_tags.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)

    __table_args__ = (
        UniqueConstraint("customer_id", "tag_id", name="uq_customer_tag"),
    )
