"""FlexMenu model — LINE Flex 圖文選單卡片（carousel）容器。

一個租戶可有多個 FlexMenu（具名/可命名）；卡片掛在 FlexMenuCard。
delivery 時取 is_active 的選單組成 LINE Flex carousel 訊息。
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
    text,
)

from saas_mvp.db import Base


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class FlexMenu(Base):
    __tablename__ = "flex_menus"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(
        Integer,
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    title = Column(String(128), nullable=True)
    is_active = Column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        onupdate=_utcnow,
    )
