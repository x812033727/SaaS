"""LineMessage model — 後台 LINE 客服對話紀錄（收/發）。

對標 vibeaico「後台直接回覆 LINE 訊息」：把顧客在 LINE 傳來的文字訊息，
以及店家從後台回覆的文字，逐筆存檔，供後台聊天視圖呈現與核對。

direction：``in`` = 顧客傳入；``out`` = 店家從後台回覆（push）。
"""

from __future__ import annotations

import datetime

from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)

from saas_mvp.db import Base

DIRECTION_IN = "in"
DIRECTION_OUT = "out"


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class LineMessage(Base):
    __tablename__ = "line_messages"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(
        Integer,
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    line_user_id = Column(String(64), nullable=False, index=True)
    customer_id = Column(
        Integer,
        ForeignKey("booking_customers.id", ondelete="SET NULL"),
        nullable=True,
    )
    direction = Column(String(8), nullable=False)  # in | out
    text = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)

    __table_args__ = (
        Index("ix_line_msg_tenant_user", "tenant_id", "line_user_id", "id"),
    )
