"""AutoReplyRule model — LINE 自動回覆關鍵字規則。"""

from __future__ import annotations

import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    text,
)

from saas_mvp.db import Base

MATCH_TYPE_EXACT = "exact"
MATCH_TYPE_PREFIX = "prefix"
MATCH_TYPE_CONTAINS = "contains"
REPLY_TYPE_TEXT = "text"
REPLY_TYPE_FLEX = "flex"


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class AutoReplyRule(Base):
    __tablename__ = "auto_reply_rules"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(
        Integer,
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    keyword = Column(String(255), nullable=False)
    match_type = Column(String(16), nullable=False, default=MATCH_TYPE_CONTAINS)
    reply_type = Column(String(16), nullable=False, default=REPLY_TYPE_TEXT)
    reply_text = Column(Text, nullable=True)
    flex_menu_id = Column(
        Integer,
        ForeignKey("flex_menus.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    priority = Column(Integer, nullable=False, default=0, server_default=text("0"))
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

    __table_args__ = (
        CheckConstraint(
            "match_type in ('exact', 'prefix', 'contains')",
            name="ck_auto_reply_rules_match_type",
        ),
        CheckConstraint(
            "reply_type in ('text', 'flex')",
            name="ck_auto_reply_rules_reply_type",
        ),
    )
