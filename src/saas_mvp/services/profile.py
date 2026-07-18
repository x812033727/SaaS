"""公開店家頁（business profile）服務層 — 一對一 upsert + slug 解析。

所有租戶範圍查詢走 tenant_query；slug 全域 unique（IntegrityError → SlugTaken）。
公開解析 get_by_slug 只回 is_published=true 的列（未發佈視同不存在）。
"""

from __future__ import annotations

import re

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from saas_mvp.models.business_profile import BusinessProfile
from saas_mvp.services.tenants import tenant_query


class SlugTakenError(Exception):
    """slug 已被其他租戶使用（全域 unique 衝突）。"""


class InvalidThemeColorError(ValueError):
    """theme_color 不符合允許的色碼格式（防 CSS 注入）。"""


# 只接受嚴格 hex 色碼（#RGB / #RGBA / #RRGGBB / #RRGGBBAA），防止公開頁
# 以 theme_color 渲染 inline CSS 時被注入（如 '#000;}body{...'）。
_THEME_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{3,8}$")


# 可由 upsert 寫入的欄位白名單（slug 另外處理 unique）。
_UPSERTABLE = frozenset(
    {
        "slug",
        "display_name",
        "banner_url",
        "theme_color",
        "social_links",
        "seo_title",
        "seo_description",
        "intro",
        "announcement",
        "review_url",
        "is_published",
    }
)


def get_by_tenant(db: Session, tenant_id: int) -> BusinessProfile | None:
    return tenant_query(db, BusinessProfile, tenant_id).first()


def get_by_slug(db: Session, slug: str) -> BusinessProfile | None:
    """以 slug 解析公開店家頁；只回已發佈（is_published=true）的列。

    跨租戶查詢（公開路由用），故刻意不套 tenant_query——但只暴露單一租戶
    自己的資料，且未發佈一律當不存在（回 None）。
    """
    return (
        db.query(BusinessProfile)
        .filter(
            BusinessProfile.slug == slug,
            BusinessProfile.is_published.is_(True),
        )
        .first()
    )


def upsert(db: Session, tenant_id: int, **fields) -> BusinessProfile:
    """建立或更新該租戶的店家頁；slug 唯一衝突 → SlugTakenError。

    只寫入 _UPSERTABLE 白名單欄位；其餘 kwargs 忽略（避免外部塞 tenant_id 等）。
    """
    profile = get_by_tenant(db, tenant_id)
    data = {k: v for k, v in fields.items() if k in _UPSERTABLE and v is not None}

    # theme_color 防 CSS 注入：只放行嚴格 hex 色碼，違者拒絕。
    theme_color = data.get("theme_color")
    if theme_color is not None and not _THEME_COLOR_RE.match(str(theme_color)):
        raise InvalidThemeColorError("色碼格式錯誤，請使用如 #1a2b3c 的十六進位色碼。")

    if profile is None:
        if not data.get("slug"):
            raise ValueError("slug is required when creating a profile")
        profile = BusinessProfile(tenant_id=tenant_id, **data)
        db.add(profile)
    else:
        for k, v in data.items():
            setattr(profile, k, v)

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise SlugTakenError("slug already taken by another tenant")
    db.refresh(profile)
    return profile
