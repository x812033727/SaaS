"""伺服器渲染管理 UI（Jinja2 + HTMX，同源伺服）。

兩個使用層級：
  - 店家自助（require_ui_user）：dashboard、LINE 設定、連線測試、店家類型。
  - 平台管理（require_ui_admin）：跨店家 bot 總覽、單一租戶管理。

認證：登入後把 JWT 放進 httpOnly cookie（SameSite=Lax）；UI 路由用獨立的
`require_ui_user` / `require_ui_admin`（cookie 路徑），完全不碰 API 的
`get_current_actor`（仍只認 header）。所有資料一律走既有 service 層，回應永不
輸出明文 channel_secret / access_token，只揭露 has_* 布林與 credential_status。

CSRF：double-submit cookie token——登入時發非 httpOnly 的 csrf_token cookie，
所有 /ui 非 GET 請求須以 X-CSRF-Token header（HTMX 由 base.html body 級
hx-headers 自動帶）或表單欄位 csrf_token 回傳同值，router 級依賴
_enforce_csrf 常數時間比對，不符回 403。SAAS_UI_CSRF_ENABLED=false 可關閉
（僅測試環境）。
"""

from __future__ import annotations

import datetime
import hmac
import html
import secrets
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Query, Request, status
from fastapi.responses import (
    HTMLResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from saas_mvp.config import settings
from saas_mvp.deps import Actor, get_db, require_ui_admin, require_ui_user
from saas_mvp.auth.dependencies import _UI_COOKIE_NAME
from saas_mvp.auth.security import create_access_token, hash_password, verify_password
from saas_mvp.line_client import (
    LineBotInfoClient,
    LinePushClient,
    LineRichMenuClient,
    get_bot_info_client,
    get_push_client,
    get_rich_menu_client,
)
from saas_mvp.models.tenant import Tenant, normalize_store_type
from saas_mvp.models.user import User
from saas_mvp.quota import get_quota_status
from saas_mvp.routers.line_webhook import webhook_url_for
from saas_mvp.services import admin as admin_svc
from saas_mvp.services import analytics as analytics_svc
from saas_mvp.services import billing as billing_svc
from saas_mvp.services import booking as booking_svc
from saas_mvp.services import coupons as coupons_svc
from saas_mvp.services import features as features_svc
from saas_mvp.services import customers as customers_svc
from saas_mvp.services import line_config as line_config_svc
from saas_mvp.services import rich_menu as rich_menu_svc
from saas_mvp.services import shop as shop_svc
from saas_mvp.services import slots as slots_svc
from saas_mvp.services import locations as locations_svc
from saas_mvp.services import staff as staff_svc
from saas_mvp.services import catalog as catalog_svc
from saas_mvp.services import marketing as marketing_svc
from saas_mvp.services import flex_menu as flex_menu_svc
from saas_mvp.services import portfolio as portfolio_svc
from saas_mvp.services import profile as profile_svc
from saas_mvp.services import pos as pos_svc
from saas_mvp.services import membership as membership_svc
from saas_mvp.services import segments as segments_svc
from saas_mvp.services import notifications_history as notif_history_svc
from saas_mvp.services import faq as faq_svc
from saas_mvp.services import push_quota as push_quota_svc
from saas_mvp.services import line_chat as line_chat_svc
from saas_mvp.services import calendar_view as calendar_view_svc
from saas_mvp.services.events import broker as event_broker
from saas_mvp.ai import AIError, get_assistant
from saas_mvp.models.campaign import Campaign
from saas_mvp.services.tenants import tenant_query
from fastapi import HTTPException

_PKG_DIR = Path(__file__).resolve().parent.parent  # src/saas_mvp
templates = Jinja2Templates(directory=str(_PKG_DIR / "templates"))

# ── CSRF（double-submit cookie token）───────────────────────────────────────

_CSRF_COOKIE_NAME = "csrf_token"
_CSRF_HEADER_NAME = "x-csrf-token"
_CSRF_FORM_FIELD = "csrf_token"
# 尚無 session 的端點（登入/註冊表單提交）豁免；其 GET 頁本就放行。
_CSRF_EXEMPT_PATHS = {"/ui/login", "/ui/register"}


async def _enforce_csrf(request: Request) -> None:
    """/ui 全端點 router 級依賴：非 GET 請求驗 double-submit CSRF token。

    token 來源依序：X-CSRF-Token header（HTMX 走 base.html body 級
    hx-headers 屬性繼承自動帶）→ 表單欄位 csrf_token（傳統 <form> hidden
    field）。與 cookie 常數時間比對，不符回 403。

    Starlette 會 cache request._form，此處 await request.form() 不影響
    後續 handler 的 Form(...)/File(...) 參數解析。
    """
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    if not settings.ui_csrf_enabled:  # 動態讀，測試可 monkeypatch
        return
    if request.url.path in _CSRF_EXEMPT_PATHS:
        return
    cookie_token = request.cookies.get(_CSRF_COOKIE_NAME, "")
    supplied = request.headers.get(_CSRF_HEADER_NAME, "")
    if not supplied:
        content_type = request.headers.get("content-type", "")
        if content_type.startswith(
            ("application/x-www-form-urlencoded", "multipart/form-data")
        ):
            form = await request.form()
            supplied = str(form.get(_CSRF_FORM_FIELD) or "")
    if (
        not cookie_token
        or not supplied
        or not hmac.compare_digest(cookie_token, supplied)
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="CSRF token missing or invalid",
        )


router = APIRouter(
    prefix="/ui",
    tags=["ui"],
    include_in_schema=False,
    dependencies=[Depends(_enforce_csrf)],
)

# 送往付費 LLM 的問題字數上限（與 routers/ai.py AskRequest 一致），防成本放大。
_AI_QUESTION_MAX_LEN = 2000


# ── 共用工具 ────────────────────────────────────────────────────────────────

def _set_auth_cookie(response: Response, token: str) -> None:
    """把 JWT 寫入 httpOnly cookie；prod 加 Secure，dev/test 不加（方便本機/測試）。

    一併發放 double-submit CSRF cookie（非 httpOnly——前端模板/HTMX 需可讀
    回傳；token 本身不含任何機密，防護力來自「攻擊者跨站無法讀取」）。
    所有登入路徑（/ui/login、/ui/register、OAuth callback）皆經此函式。
    """
    response.set_cookie(
        key=_UI_COOKIE_NAME,
        value=token,
        httponly=True,
        samesite="lax",
        secure=settings.env not in ("dev", "test"),
        max_age=settings.access_token_expire_minutes * 60,
        path="/",
    )
    response.set_cookie(
        key=_CSRF_COOKIE_NAME,
        value=secrets.token_urlsafe(32),
        httponly=False,
        samesite="lax",
        secure=settings.env not in ("dev", "test"),
        max_age=settings.access_token_expire_minutes * 60,
        path="/",
    )


def _ctx(request: Request, actor: Actor | None = None, **extra) -> dict:
    """組 template context；Jinja2Templates 需要 request。"""
    base = {
        "request": request,
        "current_user": actor.user if actor else None,
        "is_admin": bool(actor and actor.user.is_admin),
        # CSRF：模板 hidden field 與 base.html hx-headers 用
        "csrf_token": request.cookies.get(_CSRF_COOKIE_NAME, ""),
    }
    base.update(extra)
    return base


def _is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request", "").lower() == "true"


def _line_config_or_none(db: Session, tenant_id: int) -> dict | None:
    """取 LINE 設定（遮罩 dict）；未設定回 None（吞 404，與 dashboard 一致）。"""
    try:
        return line_config_svc.get_line_config(db, tenant_id)
    except HTTPException as exc:
        if exc.status_code == status.HTTP_404_NOT_FOUND:
            return None
        raise


# ── 公開：登入 / 註冊 / 登出 ───────────────────────────────────────────────────

@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    return templates.TemplateResponse("login.html", _ctx(request))


@router.post("/login", response_class=HTMLResponse)
def login_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    user = db.query(User).filter(User.email == email).first()
    if not user or not verify_password(password, user.hashed_password):
        # 統一錯誤訊息，避免帳號列舉
        return templates.TemplateResponse(
            "login.html",
            _ctx(request, error="電子郵件或密碼錯誤"),
            status_code=status.HTTP_401_UNAUTHORIZED,
        )
    token = create_access_token(user_id=user.id, tenant_id=user.tenant_id)
    resp = RedirectResponse("/ui/", status_code=status.HTTP_303_SEE_OTHER)
    _set_auth_cookie(resp, token)
    return resp


@router.get("/register", response_class=HTMLResponse)
def register_form(request: Request):
    return templates.TemplateResponse("register.html", _ctx(request))


@router.post("/register", response_class=HTMLResponse)
def register_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    tenant_name: str = Form(...),
    db: Session = Depends(get_db),
):
    # 與 auth.register 同款守衛：重複 email、租戶名唯一、密碼長度
    def _err(msg: str):
        return templates.TemplateResponse(
            "register.html",
            _ctx(request, error=msg, email=email, tenant_name=tenant_name),
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    if len(password) < 8:
        return _err("密碼至少需 8 個字元")
    if db.query(User).filter(User.email == email).first():
        return _err("此電子郵件已註冊")
    if db.query(Tenant).filter(Tenant.name == tenant_name).first():
        return _err("店家名稱已被使用，請換一個唯一名稱")

    tenant = Tenant(name=tenant_name, plan="free")
    db.add(tenant)
    db.flush()
    user = User(email=email, hashed_password=hash_password(password), tenant_id=tenant.id)
    db.add(user)
    db.commit()
    db.refresh(user)

    token = create_access_token(user_id=user.id, tenant_id=user.tenant_id)
    resp = RedirectResponse("/ui/", status_code=status.HTTP_303_SEE_OTHER)
    _set_auth_cookie(resp, token)
    return resp


@router.get("/logout")
def logout():
    resp = RedirectResponse("/ui/login", status_code=status.HTTP_303_SEE_OTHER)
    resp.delete_cookie(_UI_COOKIE_NAME, path="/")
    resp.delete_cookie(_CSRF_COOKIE_NAME, path="/")
    return resp


# ── 店家自助 ────────────────────────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    tid = actor.user.tenant_id
    tenant = db.get(Tenant, tid)
    line_config = _line_config_or_none(db, tid)
    usage = get_quota_status(db, tid, tenant.plan)
    push = push_quota_svc.get_push_quota_status(db, tid)
    return templates.TemplateResponse(
        "dashboard.html",
        _ctx(request, actor, tenant=tenant, line_config=line_config, usage=usage,
             push_quota=push),
    )


@router.get("/line-config", response_class=HTMLResponse)
def line_config_page(
    request: Request,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    tid = actor.user.tenant_id
    cfg = _line_config_or_none(db, tid)
    return templates.TemplateResponse(
        "line_config.html",
        _ctx(request, actor, cfg=cfg, webhook_url=webhook_url_for(tid)),
    )


@router.post("/line-config", response_class=HTMLResponse)
def line_config_save(
    request: Request,
    channel_secret: str = Form(..., max_length=64),
    access_token: str = Form(..., max_length=1024),
    default_target_lang: str = Form("zh-TW"),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
    bot_info_client: LineBotInfoClient = Depends(get_bot_info_client),
):
    tid = actor.user.tenant_id
    try:
        cfg = line_config_svc.upsert_line_config(
            db, tid,
            channel_secret=channel_secret,
            access_token=access_token,
            default_target_lang=default_target_lang,
            bot_info_client=bot_info_client,
        )
    except HTTPException as exc:
        return templates.TemplateResponse(
            "_line_config_status.html",
            _ctx(request, actor, cfg=None, webhook_url=webhook_url_for(tid), error=str(exc.detail)),
            status_code=exc.status_code,
        )
    return templates.TemplateResponse(
        "_line_config_status.html",
        _ctx(request, actor, cfg=cfg, webhook_url=webhook_url_for(tid)),
    )


@router.post("/line-config/verify", response_class=HTMLResponse)
def line_config_verify(
    request: Request,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
    bot_info_client: LineBotInfoClient = Depends(get_bot_info_client),
):
    tid = actor.user.tenant_id
    try:
        cfg = line_config_svc.verify_line_config(db, tid, bot_info_client=bot_info_client)
    except HTTPException as exc:
        return templates.TemplateResponse(
            "_line_config_status.html",
            _ctx(request, actor, cfg=None, webhook_url=webhook_url_for(tid), error=str(exc.detail)),
            status_code=exc.status_code,
        )
    return templates.TemplateResponse(
        "_line_config_status.html",
        _ctx(request, actor, cfg=cfg, webhook_url=webhook_url_for(tid)),
    )


@router.post("/line-config/delete", response_class=HTMLResponse)
def line_config_delete(
    request: Request,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    tid = actor.user.tenant_id
    try:
        line_config_svc.delete_line_config(db, tid)
    except HTTPException:
        pass  # 不存在即視為已刪除
    return templates.TemplateResponse(
        "_line_config_status.html",
        _ctx(request, actor, cfg=None, webhook_url=webhook_url_for(tid)),
    )


@router.post("/settings", response_class=HTMLResponse)
def settings_save(
    request: Request,
    store_type: str = Form("", max_length=32),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    tenant = db.get(Tenant, actor.user.tenant_id)
    tenant.store_type = normalize_store_type(store_type)
    db.commit()
    db.refresh(tenant)
    return templates.TemplateResponse(
        "_settings.html",
        _ctx(request, actor, tenant=tenant, saved=True),
    )


# ── 帳號 / 變更密碼 ───────────────────────────────────────────────────────────

_OAUTH_PROVIDER_LABELS = {"line": "LINE", "google": "Google"}


@router.get("/account", response_class=HTMLResponse)
def account_page(
    request: Request,
    actor: Actor = Depends(require_ui_user),
    linked: str | None = Query(default=None),
    oauth_error: str | None = Query(default=None),
):
    # 綁定結果（由 /auth/oauth/.../callback 導回時帶 query 參數）轉成可顯示文案。
    linked_label = _OAUTH_PROVIDER_LABELS.get(linked or "")
    provider_label = _OAUTH_PROVIDER_LABELS.get(actor.user.oauth_provider or "")
    return templates.TemplateResponse(
        "account.html",
        _ctx(
            request,
            actor,
            linked_label=linked_label,
            oauth_error=oauth_error,
            provider_label=provider_label,
            # 重新佈署按鈕：僅平台管理員 + 已設定觸發路徑時才顯示（template 另把關 is_admin）。
            deploy_available=bool(settings.deploy_trigger_path),
        ),
    )


@router.post("/admin/deploy", response_class=HTMLResponse)
def admin_deploy(
    request: Request,
    actor: Actor = Depends(require_ui_admin),
):
    """平台管理員手動觸發乾淨重新佈署（拉 main 最新版 + 重建容器）。

    web 容器被刻意強化（非 root、cap_drop ALL、僅綁 loopback），無法直接執行主機上
    的 /usr/local/bin/saas-deploy.sh。故此處只「原子寫入」一個觸發檔到主機掛載目錄；
    主機端 systemd.path（saas-deploy-trigger.path）監看到該檔即消費它並執行部署腳本。
    回 partial 狀態（HTMX swap）；錯誤一律回 200 確保訊息能顯示。
    """
    path = settings.deploy_trigger_path
    if not path:
        return templates.TemplateResponse(
            "_deploy_status.html",
            _ctx(request, actor,
                 deploy_error="未設定部署觸發路徑（SAAS_DEPLOY_TRIGGER_PATH），無法觸發。"),
        )
    try:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
        # 先寫 .tmp 再 rename：避免主機 systemd.path 讀到半截內容。
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_text(f"{stamp} by {actor.user.email}\n", encoding="utf-8")
        tmp.replace(target)
    except OSError as exc:
        return templates.TemplateResponse(
            "_deploy_status.html",
            _ctx(request, actor, deploy_error=f"觸發失敗：{exc}"),
        )
    return templates.TemplateResponse(
        "_deploy_status.html",
        _ctx(request, actor, deploy_triggered=True),
    )


@router.post("/account/oauth/unlink", response_class=HTMLResponse)
def account_oauth_unlink(
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    """解除社群帳號連結。使用者仍保有密碼登入，故解除後不致被鎖在門外。"""
    user = db.get(User, actor.user.id)
    user.oauth_provider = None
    user.oauth_subject = None
    db.commit()
    return RedirectResponse("/ui/account", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/account/password", response_class=HTMLResponse)
def account_change_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    """變更密碼（後台 HTMX）：驗證目前密碼 + 新密碼規則，回 partial 狀態。

    驗證錯誤回 200（partial），確保 HTMX 一定 swap 顯示訊息；不洩漏細節到 log。
    """
    user = db.get(User, actor.user.id)
    error = None
    if not verify_password(current_password, user.hashed_password):
        error = "目前密碼不正確。"
    elif len(new_password) < 8:
        error = "新密碼至少需 8 個字元。"
    elif new_password != confirm_password:
        error = "兩次輸入的新密碼不一致。"
    elif verify_password(new_password, user.hashed_password):
        error = "新密碼不可與目前密碼相同。"

    if error:
        return templates.TemplateResponse(
            "_account_password.html", _ctx(request, actor, error=error),
        )

    user.hashed_password = hash_password(new_password)
    db.add(user)
    db.commit()
    return templates.TemplateResponse(
        "_account_password.html", _ctx(request, actor, saved=True),
    )


# ── 平台管理 ────────────────────────────────────────────────────────────────

@router.get("/admin/bots", response_class=HTMLResponse)
def admin_bots(
    request: Request,
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    store_type: str | None = Query(None),
    is_active: bool | None = Query(None),
    uncategorized: bool = Query(False),
    actor: Actor = Depends(require_ui_admin),
    db: Session = Depends(get_db),
):
    # 空字串 store_type 視為「未指定」
    store_type = store_type or None
    rows = admin_svc.list_line_bots(
        db, skip=skip, limit=limit,
        store_type=store_type, is_active=is_active, uncategorized=uncategorized,
    )
    filters = {
        "store_type": store_type or "",
        "is_active": is_active,
        "uncategorized": uncategorized,
        "skip": skip,
        "limit": limit,
    }
    ctx = _ctx(request, actor, rows=rows, filters=filters)
    # HTMX 篩選請求只回表格 partial
    template = "admin/_bots_table.html" if _is_htmx(request) else "admin/bots.html"
    return templates.TemplateResponse(template, ctx)


@router.get("/admin/tenants/{tenant_id}", response_class=HTMLResponse)
def admin_tenant_detail(
    request: Request,
    tenant_id: int,
    actor: Actor = Depends(require_ui_admin),
    db: Session = Depends(get_db),
):
    tenant = db.get(Tenant, tenant_id)
    if tenant is None:
        return templates.TemplateResponse(
            "admin/tenant_detail.html",
            _ctx(request, actor, tenant=None, not_found=True, tenant_id=tenant_id),
            status_code=status.HTTP_404_NOT_FOUND,
        )
    usage = get_quota_status(db, tenant_id, tenant.plan)
    cfg = _line_config_or_none(db, tenant_id)
    return templates.TemplateResponse(
        "admin/tenant_detail.html",
        _ctx(request, actor, tenant=tenant, usage=usage, cfg=cfg,
             features=features_svc.list_for_tenant(db, tenant_id),
             action_base=f"/ui/admin/tenants/{tenant_id}/line-config"),
    )


@router.post("/admin/tenants/{tenant_id}/features/{feature}", response_class=HTMLResponse)
def admin_set_feature(
    request: Request,
    tenant_id: int,
    feature: str,
    enabled: str = Form(...),
    actor: Actor = Depends(require_ui_admin),
    db: Session = Depends(get_db),
):
    try:
        features_svc.validate_feature(feature)
        features_svc.set_enabled(
            db, tenant_id, feature, enabled == "true",
            actor_user_id=actor.user.id, source="admin",
        )
    except features_svc.UnknownFeatureError:
        pass
    return templates.TemplateResponse(
        "admin/_tenant_features.html",
        _ctx(request, actor, tenant_id=tenant_id,
             features=features_svc.list_for_tenant(db, tenant_id)),
    )


@router.post("/admin/tenants/{tenant_id}/patch", response_class=HTMLResponse)
def admin_tenant_patch(
    request: Request,
    tenant_id: int,
    plan: str = Form(...),
    is_active: str = Form(...),
    store_type: str = Form("", max_length=32),
    actor: Actor = Depends(require_ui_admin),
    db: Session = Depends(get_db),
):
    try:
        result = admin_svc.patch_tenant(
            db, tenant_id,
            is_active=(is_active == "true"),
            plan=plan,
            actor_user_id=actor.user.id,
            store_type=store_type,
            store_type_provided=True,  # 表單一律帶 store_type 欄位
        )
    except HTTPException as exc:
        tenant = db.get(Tenant, tenant_id)
        return templates.TemplateResponse(
            "admin/_tenant_summary.html",
            _ctx(request, actor, tenant=tenant, error=str(exc.detail)),
            status_code=exc.status_code,
        )
    tenant = db.get(Tenant, tenant_id)
    return templates.TemplateResponse(
        "admin/_tenant_summary.html",
        _ctx(request, actor, tenant=tenant, saved=True),
    )


@router.post("/admin/tenants/{tenant_id}/line-config", response_class=HTMLResponse)
def admin_line_config_save(
    request: Request,
    tenant_id: int,
    channel_secret: str = Form(..., max_length=64),
    access_token: str = Form(..., max_length=1024),
    default_target_lang: str = Form("zh-TW"),
    actor: Actor = Depends(require_ui_admin),
    db: Session = Depends(get_db),
    bot_info_client: LineBotInfoClient = Depends(get_bot_info_client),
):
    try:
        cfg = line_config_svc.upsert_line_config(
            db, tenant_id,
            channel_secret=channel_secret,
            access_token=access_token,
            default_target_lang=default_target_lang,
            bot_info_client=bot_info_client,
        )
    except HTTPException as exc:
        return templates.TemplateResponse(
            "_line_config_status.html",
            _ctx(request, actor, cfg=None, webhook_url=webhook_url_for(tenant_id),
                 action_base=f"/ui/admin/tenants/{tenant_id}/line-config", error=str(exc.detail)),
            status_code=exc.status_code,
        )
    return templates.TemplateResponse(
        "_line_config_status.html",
        _ctx(request, actor, cfg=cfg, webhook_url=webhook_url_for(tenant_id),
             action_base=f"/ui/admin/tenants/{tenant_id}/line-config"),
    )


@router.post("/admin/tenants/{tenant_id}/line-config/verify", response_class=HTMLResponse)
def admin_line_config_verify(
    request: Request,
    tenant_id: int,
    actor: Actor = Depends(require_ui_admin),
    db: Session = Depends(get_db),
    bot_info_client: LineBotInfoClient = Depends(get_bot_info_client),
):
    try:
        cfg = line_config_svc.verify_line_config(db, tenant_id, bot_info_client=bot_info_client)
    except HTTPException as exc:
        return templates.TemplateResponse(
            "_line_config_status.html",
            _ctx(request, actor, cfg=None, webhook_url=webhook_url_for(tenant_id),
                 action_base=f"/ui/admin/tenants/{tenant_id}/line-config", error=str(exc.detail)),
            status_code=exc.status_code,
        )
    return templates.TemplateResponse(
        "_line_config_status.html",
        _ctx(request, actor, cfg=cfg, webhook_url=webhook_url_for(tenant_id),
             action_base=f"/ui/admin/tenants/{tenant_id}/line-config"),
    )


@router.post("/admin/tenants/{tenant_id}/line-config/delete", response_class=HTMLResponse)
def admin_line_config_delete(
    request: Request,
    tenant_id: int,
    actor: Actor = Depends(require_ui_admin),
    db: Session = Depends(get_db),
):
    try:
        line_config_svc.delete_line_config(db, tenant_id)
    except HTTPException:
        pass
    return templates.TemplateResponse(
        "_line_config_status.html",
        _ctx(request, actor, cfg=None, webhook_url=webhook_url_for(tenant_id),
             action_base=f"/ui/admin/tenants/{tenant_id}/line-config"),
    )


# ── 店家自助：預約管理 ────────────────────────────────────────────────────────

def _parse_slot_start(value: str) -> datetime.datetime:
    """解析 datetime-local 表單字串（無時區）→ 視為 UTC 的 tz-aware datetime。"""
    dt = datetime.datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt


def _booking_ctx(request: Request, actor: Actor, db: Session, **extra) -> dict:
    """組預約頁 context：bot_mode、時段、預約、顧客。"""
    tid = actor.user.tenant_id
    cfg = _line_config_or_none(db, tid)
    customers = customers_svc.list_customers(db, tenant_id=tid)
    tenant_row = db.query(Tenant).filter(Tenant.id == tid).first()
    reminder_hours = (
        (tenant_row.reminder_hours_before if tenant_row else None)
        or settings.reminder_hours_before_default
    )
    return _ctx(
        request,
        actor,
        cfg=cfg,
        bot_mode=(cfg or {}).get("bot_mode", "translation"),
        has_line_config=cfg is not None,
        slots=slots_svc.list_slots(db, tenant_id=tid),
        reservations=booking_svc.list_reservations(db, tenant_id=tid),
        customers=customers,
        reminder_hours=reminder_hours,
        # 預約列以 customer_id 對應顧客檔，顯示可核對的 LINE 名稱/電話（免額外查詢）。
        customer_by_id={c.id: c for c in customers},
        **extra,
    )


@router.get("/booking", response_class=HTMLResponse)
def booking_page(
    request: Request,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    return templates.TemplateResponse(
        "booking.html", _booking_ctx(request, actor, db)
    )


@router.post("/booking/bot-mode", response_class=HTMLResponse)
def booking_set_bot_mode(
    request: Request,
    bot_mode: str = Form(...),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    tid = actor.user.tenant_id
    error = None
    try:
        line_config_svc.set_bot_mode(db, tid, bot_mode)
    except HTTPException as exc:
        error = str(exc.detail)
    return templates.TemplateResponse(
        "_booking_botmode.html", _booking_ctx(request, actor, db, error=error)
    )


@router.post("/booking/reminder-hours", response_class=HTMLResponse)
def booking_set_reminder_hours(
    request: Request,
    reminder_hours_before: int = Form(...),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    """設定「預約前幾小時提醒」（對標 vibeaico「自訂提醒時間（小時）」）。"""
    tid = actor.user.tenant_id
    error = None
    saved = False
    if reminder_hours_before < 1 or reminder_hours_before > 168:
        error = "提醒時間需介於 1 ～ 168 小時。"
    else:
        tenant_row = db.query(Tenant).filter(Tenant.id == tid).first()
        if tenant_row is not None:
            tenant_row.reminder_hours_before = reminder_hours_before
            db.commit()
            saved = True
    return templates.TemplateResponse(
        "_booking_reminder.html",
        _booking_ctx(request, actor, db, error=error, saved=saved),
    )


@router.post("/booking/slots", response_class=HTMLResponse)
def booking_create_slot(
    request: Request,
    slot_start: str = Form(...),
    max_capacity: int = Form(...),
    walkin_reserved: int = Form(0),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    tid = actor.user.tenant_id
    error = None
    try:
        slots_svc.create_slot(
            db,
            tenant_id=tid,
            slot_start=_parse_slot_start(slot_start),
            max_capacity=max_capacity,
            walkin_reserved=walkin_reserved,
        )
    except HTTPException as exc:
        error = str(exc.detail)
    except ValueError:
        error = "時段時間格式錯誤"
    return templates.TemplateResponse(
        "_booking_slots.html", _booking_ctx(request, actor, db, error=error)
    )


@router.post("/booking/slots/bulk", response_class=HTMLResponse)
def booking_bulk_slots(
    request: Request,
    date_start: str = Form(...),
    date_end: str = Form(...),
    time_start: str = Form(...),
    time_end: str = Form(...),
    interval_minutes: int = Form(...),
    max_capacity: int = Form(...),
    walkin_reserved: int = Form(0),
    weekdays: list[str] = Form(default=[]),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    """批次產生時段：日期區間 × 每日營業時間 × 間隔，一鍵展開。"""
    tid = actor.user.tenant_id
    error = None
    bulk_result = None
    try:
        wd = {int(w) for w in weekdays if w.strip() != ""}
        bulk_result = slots_svc.bulk_generate_slots(
            db,
            tenant_id=tid,
            date_start=datetime.date.fromisoformat(date_start),
            date_end=datetime.date.fromisoformat(date_end),
            time_start=datetime.time.fromisoformat(time_start),
            time_end=datetime.time.fromisoformat(time_end),
            interval_minutes=interval_minutes,
            max_capacity=max_capacity,
            walkin_reserved=walkin_reserved,
            weekdays=wd or None,
        )
    except HTTPException as exc:
        error = str(exc.detail)
    except ValueError:
        error = "日期或時間格式錯誤"
    return templates.TemplateResponse(
        "_booking_slots.html",
        _booking_ctx(request, actor, db, error=error, bulk_result=bulk_result),
    )


@router.post("/booking/slots/{slot_id}/deactivate", response_class=HTMLResponse)
def booking_deactivate_slot(
    request: Request,
    slot_id: int,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    tid = actor.user.tenant_id
    try:
        slots_svc.deactivate_slot(db, tenant_id=tid, slot_id=slot_id)
    except HTTPException:
        pass
    return templates.TemplateResponse(
        "_booking_slots.html", _booking_ctx(request, actor, db)
    )


@router.post("/booking/reservations/{reservation_id}/cancel", response_class=HTMLResponse)
def booking_cancel_reservation(
    request: Request,
    reservation_id: int,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    tid = actor.user.tenant_id
    try:
        booking_svc.cancel_reservation(
            db, tenant_id=tid, reservation_id=reservation_id
        )
    except booking_svc.ReservationNotFoundError:
        pass
    return templates.TemplateResponse(
        "_booking_reservations.html", _booking_ctx(request, actor, db)
    )


@router.post("/booking/reservations/{reservation_id}/attendance", response_class=HTMLResponse)
def booking_mark_attendance(
    request: Request,
    reservation_id: int,
    attended: str = Form(...),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    tid = actor.user.tenant_id
    try:
        booking_svc.mark_attendance(
            db, tenant_id=tid, reservation_id=reservation_id,
            attended=(attended == "true"),
        )
    except booking_svc.ReservationNotFoundError:
        pass
    return templates.TemplateResponse(
        "_booking_reservations.html", _booking_ctx(request, actor, db)
    )


# ── 店家自助：商品銷售 ────────────────────────────────────────────────────────

def _shop_ctx(request: Request, actor: Actor, db: Session, **extra) -> dict:
    tid = actor.user.tenant_id
    return _ctx(
        request, actor,
        products=shop_svc.list_products(db, tenant_id=tid),
        orders=shop_svc.list_orders(db, tenant_id=tid),
        **extra,
    )


def _feature_locked(request: Request, actor: Actor, feature: str, label: str):
    return templates.TemplateResponse(
        "feature_locked.html",
        _ctx(request, actor, feature=feature, feature_label=label),
    )


@router.get("/shop", response_class=HTMLResponse)
def shop_page(
    request: Request,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not features_svc.is_enabled(db, actor.user.tenant_id, features_svc.PRODUCT_SALES):
        return _feature_locked(request, actor, features_svc.PRODUCT_SALES, "商品銷售")
    return templates.TemplateResponse("shop.html", _shop_ctx(request, actor, db))


@router.post("/shop/products", response_class=HTMLResponse)
def shop_create_product(
    request: Request,
    name: str = Form(...),
    price_cents: int = Form(...),
    stock: str = Form(""),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    tid = actor.user.tenant_id
    error = None
    try:
        shop_svc.create_product(
            db, tenant_id=tid, name=name, price_cents=price_cents,
            stock=int(stock) if stock.strip() else None,
        )
    except HTTPException as exc:
        error = str(exc.detail)
    except ValueError:
        error = "庫存需為整數"
    return templates.TemplateResponse("_shop.html", _shop_ctx(request, actor, db, error=error))


@router.post("/shop/products/{product_id}/deactivate", response_class=HTMLResponse)
def shop_deactivate_product(
    request: Request,
    product_id: int,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    tid = actor.user.tenant_id
    try:
        shop_svc.deactivate_product(db, tenant_id=tid, product_id=product_id)
    except HTTPException:
        pass
    return templates.TemplateResponse("_shop.html", _shop_ctx(request, actor, db))


@router.get("/shop/products/{product_id}/edit", response_class=HTMLResponse)
def shop_edit_product_form(
    request: Request,
    product_id: int,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    return templates.TemplateResponse(
        "_shop.html", _shop_ctx(request, actor, db, editing_id=product_id)
    )


@router.post("/shop/products/{product_id}/update", response_class=HTMLResponse)
def shop_update_product(
    request: Request,
    product_id: int,
    name: str = Form(...),
    price_cents: int = Form(...),
    stock: str = Form(""),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    tid = actor.user.tenant_id
    error = None
    editing_id = None
    try:
        shop_svc.update_product(
            db, tenant_id=tid, product_id=product_id, name=name,
            price_cents=price_cents,
            stock=int(stock) if stock.strip() else None,
        )
    except HTTPException as exc:
        error = str(exc.detail)
        editing_id = product_id
    except ValueError:
        error = "庫存需為整數"
        editing_id = product_id
    return templates.TemplateResponse(
        "_shop.html", _shop_ctx(request, actor, db, error=error, editing_id=editing_id)
    )


@router.post("/shop/products/{product_id}/delete", response_class=HTMLResponse)
def shop_delete_product(
    request: Request,
    product_id: int,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    tid = actor.user.tenant_id
    error = None
    try:
        shop_svc.delete_product(db, tenant_id=tid, product_id=product_id)
    except HTTPException as exc:
        error = str(exc.detail)
    return templates.TemplateResponse(
        "_shop.html", _shop_ctx(request, actor, db, error=error)
    )


@router.post("/shop/orders/{order_id}/pay", response_class=HTMLResponse)
def shop_pay_order(
    request: Request,
    order_id: int,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    tid = actor.user.tenant_id
    try:
        shop_svc.mark_order_paid(db, tenant_id=tid, order_id=order_id)
    except shop_svc.OrderNotFound:
        pass
    return templates.TemplateResponse("_shop.html", _shop_ctx(request, actor, db))


@router.post("/shop/orders/{order_id}/cancel", response_class=HTMLResponse)
def shop_cancel_order(
    request: Request,
    order_id: int,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    tid = actor.user.tenant_id
    try:
        shop_svc.cancel_order(db, tenant_id=tid, order_id=order_id)
    except shop_svc.OrderNotFound:
        pass
    return templates.TemplateResponse("_shop.html", _shop_ctx(request, actor, db))


# ── 店家自助：報表 ────────────────────────────────────────────────────────────

@router.get("/reports", response_class=HTMLResponse)
def reports_page(
    request: Request,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    tid = actor.user.tenant_id
    return templates.TemplateResponse(
        "reports.html",
        _ctx(
            request, actor,
            summary=analytics_svc.booking_summary(db, tenant_id=tid),
            utilization=analytics_svc.slot_utilization(db, tenant_id=tid),
            top=analytics_svc.top_customers(db, tenant_id=tid, limit=10),
        ),
    )


# ── 店家自助：Rich Menu 圖文選單 ──────────────────────────────────────────────

def _rich_menu_ctx(request: Request, actor: Actor, db: Session, **extra) -> dict:
    tid = actor.user.tenant_id
    cfg = _line_config_or_none(db, tid)
    status_dict = None
    if cfg is not None:
        status_dict = rich_menu_svc.get_rich_menu_status(db, tid)
    return _ctx(
        request,
        actor,
        has_line_config=cfg is not None,
        status=status_dict,
        templates_opts=rich_menu_svc.TEMPLATES,
        themes_opts=list(rich_menu_svc.THEMES.keys()),
        **extra,
    )


@router.get("/rich-menu", response_class=HTMLResponse)
def rich_menu_page(
    request: Request,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    return templates.TemplateResponse(
        "rich_menu.html", _rich_menu_ctx(request, actor, db)
    )


@router.post("/rich-menu/apply", response_class=HTMLResponse)
def rich_menu_apply(
    request: Request,
    template: str = Form(...),
    theme: str = Form(...),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
    rich_menu_client: LineRichMenuClient = Depends(get_rich_menu_client),
):
    tid = actor.user.tenant_id
    error = None
    try:
        rich_menu_svc.apply_rich_menu(
            db, tid, template=template, theme=theme, client=rich_menu_client
        )
    except HTTPException as exc:
        error = str(exc.detail)
    return templates.TemplateResponse(
        "_rich_menu_status.html", _rich_menu_ctx(request, actor, db, error=error)
    )


# ── 店家自助：優惠券 ──────────────────────────────────────────────────────────

def _coupons_ctx(request: Request, actor: Actor, db: Session, **extra) -> dict:
    tid = actor.user.tenant_id
    return _ctx(
        request, actor,
        coupons=coupons_svc.list_coupons(db, tenant_id=tid),
        **extra,
    )


@router.get("/coupons", response_class=HTMLResponse)
def coupons_page(
    request: Request,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not features_svc.is_enabled(db, actor.user.tenant_id, features_svc.COUPON_SYSTEM):
        return _feature_locked(request, actor, features_svc.COUPON_SYSTEM, "優惠券／會員")
    return templates.TemplateResponse("coupons.html", _coupons_ctx(request, actor, db))


@router.post("/coupons", response_class=HTMLResponse)
def coupons_create(
    request: Request,
    code: str = Form(...),
    name: str = Form(...),
    discount_type: str = Form(...),
    discount_value: int = Form(...),
    max_redemptions: str = Form(""),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.COUPON_SYSTEM):
        return _feature_locked(request, actor, features_svc.COUPON_SYSTEM, "優惠券／會員")
    tid = actor.user.tenant_id
    error = None
    try:
        coupons_svc.create_coupon(
            db, tenant_id=tid, code=code, name=name,
            discount_type=discount_type, discount_value=discount_value,
            max_redemptions=int(max_redemptions) if max_redemptions.strip() else None,
        )
    except HTTPException as exc:
        error = str(exc.detail)
    except ValueError:
        error = "兌換上限需為整數"
    return templates.TemplateResponse(
        "_coupons_list.html", _coupons_ctx(request, actor, db, error=error)
    )


@router.post("/coupons/{coupon_id}/deactivate", response_class=HTMLResponse)
def coupons_deactivate(
    request: Request,
    coupon_id: int,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    tid = actor.user.tenant_id
    try:
        coupons_svc.deactivate_coupon(db, tenant_id=tid, coupon_id=coupon_id)
    except HTTPException:
        pass
    return templates.TemplateResponse(
        "_coupons_list.html", _coupons_ctx(request, actor, db)
    )


@router.get("/coupons/{coupon_id}/edit", response_class=HTMLResponse)
def coupons_edit_form(
    request: Request,
    coupon_id: int,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.COUPON_SYSTEM):
        return _feature_locked(request, actor, features_svc.COUPON_SYSTEM, "優惠券／會員")
    return templates.TemplateResponse(
        "_coupons_list.html", _coupons_ctx(request, actor, db, editing_id=coupon_id)
    )


@router.post("/coupons/{coupon_id}/update", response_class=HTMLResponse)
def coupons_update(
    request: Request,
    coupon_id: int,
    name: str = Form(...),
    max_redemptions: str = Form(""),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.COUPON_SYSTEM):
        return _feature_locked(request, actor, features_svc.COUPON_SYSTEM, "優惠券／會員")
    tid = actor.user.tenant_id
    error = None
    editing_id = None
    try:
        coupons_svc.update_coupon(
            db, tenant_id=tid, coupon_id=coupon_id, name=name,
            max_redemptions=int(max_redemptions) if max_redemptions.strip() else None,
        )
    except HTTPException as exc:
        error = str(exc.detail)
        editing_id = coupon_id
    except ValueError:
        error = "兌換上限需為整數"
        editing_id = coupon_id
    return templates.TemplateResponse(
        "_coupons_list.html",
        _coupons_ctx(request, actor, db, error=error, editing_id=editing_id),
    )


@router.post("/coupons/{coupon_id}/delete", response_class=HTMLResponse)
def coupons_delete(
    request: Request,
    coupon_id: int,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.COUPON_SYSTEM):
        return _feature_locked(request, actor, features_svc.COUPON_SYSTEM, "優惠券／會員")
    tid = actor.user.tenant_id
    error = None
    try:
        coupons_svc.delete_coupon(db, tenant_id=tid, coupon_id=coupon_id)
    except HTTPException as exc:
        error = str(exc.detail)
    return templates.TemplateResponse(
        "_coupons_list.html", _coupons_ctx(request, actor, db, error=error)
    )


# ── 店家自助：進階功能訂閱 ────────────────────────────────────────────────────

def _features_ctx(request: Request, actor: Actor, db: Session, **extra) -> dict:
    return _ctx(
        request, actor,
        features=features_svc.list_for_tenant(db, actor.user.tenant_id),
        **extra,
    )


@router.get("/features", response_class=HTMLResponse)
def features_page(
    request: Request,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    return templates.TemplateResponse("features.html", _features_ctx(request, actor, db))


@router.post("/features/{feature}/subscribe", response_class=HTMLResponse)
def features_subscribe(
    request: Request,
    feature: str,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    tenant = db.get(Tenant, actor.user.tenant_id)
    try:
        features_svc.validate_feature(feature)
        result = billing_svc.subscribe_feature(db, tenant, feature, actor.user.id)
    except features_svc.UnknownFeatureError:
        result = None
    # ecpay 模式：尚未開通，導向綠界定期定額付款頁（首期授權成功後自動開通）。
    if result is not None and result.checkout_url:
        url = html.escape(result.checkout_url)
        return HTMLResponse(
            '<div class="card success">'
            f"<p>請完成綠界信用卡定期定額授權以開通「{html.escape(feature)}」。</p>"
            f'<a class="btn" href="{url}" target="_blank" rel="noopener">前往綠界付款</a>'
            "<p class=\"muted\">完成首期授權後，功能將自動開通；可重新整理本頁查看狀態。</p>"
            "</div>"
        )
    return templates.TemplateResponse("_features_list.html", _features_ctx(request, actor, db))


@router.post("/features/{feature}/unsubscribe", response_class=HTMLResponse)
def features_unsubscribe(
    request: Request,
    feature: str,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    tenant = db.get(Tenant, actor.user.tenant_id)
    try:
        features_svc.validate_feature(feature)
        billing_svc.unsubscribe_feature(db, tenant, feature, actor.user.id)
    except features_svc.UnknownFeatureError:
        pass
    return templates.TemplateResponse("_features_list.html", _features_ctx(request, actor, db))


@router.post("/rich-menu/clear", response_class=HTMLResponse)
def rich_menu_clear(
    request: Request,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
    rich_menu_client: LineRichMenuClient = Depends(get_rich_menu_client),
):
    tid = actor.user.tenant_id
    error = None
    try:
        rich_menu_svc.clear_rich_menu(db, tid, client=rich_menu_client)
    except HTTPException as exc:
        error = str(exc.detail)
    return templates.TemplateResponse(
        "_rich_menu_status.html", _rich_menu_ctx(request, actor, db, error=error)
    )


# ── 共用：選填整數解析 ──────────────────────────────────────────────────────────

def _opt_int(value: str) -> int | None:
    """空字串 → None；否則轉 int（非法拋 ValueError，由呼叫端轉 error）。"""
    value = (value or "").strip()
    return int(value) if value else None


def _require_ui_feature(db: Session, actor: Actor, feature: str) -> bool:
    return features_svc.is_enabled(db, actor.user.tenant_id, feature)


# ── 店家自助：分店（MULTI_LOCATION） ─────────────────────────────────────────────

def _locations_ctx(request: Request, actor: Actor, db: Session, **extra) -> dict:
    tid = actor.user.tenant_id
    rows = locations_svc.list_locations(db, tenant_id=tid)
    active_count = sum(1 for l in rows if l.is_active)
    return _ctx(
        request, actor,
        locations=rows,
        active_count=active_count,
        max_locations=settings.max_locations_per_tenant,
        **extra,
    )


@router.get("/locations", response_class=HTMLResponse)
def locations_page(
    request: Request,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.MULTI_LOCATION):
        return _feature_locked(request, actor, features_svc.MULTI_LOCATION, "多分店")
    return templates.TemplateResponse("locations.html", _locations_ctx(request, actor, db))


@router.post("/locations", response_class=HTMLResponse)
def locations_create(
    request: Request,
    name: str = Form(...),
    address: str = Form(""),
    phone: str = Form(""),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.MULTI_LOCATION):
        return _feature_locked(request, actor, features_svc.MULTI_LOCATION, "多分店")
    tid = actor.user.tenant_id
    error = None
    try:
        locations_svc.create_location(
            db, tenant_id=tid, name=name,
            address=address or None, phone=phone or None,
        )
    except locations_svc.LocationLimitError as exc:
        error = str(exc)
    except HTTPException as exc:
        error = str(exc.detail)
    return templates.TemplateResponse(
        "_locations.html", _locations_ctx(request, actor, db, error=error)
    )


@router.post("/locations/{location_id}/update", response_class=HTMLResponse)
def locations_update(
    request: Request,
    location_id: int,
    name: str = Form(...),
    address: str = Form(""),
    phone: str = Form(""),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.MULTI_LOCATION):
        return _feature_locked(request, actor, features_svc.MULTI_LOCATION, "多分店")
    tid = actor.user.tenant_id
    try:
        locations_svc.update_location(
            db, tenant_id=tid, location_id=location_id,
            name=name, address=address or None, phone=phone or None,
        )
    except HTTPException:
        pass
    return templates.TemplateResponse(
        "_locations.html", _locations_ctx(request, actor, db)
    )


@router.post("/locations/{location_id}/deactivate", response_class=HTMLResponse)
def locations_deactivate(
    request: Request,
    location_id: int,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.MULTI_LOCATION):
        return _feature_locked(request, actor, features_svc.MULTI_LOCATION, "多分店")
    tid = actor.user.tenant_id
    try:
        locations_svc.update_location(
            db, tenant_id=tid, location_id=location_id, is_active=False
        )
    except HTTPException:
        pass
    return templates.TemplateResponse(
        "_locations.html", _locations_ctx(request, actor, db)
    )


@router.post("/locations/{location_id}/activate", response_class=HTMLResponse)
def locations_activate(
    request: Request,
    location_id: int,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.MULTI_LOCATION):
        return _feature_locked(request, actor, features_svc.MULTI_LOCATION, "多分店")
    tid = actor.user.tenant_id
    try:
        locations_svc.update_location(
            db, tenant_id=tid, location_id=location_id, is_active=True
        )
    except HTTPException:
        pass
    return templates.TemplateResponse(
        "_locations.html", _locations_ctx(request, actor, db)
    )


@router.post("/locations/{location_id}/delete", response_class=HTMLResponse)
def locations_delete(
    request: Request,
    location_id: int,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.MULTI_LOCATION):
        return _feature_locked(request, actor, features_svc.MULTI_LOCATION, "多分店")
    tid = actor.user.tenant_id
    error = None
    try:
        locations_svc.delete_location(db, tenant_id=tid, location_id=location_id)
    except HTTPException as exc:
        error = str(exc.detail)
    return templates.TemplateResponse(
        "_locations.html", _locations_ctx(request, actor, db, error=error)
    )


# ── 店家自助：員工（STAFF_SCHEDULING） ──────────────────────────────────────────

def _staff_ctx(request: Request, actor: Actor, db: Session, **extra) -> dict:
    tid = actor.user.tenant_id
    rows = staff_svc.list_staff(db, tenant_id=tid)
    shifts = {s.id: staff_svc.list_shifts(db, tenant_id=tid, staff_id=s.id) for s in rows}
    leaves = {s.id: staff_svc.list_leaves(db, tenant_id=tid, staff_id=s.id) for s in rows}
    return _ctx(
        request, actor,
        staff_rows=rows,
        staff_shifts=shifts,
        staff_leaves=leaves,
        shift_templates=staff_svc.SHIFT_TEMPLATES,
        locations=locations_svc.list_locations(db, tenant_id=tid),
        **extra,
    )


@router.get("/staff", response_class=HTMLResponse)
def staff_page(
    request: Request,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.STAFF_SCHEDULING):
        return _feature_locked(request, actor, features_svc.STAFF_SCHEDULING, "員工排班")
    return templates.TemplateResponse("staff.html", _staff_ctx(request, actor, db))


@router.post("/staff", response_class=HTMLResponse)
def staff_create(
    request: Request,
    name: str = Form(...),
    role: str = Form(""),
    location_id: str = Form(""),
    booking_mode: str = Form("capacity"),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.STAFF_SCHEDULING):
        return _feature_locked(request, actor, features_svc.STAFF_SCHEDULING, "員工排班")
    tid = actor.user.tenant_id
    error = None
    try:
        staff_svc.create_staff(
            db, tenant_id=tid, name=name, role=role or None,
            location_id=_opt_int(location_id), booking_mode=booking_mode,
        )
    except HTTPException as exc:
        error = str(exc.detail)
    except ValueError:
        error = "分店格式錯誤"
    return templates.TemplateResponse(
        "_staff_list.html", _staff_ctx(request, actor, db, error=error)
    )


@router.post("/staff/{staff_id}/update", response_class=HTMLResponse)
def staff_update(
    request: Request,
    staff_id: int,
    name: str = Form(...),
    role: str = Form(""),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.STAFF_SCHEDULING):
        return _feature_locked(request, actor, features_svc.STAFF_SCHEDULING, "員工排班")
    tid = actor.user.tenant_id
    try:
        staff_svc.update_staff(
            db, tenant_id=tid, staff_id=staff_id, name=name, role=role or None,
        )
    except HTTPException:
        pass
    return templates.TemplateResponse(
        "_staff_list.html", _staff_ctx(request, actor, db)
    )


@router.post("/staff/{staff_id}/deactivate", response_class=HTMLResponse)
def staff_deactivate(
    request: Request,
    staff_id: int,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.STAFF_SCHEDULING):
        return _feature_locked(request, actor, features_svc.STAFF_SCHEDULING, "員工排班")
    tid = actor.user.tenant_id
    try:
        staff_svc.update_staff(db, tenant_id=tid, staff_id=staff_id, is_active=False)
    except HTTPException:
        pass
    return templates.TemplateResponse("_staff_list.html", _staff_ctx(request, actor, db))


@router.post("/staff/{staff_id}/activate", response_class=HTMLResponse)
def staff_activate(
    request: Request,
    staff_id: int,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.STAFF_SCHEDULING):
        return _feature_locked(request, actor, features_svc.STAFF_SCHEDULING, "員工排班")
    tid = actor.user.tenant_id
    try:
        staff_svc.update_staff(db, tenant_id=tid, staff_id=staff_id, is_active=True)
    except HTTPException:
        pass
    return templates.TemplateResponse("_staff_list.html", _staff_ctx(request, actor, db))


@router.post("/staff/{staff_id}/delete", response_class=HTMLResponse)
def staff_delete(
    request: Request,
    staff_id: int,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.STAFF_SCHEDULING):
        return _feature_locked(request, actor, features_svc.STAFF_SCHEDULING, "員工排班")
    tid = actor.user.tenant_id
    error = None
    try:
        staff_svc.delete_staff(db, tenant_id=tid, staff_id=staff_id)
    except HTTPException as exc:
        error = str(exc.detail)
    return templates.TemplateResponse(
        "_staff_list.html", _staff_ctx(request, actor, db, error=error)
    )


@router.post("/staff/{staff_id}/rotate-token", response_class=HTMLResponse)
def staff_rotate_token(
    request: Request,
    staff_id: int,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.STAFF_SCHEDULING):
        return _feature_locked(request, actor, features_svc.STAFF_SCHEDULING, "員工排班")
    tid = actor.user.tenant_id
    try:
        staff_svc.rotate_token(db, tenant_id=tid, staff_id=staff_id)
    except HTTPException:
        pass
    return templates.TemplateResponse("_staff_list.html", _staff_ctx(request, actor, db))


@router.post("/staff/{staff_id}/shifts", response_class=HTMLResponse)
def staff_create_shift(
    request: Request,
    staff_id: int,
    start_time: str = Form(...),
    end_time: str = Form(...),
    weekday: str = Form(""),
    rotation: str = Form(""),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.STAFF_SCHEDULING):
        return _feature_locked(request, actor, features_svc.STAFF_SCHEDULING, "員工排班")
    tid = actor.user.tenant_id
    error = None
    try:
        staff_svc.create_shift(
            db, tenant_id=tid, staff_id=staff_id,
            start_time=start_time, end_time=end_time,
            weekday=_opt_int(weekday), rotation=rotation or None,
        )
    except HTTPException as exc:
        error = str(exc.detail)
    except ValueError:
        error = "星期格式錯誤"
    return templates.TemplateResponse(
        "_staff_list.html", _staff_ctx(request, actor, db, error=error)
    )


@router.post("/staff/{staff_id}/shifts/bulk", response_class=HTMLResponse)
def staff_bulk_shifts(
    request: Request,
    staff_id: int,
    template: str = Form(...),
    weekdays: list[str] = Form(default=[]),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    """以內建模板批量排班（對標 vibeaico「內建模板一鍵套用」）。"""
    if not _require_ui_feature(db, actor, features_svc.STAFF_SCHEDULING):
        return _feature_locked(request, actor, features_svc.STAFF_SCHEDULING, "員工排班")
    tid = actor.user.tenant_id
    error = None
    saved = None
    try:
        wd = [int(w) for w in weekdays if w != ""]
        result = staff_svc.bulk_create_shifts_from_template(
            db, tenant_id=tid, staff_id=staff_id, template=template, weekdays=wd,
        )
        saved = f"已套用模板：新增 {result['created']} 筆、略過 {result['skipped']} 筆（已存在）。"
    except HTTPException as exc:
        error = str(exc.detail)
    except ValueError:
        error = "星期格式錯誤"
    return templates.TemplateResponse(
        "_staff_list.html", _staff_ctx(request, actor, db, error=error, bulk_msg=saved)
    )


@router.post("/staff/{staff_id}/shifts/{shift_id}/delete", response_class=HTMLResponse)
def staff_delete_shift(
    request: Request,
    staff_id: int,
    shift_id: int,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.STAFF_SCHEDULING):
        return _feature_locked(request, actor, features_svc.STAFF_SCHEDULING, "員工排班")
    tid = actor.user.tenant_id
    try:
        staff_svc.delete_shift(db, tenant_id=tid, staff_id=staff_id, shift_id=shift_id)
    except HTTPException:
        pass
    return templates.TemplateResponse("_staff_list.html", _staff_ctx(request, actor, db))


@router.post("/staff/{staff_id}/leaves", response_class=HTMLResponse)
def staff_create_leave(
    request: Request,
    staff_id: int,
    start_at: str = Form(...),
    end_at: str = Form(...),
    reason: str = Form(""),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.STAFF_SCHEDULING):
        return _feature_locked(request, actor, features_svc.STAFF_SCHEDULING, "員工排班")
    tid = actor.user.tenant_id
    error = None
    try:
        staff_svc.create_leave(
            db, tenant_id=tid, staff_id=staff_id,
            start_at=_parse_slot_start(start_at),
            end_at=_parse_slot_start(end_at),
            reason=reason or None,
        )
    except HTTPException as exc:
        error = str(exc.detail)
    except ValueError:
        error = "請假時間格式錯誤"
    return templates.TemplateResponse(
        "_staff_list.html", _staff_ctx(request, actor, db, error=error)
    )


@router.post("/staff/{staff_id}/leaves/{leave_id}/delete", response_class=HTMLResponse)
def staff_delete_leave(
    request: Request,
    staff_id: int,
    leave_id: int,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.STAFF_SCHEDULING):
        return _feature_locked(request, actor, features_svc.STAFF_SCHEDULING, "員工排班")
    tid = actor.user.tenant_id
    try:
        staff_svc.delete_leave(db, tenant_id=tid, staff_id=staff_id, leave_id=leave_id)
    except HTTPException:
        pass
    return templates.TemplateResponse("_staff_list.html", _staff_ctx(request, actor, db))


# ── 店家自助：服務項目（SERVICE_CATALOG） ───────────────────────────────────────

def _services_ctx(request: Request, actor: Actor, db: Session, **extra) -> dict:
    tid = actor.user.tenant_id
    services = catalog_svc.list_services(db, tenant_id=tid)
    staff_rows = staff_svc.list_staff(db, tenant_id=tid)
    staff_by_id = {s.id: s for s in staff_rows}
    svc_staff: dict[int, list] = {}
    for svc in services:
        links = catalog_svc.list_service_staff(db, tenant_id=tid, service_id=svc.id)
        svc_staff[svc.id] = [staff_by_id[ln.staff_id] for ln in links if ln.staff_id in staff_by_id]
    return _ctx(
        request, actor,
        categories=catalog_svc.list_categories(db, tenant_id=tid),
        services=services,
        staff_rows=staff_rows,
        service_staff=svc_staff,
        locations=locations_svc.list_locations(db, tenant_id=tid),
        **extra,
    )


@router.get("/services", response_class=HTMLResponse)
def services_page(
    request: Request,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.SERVICE_CATALOG):
        return _feature_locked(request, actor, features_svc.SERVICE_CATALOG, "服務項目")
    return templates.TemplateResponse("services.html", _services_ctx(request, actor, db))


@router.post("/services/categories", response_class=HTMLResponse)
def services_create_category(
    request: Request,
    name: str = Form(...),
    sort_order: int = Form(0),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.SERVICE_CATALOG):
        return _feature_locked(request, actor, features_svc.SERVICE_CATALOG, "服務項目")
    tid = actor.user.tenant_id
    error = None
    try:
        catalog_svc.create_category(db, tenant_id=tid, name=name, sort_order=sort_order)
    except HTTPException as exc:
        error = str(exc.detail)
    return templates.TemplateResponse(
        "_services_list.html", _services_ctx(request, actor, db, error=error)
    )


@router.post("/services/categories/{category_id}/edit", response_class=HTMLResponse)
def services_update_category(
    request: Request,
    category_id: int,
    name: str = Form(...),
    sort_order: int = Form(0),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.SERVICE_CATALOG):
        return _feature_locked(request, actor, features_svc.SERVICE_CATALOG, "服務項目")
    tid = actor.user.tenant_id
    error = None
    try:
        catalog_svc.update_category(
            db, tenant_id=tid, category_id=category_id,
            name=name, sort_order=sort_order,
        )
    except HTTPException as exc:
        error = str(exc.detail)
    return templates.TemplateResponse(
        "_services_list.html", _services_ctx(request, actor, db, error=error)
    )


@router.post("/services/categories/{category_id}/delete", response_class=HTMLResponse)
def services_delete_category(
    request: Request,
    category_id: int,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.SERVICE_CATALOG):
        return _feature_locked(request, actor, features_svc.SERVICE_CATALOG, "服務項目")
    tid = actor.user.tenant_id
    try:
        catalog_svc.delete_category(db, tenant_id=tid, category_id=category_id)
    except HTTPException:
        pass
    return templates.TemplateResponse(
        "_services_list.html", _services_ctx(request, actor, db)
    )


@router.post("/services", response_class=HTMLResponse)
def services_create(
    request: Request,
    name: str = Form(...),
    duration_minutes: int = Form(60),
    price_cents: int = Form(0),
    category_id: str = Form(""),
    location_id: str = Form(""),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.SERVICE_CATALOG):
        return _feature_locked(request, actor, features_svc.SERVICE_CATALOG, "服務項目")
    tid = actor.user.tenant_id
    error = None
    try:
        catalog_svc.create_service(
            db, tenant_id=tid, name=name,
            duration_minutes=duration_minutes, price_cents=price_cents,
            category_id=_opt_int(category_id), location_id=_opt_int(location_id),
        )
    except HTTPException as exc:
        error = str(exc.detail)
    except ValueError:
        error = "分類或分店格式錯誤"
    return templates.TemplateResponse(
        "_services_list.html", _services_ctx(request, actor, db, error=error)
    )


@router.post("/services/{service_id}/edit", response_class=HTMLResponse)
def services_update(
    request: Request,
    service_id: int,
    name: str = Form(...),
    duration_minutes: int = Form(60),
    price_cents: int = Form(0),
    category_id: str = Form(""),
    location_id: str = Form(""),
    is_active: str = Form(""),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.SERVICE_CATALOG):
        return _feature_locked(request, actor, features_svc.SERVICE_CATALOG, "服務項目")
    tid = actor.user.tenant_id
    error = None
    try:
        catalog_svc.update_service(
            db, tenant_id=tid, service_id=service_id, name=name,
            duration_minutes=duration_minutes, price_cents=price_cents,
            category_id=_opt_int(category_id), location_id=_opt_int(location_id),
            is_active=(is_active == "on"),
        )
    except HTTPException as exc:
        error = str(exc.detail)
    except ValueError:
        error = "分類或分店格式錯誤"
    return templates.TemplateResponse(
        "_services_list.html", _services_ctx(request, actor, db, error=error)
    )


@router.post("/services/{service_id}/delete", response_class=HTMLResponse)
def services_delete(
    request: Request,
    service_id: int,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.SERVICE_CATALOG):
        return _feature_locked(request, actor, features_svc.SERVICE_CATALOG, "服務項目")
    tid = actor.user.tenant_id
    try:
        catalog_svc.delete_service(db, tenant_id=tid, service_id=service_id)
    except HTTPException:
        pass
    return templates.TemplateResponse(
        "_services_list.html", _services_ctx(request, actor, db)
    )


@router.post("/services/{service_id}/staff", response_class=HTMLResponse)
def services_assign_staff(
    request: Request,
    service_id: int,
    staff_id: int = Form(...),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.SERVICE_CATALOG):
        return _feature_locked(request, actor, features_svc.SERVICE_CATALOG, "服務項目")
    tid = actor.user.tenant_id
    error = None
    try:
        catalog_svc.assign_staff(
            db, tenant_id=tid, service_id=service_id, staff_id=staff_id
        )
    except HTTPException as exc:
        error = str(exc.detail)
    return templates.TemplateResponse(
        "_services_list.html", _services_ctx(request, actor, db, error=error)
    )


@router.post("/services/{service_id}/staff/{staff_id}/unassign", response_class=HTMLResponse)
def services_unassign_staff(
    request: Request,
    service_id: int,
    staff_id: int,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.SERVICE_CATALOG):
        return _feature_locked(request, actor, features_svc.SERVICE_CATALOG, "服務項目")
    tid = actor.user.tenant_id
    try:
        catalog_svc.unassign_staff(
            db, tenant_id=tid, service_id=service_id, staff_id=staff_id
        )
    except HTTPException:
        pass
    return templates.TemplateResponse(
        "_services_list.html", _services_ctx(request, actor, db)
    )


# ── 店家自助：顧客 CRM ─────────────────────────────────────────────────────────

_CUSTOMERS_PAGE_SIZE = 20


def _customers_ctx(
    request: Request,
    actor: Actor,
    db: Session,
    *,
    q: str = "",
    page: int = 1,
    **extra,
) -> dict:
    tid = actor.user.tenant_id
    total = customers_svc.count_customers(db, tenant_id=tid, q=q or None)
    pages = max(1, -(-total // _CUSTOMERS_PAGE_SIZE))  # ceil
    page = min(max(1, page), pages)
    rows = customers_svc.list_customers(
        db,
        tenant_id=tid,
        q=q or None,
        limit=_CUSTOMERS_PAGE_SIZE,
        offset=(page - 1) * _CUSTOMERS_PAGE_SIZE,
    )
    return _ctx(
        request, actor,
        customers=rows, q=q, page=page, pages=pages, total=total, **extra,
    )


def _customer_detail_ctx(
    request: Request, actor: Actor, db: Session, customer_id: int, **extra
) -> dict:
    from saas_mvp.models.booking_slot import BookingSlot
    from saas_mvp.models.point_transaction import PointTransaction

    tid = actor.user.tenant_id
    customer = customers_svc.get_customer(
        db, tenant_id=tid, customer_id=customer_id
    )  # 查無/跨租戶 → HTTPException 404
    all_tags = segments_svc.list_tags(db, tenant_id=tid)
    customer_tag_ids = {
        t.id
        for t in segments_svc.list_tags_for_customer(
            db, tenant_id=tid, customer_id=customer_id
        )
    }
    reservations = booking_svc.list_reservations(
        db, tenant_id=tid, line_user_id=customer.line_user_id
    )[-20:][::-1]  # 近 20 筆，新→舊
    slot_ids = [r.slot_id for r in reservations if r.slot_id is not None]
    slots = {}
    if slot_ids:
        slots = {
            s.id: s
            for s in tenant_query(db, BookingSlot, tid)
            .filter(BookingSlot.id.in_(slot_ids))
            .all()
        }
    ledger = (
        tenant_query(db, PointTransaction, tid)
        .filter(PointTransaction.customer_id == customer_id)
        .order_by(PointTransaction.id.desc())
        .limit(20)
        .all()
    )
    return _ctx(
        request, actor,
        customer=customer,
        all_tags=all_tags,
        customer_tag_ids=customer_tag_ids,
        reservations=reservations,
        slots=slots,
        ledger=ledger,
        **extra,
    )


@router.get("/customers", response_class=HTMLResponse)
def customers_page(
    request: Request,
    q: str = Query(default="", max_length=64),
    page: int = Query(default=1, ge=1),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    ctx = _customers_ctx(request, actor, db, q=q, page=page)
    if _is_htmx(request):
        return templates.TemplateResponse("_customers_list.html", ctx)
    return templates.TemplateResponse("customers.html", ctx)


@router.get("/customers/{customer_id}", response_class=HTMLResponse)
def customer_detail_page(
    request: Request,
    customer_id: int,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    try:
        ctx = _customer_detail_ctx(request, actor, db, customer_id)
    except HTTPException:
        return HTMLResponse("<h1>找不到顧客</h1>", status_code=status.HTTP_404_NOT_FOUND)
    return templates.TemplateResponse("customer_detail.html", ctx)


@router.post("/customers/tags", response_class=HTMLResponse)
def customer_create_tag(
    request: Request,
    name: str = Form(..., max_length=64),
    color: str = Form("", max_length=16),
    customer_id: int = Form(...),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    """從顧客 detail 頁快速建標籤（建後回同一 detail partial）。

    註：本路由須宣告於 /customers/{customer_id} 之前，否則 "tags" 會被
    當成 customer_id（比照 routers/customers.py 的順序註記）。
    """
    tid = actor.user.tenant_id
    error = None
    try:
        segments_svc.create_tag(
            db, tenant_id=tid, name=name.strip(), color=color.strip() or None
        )
    except HTTPException as exc:
        db.rollback()
        error = str(exc.detail)
    try:
        ctx = _customer_detail_ctx(request, actor, db, customer_id, error=error)
    except HTTPException:
        return HTMLResponse("<h1>找不到顧客</h1>", status_code=status.HTTP_404_NOT_FOUND)
    return templates.TemplateResponse("_customer_detail.html", ctx)


@router.post("/customers/{customer_id}", response_class=HTMLResponse)
def customer_update(
    request: Request,
    customer_id: int,
    phone: str = Form(""),
    note: str = Form(""),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    tid = actor.user.tenant_id
    error = None
    try:
        customers_svc.update_customer(
            db,
            tenant_id=tid,
            customer_id=customer_id,
            phone=phone.strip()[:32],
            note=note.strip()[:2048],
        )
    except HTTPException:
        return HTMLResponse("<h1>找不到顧客</h1>", status_code=status.HTTP_404_NOT_FOUND)
    return templates.TemplateResponse(
        "_customer_detail.html",
        _customer_detail_ctx(
            request, actor, db, customer_id,
            error=error, saved="基本資料已更新",
        ),
    )


@router.post("/customers/{customer_id}/tags", response_class=HTMLResponse)
async def customer_sync_tags(
    request: Request,
    customer_id: int,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    """整批同步顧客標籤：checkbox 勾選集合 vs 現況做 attach/detach。"""
    tid = actor.user.tenant_id
    form = await request.form()
    selected = {
        int(v) for v in form.getlist("tag_ids") if str(v).isdigit()
    }
    try:
        current = {
            t.id
            for t in segments_svc.list_tags_for_customer(
                db, tenant_id=tid, customer_id=customer_id
            )
        }
        for tag_id in selected - current:
            segments_svc.attach_tag(
                db, tenant_id=tid, customer_id=customer_id, tag_id=tag_id
            )
        for tag_id in current - selected:
            segments_svc.detach_tag(
                db, tenant_id=tid, customer_id=customer_id, tag_id=tag_id
            )
    except HTTPException:
        return HTMLResponse("<h1>找不到顧客</h1>", status_code=status.HTTP_404_NOT_FOUND)
    return templates.TemplateResponse(
        "_customer_detail.html",
        _customer_detail_ctx(request, actor, db, customer_id, saved="標籤已更新"),
    )


@router.post("/customers/{customer_id}/points", response_class=HTMLResponse)
def customer_adjust_points(
    request: Request,
    customer_id: int,
    delta: int = Form(...),
    reason: str = Form("manual", max_length=64),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    """店家手動調整點數（正加負扣）；扣點不足回錯誤訊息。"""
    tid = actor.user.tenant_id
    error = None
    saved = None
    try:
        customer = customers_svc.get_customer(
            db, tenant_id=tid, customer_id=customer_id
        )
        if delta > 0:
            membership_svc.earn_points(
                db, tenant_id=tid, customer=customer, delta=delta, reason=reason
            )
            db.commit()
            saved = f"已加 {delta} 點"
        elif delta < 0:
            try:
                membership_svc.redeem_points(
                    db, tenant_id=tid, customer=customer,
                    amount=-delta, reason=reason,
                )
                db.commit()
                saved = f"已扣 {-delta} 點"
            except membership_svc.InsufficientPoints:
                db.rollback()
                error = "點數不足，無法扣點"
    except HTTPException:
        return HTMLResponse("<h1>找不到顧客</h1>", status_code=status.HTTP_404_NOT_FOUND)
    return templates.TemplateResponse(
        "_customer_detail.html",
        _customer_detail_ctx(
            request, actor, db, customer_id, error=error, saved=saved
        ),
    )


# ── 店家自助：通知與推播歷程（唯讀） ───────────────────────────────────────────

_NOTIF_PAGE_SIZE = 50
_NOTIF_TABS = ("booking", "campaign", "usage")


def _notifications_ctx(
    request: Request,
    actor: Actor,
    db: Session,
    *,
    tab: str = "booking",
    status_filter: str = "",
    page: int = 1,
    **extra,
) -> dict:
    tid = actor.user.tenant_id
    if tab not in _NOTIF_TABS:
        tab = "booking"
    page = max(1, page)
    offset = (page - 1) * _NOTIF_PAGE_SIZE

    rows: list = []
    total = 0
    campaign_names: dict[int, str] = {}
    usage_history: list[dict] = []
    push_status: dict | None = None

    if tab == "booking":
        rows, total = notif_history_svc.list_booking_notifications(
            db, tenant_id=tid, status=status_filter or None,
            limit=_NOTIF_PAGE_SIZE, offset=offset,
        )
    elif tab == "campaign":
        rows, total = notif_history_svc.list_campaign_sends(
            db, tenant_id=tid, status=status_filter or None,
            limit=_NOTIF_PAGE_SIZE, offset=offset,
        )
        campaign_ids = {r.campaign_id for r in rows}
        if campaign_ids:
            campaign_names = {
                c.id: c.name
                for c in tenant_query(db, Campaign, tid)
                .filter(Campaign.id.in_(campaign_ids))
                .all()
            }
    else:  # usage
        usage_history = notif_history_svc.push_usage_history(db, tenant_id=tid)
        push_status = push_quota_svc.get_push_quota_status(db, tid)

    pages = max(1, -(-total // _NOTIF_PAGE_SIZE))  # ceil
    return _ctx(
        request, actor,
        tab=tab, status_filter=status_filter,
        rows=rows, total=total, page=min(page, pages), pages=pages,
        campaign_names=campaign_names,
        usage_history=usage_history, push_status=push_status,
        **extra,
    )


@router.get("/notifications", response_class=HTMLResponse)
def notifications_page(
    request: Request,
    tab: str = Query(default="booking"),
    status_filter: str = Query(default="", alias="status", max_length=16),
    page: int = Query(default=1, ge=1),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    ctx = _notifications_ctx(
        request, actor, db, tab=tab, status_filter=status_filter, page=page
    )
    if _is_htmx(request):
        return templates.TemplateResponse("_notifications_list.html", ctx)
    return templates.TemplateResponse("notifications.html", ctx)


# ── 店家自助：行銷活動（MARKETING_AUTO） ────────────────────────────────────────

def _describe_segment(segment_json: str | None, tag_names: dict[int, str]) -> list[str]:
    """把 segment_json 反解成人話 chips（列表頁顯示）。malformed 回原字串。"""
    import json as _json

    if not segment_json:
        return []
    try:
        filters = _json.loads(segment_json)
        if not isinstance(filters, dict):
            return [str(segment_json)]
    except ValueError:
        return [str(segment_json)]
    chips: list[str] = []
    if filters.get("tag_ids"):
        names = [
            tag_names.get(t, f"標籤#{t}")
            for t in filters["tag_ids"]
            if isinstance(t, int) or str(t).isdigit()
        ]
        if names:
            chips.append("標籤：" + "、".join(str(n) for n in names))
    if filters.get("tier"):
        chips.append(f"等級：{filters['tier']}")
    if filters.get("min_bookings") is not None:
        chips.append(f"預約 ≥ {filters['min_bookings']} 次")
    if filters.get("location_id") is not None:
        chips.append(f"分店 #{filters['location_id']}")
    return chips


def _campaigns_ctx(request: Request, actor: Actor, db: Session, **extra) -> dict:
    tid = actor.user.tenant_id
    rows = tenant_query(db, Campaign, tid).order_by(Campaign.id.desc()).all()
    tags = segments_svc.list_tags(db, tenant_id=tid)
    tag_names = {t.id: t.name for t in tags}
    locations = locations_svc.list_locations(db, tenant_id=tid)
    segment_chips = {
        c.id: _describe_segment(c.segment_json, tag_names) for c in rows
    }
    return _ctx(
        request, actor,
        campaigns=rows, tags=tags, locations=locations,
        segment_chips=segment_chips, **extra,
    )


def _campaign_or_none(db: Session, tenant_id: int, campaign_id: int) -> Campaign | None:
    return (
        tenant_query(db, Campaign, tenant_id)
        .filter(Campaign.id == campaign_id)
        .first()
    )


@router.get("/campaigns", response_class=HTMLResponse)
def campaigns_page(
    request: Request,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.MARKETING_AUTO):
        return _feature_locked(request, actor, features_svc.MARKETING_AUTO, "行銷自動化")
    return templates.TemplateResponse("campaigns.html", _campaigns_ctx(request, actor, db))


@router.post("/campaigns", response_class=HTMLResponse)
async def campaigns_create(
    request: Request,
    name: str = Form(...),
    type: str = Form(...),
    message_template: str = Form(...),
    schedule_at: str = Form(""),
    segment_tier: str = Form(""),
    segment_min_bookings: str = Form(""),
    segment_location_id: str = Form(""),
    segment_json: str = Form(""),
    reward_type: str = Form(""),
    reward_value: str = Form(""),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    import json as _json

    if not _require_ui_feature(db, actor, features_svc.MARKETING_AUTO):
        return _feature_locked(request, actor, features_svc.MARKETING_AUTO, "行銷自動化")
    tid = actor.user.tenant_id
    error = None
    try:
        schedule = _parse_slot_start(schedule_at) if schedule_at.strip() else None
        # 受眾：表單選擇器組 dict；「進階原始 JSON」有填則優先（power-user 相容）。
        seg = segment_json.strip()
        if seg:
            _json.loads(seg)  # 驗證 JSON 合法
        else:
            form = await request.form()
            filters: dict = {}
            tag_ids = [
                int(v) for v in form.getlist("segment_tag_ids")
                if str(v).isdigit()
            ]
            if tag_ids:
                filters["tag_ids"] = tag_ids
            if segment_tier.strip():
                filters["tier"] = segment_tier.strip()
            mb = _opt_int(segment_min_bookings)
            if mb is not None:
                filters["min_bookings"] = mb
            loc = _opt_int(segment_location_id)
            if loc is not None:
                filters["location_id"] = loc
            seg = _json.dumps(filters, ensure_ascii=False) if filters else ""
        campaign = Campaign(
            tenant_id=tid,
            name=name,
            type=type,
            message_template=message_template,
            schedule_at=schedule,
            segment_json=seg or None,
            reward_type=reward_type or None,
            reward_value=_opt_int(reward_value),
        )
        db.add(campaign)
        db.commit()
    except ValueError:
        db.rollback()
        error = "排程時間或受眾 JSON 格式錯誤"
    except HTTPException as exc:
        db.rollback()
        error = str(exc.detail)
    return templates.TemplateResponse(
        "_campaigns_list.html", _campaigns_ctx(request, actor, db, error=error)
    )


@router.post("/campaigns/{campaign_id}/run", response_class=HTMLResponse)
def campaigns_run(
    request: Request,
    campaign_id: int,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
    push_client: LinePushClient = Depends(get_push_client),
):
    if not _require_ui_feature(db, actor, features_svc.MARKETING_AUTO):
        return _feature_locked(request, actor, features_svc.MARKETING_AUTO, "行銷自動化")
    tid = actor.user.tenant_id
    error = None
    run_result = None
    campaign = _campaign_or_none(db, tid, campaign_id)
    if campaign is None:
        error = "活動不存在"
    else:
        run_result = marketing_svc.run_campaign(
            db,
            campaign=campaign,
            now=datetime.datetime.now(datetime.timezone.utc),
            cap=settings.marketing_max_per_run,
            push_client=push_client,
        )
    return templates.TemplateResponse(
        "_campaigns_list.html",
        _campaigns_ctx(request, actor, db, error=error, run_result=run_result),
    )


@router.post("/campaigns/{campaign_id}/deactivate", response_class=HTMLResponse)
def campaigns_deactivate(
    request: Request,
    campaign_id: int,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.MARKETING_AUTO):
        return _feature_locked(request, actor, features_svc.MARKETING_AUTO, "行銷自動化")
    tid = actor.user.tenant_id
    campaign = _campaign_or_none(db, tid, campaign_id)
    if campaign is not None:
        campaign.is_active = False
        db.commit()
    return templates.TemplateResponse(
        "_campaigns_list.html", _campaigns_ctx(request, actor, db)
    )


@router.get("/campaigns/{campaign_id}/edit", response_class=HTMLResponse)
def campaigns_edit_form(
    request: Request,
    campaign_id: int,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.MARKETING_AUTO):
        return _feature_locked(request, actor, features_svc.MARKETING_AUTO, "行銷自動化")
    return templates.TemplateResponse(
        "_campaigns_list.html",
        _campaigns_ctx(request, actor, db, editing_id=campaign_id),
    )


@router.post("/campaigns/{campaign_id}/update", response_class=HTMLResponse)
def campaigns_update(
    request: Request,
    campaign_id: int,
    name: str = Form(...),
    message_template: str = Form(...),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.MARKETING_AUTO):
        return _feature_locked(request, actor, features_svc.MARKETING_AUTO, "行銷自動化")
    tid = actor.user.tenant_id
    error = None
    editing_id = None
    try:
        marketing_svc.update_campaign(
            db, tenant_id=tid, campaign_id=campaign_id,
            name=name, message_template=message_template,
        )
    except HTTPException as exc:
        error = str(exc.detail)
        editing_id = campaign_id
    return templates.TemplateResponse(
        "_campaigns_list.html",
        _campaigns_ctx(request, actor, db, error=error, editing_id=editing_id),
    )


@router.post("/campaigns/{campaign_id}/delete", response_class=HTMLResponse)
def campaigns_delete(
    request: Request,
    campaign_id: int,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.MARKETING_AUTO):
        return _feature_locked(request, actor, features_svc.MARKETING_AUTO, "行銷自動化")
    tid = actor.user.tenant_id
    error = None
    try:
        marketing_svc.delete_campaign(db, tenant_id=tid, campaign_id=campaign_id)
    except HTTPException as exc:
        error = str(exc.detail)
    return templates.TemplateResponse(
        "_campaigns_list.html", _campaigns_ctx(request, actor, db, error=error)
    )


# ── 店家自助：圖文選單卡片（FLEX_MENU） ─────────────────────────────────────────

def _get_or_create_flex_menu(db: Session, tenant_id: int) -> "flex_menu_svc.FlexMenu":
    menu = flex_menu_svc.get_active_menu(db, tenant_id=tenant_id)
    if menu is None:
        menus = flex_menu_svc.list_menus(db, tenant_id=tenant_id)
        menu = menus[0] if menus else flex_menu_svc.create_menu(db, tenant_id=tenant_id)
    return menu


def _flex_ctx(request: Request, actor: Actor, db: Session, **extra) -> dict:
    tid = actor.user.tenant_id
    menu = _get_or_create_flex_menu(db, tid)
    cards = flex_menu_svc.list_cards(db, tenant_id=tid, menu_id=menu.id)
    preview = flex_menu_svc.build_flex_payload(menu, cards)
    return _ctx(
        request, actor,
        menu=menu,
        cards=cards,
        preview=preview,
        max_cards=flex_menu_svc.MAX_CARDS,
        **extra,
    )


@router.get("/flex-menu", response_class=HTMLResponse)
def flex_menu_page(
    request: Request,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.FLEX_MENU):
        return _feature_locked(request, actor, features_svc.FLEX_MENU, "圖文選單卡片")
    return templates.TemplateResponse("flex_menu.html", _flex_ctx(request, actor, db))


@router.post("/flex-menu/title", response_class=HTMLResponse)
def flex_menu_set_title(
    request: Request,
    title: str = Form(""),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.FLEX_MENU):
        return _feature_locked(request, actor, features_svc.FLEX_MENU, "圖文選單卡片")
    tid = actor.user.tenant_id
    menu = _get_or_create_flex_menu(db, tid)
    flex_menu_svc.update_menu(db, tenant_id=tid, menu_id=menu.id, title=title or "")
    return templates.TemplateResponse("_flex_menu.html", _flex_ctx(request, actor, db))


@router.post("/flex-menu/cards", response_class=HTMLResponse)
def flex_menu_add_card(
    request: Request,
    title: str = Form(...),
    action_type: str = Form(...),
    action_data: str = Form(...),
    subtitle: str = Form(""),
    image_url: str = Form(""),
    bg_color: str = Form(""),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.FLEX_MENU):
        return _feature_locked(request, actor, features_svc.FLEX_MENU, "圖文選單卡片")
    tid = actor.user.tenant_id
    error = None
    menu = _get_or_create_flex_menu(db, tid)
    try:
        flex_menu_svc.add_card(
            db, tenant_id=tid, menu_id=menu.id,
            title=title, action_type=action_type, action_data=action_data,
            subtitle=subtitle or None, image_url=image_url or None,
            bg_color=bg_color or None,
        )
    except HTTPException as exc:
        error = str(exc.detail)
    return templates.TemplateResponse(
        "_flex_menu.html", _flex_ctx(request, actor, db, error=error)
    )


@router.post("/flex-menu/cards/{card_id}/delete", response_class=HTMLResponse)
def flex_menu_delete_card(
    request: Request,
    card_id: int,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.FLEX_MENU):
        return _feature_locked(request, actor, features_svc.FLEX_MENU, "圖文選單卡片")
    tid = actor.user.tenant_id
    menu = _get_or_create_flex_menu(db, tid)
    try:
        flex_menu_svc.delete_card(db, tenant_id=tid, menu_id=menu.id, card_id=card_id)
    except HTTPException:
        pass
    return templates.TemplateResponse("_flex_menu.html", _flex_ctx(request, actor, db))


@router.get("/flex-menu/cards/{card_id}/edit", response_class=HTMLResponse)
def flex_menu_edit_card_form(
    request: Request,
    card_id: int,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.FLEX_MENU):
        return _feature_locked(request, actor, features_svc.FLEX_MENU, "圖文選單卡片")
    return templates.TemplateResponse(
        "_flex_menu.html", _flex_ctx(request, actor, db, editing_card_id=card_id)
    )


@router.post("/flex-menu/cards/{card_id}/update", response_class=HTMLResponse)
def flex_menu_update_card(
    request: Request,
    card_id: int,
    title: str = Form(...),
    action_type: str = Form(...),
    action_data: str = Form(...),
    subtitle: str = Form(""),
    image_url: str = Form(""),
    bg_color: str = Form(""),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.FLEX_MENU):
        return _feature_locked(request, actor, features_svc.FLEX_MENU, "圖文選單卡片")
    tid = actor.user.tenant_id
    error = None
    editing_card_id = None
    menu = _get_or_create_flex_menu(db, tid)
    try:
        flex_menu_svc.update_card(
            db, tenant_id=tid, menu_id=menu.id, card_id=card_id,
            title=title, action_type=action_type, action_data=action_data,
            subtitle=subtitle, image_url=image_url, bg_color=bg_color,
        )
    except HTTPException as exc:
        error = str(exc.detail)
        editing_card_id = card_id
    return templates.TemplateResponse(
        "_flex_menu.html",
        _flex_ctx(request, actor, db, error=error, editing_card_id=editing_card_id),
    )


# ── 店家自助：作品集（PUBLIC_PROFILE） ──────────────────────────────────────────

def _portfolio_ctx(request: Request, actor: Actor, db: Session, **extra) -> dict:
    tid = actor.user.tenant_id
    return _ctx(
        request, actor,
        categories=portfolio_svc.list_categories(db, tenant_id=tid),
        items=portfolio_svc.list_items(db, tenant_id=tid),
        **extra,
    )


@router.get("/portfolio", response_class=HTMLResponse)
def portfolio_page(
    request: Request,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.PUBLIC_PROFILE):
        return _feature_locked(request, actor, features_svc.PUBLIC_PROFILE, "公開店家頁")
    return templates.TemplateResponse("portfolio.html", _portfolio_ctx(request, actor, db))


@router.post("/portfolio/categories", response_class=HTMLResponse)
def portfolio_create_category(
    request: Request,
    name: str = Form(...),
    sort_order: int = Form(0),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.PUBLIC_PROFILE):
        return _feature_locked(request, actor, features_svc.PUBLIC_PROFILE, "公開店家頁")
    tid = actor.user.tenant_id
    error = None
    try:
        portfolio_svc.create_category(db, tenant_id=tid, name=name, sort_order=sort_order)
    except HTTPException as exc:
        error = str(exc.detail)
    return templates.TemplateResponse(
        "_portfolio.html", _portfolio_ctx(request, actor, db, error=error)
    )


@router.post("/portfolio/categories/{category_id}/delete", response_class=HTMLResponse)
def portfolio_delete_category(
    request: Request,
    category_id: int,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.PUBLIC_PROFILE):
        return _feature_locked(request, actor, features_svc.PUBLIC_PROFILE, "公開店家頁")
    tid = actor.user.tenant_id
    try:
        portfolio_svc.delete_category(db, tenant_id=tid, category_id=category_id)
    except HTTPException:
        pass
    return templates.TemplateResponse("_portfolio.html", _portfolio_ctx(request, actor, db))


@router.get("/portfolio/categories/{category_id}/edit", response_class=HTMLResponse)
def portfolio_edit_category_form(
    request: Request,
    category_id: int,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.PUBLIC_PROFILE):
        return _feature_locked(request, actor, features_svc.PUBLIC_PROFILE, "公開店家頁")
    return templates.TemplateResponse(
        "_portfolio.html",
        _portfolio_ctx(request, actor, db, editing_category_id=category_id),
    )


@router.post("/portfolio/categories/{category_id}/update", response_class=HTMLResponse)
def portfolio_update_category(
    request: Request,
    category_id: int,
    name: str = Form(...),
    sort_order: int = Form(0),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.PUBLIC_PROFILE):
        return _feature_locked(request, actor, features_svc.PUBLIC_PROFILE, "公開店家頁")
    tid = actor.user.tenant_id
    error = None
    editing_category_id = None
    try:
        portfolio_svc.update_category(
            db, tenant_id=tid, category_id=category_id,
            name=name, sort_order=sort_order,
        )
    except HTTPException as exc:
        error = str(exc.detail)
        editing_category_id = category_id
    return templates.TemplateResponse(
        "_portfolio.html",
        _portfolio_ctx(request, actor, db, error=error,
                       editing_category_id=editing_category_id),
    )


@router.post("/portfolio/items", response_class=HTMLResponse)
def portfolio_create_item(
    request: Request,
    image_url: str = Form(...),
    caption: str = Form(""),
    category_id: str = Form(""),
    sort_order: int = Form(0),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.PUBLIC_PROFILE):
        return _feature_locked(request, actor, features_svc.PUBLIC_PROFILE, "公開店家頁")
    tid = actor.user.tenant_id
    error = None
    try:
        portfolio_svc.create_item(
            db, tenant_id=tid, image_url=image_url,
            caption=caption or None, category_id=_opt_int(category_id),
            sort_order=sort_order,
        )
    except HTTPException as exc:
        error = str(exc.detail)
    except ValueError:
        error = "分類格式錯誤"
    return templates.TemplateResponse(
        "_portfolio.html", _portfolio_ctx(request, actor, db, error=error)
    )


@router.post("/portfolio/items/{item_id}/delete", response_class=HTMLResponse)
def portfolio_delete_item(
    request: Request,
    item_id: int,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.PUBLIC_PROFILE):
        return _feature_locked(request, actor, features_svc.PUBLIC_PROFILE, "公開店家頁")
    tid = actor.user.tenant_id
    try:
        portfolio_svc.delete_item(db, tenant_id=tid, item_id=item_id)
    except HTTPException:
        pass
    return templates.TemplateResponse("_portfolio.html", _portfolio_ctx(request, actor, db))


@router.get("/portfolio/items/{item_id}/edit", response_class=HTMLResponse)
def portfolio_edit_item_form(
    request: Request,
    item_id: int,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.PUBLIC_PROFILE):
        return _feature_locked(request, actor, features_svc.PUBLIC_PROFILE, "公開店家頁")
    return templates.TemplateResponse(
        "_portfolio.html", _portfolio_ctx(request, actor, db, editing_item_id=item_id)
    )


@router.post("/portfolio/items/{item_id}/update", response_class=HTMLResponse)
def portfolio_update_item(
    request: Request,
    item_id: int,
    image_url: str = Form(...),
    caption: str = Form(""),
    sort_order: int = Form(0),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.PUBLIC_PROFILE):
        return _feature_locked(request, actor, features_svc.PUBLIC_PROFILE, "公開店家頁")
    tid = actor.user.tenant_id
    error = None
    editing_item_id = None
    try:
        portfolio_svc.update_item(
            db, tenant_id=tid, item_id=item_id,
            image_url=image_url, caption=caption, sort_order=sort_order,
        )
    except HTTPException as exc:
        error = str(exc.detail)
        editing_item_id = item_id
    return templates.TemplateResponse(
        "_portfolio.html",
        _portfolio_ctx(request, actor, db, error=error, editing_item_id=editing_item_id),
    )


# ── 店家自助：公開店家頁（PUBLIC_PROFILE） ──────────────────────────────────────

def _profile_ctx(request: Request, actor: Actor, db: Session, **extra) -> dict:
    tid = actor.user.tenant_id
    return _ctx(
        request, actor,
        profile=profile_svc.get_by_tenant(db, tid),
        **extra,
    )


@router.get("/profile", response_class=HTMLResponse)
def profile_page(
    request: Request,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.PUBLIC_PROFILE):
        return _feature_locked(request, actor, features_svc.PUBLIC_PROFILE, "公開店家頁")
    return templates.TemplateResponse("profile.html", _profile_ctx(request, actor, db))


@router.post("/profile", response_class=HTMLResponse)
def profile_save(
    request: Request,
    slug: str = Form(...),
    display_name: str = Form(""),
    banner_url: str = Form(""),
    theme_color: str = Form(""),
    social_links: str = Form(""),
    seo_title: str = Form(""),
    seo_description: str = Form(""),
    intro: str = Form(""),
    announcement: str = Form(""),
    is_published: str = Form(""),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.PUBLIC_PROFILE):
        return _feature_locked(request, actor, features_svc.PUBLIC_PROFILE, "公開店家頁")
    tid = actor.user.tenant_id
    error = None
    saved = False
    try:
        profile_svc.upsert(
            db, tid,
            slug=slug,
            display_name=display_name or None,
            banner_url=banner_url or None,
            theme_color=theme_color or None,
            social_links=social_links or None,
            seo_title=seo_title or None,
            seo_description=seo_description or None,
            intro=intro or None,
            announcement=announcement or None,
            is_published=(is_published == "true"),
        )
        saved = True
    except profile_svc.SlugTakenError:
        error = "此網址代稱已被使用，請換一個。"
    except ValueError as exc:
        error = str(exc)
    return templates.TemplateResponse(
        "_profile.html", _profile_ctx(request, actor, db, error=error, saved=saved)
    )


# ── 店家自助：POS 結帳（PRODUCT_SALES） ─────────────────────────────────────────

def _pos_ctx(request: Request, actor: Actor, db: Session, **extra) -> dict:
    tid = actor.user.tenant_id
    return _ctx(
        request, actor,
        products=shop_svc.list_products(db, tenant_id=tid),
        **extra,
    )


@router.get("/pos", response_class=HTMLResponse)
def pos_page(
    request: Request,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.PRODUCT_SALES):
        return _feature_locked(request, actor, features_svc.PRODUCT_SALES, "商品銷售")
    return templates.TemplateResponse("pos.html", _pos_ctx(request, actor, db))


@router.post("/pos/lookup", response_class=HTMLResponse)
def pos_lookup(
    request: Request,
    phone: str = Form(...),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.PRODUCT_SALES):
        return _feature_locked(request, actor, features_svc.PRODUCT_SALES, "商品銷售")
    tid = actor.user.tenant_id
    result = pos_svc.lookup_by_phone(db, tenant_id=tid, phone=phone)
    extra = {"lookup_done": True, "phone": phone}
    if result is not None:
        extra.update(
            customer=result["customer"],
            points_balance=result["points_balance"],
            tier_discount_percent=result["tier_discount_percent"],
            active_coupons=result["active_coupons"],
        )
    return templates.TemplateResponse("_pos.html", _pos_ctx(request, actor, db, **extra))


@router.post("/pos/checkout", response_class=HTMLResponse)
async def pos_checkout(
    request: Request,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.PRODUCT_SALES):
        return _feature_locked(request, actor, features_svc.PRODUCT_SALES, "商品銷售")
    tid = actor.user.tenant_id
    form = await request.form()
    phone = (form.get("phone") or "").strip()
    customer_id = _opt_int(form.get("customer_id") or "")
    coupon_code = (form.get("coupon_code") or "").strip() or None
    try:
        points_to_redeem = int(form.get("points_to_redeem") or 0)
    except ValueError:
        points_to_redeem = 0

    # 從 qty_<product_id> 欄位組裝結帳明細（數量 > 0 才納入）。
    items: list[dict] = []
    for key, value in form.items():
        if not key.startswith("qty_"):
            continue
        try:
            qty = int(value)
        except (TypeError, ValueError):
            continue
        if qty > 0:
            items.append({"product_id": int(key[4:]), "qty": qty})

    error = None
    order = None
    if not items:
        error = "請至少選擇一項商品數量。"
    else:
        try:
            order = pos_svc.checkout(
                db, tenant_id=tid, customer_id=customer_id, items=items,
                coupon_code=coupon_code, points_to_redeem=points_to_redeem,
            )
        except pos_svc.CustomerNotFound:
            error = "找不到該會員。"
        except shop_svc.ProductNotFound:
            error = "找不到商品。"
        except shop_svc.ProductInactive:
            error = "商品已停用。"
        except shop_svc.OutOfStock:
            error = "庫存不足。"
        except membership_svc.InsufficientPoints:
            error = "點數不足。"
        except coupons_svc.CouponError as exc:
            error = str(exc)
        except HTTPException as exc:
            error = str(exc.detail)

    extra = {"phone": phone}
    if customer_id is not None:
        result = pos_svc.lookup_by_phone(db, tenant_id=tid, phone=phone) if phone else None
        if result is not None:
            extra.update(
                lookup_done=True,
                customer=result["customer"],
                points_balance=result["points_balance"],
                active_coupons=result["active_coupons"],
            )
    return templates.TemplateResponse(
        "_pos.html", _pos_ctx(request, actor, db, order=order, error=error, **extra)
    )


# ── 店家自助：AI 客服 / FAQ（AI_ASSISTANT） ─────────────────────────────────────

def _faq_ctx(request: Request, actor: Actor, db: Session, **extra) -> dict:
    tid = actor.user.tenant_id
    extra.setdefault("editing_id", None)
    return _ctx(
        request, actor,
        faqs=faq_svc.list_faqs(db, tenant_id=tid),
        **extra,
    )


@router.get("/faq", response_class=HTMLResponse)
def faq_page(
    request: Request,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.AI_ASSISTANT):
        return _feature_locked(request, actor, features_svc.AI_ASSISTANT, "AI 客服")
    return templates.TemplateResponse("faq.html", _faq_ctx(request, actor, db))


@router.post("/faq", response_class=HTMLResponse)
def faq_create(
    request: Request,
    question: str = Form(...),
    answer: str = Form(...),
    sort_order: int = Form(0),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.AI_ASSISTANT):
        return _feature_locked(request, actor, features_svc.AI_ASSISTANT, "AI 客服")
    tid = actor.user.tenant_id
    error = None
    try:
        faq_svc.create_faq(
            db, tenant_id=tid, question=question, answer=answer, sort_order=sort_order
        )
    except HTTPException as exc:
        error = str(exc.detail)
    return templates.TemplateResponse(
        "_faq_list.html", _faq_ctx(request, actor, db, error=error)
    )


@router.post("/faq/{faq_id}/delete", response_class=HTMLResponse)
def faq_delete(
    request: Request,
    faq_id: int,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.AI_ASSISTANT):
        return _feature_locked(request, actor, features_svc.AI_ASSISTANT, "AI 客服")
    tid = actor.user.tenant_id
    try:
        faq_svc.delete_faq(db, tenant_id=tid, faq_id=faq_id)
    except HTTPException:
        pass
    return templates.TemplateResponse("_faq_list.html", _faq_ctx(request, actor, db))


@router.post("/faq/{faq_id}/toggle", response_class=HTMLResponse)
def faq_toggle(
    request: Request,
    faq_id: int,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.AI_ASSISTANT):
        return _feature_locked(request, actor, features_svc.AI_ASSISTANT, "AI 客服")
    tid = actor.user.tenant_id
    error = None
    try:
        faq = faq_svc.get_faq(db, tenant_id=tid, faq_id=faq_id)
        faq_svc.update_faq(
            db, tenant_id=tid, faq_id=faq_id, is_active=not faq.is_active
        )
    except HTTPException as exc:
        error = str(exc.detail)
    return templates.TemplateResponse(
        "_faq_list.html", _faq_ctx(request, actor, db, error=error)
    )


@router.get("/faq/{faq_id}/edit", response_class=HTMLResponse)
def faq_edit_form(
    request: Request,
    faq_id: int,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.AI_ASSISTANT):
        return _feature_locked(request, actor, features_svc.AI_ASSISTANT, "AI 客服")
    return templates.TemplateResponse(
        "_faq_list.html", _faq_ctx(request, actor, db, editing_id=faq_id)
    )


@router.post("/faq/{faq_id}/update", response_class=HTMLResponse)
def faq_update(
    request: Request,
    faq_id: int,
    question: str = Form(...),
    answer: str = Form(...),
    sort_order: int = Form(0),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.AI_ASSISTANT):
        return _feature_locked(request, actor, features_svc.AI_ASSISTANT, "AI 客服")
    tid = actor.user.tenant_id
    error = None
    editing_id = None
    try:
        faq_svc.update_faq(
            db,
            tenant_id=tid,
            faq_id=faq_id,
            question=question,
            answer=answer,
            sort_order=sort_order,
        )
    except HTTPException as exc:
        error = str(exc.detail)
        editing_id = faq_id
    return templates.TemplateResponse(
        "_faq_list.html",
        _faq_ctx(request, actor, db, error=error, editing_id=editing_id),
    )


@router.post("/ai-widget/ask", response_class=HTMLResponse)
def ai_widget_ask(
    request: Request,
    question: str = Form(...),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    """右下角浮動 AI 客服 widget 的問答端點（對標 vibeaico AI 客服 widget）。"""
    tid = actor.user.tenant_id
    answer = None
    error = None
    if not features_svc.is_enabled(db, tid, features_svc.AI_ASSISTANT):
        error = "AI 客服未開通（專業版功能）。"
    elif len(question) > _AI_QUESTION_MAX_LEN:
        error = f"問題過長（上限 {_AI_QUESTION_MAX_LEN} 字），請精簡後再試。"
    else:
        assistant = get_assistant()
        context = faq_svc.build_context(
            db, tid, question, max_entries=assistant.context_max_entries
        )
        try:
            result = assistant.answer(question, context)
            answer = result.answer
        except AIError as exc:
            error = f"AI 後端錯誤：{exc}"
    return templates.TemplateResponse(
        "_ai_widget_answer.html",
        _ctx(request, actor, question=question, answer=answer, error=error),
    )


@router.post("/faq/ask", response_class=HTMLResponse)
def faq_ask(
    request: Request,
    question: str = Form(...),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.AI_ASSISTANT):
        return _feature_locked(request, actor, features_svc.AI_ASSISTANT, "AI 客服")
    tid = actor.user.tenant_id
    answer = None
    source = None
    error = None
    # 成本防護：避免超長問題被送往付費 LLM（cost amplification）。
    if len(question) > _AI_QUESTION_MAX_LEN:
        error = f"問題過長（上限 {_AI_QUESTION_MAX_LEN} 字），請精簡後再試。"
        return templates.TemplateResponse(
            "_ai_test.html",
            _ctx(request, actor, question=question, answer=answer, source=source, error=error),
        )
    assistant = get_assistant()
    context = faq_svc.build_context(
        db, tid, question, max_entries=assistant.context_max_entries
    )
    try:
        result = assistant.answer(question, context)
        answer = result.answer
        source = result.source
    except AIError as exc:
        error = f"AI 後端錯誤：{exc}"
    return templates.TemplateResponse(
        "_ai_test.html",
        _ctx(request, actor, question=question, answer=answer, source=source, error=error),
    )


# ── 後台 LINE 客服訊息 + SSE 即時通知 ────────────────────────────────────────
@router.get("/line-chat", response_class=HTMLResponse)
def line_chat_page(
    request: Request,
    u: str | None = Query(default=None),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    """客服對話頁：左側對話列表，右側選定對話的訊息序列 + 回覆框。"""
    tid = actor.user.tenant_id
    conversations = line_chat_svc.list_conversations(db, tenant_id=tid)
    selected = u
    selected_name = None
    messages = []
    if selected:
        messages = line_chat_svc.list_messages(
            db, tenant_id=tid, line_user_id=selected
        )
        for c in conversations:
            if c["line_user_id"] == selected:
                selected_name = c["display_name"]
                break
    return templates.TemplateResponse(
        "line_chat.html",
        _ctx(
            request, actor,
            conversations=conversations,
            selected=selected,
            selected_name=selected_name,
            line_user_id=selected,
            messages=messages,
        ),
    )


@router.post("/line-chat/{line_user_id}/reply", response_class=HTMLResponse)
def line_chat_reply(
    request: Request,
    line_user_id: str,
    text: str = Form(...),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
    push_client: LinePushClient = Depends(get_push_client),
):
    """店家從後台回覆顧客：LINE push → 存檔 outbound → SSE 廣播。"""
    from saas_mvp.line_client import LinePushError
    from saas_mvp.models.line_channel_config import LineChannelConfig
    from saas_mvp.services.events import publish_event

    tid = actor.user.tenant_id
    text = (text or "").strip()
    error = None
    if not text:
        error = "回覆內容不可為空。"
    else:
        cfg = (
            db.query(LineChannelConfig)
            .filter(LineChannelConfig.tenant_id == tid)
            .first()
        )
        token = None
        try:
            token = cfg.access_token if cfg else None
        except Exception:  # noqa: BLE001 — 解密失敗視同未設定
            token = None
        if not token:
            error = "尚未設定 LINE channel access token，無法回覆。"
        else:
            try:
                push_client.push(line_user_id, text, access_token=token)
                line_chat_svc.record_outbound(
                    db, tenant_id=tid, line_user_id=line_user_id, text=text
                )
                publish_event(
                    tid, "line_message",
                    line_user_id=line_user_id, text=text, direction="out",
                )
            except LinePushError as exc:
                error = f"LINE 推播失敗：{exc}"

    messages = line_chat_svc.list_messages(db, tenant_id=tid, line_user_id=line_user_id)
    return templates.TemplateResponse(
        "_line_chat_messages.html",
        _ctx(request, actor, messages=messages, line_user_id=line_user_id, error=error),
    )


@router.get("/events")
async def line_events_stream(
    request: Request,
    actor: Actor = Depends(require_ui_user),
):
    """SSE 即時通知串流：新預約 / 取消 / 新訊息即時推送到後台。

    以 cookie 認證（EventSource 會自動帶上同源 cookie）。每租戶一條訂閱。
    """
    import asyncio
    import json as _json

    tenant_id = actor.user.tenant_id
    queue = await event_broker.subscribe(tenant_id)

    async def gen():
        try:
            yield ": connected\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"  # 心跳，維持連線
                    continue
                etype = event.get("type", "message")
                data = _json.dumps(event, ensure_ascii=False)
                yield f"event: {etype}\ndata: {data}\n\n"
        finally:
            event_broker.unsubscribe(tenant_id, queue)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── 預約行事曆（月曆 / 週曆 + 雙模式：顧客預約 / 員工排班） ─────────────────────
@router.get("/calendar", response_class=HTMLResponse)
def calendar_page(
    request: Request,
    view: str = Query(default="month"),
    mode: str = Query(default="reservations"),
    date: str | None = Query(default=None),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    """後台預約行事曆。view=month|week；mode=reservations|staff；date=錨點(YYYY-MM-DD)。"""
    tid = actor.user.tenant_id
    today = datetime.date.today()
    try:
        anchor = datetime.date.fromisoformat(date) if date else today
    except ValueError:
        anchor = today

    month_data = week_data = staff_grid = None
    if mode == "staff":
        staff_grid = calendar_view_svc.build_staff_grid(db, tenant_id=tid)
    elif view == "week":
        week_data = calendar_view_svc.build_week(db, tenant_id=tid, anchor=anchor)
    else:
        view = "month"
        month_data = calendar_view_svc.build_month(
            db, tenant_id=tid, year=anchor.year, month=anchor.month
        )

    return templates.TemplateResponse(
        "calendar.html",
        _ctx(
            request, actor,
            view=view, mode=mode, today=today,
            month_data=month_data, week_data=week_data, staff_grid=staff_grid,
        ),
    )
