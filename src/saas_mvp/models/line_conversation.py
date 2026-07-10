"""AI 對話狀態（A2.1）— server-side session，補齊「純文字多輪對話」能力。

既有引導式預約以 postback 前向攜帶狀態（stateless）；自然語言多輪（「我要
約明天剪髮」→「兩位」）需要伺服器端記住已蒐集的槽位。存 DB 不存 Redis：
量小（每好友至多一列 upsert）、跨 worker/重啟存活、TTL 過期即重置。

設計鐵則（A2.2）：AI 只**填槽**，建單永遠走既有 postback 確認 → book_slot
確定性路徑；本表絕不直接產生訂單。
"""

from __future__ import annotations

import datetime
import json

from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)

from saas_mvp.db import Base

STATE_IDLE = "idle"
STATE_FILLING = "filling"   # AI 逐輪補槽中


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class LineConversation(Base):
    __tablename__ = "line_conversations"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(
        Integer,
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    line_user_id = Column(String(64), nullable=False)
    state = Column(String(16), nullable=False, default=STATE_IDLE)
    # 已蒐集槽位 JSON：{service_id, date, party_size}
    slots_json = Column(Text, nullable=True)
    turn_count = Column(Integer, nullable=False, default=0)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    __table_args__ = (
        UniqueConstraint("tenant_id", "line_user_id", name="uq_line_conversation"),
    )

    @property
    def slots(self) -> dict:
        try:
            return json.loads(self.slots_json) if self.slots_json else {}
        except ValueError:
            return {}

    @slots.setter
    def slots(self, value: dict) -> None:
        self.slots_json = json.dumps(value, ensure_ascii=False)
