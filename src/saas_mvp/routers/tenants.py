"""Tenants router — 租戶資訊端點。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from saas_mvp.deps import get_current_user, get_db, require_rate_limit
from saas_mvp.models.tenant import Tenant
from saas_mvp.models.user import User
from saas_mvp.routers.line_webhook import webhook_url_for
from saas_mvp.services import line_config as line_config_svc

router = APIRouter(prefix="/tenants", tags=["tenants"])


class TenantInfo(BaseModel):
    id: int
    name: str
    plan: str

    model_config = {"from_attributes": True}


class TenantLineConfigResponse(BaseModel):
    """租戶自助 LINE 設定回應（遮罩 secret/token）。"""

    tenant_id: int
    has_channel_secret: bool
    has_access_token: bool
    default_target_lang: str
    # service 層已 .isoformat() 序列化，宣告為 str | None 避免重複序列化
    created_at: str | None = None
    updated_at: str | None = None
    # 相對路徑；租戶需自行拼接 host 後填入 LINE Console
    webhook_url: str


class TenantLineConfigUpsertBody(BaseModel):
    # LINE channel secret 固定 32 字元、access token 通常 ≤512 字元；
    # 設上限提早攔截異常輸入，避免認證後租戶塞超大字串造成儲存/加密 DoS。
    channel_secret: str = Field(..., min_length=1, max_length=64)
    access_token: str = Field(..., min_length=1, max_length=1024)
    default_target_lang: str = "zh-TW"


@router.get("/me", response_model=TenantInfo)
def get_my_tenant(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> TenantInfo:
    """回傳當前使用者所屬租戶資訊。"""
    tenant = db.get(Tenant, current_user.tenant_id)
    if tenant is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Tenant not found",
        )
    return TenantInfo.model_validate(tenant)


# ── 租戶自助 LINE Channel Config 端點 ─────────────────────────────────────────
# tenant_id 唯一來源為 current_user.tenant_id，無 path/body 參數，結構性保證隔離。
# 寫端點 + 查端點皆掛 require_rate_limit，對齊 notes.py 等同層 self-service 慣例。
# webhook_url 由 line_webhook.webhook_url_for() 組裝——與 webhook router 的實際掛載
# 路徑共用同一組常數，路由改名不會靜默脫節（有測試斷言一致性作保底）。


def _line_config_response(svc_dict: dict, tenant_id: int) -> TenantLineConfigResponse:
    return TenantLineConfigResponse(
        **svc_dict,
        webhook_url=webhook_url_for(tenant_id),
    )


@router.get(
    "/me/line-config",
    response_model=TenantLineConfigResponse,
    dependencies=[Depends(require_rate_limit)],
)
def get_my_line_config(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> TenantLineConfigResponse:
    """取得當前租戶 LINE 設定（遮罩版）；未設定回 404。"""
    tid = current_user.tenant_id
    svc_dict = line_config_svc.get_line_config(db, tid)
    return _line_config_response(svc_dict, tid)


@router.put(
    "/me/line-config",
    response_model=TenantLineConfigResponse,
    dependencies=[Depends(require_rate_limit)],
)
def upsert_my_line_config(
    body: TenantLineConfigUpsertBody,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> TenantLineConfigResponse:
    """建立或更新當前租戶 LINE 設定；無效 default_target_lang 回 400。"""
    tid = current_user.tenant_id
    svc_dict = line_config_svc.upsert_line_config(
        db,
        tid,
        channel_secret=body.channel_secret,
        access_token=body.access_token,
        default_target_lang=body.default_target_lang,
    )
    return _line_config_response(svc_dict, tid)


@router.delete(
    "/me/line-config",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_rate_limit)],
)
def delete_my_line_config(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Response:
    """刪除當前租戶 LINE 設定；不存在回 404。"""
    line_config_svc.delete_line_config(db, current_user.tenant_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
