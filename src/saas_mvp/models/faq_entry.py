"""FAQEntry model — 店家自訂常見問答（PHASE 4-1 AI 客服知識庫）。

每筆一組 question/answer，供 services/faq.match 以關鍵字比對挑出相關條目，
組裝成 AI 助手的 context（system prompt）。sort_order 控制顯示/比對排序。
"""

from __future__ import annotations

import datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    Text,
    text,
)

from saas_mvp.db import Base


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class FAQEntry(Base):
    __tablename__ = "faq_entries"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(
        Integer,
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    question = Column(Text, nullable=False)
    answer = Column(Text, nullable=False)
    is_active = Column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    sort_order = Column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        onupdate=_utcnow,
    )
