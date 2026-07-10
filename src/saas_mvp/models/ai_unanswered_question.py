"""AI 答不好的問題(D4)— FAQ 自學素材。

AI 回答時無 FAQ 命中或整體失敗,將顧客原句 upsert 至此
(question_hash 去重 + hit_count 累加);店家在 /ui/faq「AI 答不好的問題」
區一鍵補答案轉正式 FAQEntry。
"""

from __future__ import annotations

import datetime

from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)

from saas_mvp.db import Base

UNANSWERED_OPEN = "open"
UNANSWERED_CONVERTED = "converted"
UNANSWERED_DISMISSED = "dismissed"


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class AiUnansweredQuestion(Base):
    __tablename__ = "ai_unanswered_questions"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "question_hash", name="uq_ai_unanswered_tenant_hash"
        ),
    )

    id = Column(Integer, primary_key=True)
    tenant_id = Column(
        Integer,
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    question = Column(Text, nullable=False)
    question_hash = Column(String(64), nullable=False)
    hit_count = Column(Integer, nullable=False, default=1, server_default=text("1"))
    status = Column(
        String(16), nullable=False, default=UNANSWERED_OPEN,
        server_default=text("'open'"),
    )
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )
