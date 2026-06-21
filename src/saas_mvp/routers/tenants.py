"""Tenants router — 租戶資訊端點。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from saas_mvp.deps import get_current_user, get_db, require_rate_limit
from saas_mvp.line_client import LineBotInfoClient, get_bot_info_client
from saas_mvp.models.tenant import Tenant, normalize_store_type
from saas_mvp.models.user import User
from saas_mvp.quota import get_quota_status
from saas_mvp.routers.line_webhook import webhook_url_for
from saas_mvp.services import line_config as line_config_svc

router = APIRouter(prefix="/tenants", tags=["tenants"])


class TenantInfo(BaseModel):
    id: int
    name: str
    plan: str
    store_type: str | None = None

    model_config = {"from_attributes": True}


class TenantUpdateBody(BaseModel):
    """租戶自助更新；目前僅開放 store_type（標籤）。plan/is_active 仍歸 admin/billing。"""

    store_type: str | None = Field(default=None, max_length=32)


class TenantLineConfigResponse(BaseModel):
    """租戶自助 LINE 設定回應（遮罩 secret/token）。"""

    tenant_id: int
    has_channel_secret: bool
    has_access_token: bool
    default_target_lang: str
    bot_mode: str = "translation"
    credential_status: str = "unchecked"
    credential_last_error: str | None = None
    credential_checked_at: str | None = None
    # service 層已 .isoformat() 序列化，宣告為 str | None 避免重複序列化
    created_at: str | None = None
    updated_at: str | None = None
    # 相對路徑；租戶需自行拼接 host 後填入 LINE Console
    webhook_url: str


class MyDashboardResponse(BaseModel):
    """店家自助總覽：租戶資訊 + bot 狀態 + 今日用量（一站式）。"""

    tenant: TenantInfo
    has_line_config: bool
    line_config: TenantLineConfigResponse | None = None
    usage: dict


class TenantLineConfigUpsertBody(BaseModel):
    # LINE channel secret 固定 32 字元、access token 通常 ≤512 字元；
    # 設上限提早攔截異常輸入，避免認證後租戶塞超大字串造成儲存/加密 DoS。
    channel_secret: str = Field(..., min_length=1, max_length=64)
    access_token: str = Field(..., min_length=1, max_length=1024)
    default_target_lang: str = "zh-TW"
    # bot 模式：translation（預設）/ booking；None/省略時不更動既有值。
    bot_mode: str | None = None


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


@router.put("/me", response_model=TenantInfo, dependencies=[Depends(require_rate_limit)])
def update_my_tenant(
    body: TenantUpdateBody,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> TenantInfo:
    """租戶自助更新（目前僅 store_type 標籤）；plan/is_active 仍歸 admin/billing。

    用 model_fields_set 區分「未提供＝不動」與「提供 null＝清空」，與
    admin PATCH 行為一致：空 body ``{}`` 不會誤清既有 store_type。
    """
    tenant = db.get(Tenant, current_user.tenant_id)
    if tenant is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Tenant not found",
        )
    if "store_type" in body.model_fields_set:
        tenant.store_type = normalize_store_type(body.store_type)
        db.commit()
        db.refresh(tenant)
    return TenantInfo.model_validate(tenant)


@router.get("/me/dashboard", response_model=MyDashboardResponse)
def get_my_dashboard(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> MyDashboardResponse:
    """店家自助總覽：租戶資訊 + bot 狀態 + 今日用量。

    尚未設定 LINE bot 的店家也能正常檢視（line_config 回 null，不 404）。
    tenant_id 唯一來源為 current_user.tenant_id，結構性保證租戶隔離。
    """
    tid = current_user.tenant_id
    tenant = db.get(Tenant, tid)
    if tenant is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Tenant not found",
        )

    # bot 狀態：吞掉「未設定」的 404，讓尚未設定 bot 的店家也能看 dashboard。
    line_config: TenantLineConfigResponse | None = None
    try:
        svc_dict = line_config_svc.get_line_config(db, tid)
        line_config = _line_config_response(svc_dict, tid)
    except HTTPException as exc:
        if exc.status_code != status.HTTP_404_NOT_FOUND:
            raise

    return MyDashboardResponse(
        tenant=TenantInfo.model_validate(tenant),
        has_line_config=line_config is not None,
        line_config=line_config,
        usage=get_quota_status(db, tid, tenant.plan),
    )


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
    bot_info_client: LineBotInfoClient = Depends(get_bot_info_client),
) -> TenantLineConfigResponse:
    """建立或更新當前租戶 LINE 設定；無效 default_target_lang 回 400。"""
    tid = current_user.tenant_id
    svc_dict = line_config_svc.upsert_line_config(
        db,
        tid,
        channel_secret=body.channel_secret,
        access_token=body.access_token,
        default_target_lang=body.default_target_lang,
        bot_mode=body.bot_mode,
        bot_info_client=bot_info_client,
    )
    return _line_config_response(svc_dict, tid)


@router.post(
    "/me/line-config/verify",
    response_model=TenantLineConfigResponse,
    dependencies=[Depends(require_rate_limit)],
)
def verify_my_line_config(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    bot_info_client: LineBotInfoClient = Depends(get_bot_info_client),
) -> TenantLineConfigResponse:
    """測試當前租戶 LINE bot 連線（重新驗證憑證）；未設定回 404。"""
    tid = current_user.tenant_id
    svc_dict = line_config_svc.verify_line_config(
        db,
        tid,
        bot_info_client=bot_info_client,
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
