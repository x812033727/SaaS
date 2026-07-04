"""API Key 管理路由。

端點：
  POST   /api-keys/        建立新 key（明文只回傳一次）
  GET    /api-keys/        列出當前租戶 keys（只露 key_prefix，不含明文或 hash）
  DELETE /api-keys/{id}    撤銷 key（軟刪除，is_active=False）

商業邏輯在 services/api_keys.py（與 UI 頁共用）。
"""

from __future__ import annotations

import datetime

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from saas_mvp.deps import get_current_user, get_db
from saas_mvp.models.user import User
from saas_mvp.services import api_keys as api_keys_svc

router = APIRouter(prefix="/api-keys", tags=["api-keys"])


# ── Schemas ───────────────────────────────────────────────────

class ApiKeyCreate(BaseModel):
    # P2: 空字串或超長名稱在 DB 層會炸（PostgreSQL VARCHAR 截斷/DataError），改在 Pydantic 層攔截
    name: str = Field(..., min_length=1, max_length=128)


class ApiKeyCreated(BaseModel):
    """建立回應：包含 plain_key（唯一一次）。"""
    id: int
    name: str
    key_prefix: str
    plain_key: str
    created_at: datetime.datetime

    model_config = {"from_attributes": True}


class ApiKeyItem(BaseModel):
    """列出回應：不含 plain_key 或 key_hash。"""
    id: int
    name: str
    key_prefix: str
    is_active: bool
    created_at: datetime.datetime

    model_config = {"from_attributes": True}


# ── Endpoints ─────────────────────────────────────────────────

@router.post("/", response_model=ApiKeyCreated, status_code=status.HTTP_201_CREATED)
def create_key(
    body: ApiKeyCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ApiKeyCreated:
    """建立 API key。明文 plain_key 僅此一次，請妥善保存。"""
    api_key, plain_key = api_keys_svc.create_key(
        db,
        tenant_id=current_user.tenant_id,
        user_id=current_user.id,
        name=body.name,
    )
    return ApiKeyCreated(
        id=api_key.id,
        name=api_key.name,
        key_prefix=api_key.key_prefix,
        plain_key=plain_key,
        created_at=api_key.created_at,
    )


@router.get("/", response_model=list[ApiKeyItem])
def list_keys(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[ApiKeyItem]:
    """列出當前租戶所有 API keys（只回 key_prefix，永不回明文或 hash）。"""
    rows = api_keys_svc.list_keys(db, tenant_id=current_user.tenant_id)
    return [ApiKeyItem.model_validate(r) for r in rows]


@router.delete("/{key_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
def revoke_key(
    key_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Response:
    """撤銷 API key（軟刪除；usage 歷史記錄保留）。撤銷後立即失效。"""
    api_keys_svc.revoke_key(
        db, tenant_id=current_user.tenant_id, key_id=key_id
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
