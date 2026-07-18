"""BusinessProfile model — 每租戶公開店家頁設定，一對一（比照 LineChannelConfig）。

slug 為公開 URL key（/p/{slug}），全域 unique；只有 is_published=true 時對外可見。
social_links / intro / seo_* 等皆 nullable，未填不影響既有租戶。
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
from sqlalchemy.orm import relationship

from saas_mvp.db import Base


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class BusinessProfile(Base):
    __tablename__ = "business_profiles"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(
        Integer,
        ForeignKey("tenants.id", ondelete="CASCADE"),  # 裸 SQL 刪 tenant 也清孤兒行
        nullable=False,
        unique=True,   # 一對一
        index=True,
    )

    # 公開 URL key；全域 unique（跨租戶不可重複）。
    slug = Column(String(64), nullable=False, unique=True, index=True)

    display_name = Column(String(128), nullable=True)
    banner_url = Column(String(512), nullable=True)
    theme_color = Column(String(16), nullable=True)
    social_links = Column(Text, nullable=True)  # JSON 字串
    seo_title = Column(String(256), nullable=True)
    seo_description = Column(String(512), nullable=True)
    intro = Column(Text, nullable=True)
    # 公告文字（對標 vibeaico 公開頁「公告」）；既有 DB 由 _migrate_add_profile_announcement 補欄。
    announcement = Column(Text, nullable=True)
    # R11-B:外部評論連結(Google/FB),post_visit 訊息 {review_url} 佔位符來源
    review_url = Column(String(512), nullable=True)

    is_published = Column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )

    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        onupdate=_utcnow,
    )

    tenant = relationship("Tenant", back_populates="business_profile")
