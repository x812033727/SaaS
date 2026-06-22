"""公開店家頁管理 router（authed JSON）— 店主取得 / upsert 自己的店家頁。

受認證 + 租戶隔離 + rate limit + require_feature(PUBLIC_PROFILE)。
公開消費（對訪客渲染）在 routers/public.py（/p/{slug}），與此分離。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from saas_mvp.deps import get_current_user, get_db, require_rate_limit
from saas_mvp.models.user import User
from saas_mvp.services import profile as profile_svc
from saas_mvp.services.features import PUBLIC_PROFILE, require_feature

router = APIRouter(
    prefix="/booking/profile",
    tags=["booking-profile"],
    dependencies=[
        Depends(require_rate_limit),
        Depends(require_feature(PUBLIC_PROFILE)),
    ],
)


# ─────────────────────────────── Schemas ─────────────────────────────────────

class ProfileUpsert(BaseModel):
    slug: str | None = Field(default=None, max_length=64)
    display_name: str | None = Field(default=None, max_length=128)
    banner_url: str | None = Field(default=None, max_length=512)
    theme_color: str | None = Field(default=None, max_length=16)
    social_links: str | None = None  # JSON 字串
    seo_title: str | None = Field(default=None, max_length=256)
    seo_description: str | None = Field(default=None, max_length=512)
    intro: str | None = None
    is_published: bool | None = None


class ProfileResponse(BaseModel):
    id: int
    tenant_id: int
    slug: str
    display_name: str | None
    banner_url: str | None
    theme_color: str | None
    social_links: str | None
    seo_title: str | None
    seo_description: str | None
    intro: str | None
    is_published: bool

    model_config = {"from_attributes": True}


# ─────────────────────────────── Endpoints ───────────────────────────────────

@router.get("", response_model=ProfileResponse)
def get_profile(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ProfileResponse:
    profile = profile_svc.get_by_tenant(db, current_user.tenant_id)
    if profile is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Profile not set up"
        )
    return ProfileResponse.model_validate(profile)


@router.put("", response_model=ProfileResponse)
def upsert_profile(
    body: ProfileUpsert,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ProfileResponse:
    try:
        profile = profile_svc.upsert(
            db,
            current_user.tenant_id,
            **body.model_dump(exclude_unset=True),
        )
    except profile_svc.SlugTakenError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="此網址代稱已被使用，請換一個",
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        )
    return ProfileResponse.model_validate(profile)
