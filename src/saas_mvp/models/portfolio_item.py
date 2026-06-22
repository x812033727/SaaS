"""PortfolioItem model — 作品集單張作品。

image_url 僅存外部 URL（不託管 blob）；category_id 為 nullable（未分類）。
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
    Text,
    text,
)

from saas_mvp.db import Base


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class PortfolioItem(Base):
    __tablename__ = "portfolio_items"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(
        Integer,
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    category_id = Column(Integer, nullable=True, index=True)
    image_url = Column(String(512), nullable=False)  # 外部 URL，不託管 blob
    caption = Column(Text, nullable=True)
    sort_order = Column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    is_active = Column(
        Boolean, nullable=False, default=True, server_default=text("1")
    )
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        onupdate=_utcnow,
    )
