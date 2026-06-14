"""LineUserLanguage model — 每位 LINE 使用者在某租戶下的語言偏好。

使用者透過 `/lang xx` 指令設定後，翻譯時優先使用此設定。
未設定時 fallback 到 LineChannelConfig.default_target_lang。

唯一約束：(tenant_id, line_user_id) 一對一，upsert 更新即可。
"""

from __future__ import annotations

import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Session

from saas_mvp.db import Base


class LineUserLanguage(Base):
    __tablename__ = "line_user_languages"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(
        Integer,
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    line_user_id = Column(String(64), nullable=False, index=True)
    target_lang = Column(String(16), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.datetime.now(datetime.timezone.utc),
        onupdate=lambda: datetime.datetime.now(datetime.timezone.utc),
    )

    __table_args__ = (
        UniqueConstraint("tenant_id", "line_user_id", name="uq_line_user_lang"),
    )


# ── 服務函式（供 webhook handler 直接呼叫） ────────────────────────────────────

def upsert_user_lang(
    db: Session,
    tenant_id: int,
    line_user_id: str,
    target_lang: str,
) -> None:
    """建立或更新使用者語言偏好（commit 已包含）。"""
    from sqlalchemy import select  # 避免頂層循環 import

    row = db.execute(
        select(LineUserLanguage).where(
            LineUserLanguage.tenant_id == tenant_id,
            LineUserLanguage.line_user_id == line_user_id,
        )
    ).scalar_one_or_none()

    if row is None:
        db.add(LineUserLanguage(
            tenant_id=tenant_id,
            line_user_id=line_user_id,
            target_lang=target_lang,
        ))
    else:
        row.target_lang = target_lang

    db.commit()


def get_user_lang(
    db: Session,
    tenant_id: int,
    line_user_id: str,
) -> str | None:
    """查詢使用者語言偏好；無記錄回 None。"""
    from sqlalchemy import select

    row = db.execute(
        select(LineUserLanguage).where(
            LineUserLanguage.tenant_id == tenant_id,
            LineUserLanguage.line_user_id == line_user_id,
        )
    ).scalar_one_or_none()

    return row.target_lang if row else None
