"""Admin router — /admin/* 端點。

所有端點掛 require_admin dependency；非 admin 回 403（不回 401）。
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from saas_mvp.deps import get_db, get_current_actor, require_admin
from saas_mvp.auth.dependencies import Actor
from saas_mvp.line_client import LineBotInfoClient, get_bot_info_client
from saas_mvp.services import admin as admin_svc
from saas_mvp.services import line_config as line_config_svc
from pydantic import BaseModel, Field


router = APIRouter(
    prefix="/admin",
    tags=["admin"],
    dependencies=[Depends(require_admin)],
)


class TenantPatchBody(BaseModel):
    is_active: Optional[bool] = None
    plan: Optional[str] = None


@router.get("/tenants", summary="列出所有租戶（分頁）")
def list_tenants(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
):
    return admin_svc.list_tenants(db, skip=skip, limit=limit)


@router.get("/tenants/{tenant_id}/usage", summary="租戶今日用量 + per-key 明細")
def tenant_usage(
    tenant_id: int,
    db: Session = Depends(get_db),
):
    return admin_svc.get_tenant_usage(db, tenant_id)


@router.patch("/tenants/{tenant_id}", summary="停/啟用租戶或改方案")
def patch_tenant(
    tenant_id: int,
    body: TenantPatchBody,
    # FastAPI 快取同請求內 dependency，不會重複執行 get_current_actor
    actor: Actor = Depends(get_current_actor),
    db: Session = Depends(get_db),
):
    return admin_svc.patch_tenant(
        db,
        tenant_id,
        is_active=body.is_active,
        plan=body.plan,
        actor_user_id=actor.user.id,
    )


@router.get("/api-keys", summary="跨租戶 API key 概況")
def list_api_keys(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
):
    return admin_svc.list_api_keys(db, skip=skip, limit=limit)


# ── LINE Channel Config 管理端點 ──────────────────────────────────────────────

class LineConfigUpsertBody(BaseModel):
    channel_secret: str = Field(..., min_length=1, max_length=64)
    access_token: str = Field(..., min_length=1, max_length=1024)
    default_target_lang: str = "zh-TW"


class AdminLineConfigResponse(BaseModel):
    tenant_id: int
    has_channel_secret: bool
    has_access_token: bool
    default_target_lang: str
    credential_status: str = "unchecked"
    credential_last_error: str | None = None
    credential_checked_at: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


@router.get(
    "/line-configs/{tenant_id}",
    response_model=AdminLineConfigResponse,
    summary="查詢租戶 LINE 設定（遮罩版）",
)
def get_line_config(
    tenant_id: int,
    db: Session = Depends(get_db),
):
    return line_config_svc.get_line_config(db, tenant_id)


@router.put(
    "/line-configs/{tenant_id}",
    response_model=AdminLineConfigResponse,
    summary="建立或更新租戶 LINE 設定",
)
def upsert_line_config(
    tenant_id: int,
    body: LineConfigUpsertBody,
    db: Session = Depends(get_db),
    bot_info_client: LineBotInfoClient = Depends(get_bot_info_client),
):
    return line_config_svc.upsert_line_config(
        db,
        tenant_id,
        channel_secret=body.channel_secret,
        access_token=body.access_token,
        default_target_lang=body.default_target_lang,
        bot_info_client=bot_info_client,
    )


@router.delete("/line-configs/{tenant_id}", summary="刪除租戶 LINE 設定")
def delete_line_config(
    tenant_id: int,
    db: Session = Depends(get_db),
):
    return line_config_svc.delete_line_config(db, tenant_id)
