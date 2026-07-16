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

import csv
import datetime
import hmac
import html
import io
import secrets
import json
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Query, Request, status
from fastapi.responses import (
    HTMLResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from saas_mvp.config import settings
from saas_mvp.deps import (
    Actor,
    get_db,
    require_ui_admin,
    require_ui_owner,
    require_ui_user,
)
from saas_mvp.auth.dependencies import UILoginRequired, _UI_COOKIE_NAME
from saas_mvp.auth.security import create_access_token, hash_password, verify_password
from saas_mvp.line_client import (
    LineBotInfoClient,
    LineWebhookAdminClient,
    LineWebhookAdminError,
    LinePushClient,
    LineRichMenuClient,
    get_bot_info_client,
    get_webhook_admin_client,
    get_push_client,
    get_rich_menu_client,
)
from saas_mvp.models.tenant import Tenant, normalize_store_type
from saas_mvp.models.service import Service
from saas_mvp.models.user import User
from saas_mvp.quota import get_quota_status
from saas_mvp.routers.line_webhook import webhook_url_for
from saas_mvp.services import admin as admin_svc
from saas_mvp.services import analytics as analytics_svc
from saas_mvp.services import reporting as reporting_svc
from saas_mvp.services import billing as billing_svc
from saas_mvp.services import booking as booking_svc
from saas_mvp.services import appointment_series as appointment_series_svc
from saas_mvp.services import deposit as deposit_svc
from saas_mvp.services import waitlist as waitlist_svc
from saas_mvp.services import coupons as coupons_svc
from saas_mvp.services import features as features_svc
from saas_mvp.services import api_keys as api_keys_svc
from saas_mvp.services import auto_reply as auto_reply_svc
from saas_mvp.services import customers as customers_svc
from saas_mvp.services import account_email as account_email_svc
from saas_mvp.services import audit as audit_svc
from saas_mvp.services import line_config as line_config_svc
from saas_mvp.services import onboarding as onboarding_svc
from saas_mvp.services import oauth as oauth_svc
from saas_mvp.services import platform_oauth_config as platform_oauth_svc
from saas_mvp.services import platform_email_config as platform_email_svc
from saas_mvp.services import platform_ai_config as platform_ai_svc
from saas_mvp.services import (
    platform_observability_config as platform_observability_svc,
)
from saas_mvp.services import platform_payment_config as platform_payment_svc
from saas_mvp.services import platform_invoice_config as platform_invoice_svc
from saas_mvp.services import readiness_dashboard as readiness_dashboard_svc
from saas_mvp.services import invoice_profiles as invoice_profiles_svc
from saas_mvp.services import plans as plans_svc
from saas_mvp.services.mailer import Mailer, MailerError, get_mailer
from saas_mvp.services import rich_menu as rich_menu_svc
from saas_mvp.services import shop as shop_svc
from saas_mvp.services import slots as slots_svc
from saas_mvp.services import locations as locations_svc
from saas_mvp.services import staff as staff_svc
from saas_mvp.services import catalog as catalog_svc
from saas_mvp.services import marketing as marketing_svc
from saas_mvp.services import notes as notes_svc
from saas_mvp.services import flex_menu as flex_menu_svc
from saas_mvp.services import portfolio as portfolio_svc
from saas_mvp.services import profile as profile_svc
from saas_mvp.services import pos as pos_svc
from saas_mvp.services import membership as membership_svc
from saas_mvp.services import service_packages as packages_svc
from saas_mvp.services import gift_cards as gift_cards_svc
from saas_mvp.services import client_forms as client_forms_svc
from saas_mvp.services import bookable_resources as resources_svc
from saas_mvp.services import commissions as commissions_svc
from saas_mvp.services import segments as segments_svc
from saas_mvp.services import notifications_history as notif_history_svc
from saas_mvp.services import faq as faq_svc
from saas_mvp.services import push_quota as push_quota_svc
from saas_mvp.services import line_chat as line_chat_svc
from saas_mvp.services import calendar_view as calendar_view_svc
from saas_mvp.services.events import broker as event_broker
from saas_mvp.auth.ratelimit import (
    email_identity_limiter,
    email_ip_limiter,
    email_user_limiter,
)
from saas_mvp.ai import AIError, get_assistant
from saas_mvp.models.campaign import Campaign
from saas_mvp.services.tenants import tenant_query
from fastapi import HTTPException

_PKG_DIR = Path(__file__).resolve().parent.parent  # src/saas_mvp
templates = Jinja2Templates(directory=str(_PKG_DIR / "templates"))
templates.env.filters["money"] = lambda cents: f"{int(cents or 0) / 100:,.2f}"

# ── CSRF（double-submit cookie token）───────────────────────────────────────

_CSRF_COOKIE_NAME = "csrf_token"
_CSRF_HEADER_NAME = "x-csrf-token"
_CSRF_FORM_FIELD = "csrf_token"
# 尚無 session 的端點（登入/註冊表單提交）豁免；其 GET 頁本就放行。
_CSRF_EXEMPT_PATHS = {"/ui/login", "/ui/register"}


class UICSRFInvalid(Exception):
    """登入仍存在但頁面安全憑證無效；由 app 層顯示可操作的 HTML 說明。"""


def _line_webhook_url_for(tenant_id: int) -> str:
    """Return the copy-ready public webhook URL shown in the management UI."""
    path = webhook_url_for(tenant_id)
    base = settings.public_base_url.rstrip("/")
    return f"{base}{path}" if base else path


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
        # 工作階段已結束時，表單頁仍可能停在瀏覽器分頁中。這不是權限不足，
        # 直接導回登入頁，避免使用者把裸 403 誤認成 SMTP 等業務功能失敗。
        if not request.cookies.get(_UI_COOKIE_NAME):
            raise UILoginRequired()
        raise UICSRFInvalid()


def maybe_renew_ui_cookie(request: Request, response: Response) -> None:
    """滑動續期(R4-C1):有效 token 剩餘 < 門檻時靜默換新並重設 cookie。

    由 app 層 middleware 對 /ui 回應呼叫(handler 都直接回 TemplateResponse,
    dependency 注入的 Response 上 set_cookie 不會合併進最終回應,故不能走
    dependency)。best-effort:任何失敗(壞票/過期)一律吞掉,絕不影響原請求
    —— 過期自然由既有登入守門處理。``imp`` 代管票不續(30 分硬上限);超過
    session_renew_max_hours 總視窗不續(強制重登)。csrf 沿用舊值不輪替。
    """
    import datetime as _dt

    token = request.cookies.get(_UI_COOKIE_NAME)
    if not token:
        return
    try:
        from saas_mvp.auth.security import create_access_token, decode_access_token

        payload = decode_access_token(token)
        if payload.get("imp") is not None:
            return
        now_ts = int(_dt.datetime.now(_dt.timezone.utc).timestamp())
        if payload["exp"] - now_ts > settings.session_renew_threshold_minutes * 60:
            return
        oa = int(payload.get("oa") or now_ts)
        if now_ts - oa > settings.session_renew_max_hours * 3600:
            return
        new_token = create_access_token(
            user_id=int(payload["sub"]),
            tenant_id=payload["tenant_id"],
            original_auth_ts=oa,
        )
        _set_auth_cookie(
            response, new_token,
            csrf_value=request.cookies.get(_CSRF_COOKIE_NAME) or None,
        )
    except Exception:  # noqa: BLE001 — 續期永不影響原請求
        pass


router = APIRouter(
    prefix="/ui",
    tags=["ui"],
    include_in_schema=False,
    dependencies=[Depends(_enforce_csrf)],
)

# 送往付費 LLM 的問題字數上限（與 routers/ai.py AskRequest 一致），防成本放大。
_AI_QUESTION_MAX_LEN = 2000


# ── 共用工具 ────────────────────────────────────────────────────────────────


def _set_auth_cookie(
    response: Response, token: str, *, csrf_value: str | None = None
) -> None:
    """把 JWT 寫入 httpOnly cookie；prod 加 Secure，dev/test 不加（方便本機/測試）。

    一併發放 double-submit CSRF cookie（非 httpOnly——前端模板/HTMX 需可讀
    回傳；token 本身不含任何機密，防護力來自「攻擊者跨站無法讀取」）。
    所有登入路徑（/ui/login、/ui/register、OAuth callback）皆經此函式。

    csrf_value(R4-C1 滑動續期用):續期時**沿用舊 csrf 值只延長壽命** —
    輪替會讓已渲染頁面的 hidden field 與新 cookie 不符,壞掉 in-flight 表單。
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
        value=csrf_value or secrets.token_urlsafe(32),
        httponly=False,
        samesite="lax",
        secure=settings.env not in ("dev", "test"),
        max_age=settings.access_token_expire_minutes * 60,
        path="/",
    )
    # SSO 橋(R3-C2):console(saas-console)以 `saas_access_token` cookie 裝
    # **同一顆 JWT**(同 secret_key 簽);/ui 登入時一併種下,console 免二次登入。
    # console 端登入亦會回種 /ui 的兩個 cookie(見 frontend session/login route)。
    response.set_cookie(
        key="saas_access_token",
        value=token,
        httponly=True,
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
        # F2 代管:banner 顯示 + 稽核聯動
        "impersonating": bool(actor and actor.impersonator_user_id is not None),
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
def login_form(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(
        "login.html",
        _ctx(
            request,
            line_login_configured=oauth_svc.provider_credentials_present(
                "line", settings=settings, db=db
            ),
            google_login_configured=oauth_svc.provider_credentials_present(
                "google", settings=settings, db=db
            ),
        ),
    )


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
    mailer: Mailer = Depends(get_mailer),
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

    from saas_mvp.services import organizations as organizations_svc

    organization = organizations_svc.create_organization(
        db, name=tenant_name, flush=True
    )
    tenant = Tenant(name=tenant_name, plan="free", organization_id=organization.id)
    db.add(tenant)
    db.flush()
    # 註冊即開試用（預設 pro 14 天；SAAS_TRIAL_DAYS=0 停用）。
    plans_svc.start_trial(tenant)
    user = User(
        email=email, hashed_password=hash_password(password), tenant_id=tenant.id
    )
    db.add(user)
    db.flush()
    organizations_svc.add_owner_memberships(
        db, organization_id=organization.id, tenant_id=tenant.id, user_id=user.id
    )
    db.commit()
    db.refresh(user)
    # 驗證信 best-effort：寄失敗不阻擋註冊（dashboard banner 可重寄）。
    account_email_svc.send_verification_email(db, user, mailer)

    token = create_access_token(user_id=user.id, tenant_id=user.tenant_id)
    resp = RedirectResponse("/ui/", status_code=status.HTTP_303_SEE_OTHER)
    _set_auth_cookie(resp, token)
    return resp


# ── Email 驗證 / 忘記密碼（B3） ─────────────────────────────────────────────


@router.get("/verify-email/{token}", response_class=HTMLResponse)
def verify_email(token: str, db: Session = Depends(get_db)):
    try:
        account_email_svc.verify_email(db, token)
    except account_email_svc.TokenInvalid:
        return HTMLResponse(
            "<h1>連結無效或已過期</h1><p>請登入後台重新寄送驗證信。</p>",
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    return RedirectResponse("/ui/?verified=1", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/resend-verification", response_class=HTMLResponse)
def resend_verification(
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
    mailer: Mailer = Depends(get_mailer),
):
    if actor.user.email_verified_at is None:
        if settings.rate_limit_enabled:
            try:
                email_user_limiter._check_rate_limit(f"user:{actor.user.id}")
            except HTTPException as exc:
                if exc.status_code == status.HTTP_429_TOO_MANY_REQUESTS:
                    return RedirectResponse(
                        "/ui/?verification_rate_limited=1",
                        status_code=status.HTTP_303_SEE_OTHER,
                    )
                raise
        outcome = account_email_svc.send_verification_email(db, actor.user, mailer)
        target = (
            "/ui/?verification_resent=1"
            if outcome == "sent"
            else "/ui/?verification_queued=1"
        )
        return RedirectResponse(target, status_code=status.HTTP_303_SEE_OTHER)
    return RedirectResponse("/ui/", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/forgot-password", response_class=HTMLResponse)
def forgot_password_form(request: Request):
    return templates.TemplateResponse("forgot_password.html", _ctx(request))


@router.post("/forgot-password", response_class=HTMLResponse)
def forgot_password_submit(
    request: Request,
    email: str = Form(...),
    db: Session = Depends(get_db),
    mailer: Mailer = Depends(get_mailer),
):
    try:
        email_ip_limiter(request)
        if settings.rate_limit_enabled:
            email_identity_limiter._check_rate_limit(email.strip().lower())
    except HTTPException as exc:
        if exc.status_code != status.HTTP_429_TOO_MANY_REQUESTS:
            raise
        return templates.TemplateResponse(
            "forgot_password.html",
            _ctx(request, error="申請次數過多，請在 15 分鐘後再試。"),
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            headers=exc.headers,
        )
    account_email_svc.request_password_reset(db, email, mailer)
    # 查無 email 也回相同訊息（防帳號列舉）。
    return templates.TemplateResponse("forgot_password.html", _ctx(request, sent=True))


@router.get("/reset-password/{token}", response_class=HTMLResponse)
def reset_password_form(token: str, request: Request):
    return templates.TemplateResponse("reset_password.html", _ctx(request, token=token))


@router.post("/reset-password/{token}", response_class=HTMLResponse)
def reset_password_submit(
    token: str,
    request: Request,
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    if len(password) < 8:
        return templates.TemplateResponse(
            "reset_password.html",
            _ctx(request, token=token, error="密碼至少需 8 個字元"),
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    try:
        account_email_svc.reset_password(db, token, password)
    except account_email_svc.TokenInvalid:
        return HTMLResponse(
            "<h1>連結無效或已過期</h1><p>請重新申請重設密碼。</p>",
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    return RedirectResponse("/ui/login?reset=1", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/logout")
def logout():
    resp = RedirectResponse("/ui/login", status_code=status.HTTP_303_SEE_OTHER)
    resp.delete_cookie(_UI_COOKIE_NAME, path="/")
    resp.delete_cookie(_CSRF_COOKIE_NAME, path="/")
    # 一併登出 console(雙 cookie 漂移防範:任一邊登出都清三個)。
    resp.delete_cookie("saas_access_token", path="/")
    return resp


# ── 方案 / 定價（B1） ───────────────────────────────────────────────────────


def _plan_info(tenant: Tenant) -> dict:
    """dashboard/選方案頁的方案摘要：生效方案、試用倒數。"""
    import datetime as _dt

    now = _dt.datetime.now(_dt.timezone.utc)
    effective = plans_svc.effective_plan(tenant, now=now)
    on_trial = plans_svc.trial_active(tenant, now=now)
    days_left = None
    if on_trial:
        ends = tenant.trial_ends_at
        if ends.tzinfo is None:
            ends = ends.replace(tzinfo=_dt.timezone.utc)
        days_left = max(0, (ends - now).days)
    return {
        "effective": effective,
        "effective_label": plans_svc.plan_label(effective),
        "paid": plans_svc.normalize_plan(tenant.plan),
        "paid_label": plans_svc.plan_label(plans_svc.normalize_plan(tenant.plan)),
        "trial_active": on_trial,
        "trial_days_left": days_left,
    }


def _line_insights(db: Session, tenant_id: int) -> dict:
    """dashboard「LINE 經營」卡（A3.4）：本月事件/bot 建單/調查均分。"""
    import datetime as _dt

    from sqlalchemy import func as _func, select as _select

    from saas_mvp.models.line_webhook_event import LineWebhookEvent
    from saas_mvp.models.reservation import Reservation
    from saas_mvp.services import feedback as feedback_svc

    now = _dt.datetime.now(_dt.timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    events = db.execute(
        _select(_func.count(LineWebhookEvent.id)).where(
            LineWebhookEvent.tenant_id == tenant_id,
            LineWebhookEvent.created_at >= month_start,
        )
    ).scalar_one()
    bot_bookings = db.execute(
        _select(_func.count(Reservation.id)).where(
            Reservation.tenant_id == tenant_id,
            Reservation.line_user_id.is_not(None),
            Reservation.created_at >= month_start,
        )
    ).scalar_one()
    return {
        "month_events": int(events),
        "month_bot_bookings": int(bot_bookings),
        "feedback": feedback_svc.summary(db, tenant_id),
    }


@router.get("/pricing", response_class=HTMLResponse)
def pricing_page(request: Request):
    """公開定價頁（免登入）。"""
    return templates.TemplateResponse(
        "pricing.html", _ctx(request, plans=plans_svc.list_plans())
    )


@router.get("/plan", response_class=HTMLResponse)
def plan_page(
    request: Request,
    actor: Actor = Depends(require_ui_owner),
    db: Session = Depends(get_db),
):
    """登入後選方案頁：目前方案 + 各方案內容；訂閱按鈕（B2 接金流）。"""
    tenant = db.get(Tenant, actor.user.tenant_id)
    info = _plan_info(tenant)
    return templates.TemplateResponse(
        "plan.html",
        _ctx(
            request,
            actor,
            plans=plans_svc.list_plans(current=info["effective"]),
            plan_info=info,
        ),
    )


@router.post("/plan/{plan}/subscribe", response_class=HTMLResponse)
def plan_subscribe(
    plan: str,
    request: Request,
    actor: Actor = Depends(require_ui_owner),
    db: Session = Depends(get_db),
):
    """訂閱方案。stub 立即生效導回；ecpay 顯示前往綠界付款卡片。"""
    bundle_key = {v: k for k, v in features_svc.BUNDLE_TO_PLAN.items()}.get(plan)
    tenant = db.get(Tenant, actor.user.tenant_id)
    if bundle_key is None:
        return RedirectResponse("/ui/plan", status_code=status.HTTP_303_SEE_OTHER)
    try:
        result = billing_svc.subscribe_bundle(db, tenant, bundle_key, actor.user.id)
    except HTTPException as exc:
        info = _plan_info(tenant)
        return templates.TemplateResponse(
            "plan.html",
            _ctx(
                request,
                actor,
                plans=plans_svc.list_plans(current=info["effective"]),
                plan_info=info,
                error=str(exc.detail),
            ),
            status_code=exc.status_code,
        )
    audit_svc.record_from_actor(
        db,
        actor,
        action="billing.plan.subscribe",
        target=f"tenant:{tenant.id}",
        detail={"plan": plan, "mode": result.mode},
        request=request,
    )
    db.commit()
    if result.checkout_url:
        url = html.escape(result.checkout_url)
        label = html.escape(plans_svc.plan_label(plan))
        return HTMLResponse(
            '<div class="card success">'
            f"<p>請完成綠界信用卡定期定額授權以啟用「{label}」。</p>"
            f'<a class="btn" href="{url}" target="_blank" rel="noopener">前往綠界付款</a>'
            '<p class="muted">完成首期授權後方案自動生效；可回<a href="/ui/plan">方案頁</a>查看狀態。</p>'
            "</div>"
        )
    return RedirectResponse("/ui/plan", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/plan/unsubscribe", response_class=HTMLResponse)
def plan_unsubscribe(
    request: Request,
    actor: Actor = Depends(require_ui_owner),
    db: Session = Depends(get_db),
):
    """退訂方案 → 降 free（已付費者保留原方案至最後扣款日 + 31 天）。"""
    tenant = db.get(Tenant, actor.user.tenant_id)
    billing_svc.unsubscribe_bundle(db, tenant, actor.user.id)
    audit_svc.record_from_actor(
        db,
        actor,
        action="billing.plan.unsubscribe",
        target=f"tenant:{tenant.id}",
        request=request,
    )
    db.commit()
    return RedirectResponse("/ui/plan", status_code=status.HTTP_303_SEE_OTHER)


def _billing_ctx(request: Request, actor: Actor, db: Session, **extra) -> dict:
    """帳單頁共用資料；儲存發票資料失敗時可原頁顯示錯誤。"""
    from saas_mvp.models.feature_subscription import FeatureSubscription
    from saas_mvp.models.subscription_charge import SubscriptionCharge
    from sqlalchemy import select as _select

    tid = actor.user.tenant_id
    tenant = db.get(Tenant, tid)
    sub = (
        db.execute(
            _select(FeatureSubscription)
            .where(
                FeatureSubscription.tenant_id == tid,
                FeatureSubscription.feature.in_(features_svc.VALID_BUNDLES),
            )
            .order_by(FeatureSubscription.id.desc())
        )
        .scalars()
        .first()
    )
    charges = []
    next_charge_at = None
    if sub is not None:
        charges = (
            db.execute(
                _select(SubscriptionCharge)
                .where(SubscriptionCharge.subscription_id == sub.id)
                .order_by(SubscriptionCharge.id.desc())
                .limit(24)
            )
            .scalars()
            .all()
        )
        if sub.status == "active" and sub.last_charged_at is not None:
            import datetime as _dt

            next_charge_at = sub.last_charged_at + _dt.timedelta(days=30)
    # C2:發票對照(charge_id → Invoice)
    invoice_by_charge: dict = {}
    if charges:
        from saas_mvp.models.invoice import Invoice

        rows = (
            db.execute(
                _select(Invoice).where(
                    Invoice.subscription_charge_id.in_([c.id for c in charges])
                )
            )
            .scalars()
            .all()
        )
        invoice_by_charge = {r.subscription_charge_id: r for r in rows}
    return _ctx(
        request,
        actor,
        plan_info=_plan_info(tenant),
        subscription=sub,
        bundle_label=(
            features_svc.BUNDLE_LABELS.get(sub.feature, sub.feature) if sub else None
        ),
        charges=charges,
        next_charge_at=next_charge_at,
        invoice_by_charge=invoice_by_charge,
        invoice_profile=invoice_profiles_svc.profile_status(db, tid),
        **extra,
    )


@router.get("/billing", response_class=HTMLResponse)
def billing_page(
    request: Request,
    invoice_profile_saved: bool = Query(False),
    actor: Actor = Depends(require_ui_owner),
    db: Session = Depends(get_db),
):
    """帳單頁：方案、扣款明細與發票買受資訊。"""
    return templates.TemplateResponse(
        "billing.html",
        _billing_ctx(
            request,
            actor,
            db,
            invoice_profile_saved=invoice_profile_saved,
        ),
    )


@router.post("/billing/invoice-profile", response_class=HTMLResponse)
def billing_invoice_profile_save(
    request: Request,
    mode: str = Form(..., max_length=16),
    buyer_name: str = Form("", max_length=60),
    buyer_identifier: str = Form("", max_length=8),
    carrier_type: str = Form("ecpay", max_length=16),
    carrier_number: str = Form("", max_length=64),
    donation_code: str = Form("", max_length=7),
    actor: Actor = Depends(require_ui_owner),
    db: Session = Depends(get_db),
):
    """店家 owner 自助保存發票資料；敏感載具號碼加密保存。"""
    try:
        row = invoice_profiles_svc.save_profile(
            db,
            tenant_id=actor.user.tenant_id,
            mode=mode,
            buyer_name=buyer_name,
            buyer_identifier=buyer_identifier,
            carrier_type=carrier_type,
            carrier_number=carrier_number,
            donation_code=donation_code,
            actor_user_id=actor.user.id,
        )
    except invoice_profiles_svc.InvoiceProfileError as exc:
        db.rollback()
        return templates.TemplateResponse(
            "billing.html",
            _billing_ctx(request, actor, db, invoice_profile_error=str(exc)),
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    audit_svc.record_from_actor(
        db,
        actor,
        action="billing.invoice_profile.update",
        target=f"tenant:{actor.user.tenant_id}",
        detail={
            "mode": row.mode,
            "carrier_type": row.carrier_type,
            "has_identifier": bool(row.buyer_identifier),
            "has_donation_code": bool(row.donation_code),
        },
        request=request,
    )
    db.commit()
    return RedirectResponse(
        "/ui/billing?invoice_profile_saved=1",
        status_code=status.HTTP_303_SEE_OTHER,
    )


# ── 成員管理（B5 RBAC）───────────────────────────────────────────────────────


@router.get("/members", response_class=HTMLResponse)
def members_page(
    request: Request,
    actor: Actor = Depends(require_ui_owner),
    db: Session = Depends(get_db),
):
    """成員清單 + 邀請連結產生（owner 限定）。"""
    users = db.query(User).filter(User.tenant_id == actor.user.tenant_id).all()
    return templates.TemplateResponse(
        "members.html", _ctx(request, actor, members=users, invite_url=None)
    )


@router.post("/members/invite", response_class=HTMLResponse)
def members_invite(
    request: Request,
    actor: Actor = Depends(require_ui_owner),
    db: Session = Depends(get_db),
):
    """產生邀請連結（email_tokens purpose=invite，掛在 owner 身上；
    受邀者開連結自行設定 email/密碼，建為 staff）。"""
    import datetime as _dt

    from saas_mvp.models.email_token import (
        PURPOSE_INVITE,
        EmailToken,
        generate_token,
        hash_token,
    )

    token = generate_token()
    db.add(
        EmailToken(
            user_id=actor.user.id,
            purpose=PURPOSE_INVITE,
            token_hash=hash_token(token),
            expires_at=_dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(days=7),
        )
    )
    db.commit()
    audit_svc.record_from_actor(
        db,
        actor,
        action="member.invite",
        target=f"tenant:{actor.user.tenant_id}",
        request=request,
    )
    db.commit()
    base = settings.public_base_url.rstrip("/") or ""
    invite_url = f"{base}/ui/join/{token}"
    users = db.query(User).filter(User.tenant_id == actor.user.tenant_id).all()
    return templates.TemplateResponse(
        "members.html", _ctx(request, actor, members=users, invite_url=invite_url)
    )


@router.get("/join/{token}", response_class=HTMLResponse)
def join_form(token: str, request: Request):
    return templates.TemplateResponse("join.html", _ctx(request, token=token))


@router.post("/join/{token}", response_class=HTMLResponse)
def join_submit(
    token: str,
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    """受邀者建立 staff 帳號並登入（token 一次性、7 天效期）。"""
    from saas_mvp.services import account_email as ae_svc

    def _err(msg: str, code: int = status.HTTP_400_BAD_REQUEST):
        return templates.TemplateResponse(
            "join.html", _ctx(request, token=token, error=msg), status_code=code
        )

    if len(password) < 8:
        return _err("密碼至少需 8 個字元")
    if db.query(User).filter(User.email == email).first():
        return _err("此電子郵件已註冊")
    try:
        row = ae_svc._consume(db, token, "invite")  # noqa: SLF001 — 同套 token 機制
    except ae_svc.TokenInvalid:
        db.rollback()
        return _err("邀請連結無效或已過期，請向店家索取新連結")

    inviter = db.get(User, row.user_id)
    if inviter is None:
        db.rollback()
        return _err("邀請連結無效")
    user = User(
        email=email,
        hashed_password=hash_password(password),
        tenant_id=inviter.tenant_id,
        role="staff",
    )
    db.add(user)
    db.flush()
    tenant = db.get(Tenant, inviter.tenant_id)
    if tenant is None:
        db.rollback()
        return _err("邀請連結無效")
    from saas_mvp.services import organizations as organizations_svc

    organizations_svc.ensure_user_memberships(db, tenant=tenant, user=user)
    db.commit()
    db.refresh(user)
    audit_svc.record(
        db,
        action="member.join",
        actor_user_id=user.id,
        tenant_id=user.tenant_id,
        target=f"user:{user.id}",
    )
    db.commit()

    jwt_token = create_access_token(user_id=user.id, tenant_id=user.tenant_id)
    resp = RedirectResponse("/ui/", status_code=status.HTTP_303_SEE_OTHER)
    _set_auth_cookie(resp, jwt_token)
    return resp


# ── Google Calendar 連結（E1 Step B,owner 限定）──────────────────────────────


def _oauth_callback_base(request: Request) -> str:
    return (
        settings.oauth_redirect_base
        or settings.public_base_url
        or str(request.base_url)
    ).rstrip("/")


@router.get("/gcal/connect")
def gcal_connect(
    request: Request,
    actor: Actor = Depends(require_ui_owner),
    db: Session = Depends(get_db),
):
    """導向 Google OAuth(calendar.events scope,offline 拿 refresh token)。

    平台未設定 Google OAuth 憑證時顯示說明頁(stub-ready)。
    """
    import secrets as _secrets
    import urllib.parse as _up

    credentials = platform_oauth_svc.effective_google_credentials(db, settings)
    if not credentials:
        admin_link = (
            '<p><a href="/ui/admin/oauth-settings">前往平台登入設定</a></p>'
            if actor.user.is_admin
            else "<p>請聯絡平台管理員完成 Google OAuth 設定。</p>"
        )
        return HTMLResponse(
            "<h1>Google 整合尚未設定</h1>"
            "<p>平台管理員可在後台設定 Google OAuth，儲存後立即生效，不需重啟。"
            "設定前仍可使用行事曆頁的 ICS 訂閱方案。</p>" + admin_link
        )
    state = _secrets.token_urlsafe(24)
    base = _oauth_callback_base(request)
    params = _up.urlencode(
        {
            "client_id": credentials[0],
            "redirect_uri": f"{base}/ui/gcal/callback",
            "response_type": "code",
            "scope": "https://www.googleapis.com/auth/calendar.events",
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
        }
    )
    resp = RedirectResponse(
        f"https://accounts.google.com/o/oauth2/v2/auth?{params}",
        status_code=status.HTTP_303_SEE_OTHER,
    )
    resp.set_cookie(
        "gcal_state",
        state,
        httponly=True,
        max_age=600,
        path="/",
        samesite="lax",
        secure=settings.env not in ("dev", "test"),
    )
    return resp


@router.get("/gcal/callback")
def gcal_callback(
    request: Request,
    code: str = "",
    state: str = "",
    actor: Actor = Depends(require_ui_owner),
    db: Session = Depends(get_db),
):
    from saas_mvp.models.tenant_gcal_credential import TenantGcalCredential
    from saas_mvp.services.oauth import _post_form

    if not state or state != request.cookies.get("gcal_state"):
        return HTMLResponse("<h1>狀態驗證失敗,請重試</h1>", status_code=400)
    if not code:
        return HTMLResponse("<h1>未取得授權碼</h1>", status_code=400)
    credentials = platform_oauth_svc.effective_google_credentials(db, settings)
    if not credentials:
        return HTMLResponse(
            "<h1>Google 整合設定已移除</h1><p>請聯絡平台管理員重新設定後再試。</p>",
            status_code=503,
        )
    base = _oauth_callback_base(request)
    try:
        token_resp = _post_form(
            "https://oauth2.googleapis.com/token",
            {
                "client_id": credentials[0],
                "client_secret": credentials[1],
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": f"{base}/ui/gcal/callback",
            },
        )
    except Exception:  # noqa: BLE001
        return HTMLResponse("<h1>Google 授權交換失敗,請重試</h1>", status_code=502)
    refresh_token = token_resp.get("refresh_token")
    if not refresh_token:
        return HTMLResponse(
            "<h1>未取得 refresh token</h1><p>請至 Google 帳戶移除本應用授權後重試。</p>",
            status_code=400,
        )
    tid = actor.user.tenant_id
    cred = db.execute(
        select(TenantGcalCredential).where(TenantGcalCredential.tenant_id == tid)
    ).scalar_one_or_none()
    if cred is None:
        cred = TenantGcalCredential(tenant_id=tid, calendar_id="primary")
        db.add(cred)
    cred.refresh_token = refresh_token
    cred.status = "connected"
    cred.last_error = None
    db.flush()
    # 首次連結／重新授權時，把尚未結束的預約排入同步，不要求店家逐筆異動。
    from saas_mvp.models.booking_slot import BookingSlot
    from saas_mvp.models.reservation import RESERVATION_CONFIRMED, Reservation
    from saas_mvp.services import gcal as gcal_svc

    upcoming = list(
        db.execute(
            select(Reservation)
            .join(BookingSlot, BookingSlot.id == Reservation.slot_id)
            .where(
                Reservation.tenant_id == tid,
                Reservation.status == RESERVATION_CONFIRMED,
                BookingSlot.slot_start >= datetime.datetime.now(datetime.timezone.utc),
            )
        ).scalars()
    )
    for reservation in upcoming:
        gcal_svc.enqueue_reservation_sync(db, reservation, "upsert")
    audit_svc.record_from_actor(
        db,
        actor,
        action="gcal.connect",
        target=f"tenant:{tid}",
        request=request,
    )
    db.commit()
    resp = RedirectResponse("/ui/calendar", status_code=status.HTTP_303_SEE_OTHER)
    resp.delete_cookie("gcal_state", path="/")  # 用完即清,避免殘留可重放的 state
    return resp


@router.post("/gcal/disconnect")
def gcal_disconnect(
    request: Request,
    actor: Actor = Depends(require_ui_owner),
    db: Session = Depends(get_db),
):
    from saas_mvp.models.tenant_gcal_credential import TenantGcalCredential

    tid = actor.user.tenant_id
    cred = db.execute(
        select(TenantGcalCredential).where(TenantGcalCredential.tenant_id == tid)
    ).scalar_one_or_none()
    if cred is not None:
        db.delete(cred)
        audit_svc.record_from_actor(
            db,
            actor,
            action="gcal.disconnect",
            target=f"tenant:{tid}",
            request=request,
        )
        db.commit()
    return RedirectResponse("/ui/calendar", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/gcal/retry")
def gcal_retry_failed(
    request: Request,
    actor: Actor = Depends(require_ui_owner),
    db: Session = Depends(get_db),
):
    from saas_mvp.services import gcal as gcal_svc

    count = gcal_svc.retry_failed(db, actor.user.tenant_id)
    audit_svc.record_from_actor(
        db,
        actor,
        action="gcal.retry",
        target=f"tenant:{actor.user.tenant_id}",
        detail={"count": count},
        request=request,
    )
    db.commit()
    return RedirectResponse(
        f"/ui/calendar?gcal_retry_queued={count}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


# ── 店家自助 ────────────────────────────────────────────────────────────────


@router.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request,
    verification_resent: int = Query(0),
    verification_error: int = Query(0),
    verification_queued: int = Query(0),
    verification_rate_limited: int = Query(0),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    tid = actor.user.tenant_id
    tenant = db.get(Tenant, tid)
    line_config = _line_config_or_none(db, tid)
    usage = get_quota_status(db, tid, plans_svc.effective_plan(tenant))
    push = push_quota_svc.get_push_quota_status(db, tid)
    checklist = onboarding_svc.checklist(db, tenant=tenant, user=actor.user)
    return templates.TemplateResponse(
        "dashboard.html",
        _ctx(
            request,
            actor,
            tenant=tenant,
            line_config=line_config,
            usage=usage,
            push_quota=push,
            plan_info=_plan_info(tenant),
            onboarding=checklist,
            onboarding_done=onboarding_svc.all_done(checklist),
            email_unverified=actor.user.email_verified_at is None,
            verification_resent=bool(verification_resent),
            verification_error=bool(verification_error),
            verification_queued=bool(verification_queued),
            verification_rate_limited=bool(verification_rate_limited),
            line_insights=_line_insights(db, tid),
        ),
    )


@router.get("/line-config", response_class=HTMLResponse)
def line_config_page(
    request: Request,
    actor: Actor = Depends(require_ui_owner),
    db: Session = Depends(get_db),
):
    tid = actor.user.tenant_id
    cfg = _line_config_or_none(db, tid)
    return templates.TemplateResponse(
        "line_config.html",
        _ctx(request, actor, cfg=cfg, webhook_url=_line_webhook_url_for(tid)),
    )


@router.post("/line-config", response_class=HTMLResponse)
def line_config_save(
    request: Request,
    channel_secret: str = Form(..., max_length=64),
    access_token: str = Form(..., max_length=1024),
    default_target_lang: str = Form("zh-TW"),
    actor: Actor = Depends(require_ui_owner),
    db: Session = Depends(get_db),
    bot_info_client: LineBotInfoClient = Depends(get_bot_info_client),
):
    tid = actor.user.tenant_id
    try:
        cfg = line_config_svc.upsert_line_config(
            db,
            tid,
            channel_secret=channel_secret,
            access_token=access_token,
            default_target_lang=default_target_lang,
            bot_info_client=bot_info_client,
        )
    except HTTPException as exc:
        return templates.TemplateResponse(
            "_line_config_status.html",
            _ctx(
                request,
                actor,
                cfg=None,
                webhook_url=_line_webhook_url_for(tid),
                error=str(exc.detail),
            ),
            status_code=exc.status_code,
        )
    audit_svc.record_from_actor(
        db,
        actor,
        action="line_config.upsert",
        target=f"tenant:{tid}",
        detail={"by": "owner"},
        request=request,
    )
    db.commit()
    return templates.TemplateResponse(
        "_line_config_status.html",
        _ctx(request, actor, cfg=cfg, webhook_url=_line_webhook_url_for(tid)),
    )


@router.post("/line-config/welcome", response_class=HTMLResponse)
def line_config_welcome_save(
    welcome_message: str = Form("", max_length=1000),
    actor: Actor = Depends(require_ui_owner),
    db: Session = Depends(get_db),
):
    """更新 follow 歡迎訊息（HTMX 局部回應）；空白＝清空、回內建預設。"""
    tid = actor.user.tenant_id
    try:
        line_config_svc.set_welcome_message(db, tid, welcome_message)
    except HTTPException as exc:
        return HTMLResponse(
            f'<p class="error">儲存失敗：{exc.detail}</p>', status_code=exc.status_code
        )
    return HTMLResponse('<p class="muted">✅ 歡迎訊息已更新</p>')


@router.post("/line-config/verify", response_class=HTMLResponse)
def line_config_verify(
    request: Request,
    actor: Actor = Depends(require_ui_owner),
    db: Session = Depends(get_db),
    bot_info_client: LineBotInfoClient = Depends(get_bot_info_client),
):
    tid = actor.user.tenant_id
    try:
        cfg = line_config_svc.verify_line_config(
            db, tid, bot_info_client=bot_info_client
        )
    except HTTPException as exc:
        return templates.TemplateResponse(
            "_line_config_status.html",
            _ctx(
                request,
                actor,
                cfg=None,
                webhook_url=_line_webhook_url_for(tid),
                error=str(exc.detail),
            ),
            status_code=exc.status_code,
        )
    return templates.TemplateResponse(
        "_line_config_status.html",
        _ctx(request, actor, cfg=cfg, webhook_url=_line_webhook_url_for(tid)),
    )


@router.post("/line-config/webhook/setup", response_class=HTMLResponse)
def line_config_webhook_setup(
    request: Request,
    actor: Actor = Depends(require_ui_owner),
    db: Session = Depends(get_db),
    webhook_admin_client: LineWebhookAdminClient = Depends(get_webhook_admin_client),
):
    """一鍵設定租戶專屬 LINE Webhook URL 並執行官方連通測試。"""
    tid = actor.user.tenant_id
    endpoint = _line_webhook_url_for(tid)
    if not endpoint.startswith("https://"):
        return templates.TemplateResponse(
            "_line_webhook_setup_result.html",
            _ctx(
                request,
                actor,
                error="平台尚未設定 HTTPS 對外網址，請聯絡平台管理員。",
            ),
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    try:
        result = line_config_svc.configure_line_webhook(
            db,
            tid,
            endpoint=endpoint,
            webhook_admin_client=webhook_admin_client,
        )
    except HTTPException as exc:
        return templates.TemplateResponse(
            "_line_webhook_setup_result.html",
            _ctx(request, actor, error=str(exc.detail)),
            status_code=exc.status_code,
        )
    except LineWebhookAdminError as exc:
        return templates.TemplateResponse(
            "_line_webhook_setup_result.html",
            _ctx(request, actor, error=str(exc)),
            status_code=status.HTTP_502_BAD_GATEWAY,
        )
    audit_svc.record_from_actor(
        db,
        actor,
        action="line_config.webhook_setup",
        target=f"tenant:{tid}",
        detail={
            "success": result.success,
            "active": result.active,
            "status_code": result.status_code,
        },
        request=request,
    )
    db.commit()
    return templates.TemplateResponse(
        "_line_webhook_setup_result.html",
        _ctx(request, actor, result=result),
    )


@router.post("/line-config/delete", response_class=HTMLResponse)
def line_config_delete(
    request: Request,
    actor: Actor = Depends(require_ui_owner),
    db: Session = Depends(get_db),
):
    tid = actor.user.tenant_id
    try:
        line_config_svc.delete_line_config(db, tid)
    except HTTPException:
        pass  # 不存在即視為已刪除
    return templates.TemplateResponse(
        "_line_config_status.html",
        _ctx(request, actor, cfg=None, webhook_url=_line_webhook_url_for(tid)),
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
    db: Session = Depends(get_db),
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
            line_login_configured=oauth_svc.provider_credentials_present(
                "line", settings=settings, db=db
            ),
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
            _ctx(
                request,
                actor,
                deploy_error="未設定部署觸發路徑（SAAS_DEPLOY_TRIGGER_PATH），無法觸發。",
            ),
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
            "_account_password.html",
            _ctx(request, actor, error=error),
        )

    user.hashed_password = hash_password(new_password)
    db.add(user)
    db.commit()
    return templates.TemplateResponse(
        "_account_password.html",
        _ctx(request, actor, saved=True),
    )


# ── 平台管理 ────────────────────────────────────────────────────────────────


@router.get("/admin", response_class=HTMLResponse)
def admin_overview(
    request: Request,
    actor: Actor = Depends(require_ui_admin),
    db: Session = Depends(get_db),
):
    """平台總覽（B4）：租戶/方案分佈/試用/MRR/本月扣款。"""
    readiness = readiness_dashboard_svc.build_dashboard(db)
    return templates.TemplateResponse(
        "admin/overview.html",
        _ctx(
            request,
            actor,
            overview=admin_svc.platform_overview(db),
            readiness=readiness,
        ),
    )


@router.get("/admin/readiness", response_class=HTMLResponse)
def admin_readiness(
    request: Request,
    actor: Actor = Depends(require_ui_admin),
    db: Session = Depends(get_db),
):
    """平台上線檢查中心：將技術檢查轉成可理解、可操作的後台頁面。"""
    return templates.TemplateResponse(
        "admin/readiness.html",
        _ctx(request, actor, readiness=readiness_dashboard_svc.build_dashboard(db)),
    )


def _platform_oauth_ctx(
    request: Request,
    actor: Actor,
    db: Session,
    **extra,
) -> dict:
    callback_base = _oauth_callback_base(request)
    return _ctx(
        request,
        actor,
        line_status=platform_oauth_svc.line_status(db, settings),
        google_status=platform_oauth_svc.google_status(db, settings),
        line_callback_url=f"{callback_base}/auth/oauth/line/callback",
        google_login_callback_url=f"{callback_base}/auth/oauth/google/callback",
        google_calendar_callback_url=f"{callback_base}/ui/gcal/callback",
        **extra,
    )


@router.get("/admin/oauth-settings", response_class=HTMLResponse)
def admin_oauth_settings(
    request: Request,
    saved: int = Query(0),
    google_saved: int = Query(0),
    actor: Actor = Depends(require_ui_admin),
    db: Session = Depends(get_db),
):
    """平台共用 LINE / Google OAuth；只有平台管理員可讀取或修改。"""
    return templates.TemplateResponse(
        "admin/oauth_settings.html",
        _platform_oauth_ctx(
            request,
            actor,
            db,
            saved=bool(saved),
            google_saved=bool(google_saved),
        ),
    )


@router.post("/admin/oauth-settings/line", response_class=HTMLResponse)
def admin_oauth_settings_save(
    request: Request,
    channel_id: str = Form(..., max_length=255),
    channel_secret: str = Form("", max_length=255),
    actor: Actor = Depends(require_ui_admin),
    db: Session = Depends(get_db),
):
    try:
        platform_oauth_svc.save_line_credentials(
            db,
            channel_id=channel_id,
            channel_secret=channel_secret,
            actor_user_id=actor.user.id,
        )
    except platform_oauth_svc.PlatformOAuthConfigError as exc:
        db.rollback()
        return templates.TemplateResponse(
            "admin/oauth_settings.html",
            _platform_oauth_ctx(request, actor, db, line_error=str(exc)),
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    audit_svc.record_from_actor(
        db,
        actor,
        action="platform.oauth.line.update",
        target="oauth:line",
        detail={
            "channel_id": channel_id.strip(),
            "secret_changed": bool(channel_secret),
        },
        request=request,
    )
    db.commit()
    return RedirectResponse(
        "/ui/admin/oauth-settings?saved=1",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/admin/oauth-settings/line/reset", response_class=HTMLResponse)
def admin_oauth_settings_reset(
    request: Request,
    actor: Actor = Depends(require_ui_admin),
    db: Session = Depends(get_db),
):
    removed = platform_oauth_svc.clear_line_override(db)
    if removed:
        audit_svc.record_from_actor(
            db,
            actor,
            action="platform.oauth.line.reset",
            target="oauth:line",
            request=request,
        )
    db.commit()
    return RedirectResponse(
        "/ui/admin/oauth-settings",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/admin/oauth-settings/google", response_class=HTMLResponse)
def admin_google_oauth_settings_save(
    request: Request,
    client_id: str = Form(..., max_length=255),
    client_secret: str = Form("", max_length=255),
    actor: Actor = Depends(require_ui_admin),
    db: Session = Depends(get_db),
):
    try:
        platform_oauth_svc.save_google_credentials(
            db,
            client_id=client_id,
            client_secret=client_secret,
            actor_user_id=actor.user.id,
        )
    except platform_oauth_svc.PlatformOAuthConfigError as exc:
        db.rollback()
        return templates.TemplateResponse(
            "admin/oauth_settings.html",
            _platform_oauth_ctx(request, actor, db, google_error=str(exc)),
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    audit_svc.record_from_actor(
        db,
        actor,
        action="platform.oauth.google.update",
        target="oauth:google",
        detail={"client_id": client_id.strip(), "secret_changed": bool(client_secret)},
        request=request,
    )
    db.commit()
    return RedirectResponse(
        "/ui/admin/oauth-settings?google_saved=1",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/admin/oauth-settings/google/reset", response_class=HTMLResponse)
def admin_google_oauth_settings_reset(
    request: Request,
    actor: Actor = Depends(require_ui_admin),
    db: Session = Depends(get_db),
):
    removed = platform_oauth_svc.clear_google_override(db)
    if removed:
        if platform_oauth_svc.effective_google_credentials(db, settings) is None:
            from saas_mvp.models.tenant_gcal_credential import (
                GCAL_ERROR,
                TenantGcalCredential,
            )

            db.query(TenantGcalCredential).update(
                {
                    TenantGcalCredential.status: GCAL_ERROR,
                    TenantGcalCredential.last_error: (
                        "平台 Google OAuth 設定已移除，請聯絡平台管理員"
                    ),
                }
            )
        audit_svc.record_from_actor(
            db,
            actor,
            action="platform.oauth.google.reset",
            target="oauth:google",
            request=request,
        )
    db.commit()
    return RedirectResponse(
        "/ui/admin/oauth-settings",
        status_code=status.HTTP_303_SEE_OTHER,
    )


def _platform_email_ctx(
    request: Request,
    actor: Actor,
    db: Session,
    **extra,
) -> dict:
    from saas_mvp.services import email_delivery as delivery_svc

    return _ctx(
        request,
        actor,
        email_status=platform_email_svc.email_status(db, settings),
        email_delivery_summary=delivery_svc.summary(db),
        email_deliveries=delivery_svc.recent(db),
        **extra,
    )


@router.get("/admin/email-settings", response_class=HTMLResponse)
def admin_email_settings(
    request: Request,
    saved: int = Query(0),
    retry_queued: int = Query(0),
    actor: Actor = Depends(require_ui_admin),
    db: Session = Depends(get_db),
):
    return templates.TemplateResponse(
        "admin/email_settings.html",
        _platform_email_ctx(
            request, actor, db, saved=bool(saved), retry_queued=bool(retry_queued)
        ),
    )


@router.post("/admin/email-settings", response_class=HTMLResponse)
def admin_email_settings_save(
    request: Request,
    smtp_host: str = Form(..., max_length=255),
    smtp_port: int = Form(587),
    smtp_user: str = Form("", max_length=255),
    smtp_password: str = Form("", max_length=255),
    smtp_from: str = Form(..., max_length=255),
    actor: Actor = Depends(require_ui_admin),
    db: Session = Depends(get_db),
):
    try:
        platform_email_svc.save_email_config(
            db,
            host=smtp_host,
            port=smtp_port,
            user=smtp_user,
            password=smtp_password,
            from_address=smtp_from,
            actor_user_id=actor.user.id,
        )
    except platform_email_svc.PlatformEmailConfigError as exc:
        db.rollback()
        return templates.TemplateResponse(
            "admin/email_settings.html",
            _platform_email_ctx(request, actor, db, error=str(exc)),
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    audit_svc.record_from_actor(
        db,
        actor,
        action="platform.email.update",
        target="email:smtp",
        detail={"host": smtp_host.strip(), "port": smtp_port},
        request=request,
    )
    db.commit()
    return RedirectResponse(
        "/ui/admin/email-settings?saved=1",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/admin/email-settings/test", response_class=HTMLResponse)
def admin_email_settings_test(
    request: Request,
    actor: Actor = Depends(require_ui_admin),
    db: Session = Depends(get_db),
    mailer: Mailer = Depends(get_mailer),
):
    try:
        mailer.send(
            to=actor.user.email,
            subject="寄信設定測試 — LINE 預約平台",
            body="這是一封平台 SMTP 設定測試信。若你收到此信，代表寄信服務設定成功。",
        )
    except MailerError as exc:
        audit_svc.record_from_actor(
            db,
            actor,
            action="platform.email.test",
            target="email:smtp",
            detail={"result": "failed", "reason": str(exc)},
            request=request,
        )
        db.commit()
        return templates.TemplateResponse(
            "admin/email_settings.html",
            _platform_email_ctx(
                request, actor, db, test_error=f"測試信寄送失敗：{exc}"
            ),
            status_code=status.HTTP_502_BAD_GATEWAY,
        )
    audit_svc.record_from_actor(
        db,
        actor,
        action="platform.email.test",
        target="email:smtp",
        detail={"result": "sent"},
        request=request,
    )
    db.commit()
    return templates.TemplateResponse(
        "admin/email_settings.html",
        _platform_email_ctx(request, actor, db, test_sent=True),
    )


@router.post("/admin/email-settings/reset", response_class=HTMLResponse)
def admin_email_settings_reset(
    request: Request,
    actor: Actor = Depends(require_ui_admin),
    db: Session = Depends(get_db),
):
    removed = platform_email_svc.clear_email_override(db)
    if removed:
        audit_svc.record_from_actor(
            db,
            actor,
            action="platform.email.reset",
            target="email:smtp",
            request=request,
        )
    db.commit()
    return RedirectResponse(
        "/ui/admin/email-settings", status_code=status.HTTP_303_SEE_OTHER
    )


@router.post("/admin/email-settings/retry", response_class=HTMLResponse)
def admin_email_settings_retry(
    request: Request,
    actor: Actor = Depends(require_ui_admin),
    db: Session = Depends(get_db),
):
    from saas_mvp.services import email_delivery as delivery_svc

    count = delivery_svc.retry_unsent(db)
    audit_svc.record_from_actor(
        db,
        actor,
        action="platform.email.retry",
        target="email:outbox",
        detail={"count": count},
        request=request,
    )
    db.commit()
    return RedirectResponse(
        "/ui/admin/email-settings?retry_queued=1",
        status_code=status.HTTP_303_SEE_OTHER,
    )


def _platform_ai_ctx(
    request: Request,
    actor: Actor,
    db: Session,
    **extra,
) -> dict:
    return _ctx(
        request,
        actor,
        ai_status=platform_ai_svc.ai_status(db, settings),
        **extra,
    )


@router.get("/admin/ai-settings", response_class=HTMLResponse)
def admin_ai_settings(
    request: Request,
    saved: int = Query(0),
    actor: Actor = Depends(require_ui_admin),
    db: Session = Depends(get_db),
):
    return templates.TemplateResponse(
        "admin/ai_settings.html",
        _platform_ai_ctx(request, actor, db, saved=bool(saved)),
    )


@router.post("/admin/ai-settings", response_class=HTMLResponse)
def admin_ai_settings_save(
    request: Request,
    api_key: str = Form("", max_length=255),
    model: str = Form(..., max_length=128),
    actor: Actor = Depends(require_ui_admin),
    db: Session = Depends(get_db),
):
    try:
        platform_ai_svc.save_ai_config(
            db,
            api_key=api_key,
            model=model,
            actor_user_id=actor.user.id,
        )
    except platform_ai_svc.PlatformAIConfigError as exc:
        db.rollback()
        return templates.TemplateResponse(
            "admin/ai_settings.html",
            _platform_ai_ctx(request, actor, db, error=str(exc)),
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    audit_svc.record_from_actor(
        db,
        actor,
        action="platform.ai.update",
        target="ai:minimax",
        detail={"model": model.strip(), "key_changed": bool(api_key.strip())},
        request=request,
    )
    db.commit()
    return RedirectResponse(
        "/ui/admin/ai-settings?saved=1",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/admin/ai-settings/test", response_class=HTMLResponse)
def admin_ai_settings_test(
    request: Request,
    actor: Actor = Depends(require_ui_admin),
    db: Session = Depends(get_db),
):
    try:
        platform_ai_svc.test_ai_config(db, settings)
    except platform_ai_svc.PlatformAIConfigError as exc:
        return templates.TemplateResponse(
            "admin/ai_settings.html",
            _platform_ai_ctx(request, actor, db, test_error=str(exc)),
            status_code=status.HTTP_502_BAD_GATEWAY,
        )
    audit_svc.record_from_actor(
        db,
        actor,
        action="platform.ai.test",
        target="ai:minimax",
        request=request,
    )
    db.commit()
    return templates.TemplateResponse(
        "admin/ai_settings.html",
        _platform_ai_ctx(request, actor, db, test_ok=True),
    )


@router.post("/admin/ai-settings/reset", response_class=HTMLResponse)
def admin_ai_settings_reset(
    request: Request,
    actor: Actor = Depends(require_ui_admin),
    db: Session = Depends(get_db),
):
    removed = platform_ai_svc.clear_ai_override(db)
    if removed:
        audit_svc.record_from_actor(
            db,
            actor,
            action="platform.ai.reset",
            target="ai:minimax",
            request=request,
        )
    db.commit()
    return RedirectResponse(
        "/ui/admin/ai-settings",
        status_code=status.HTTP_303_SEE_OTHER,
    )


def _platform_observability_ctx(
    request: Request, actor: Actor, db: Session, **extra
) -> dict:
    return _ctx(
        request,
        actor,
        observability_status=platform_observability_svc.observability_status(
            db, settings
        ),
        **extra,
    )


@router.get("/admin/observability-settings", response_class=HTMLResponse)
def admin_observability_settings(
    request: Request,
    saved: int = Query(0),
    actor: Actor = Depends(require_ui_admin),
    db: Session = Depends(get_db),
):
    return templates.TemplateResponse(
        "admin/observability_settings.html",
        _platform_observability_ctx(request, actor, db, saved=bool(saved)),
    )


@router.post("/admin/observability-settings", response_class=HTMLResponse)
def admin_observability_settings_save(
    request: Request,
    sentry_dsn: str = Form(..., max_length=1024),
    actor: Actor = Depends(require_ui_admin),
    db: Session = Depends(get_db),
):
    try:
        platform_observability_svc.save_observability_config(
            db, sentry_dsn=sentry_dsn, actor_user_id=actor.user.id
        )
    except platform_observability_svc.PlatformObservabilityConfigError as exc:
        db.rollback()
        return templates.TemplateResponse(
            "admin/observability_settings.html",
            _platform_observability_ctx(request, actor, db, error=str(exc)),
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    audit_svc.record_from_actor(
        db,
        actor,
        action="platform.observability.update",
        target="observability:sentry",
        request=request,
    )
    db.commit()
    platform_observability_svc.apply_effective_observability_config(db, settings)
    return RedirectResponse(
        "/ui/admin/observability-settings?saved=1",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/admin/observability-settings/test", response_class=HTMLResponse)
def admin_observability_settings_test(
    request: Request,
    actor: Actor = Depends(require_ui_admin),
    db: Session = Depends(get_db),
):
    try:
        platform_observability_svc.send_test_event(db, settings)
    except platform_observability_svc.PlatformObservabilityConfigError as exc:
        return templates.TemplateResponse(
            "admin/observability_settings.html",
            _platform_observability_ctx(request, actor, db, test_error=str(exc)),
            status_code=status.HTTP_502_BAD_GATEWAY,
        )
    audit_svc.record_from_actor(
        db,
        actor,
        action="platform.observability.test",
        target="observability:sentry",
        request=request,
    )
    db.commit()
    return templates.TemplateResponse(
        "admin/observability_settings.html",
        _platform_observability_ctx(request, actor, db, test_ok=True),
    )


@router.post("/admin/observability-settings/reset", response_class=HTMLResponse)
def admin_observability_settings_reset(
    request: Request,
    actor: Actor = Depends(require_ui_admin),
    db: Session = Depends(get_db),
):
    removed = platform_observability_svc.clear_observability_override(db)
    if removed:
        audit_svc.record_from_actor(
            db,
            actor,
            action="platform.observability.reset",
            target="observability:sentry",
            request=request,
        )
    db.commit()
    platform_observability_svc.apply_effective_observability_config(db, settings)
    return RedirectResponse(
        "/ui/admin/observability-settings",
        status_code=status.HTTP_303_SEE_OTHER,
    )


def _platform_payment_ctx(
    request: Request,
    actor: Actor,
    db: Session,
    **extra,
) -> dict:
    from saas_mvp.models.feature_subscription import (
        SUB_ACTIVE,
        SUB_CANCEL_FAILED,
        SUB_PENDING,
        FeatureSubscription,
    )

    base = settings.public_base_url.rstrip("/")
    unsettled = (
        db.query(FeatureSubscription)
        .filter(
            FeatureSubscription.status.in_((SUB_PENDING, SUB_ACTIVE, SUB_CANCEL_FAILED))
        )
        .count()
    )
    return _ctx(
        request,
        actor,
        payment_status=platform_payment_svc.payment_status(db, settings),
        payment_public_base_url=base,
        payment_callbacks={
            "order": f"{base}/payments/ecpay/callback",
            "subscription": f"{base}/payments/ecpay/subscribe-callback",
            "period": f"{base}/payments/ecpay/period-callback",
            "deposit": f"{base}/payments/ecpay/deposit-callback",
        },
        unsettled_subscriptions=unsettled,
        refundable_deposits=platform_payment_svc.refundable_deposit_count(db),
        **extra,
    )


@router.get("/admin/payment-settings", response_class=HTMLResponse)
def admin_payment_settings(
    request: Request,
    saved: int = Query(0),
    actor: Actor = Depends(require_ui_admin),
    db: Session = Depends(get_db),
):
    return templates.TemplateResponse(
        "admin/payment_settings.html",
        _platform_payment_ctx(request, actor, db, saved=bool(saved)),
    )


@router.post("/admin/payment-settings/ecpay", response_class=HTMLResponse)
def admin_payment_settings_save(
    request: Request,
    merchant_id: str = Form(..., max_length=64),
    hash_key: str = Form("", max_length=128),
    hash_iv: str = Form("", max_length=128),
    environment: str = Form(..., max_length=16),
    actor: Actor = Depends(require_ui_admin),
    db: Session = Depends(get_db),
):
    try:
        platform_payment_svc.save_ecpay_config(
            db,
            merchant_id=merchant_id,
            hash_key=hash_key,
            hash_iv=hash_iv,
            environment=environment,
            actor_user_id=actor.user.id,
            public_base_url=settings.public_base_url,
        )
    except platform_payment_svc.PlatformPaymentConfigError as exc:
        db.rollback()
        return templates.TemplateResponse(
            "admin/payment_settings.html",
            _platform_payment_ctx(request, actor, db, error=str(exc)),
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    audit_svc.record_from_actor(
        db,
        actor,
        action="platform.payment.ecpay.update",
        target="payment:ecpay",
        detail={
            "merchant_id": merchant_id.strip(),
            "environment": environment.strip().lower(),
            "hash_key_changed": bool(hash_key.strip()),
            "hash_iv_changed": bool(hash_iv.strip()),
        },
        request=request,
    )
    db.commit()
    return RedirectResponse(
        "/ui/admin/payment-settings?saved=1",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/admin/payment-settings/check", response_class=HTMLResponse)
def admin_payment_settings_check(
    request: Request,
    actor: Actor = Depends(require_ui_admin),
    db: Session = Depends(get_db),
):
    try:
        platform_payment_svc.self_check(db, settings)
    except platform_payment_svc.PlatformPaymentConfigError as exc:
        return templates.TemplateResponse(
            "admin/payment_settings.html",
            _platform_payment_ctx(request, actor, db, check_error=str(exc)),
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    audit_svc.record_from_actor(
        db,
        actor,
        action="platform.payment.ecpay.check",
        target="payment:ecpay",
        request=request,
    )
    db.commit()
    return templates.TemplateResponse(
        "admin/payment_settings.html",
        _platform_payment_ctx(request, actor, db, check_ok=True),
    )


@router.post("/admin/payment-settings/disable", response_class=HTMLResponse)
def admin_payment_settings_disable(
    request: Request,
    actor: Actor = Depends(require_ui_admin),
    db: Session = Depends(get_db),
):
    try:
        platform_payment_svc.disable_payment(db, actor_user_id=actor.user.id)
    except platform_payment_svc.PlatformPaymentConfigError as exc:
        db.rollback()
        return templates.TemplateResponse(
            "admin/payment_settings.html",
            _platform_payment_ctx(request, actor, db, error=str(exc)),
            status_code=status.HTTP_409_CONFLICT,
        )
    audit_svc.record_from_actor(
        db,
        actor,
        action="platform.payment.disable",
        target="payment:stub",
        request=request,
    )
    db.commit()
    return RedirectResponse(
        "/ui/admin/payment-settings",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/admin/payment-settings/reset", response_class=HTMLResponse)
def admin_payment_settings_reset(
    request: Request,
    actor: Actor = Depends(require_ui_admin),
    db: Session = Depends(get_db),
):
    try:
        removed = platform_payment_svc.clear_payment_override(db)
    except platform_payment_svc.PlatformPaymentConfigError as exc:
        db.rollback()
        return templates.TemplateResponse(
            "admin/payment_settings.html",
            _platform_payment_ctx(request, actor, db, error=str(exc)),
            status_code=status.HTTP_409_CONFLICT,
        )
    if removed:
        audit_svc.record_from_actor(
            db,
            actor,
            action="platform.payment.reset",
            target="payment:environment",
            request=request,
        )
    db.commit()
    return RedirectResponse(
        "/ui/admin/payment-settings",
        status_code=status.HTTP_303_SEE_OTHER,
    )


def _platform_invoice_ctx(
    request: Request,
    actor: Actor,
    db: Session,
    **extra,
) -> dict:
    from sqlalchemy import func
    from saas_mvp.models.invoice import Invoice

    counts = dict(
        db.query(Invoice.status, func.count(Invoice.id)).group_by(Invoice.status).all()
    )
    invoices = db.query(Invoice).order_by(Invoice.id.desc()).limit(50).all()
    return _ctx(
        request,
        actor,
        invoice_status=platform_invoice_svc.invoice_status(db, settings),
        invoice_counts={
            "pending": counts.get("pending", 0),
            "issued": counts.get("issued", 0),
            "failed": counts.get("failed", 0),
            "voiding": counts.get("voiding", 0),
            "void": counts.get("void", 0),
        },
        invoices=invoices,
        invoice_buyer_summaries={
            row.id: invoice_profiles_svc.invoice_buyer_summary(row) for row in invoices
        },
        **extra,
    )


@router.get("/admin/invoice-settings", response_class=HTMLResponse)
def admin_invoice_settings(
    request: Request,
    saved: int = Query(0),
    retried: int = Query(-1),
    voided: str = Query("", max_length=16),
    actor: Actor = Depends(require_ui_admin),
    db: Session = Depends(get_db),
):
    return templates.TemplateResponse(
        "admin/invoice_settings.html",
        _platform_invoice_ctx(
            request,
            actor,
            db,
            saved=bool(saved),
            retried=None if retried < 0 else retried,
            voided=voided,
        ),
    )


@router.post("/admin/invoice-settings/ecpay", response_class=HTMLResponse)
def admin_invoice_settings_save(
    request: Request,
    merchant_id: str = Form(..., max_length=64),
    hash_key: str = Form("", max_length=128),
    hash_iv: str = Form("", max_length=128),
    environment: str = Form(..., max_length=16),
    actor: Actor = Depends(require_ui_admin),
    db: Session = Depends(get_db),
):
    try:
        platform_invoice_svc.save_ecpay_config(
            db,
            merchant_id=merchant_id,
            hash_key=hash_key,
            hash_iv=hash_iv,
            environment=environment,
            actor_user_id=actor.user.id,
        )
    except platform_invoice_svc.PlatformInvoiceConfigError as exc:
        db.rollback()
        return templates.TemplateResponse(
            "admin/invoice_settings.html",
            _platform_invoice_ctx(request, actor, db, error=str(exc)),
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    audit_svc.record_from_actor(
        db,
        actor,
        action="platform.invoice.ecpay.update",
        target="invoice:ecpay",
        detail={
            "merchant_id": merchant_id.strip(),
            "environment": environment.strip().lower(),
            "hash_key_changed": bool(hash_key.strip()),
            "hash_iv_changed": bool(hash_iv.strip()),
        },
        request=request,
    )
    db.commit()
    return RedirectResponse(
        "/ui/admin/invoice-settings?saved=1",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/admin/invoice-settings/check", response_class=HTMLResponse)
def admin_invoice_settings_check(
    request: Request,
    actor: Actor = Depends(require_ui_admin),
    db: Session = Depends(get_db),
):
    try:
        platform_invoice_svc.self_check(db, settings)
    except platform_invoice_svc.PlatformInvoiceConfigError as exc:
        return templates.TemplateResponse(
            "admin/invoice_settings.html",
            _platform_invoice_ctx(request, actor, db, check_error=str(exc)),
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    audit_svc.record_from_actor(
        db,
        actor,
        action="platform.invoice.ecpay.check",
        target="invoice:ecpay",
        request=request,
    )
    db.commit()
    return templates.TemplateResponse(
        "admin/invoice_settings.html",
        _platform_invoice_ctx(request, actor, db, check_ok=True),
    )


@router.post("/admin/invoice-settings/retry", response_class=HTMLResponse)
def admin_invoice_settings_retry(
    request: Request,
    actor: Actor = Depends(require_ui_admin),
    db: Session = Depends(get_db),
):
    from saas_mvp.models.invoice import INVOICE_FAILED, Invoice
    from saas_mvp.services.invoices import _attempt_issue

    rows = db.query(Invoice).filter(Invoice.status == INVOICE_FAILED).all()
    for row in rows:
        _attempt_issue(db, row)
    audit_svc.record_from_actor(
        db,
        actor,
        action="platform.invoice.retry",
        target="invoice:failed",
        detail={"count": len(rows)},
        request=request,
    )
    db.commit()
    return RedirectResponse(
        f"/ui/admin/invoice-settings?retried={len(rows)}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


def _invoice_config_error_response(request, actor, db, exc):
    db.rollback()
    return templates.TemplateResponse(
        "admin/invoice_settings.html",
        _platform_invoice_ctx(request, actor, db, error=str(exc)),
        status_code=status.HTTP_400_BAD_REQUEST,
    )


@router.post("/admin/invoice-settings/disable", response_class=HTMLResponse)
def admin_invoice_settings_disable(
    request: Request,
    actor: Actor = Depends(require_ui_admin),
    db: Session = Depends(get_db),
):
    try:
        platform_invoice_svc.disable_invoice(db, actor_user_id=actor.user.id)
    except platform_invoice_svc.PlatformInvoiceConfigError as exc:
        return _invoice_config_error_response(request, actor, db, exc)
    audit_svc.record_from_actor(
        db,
        actor,
        action="platform.invoice.disable",
        target="invoice:ecpay",
        request=request,
    )
    db.commit()
    return RedirectResponse(
        "/ui/admin/invoice-settings", status_code=status.HTTP_303_SEE_OTHER
    )


@router.post("/admin/invoice-settings/reset", response_class=HTMLResponse)
def admin_invoice_settings_reset(
    request: Request,
    actor: Actor = Depends(require_ui_admin),
    db: Session = Depends(get_db),
):
    try:
        removed = platform_invoice_svc.clear_invoice_override(db)
    except platform_invoice_svc.PlatformInvoiceConfigError as exc:
        return _invoice_config_error_response(request, actor, db, exc)
    if removed:
        audit_svc.record_from_actor(
            db,
            actor,
            action="platform.invoice.reset",
            target="invoice:ecpay",
            request=request,
        )
    db.commit()
    return RedirectResponse(
        "/ui/admin/invoice-settings", status_code=status.HTTP_303_SEE_OTHER
    )


@router.post("/admin/invoice-settings/{invoice_id}/void", response_class=HTMLResponse)
def admin_invoice_void(
    invoice_id: int,
    request: Request,
    reason: str = Form(..., min_length=1, max_length=20),
    actor: Actor = Depends(require_ui_admin),
    db: Session = Depends(get_db),
):
    from saas_mvp.services import invoices as invoices_svc

    try:
        row = invoices_svc.void_invoice(db, invoice_id, reason=reason)
    except invoices_svc.InvoiceProviderError as exc:
        audit_svc.record_from_actor(
            db,
            actor,
            action="platform.invoice.void_failed",
            target=f"invoice:{invoice_id}",
            detail={"reason": reason.strip(), "error": str(exc)[:255]},
            request=request,
        )
        db.commit()
        return templates.TemplateResponse(
            "admin/invoice_settings.html",
            _platform_invoice_ctx(request, actor, db, operation_error=str(exc)),
            status_code=status.HTTP_502_BAD_GATEWAY,
        )
    except invoices_svc.InvoiceOperationError as exc:
        db.rollback()
        return templates.TemplateResponse(
            "admin/invoice_settings.html",
            _platform_invoice_ctx(request, actor, db, operation_error=str(exc)),
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    audit_svc.record_from_actor(
        db,
        actor,
        action="platform.invoice.void",
        target=f"invoice:{row.id}",
        detail={"invoice_no": row.invoice_no, "reason": row.void_reason},
        request=request,
    )
    db.commit()
    return RedirectResponse(
        f"/ui/admin/invoice-settings?voided={row.invoice_no}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.get("/admin/audit", response_class=HTMLResponse)
def admin_audit(
    request: Request,
    tenant_id: int | None = Query(None),
    action: str = Query(""),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    actor: Actor = Depends(require_ui_admin),
    db: Session = Depends(get_db),
):
    """稽核日誌檢視（F1）：篩 tenant/action、分頁。"""
    from saas_mvp.models.audit_log import AuditLog

    stmt = select(AuditLog).order_by(AuditLog.id.desc())
    if tenant_id is not None:
        stmt = stmt.where(AuditLog.tenant_id == tenant_id)
    if action.strip():
        stmt = stmt.where(AuditLog.action.like(f"{action.strip()}%"))
    rows = db.execute(stmt.offset(skip).limit(limit)).scalars().all()
    ctx = _ctx(
        request,
        actor,
        rows=rows,
        filters={
            "tenant_id": tenant_id,
            "action": action,
            "skip": skip,
            "limit": limit,
        },
    )
    template = "admin/_audit_table.html" if _is_htmx(request) else "admin/audit.html"
    return templates.TemplateResponse(template, ctx)


@router.post("/admin/tenants/{tenant_id}/impersonate", response_class=HTMLResponse)
def admin_impersonate(
    request: Request,
    tenant_id: int,
    actor: Actor = Depends(require_ui_admin),
    db: Session = Depends(get_db),
):
    """代管（F2）:以該租戶 owner 身分開 30 分鐘短票 session。

    安全:拒絕代管 admin(禁權限橫向移動)、拒絕鏈式代管、audit start、
    代管票 actor=owner 天然進不了 /ui/admin。
    """
    if actor.impersonator_user_id is not None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="已在代管中,不可鏈式代管"
        )
    target_owner = (
        db.execute(
            select(User)
            .where(
                User.tenant_id == tenant_id,
                User.role == "owner",
            )
            .order_by(User.id)
        )
        .scalars()
        .first()
    )
    if target_owner is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="該租戶沒有 owner 帳號"
        )
    if target_owner.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="不可代管平台管理員帳號"
        )

    audit_svc.record(
        db,
        action="impersonation.start",
        actor_user_id=target_owner.id,
        impersonator_user_id=actor.user.id,
        tenant_id=tenant_id,
        target=f"user:{target_owner.id}",
    )
    db.commit()
    token = create_access_token(
        user_id=target_owner.id,
        tenant_id=tenant_id,
        impersonator_id=actor.user.id,
    )
    resp = RedirectResponse("/ui/", status_code=status.HTTP_303_SEE_OTHER)
    _set_auth_cookie(resp, token)
    return resp


@router.post("/impersonation/stop", response_class=HTMLResponse)
def impersonation_stop(
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    """結束代管:以 imp 身分重簽正常 admin token(再驗仍是 admin)覆寫 cookie。"""
    if actor.impersonator_user_id is None:
        return RedirectResponse("/ui/", status_code=status.HTTP_303_SEE_OTHER)
    admin_user = db.get(User, actor.impersonator_user_id)
    if admin_user is None or not admin_user.is_admin:
        # fail-closed:admin 已失效 → 直接登出
        resp = RedirectResponse("/ui/login", status_code=status.HTTP_303_SEE_OTHER)
        resp.delete_cookie(_UI_COOKIE_NAME, path="/")
        return resp
    audit_svc.record(
        db,
        action="impersonation.stop",
        actor_user_id=actor.user.id,
        impersonator_user_id=admin_user.id,
        tenant_id=actor.user.tenant_id,
    )
    db.commit()
    token = create_access_token(user_id=admin_user.id, tenant_id=admin_user.tenant_id)
    resp = RedirectResponse(
        f"/ui/admin/tenants/{actor.user.tenant_id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )
    _set_auth_cookie(resp, token)
    return resp


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
        db,
        skip=skip,
        limit=limit,
        store_type=store_type,
        is_active=is_active,
        uncategorized=uncategorized,
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
        _ctx(
            request,
            actor,
            tenant=tenant,
            usage=usage,
            cfg=cfg,
            features=features_svc.list_for_tenant(db, tenant_id),
            action_base=f"/ui/admin/tenants/{tenant_id}/line-config",
        ),
    )


@router.post(
    "/admin/tenants/{tenant_id}/features/{feature}", response_class=HTMLResponse
)
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
            db,
            tenant_id,
            feature,
            enabled == "true",
            actor_user_id=actor.user.id,
            source="admin",
        )
        audit_svc.record_from_actor(
            db,
            actor,
            action="admin.feature.set",
            target=f"tenant:{tenant_id}",
            detail={"feature": feature, "enabled": enabled == "true"},
            request=request,
        )
        db.commit()  # set_enabled 已自行 commit;這筆補稽核
    except features_svc.UnknownFeatureError:
        pass
    return templates.TemplateResponse(
        "admin/_tenant_features.html",
        _ctx(
            request,
            actor,
            tenant_id=tenant_id,
            features=features_svc.list_for_tenant(db, tenant_id),
        ),
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
        admin_svc.patch_tenant(
            db,
            tenant_id,
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
    audit_svc.record_from_actor(
        db,
        actor,
        action="admin.tenant.patch",
        target=f"tenant:{tenant_id}",
        detail={
            "plan": plan,
            "is_active": is_active == "true",
            "store_type": store_type,
        },
        request=request,
    )
    db.commit()  # patch_tenant 已自行 commit;這筆補稽核
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
            db,
            tenant_id,
            channel_secret=channel_secret,
            access_token=access_token,
            default_target_lang=default_target_lang,
            bot_info_client=bot_info_client,
        )
    except HTTPException as exc:
        return templates.TemplateResponse(
            "_line_config_status.html",
            _ctx(
                request,
                actor,
                cfg=None,
                webhook_url=_line_webhook_url_for(tenant_id),
                action_base=f"/ui/admin/tenants/{tenant_id}/line-config",
                error=str(exc.detail),
            ),
            status_code=exc.status_code,
        )
    audit_svc.record_from_actor(
        db,
        actor,
        action="line_config.upsert",
        target=f"tenant:{tenant_id}",
        detail={"by": "admin"},
        request=request,
    )
    db.commit()
    return templates.TemplateResponse(
        "_line_config_status.html",
        _ctx(
            request,
            actor,
            cfg=cfg,
            webhook_url=_line_webhook_url_for(tenant_id),
            action_base=f"/ui/admin/tenants/{tenant_id}/line-config",
        ),
    )


@router.post(
    "/admin/tenants/{tenant_id}/line-config/verify", response_class=HTMLResponse
)
def admin_line_config_verify(
    request: Request,
    tenant_id: int,
    actor: Actor = Depends(require_ui_admin),
    db: Session = Depends(get_db),
    bot_info_client: LineBotInfoClient = Depends(get_bot_info_client),
):
    try:
        cfg = line_config_svc.verify_line_config(
            db, tenant_id, bot_info_client=bot_info_client
        )
    except HTTPException as exc:
        return templates.TemplateResponse(
            "_line_config_status.html",
            _ctx(
                request,
                actor,
                cfg=None,
                webhook_url=_line_webhook_url_for(tenant_id),
                action_base=f"/ui/admin/tenants/{tenant_id}/line-config",
                error=str(exc.detail),
            ),
            status_code=exc.status_code,
        )
    return templates.TemplateResponse(
        "_line_config_status.html",
        _ctx(
            request,
            actor,
            cfg=cfg,
            webhook_url=_line_webhook_url_for(tenant_id),
            action_base=f"/ui/admin/tenants/{tenant_id}/line-config",
        ),
    )


@router.post(
    "/admin/tenants/{tenant_id}/line-config/delete", response_class=HTMLResponse
)
def admin_line_config_delete(
    request: Request,
    tenant_id: int,
    actor: Actor = Depends(require_ui_admin),
    db: Session = Depends(get_db),
):
    try:
        line_config_svc.delete_line_config(db, tenant_id)
        audit_svc.record_from_actor(
            db,
            actor,
            action="line_config.delete",
            target=f"tenant:{tenant_id}",
            detail={"by": "admin"},
            request=request,
        )
        db.commit()
    except HTTPException:
        pass
    return templates.TemplateResponse(
        "_line_config_status.html",
        _ctx(
            request,
            actor,
            cfg=None,
            webhook_url=_line_webhook_url_for(tenant_id),
            action_base=f"/ui/admin/tenants/{tenant_id}/line-config",
        ),
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
    booking_slots = slots_svc.list_slots(db, tenant_id=tid)
    reservations = booking_svc.list_reservations(db, tenant_id=tid)
    waitlist_rows = waitlist_svc.list_waitlist(db, tenant_id=tid)
    appointment_series, series_occurrences = appointment_series_svc.list_series(
        db, tenant_id=tid
    )
    occurrence_by_reservation = {
        item.reservation_id: item
        for items in series_occurrences.values()
        for item in items
        if item.reservation_id is not None
    }
    from saas_mvp.models.service_package import PackageCreditLedger

    package_reservation_ids = {
        reservation_id
        for (reservation_id,) in tenant_query(db, PackageCreditLedger, tid)
        .filter(
            PackageCreditLedger.kind == "redeem",
            PackageCreditLedger.reservation_id.is_not(None),
        )
        .with_entities(PackageCreditLedger.reservation_id)
        .all()
    }
    reminder_hours = (
        tenant_row.reminder_hours_before if tenant_row else None
    ) or settings.reminder_hours_before_default
    return _ctx(
        request,
        actor,
        cfg=cfg,
        tenant=tenant_row,
        bot_mode=(cfg or {}).get("bot_mode", "translation"),
        has_line_config=cfg is not None,
        slots=booking_slots,
        reservations=reservations,
        appointment_series=appointment_series,
        series_occurrences=series_occurrences,
        occurrence_by_reservation=occurrence_by_reservation,
        resource_allocations=resources_svc.allocations_for_reservations(
            db,
            tenant_id=tid,
            reservation_ids=[row.id for row in reservations],
        ),
        waitlist_entries=waitlist_rows,
        waitlist_offer_minutes=(
            (tenant_row.waitlist_offer_minutes if tenant_row else None)
            or settings.waitlist_offer_minutes_default
        ),
        slot_by_id={slot.id: slot for slot in booking_slots},
        customers=customers,
        reminder_hours=reminder_hours,
        can_manage_deposits=(
            (getattr(actor.user, "role", None) or "owner") == "owner"
            or actor.user.is_admin
        ),
        can_manage_waitlist_settings=(
            (getattr(actor.user, "role", None) or "owner") == "owner"
            or actor.user.is_admin
        ),
        # 預約列以 customer_id 對應顧客檔，顯示可核對的 LINE 名稱/電話（免額外查詢）。
        customer_by_id={c.id: c for c in customers},
        customer_by_line={c.line_user_id: c for c in customers if c.line_user_id},
        package_reservation_ids=package_reservation_ids,
        **extra,
    )


@router.get("/booking", response_class=HTMLResponse)
def booking_page(
    request: Request,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    return templates.TemplateResponse("booking.html", _booking_ctx(request, actor, db))


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


@router.post("/booking/deposit-settings", response_class=HTMLResponse)
def booking_set_deposit(
    request: Request,
    deposit_twd: str = Form(""),
    deposit_hold_minutes: str = Form(""),
    actor: Actor = Depends(require_ui_owner),
    db: Session = Depends(get_db),
):
    """定金設定（C4,owner 限定）:金額(0=停用)與保留分鐘數。"""
    tenant = db.get(Tenant, actor.user.tenant_id)
    error = None
    try:
        amount = int(deposit_twd) if deposit_twd.strip() else 0
        hold = int(deposit_hold_minutes) if deposit_hold_minutes.strip() else None
        if amount < 0 or (hold is not None and hold < 5):
            raise ValueError
        tenant.deposit_cents = amount * 100 if amount else None
        tenant.deposit_hold_minutes = hold
        audit_svc.record_from_actor(
            db,
            actor,
            action="booking.deposit.settings",
            target=f"tenant:{tenant.id}",
            detail={"deposit_twd": amount, "hold_minutes": hold},
            request=request,
        )
        db.commit()
    except ValueError:
        db.rollback()
        error = "金額需為非負整數;保留分鐘數至少 5 分鐘"
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


@router.post("/booking/waitlist-settings", response_class=HTMLResponse)
def booking_set_waitlist_settings(
    request: Request,
    waitlist_offer_minutes: int = Form(...),
    actor: Actor = Depends(require_ui_owner),
    db: Session = Depends(get_db),
):
    """候補回應窗口設定；owner 限定。"""
    error = None
    saved = False
    if not 5 <= waitlist_offer_minutes <= 120:
        error = "候補回應時間需介於 5～120 分鐘。"
    else:
        tenant = db.get(Tenant, actor.user.tenant_id)
        tenant.waitlist_offer_minutes = waitlist_offer_minutes
        audit_svc.record_from_actor(
            db,
            actor,
            action="booking.waitlist.settings",
            target=f"tenant:{tenant.id}",
            detail={"offer_minutes": waitlist_offer_minutes},
            request=request,
        )
        db.commit()
        saved = True
    return templates.TemplateResponse(
        "_booking_waitlist.html",
        _booking_ctx(
            request,
            actor,
            db,
            waitlist_error=error,
            waitlist_saved=saved,
        ),
    )


@router.post("/booking/waitlist/{entry_id}/cancel", response_class=HTMLResponse)
def booking_cancel_waitlist_entry(
    request: Request,
    entry_id: int,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    error = None
    try:
        waitlist_svc.cancel_waitlist_by_staff(
            db, tenant_id=actor.user.tenant_id, entry_id=entry_id
        )
        audit_svc.record_from_actor(
            db,
            actor,
            action="booking.waitlist.cancel",
            target=f"waitlist:{entry_id}",
            request=request,
        )
        db.commit()
    except waitlist_svc.WaitlistEntryNotFound:
        db.rollback()
        error = "候補紀錄不存在。"
    return templates.TemplateResponse(
        "_booking_waitlist.html",
        _booking_ctx(request, actor, db, waitlist_error=error),
    )


@router.post("/booking/slots", response_class=HTMLResponse)
def booking_create_slot(
    request: Request,
    slot_start: str = Form(...),
    max_capacity: int = Form(...),
    walkin_reserved: int = Form(0),
    duration_minutes: str = Form(""),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    tid = actor.user.tenant_id
    error = None
    try:
        start = _parse_slot_start(slot_start)
        # 選填時長（分）→ slot_end；供 LINE 引導流程依服務時長過濾時段。
        duration = _opt_int(duration_minutes)
        slot_end = None
        if duration is not None:
            if duration <= 0:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="時長需為正整數（分鐘）",
                )
            slot_end = start + datetime.timedelta(minutes=duration)
        slots_svc.create_slot(
            db,
            tenant_id=tid,
            slot_start=start,
            slot_end=slot_end,
            max_capacity=max_capacity,
            walkin_reserved=walkin_reserved,
        )
    except HTTPException as exc:
        error = str(exc.detail)
    except ValueError:
        error = "時段時間或時長格式錯誤"
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


@router.get("/booking/slots", response_class=HTMLResponse)
def booking_slots_partial(
    request: Request,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    """時段列表 partial（編輯列「取消」的 hx-get 目標）。"""
    return templates.TemplateResponse(
        "_booking_slots.html", _booking_ctx(request, actor, db)
    )


@router.get("/booking/slots/{slot_id}/edit", response_class=HTMLResponse)
def booking_edit_slot_form(
    request: Request,
    slot_id: int,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    return templates.TemplateResponse(
        "_booking_slots.html",
        _booking_ctx(request, actor, db, editing_slot_id=slot_id),
    )


@router.post("/booking/slots/{slot_id}/update", response_class=HTMLResponse)
def booking_update_slot(
    request: Request,
    slot_id: int,
    max_capacity: int = Form(...),
    walkin_reserved: int = Form(0),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    tid = actor.user.tenant_id
    error = None
    editing_slot_id = None
    try:
        slots_svc.update_slot(
            db,
            tenant_id=tid,
            slot_id=slot_id,
            max_capacity=max_capacity,
            walkin_reserved=walkin_reserved,
        )
    except HTTPException as exc:
        error = str(exc.detail)
        editing_slot_id = slot_id  # 失敗時停在編輯列，讓使用者修正
    return templates.TemplateResponse(
        "_booking_slots.html",
        _booking_ctx(request, actor, db, error=error, editing_slot_id=editing_slot_id),
    )


@router.post("/booking/slots/{slot_id}/delete", response_class=HTMLResponse)
def booking_delete_slot(
    request: Request,
    slot_id: int,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    tid = actor.user.tenant_id
    error = None
    try:
        slots_svc.delete_slot(db, tenant_id=tid, slot_id=slot_id)
    except HTTPException as exc:
        error = str(exc.detail)
    return templates.TemplateResponse(
        "_booking_slots.html", _booking_ctx(request, actor, db, error=error)
    )


@router.post("/booking/slots/{slot_id}/deactivate", response_class=HTMLResponse)
def booking_deactivate_slot(
    request: Request,
    slot_id: int,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    tid = actor.user.tenant_id
    error = None
    try:
        slots_svc.deactivate_slot(db, tenant_id=tid, slot_id=slot_id)
    except HTTPException as exc:
        error = str(exc.detail)
    return templates.TemplateResponse(
        "_booking_slots.html", _booking_ctx(request, actor, db, error=error)
    )


@router.post(
    "/booking/reservations/{reservation_id}/cancel", response_class=HTMLResponse
)
def booking_cancel_reservation(
    request: Request,
    reservation_id: int,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    tid = actor.user.tenant_id
    error = None
    try:
        booking_svc.cancel_reservation(db, tenant_id=tid, reservation_id=reservation_id)
    except booking_svc.ReservationNotFoundError:
        error = "預約不存在或已取消"
    return templates.TemplateResponse(
        "_booking_reservations.html",
        _booking_ctx(request, actor, db, error=error, refresh_series=True),
    )


@router.post(
    "/booking/reservations/{reservation_id}/series", response_class=HTMLResponse
)
def booking_create_appointment_series(
    request: Request,
    reservation_id: int,
    recurrence_unit: str = Form(...),
    recurrence_interval: int = Form(1),
    occurrence_count: int = Form(...),
    auto_create_slots: bool = Form(False),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    error = None
    series_success = None
    try:
        result = appointment_series_svc.create_from_reservation(
            db,
            tenant_id=actor.user.tenant_id,
            reservation_id=reservation_id,
            recurrence_unit=recurrence_unit,
            recurrence_interval=recurrence_interval,
            occurrence_count=occurrence_count,
            auto_create_slots=auto_create_slots,
            actor_user_id=actor.user.id,
        )
        series_success = (
            f"系列 #{result['series'].id} 已建立：{result['booked']} 筆成功"
            + (f"，{result['conflicts']} 筆衝突待處理。" if result["conflicts"] else "。")
        )
        audit_svc.record_from_actor(
            db,
            actor,
            action="booking.series.create",
            target=f"series:{result['series'].id}",
            detail={
                "source_reservation_id": reservation_id,
                "booked": result["booked"],
                "conflicts": result["conflicts"],
            },
            request=request,
        )
        db.commit()
    except appointment_series_svc.AppointmentSeriesError as exc:
        db.rollback()
        error = str(exc)
    return templates.TemplateResponse(
        "_booking_series.html",
        _booking_ctx(
            request,
            actor,
            db,
            series_error=error,
            series_success=series_success,
            refresh_reservations=True,
        ),
    )


@router.post("/booking/series/{series_id}/cancel", response_class=HTMLResponse)
def booking_cancel_appointment_series(
    request: Request,
    series_id: int,
    sequence_from: int = Form(...),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    error = None
    series_success = None
    try:
        count = appointment_series_svc.cancel_from_sequence(
            db,
            tenant_id=actor.user.tenant_id,
            series_id=series_id,
            sequence_from=sequence_from,
        )
        series_success = f"已取消系列 #{series_id} 自第 {sequence_from} 次起的 {count} 筆有效預約。"
        audit_svc.record_from_actor(
            db,
            actor,
            action="booking.series.cancel_following",
            target=f"series:{series_id}",
            detail={"sequence_from": sequence_from, "cancelled": count},
            request=request,
        )
        db.commit()
    except appointment_series_svc.AppointmentSeriesError as exc:
        db.rollback()
        error = str(exc)
    return templates.TemplateResponse(
        "_booking_series.html",
        _booking_ctx(
            request,
            actor,
            db,
            series_error=error,
            series_success=series_success,
            refresh_reservations=True,
        ),
    )


@router.post(
    "/booking/series/{series_id}/occurrences/{occurrence_id}/retry",
    response_class=HTMLResponse,
)
def booking_retry_series_occurrence(
    request: Request,
    series_id: int,
    occurrence_id: int,
    auto_create_slot: bool = Form(False),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    error = None
    series_success = None
    try:
        result = appointment_series_svc.retry_conflict(
            db,
            tenant_id=actor.user.tenant_id,
            series_id=series_id,
            occurrence_id=occurrence_id,
            auto_create_slot=auto_create_slot,
        )
        if result["booked"]:
            series_success = f"衝突日期已成功建立為預約 #{result['reservation_id']}。"
        else:
            error = f"仍無法建立：{result['reason']}"
        audit_svc.record_from_actor(
            db,
            actor,
            action="booking.series.retry",
            target=f"series_occurrence:{occurrence_id}",
            detail={"result": "booked" if result["booked"] else "conflict"},
            request=request,
        )
        db.commit()
    except appointment_series_svc.AppointmentSeriesError as exc:
        db.rollback()
        error = str(exc)
    return templates.TemplateResponse(
        "_booking_series.html",
        _booking_ctx(
            request,
            actor,
            db,
            series_error=error,
            series_success=series_success,
            refresh_reservations=True,
        ),
    )


@router.post(
    "/booking/reservations/{reservation_id}/deposit-refund",
    response_class=HTMLResponse,
)
def booking_refund_deposit(
    request: Request,
    reservation_id: int,
    amount_twd: int | None = Form(None, ge=1),
    actor: Actor = Depends(require_ui_owner),
    db: Session = Depends(get_db),
):
    """取消後退還已付定金(可部分,預設全額);owner 限定、服務層鎖列防重。"""
    error = None
    refund_success = None
    try:
        row = deposit_svc.request_full_refund(
            db,
            tenant_id=actor.user.tenant_id,
            reservation_id=reservation_id,
            actor_user_id=actor.user.id,
            amount_cents=amount_twd * 100 if amount_twd is not None else None,
        )
        refunded_twd = (row.deposit_refunded_cents or row.deposit_cents or 0) // 100
        audit_svc.record_from_actor(
            db,
            actor,
            action="booking.deposit.refund",
            target=f"reservation:{reservation_id}",
            detail={
                "result": "refunded",
                "amount_twd": refunded_twd,
                "deposit_twd": (row.deposit_cents or 0) // 100,
                "provider": row.deposit_provider,
            },
            request=request,
        )
        db.commit()
        refund_success = f"預約 #{reservation_id} 定金已退款 NT${refunded_twd}。"
    except deposit_svc.DepositRefundError as exc:
        db.rollback()
        error = str(exc)
        audit_svc.record_from_actor(
            db,
            actor,
            action="booking.deposit.refund",
            target=f"reservation:{reservation_id}",
            detail={"result": "failed", "reason": error},
            request=request,
        )
        db.commit()
    return templates.TemplateResponse(
        "_booking_reservations.html",
        _booking_ctx(request, actor, db, error=error, refund_success=refund_success),
    )


@router.post(
    "/booking/reservations/{reservation_id}/deposit-refund/manual",
    response_class=HTMLResponse,
)
def booking_confirm_manual_deposit_refund(
    request: Request,
    reservation_id: int,
    note: str = Form(..., min_length=2, max_length=200),
    amount_twd: int | None = Form(None, ge=1),
    actor: Actor = Depends(require_ui_owner),
    db: Session = Depends(get_db),
):
    """外部金流後台已退款後人工對帳(可部分,預設全額);不呼叫金流、不會重複退刷。"""
    error = None
    refund_success = None
    try:
        row = deposit_svc.confirm_manual_refund(
            db,
            tenant_id=actor.user.tenant_id,
            reservation_id=reservation_id,
            actor_user_id=actor.user.id,
            note=note,
            amount_cents=amount_twd * 100 if amount_twd is not None else None,
        )
        refunded_twd = (row.deposit_refunded_cents or row.deposit_cents or 0) // 100
        audit_svc.record_from_actor(
            db,
            actor,
            action="booking.deposit.refund_manual",
            target=f"reservation:{reservation_id}",
            detail={
                "result": "confirmed",
                "amount_twd": refunded_twd,
                "deposit_twd": (row.deposit_cents or 0) // 100,
                "note": note,
            },
            request=request,
        )
        db.commit()
        refund_success = f"預約 #{reservation_id} 已標記為人工退款完成(NT${refunded_twd})。"
    except deposit_svc.DepositRefundError as exc:
        db.rollback()
        error = str(exc)
    return templates.TemplateResponse(
        "_booking_reservations.html",
        _booking_ctx(request, actor, db, error=error, refund_success=refund_success),
    )


@router.post(
    "/booking/reservations/{reservation_id}/attendance", response_class=HTMLResponse
)
def booking_mark_attendance(
    request: Request,
    reservation_id: int,
    attended: str = Form(...),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    tid = actor.user.tenant_id
    error = None
    try:
        booking_svc.mark_attendance(
            db,
            tenant_id=tid,
            reservation_id=reservation_id,
            attended=(attended == "true"),
        )
    except booking_svc.ReservationNotFoundError:
        error = "預約不存在或已取消"
    return templates.TemplateResponse(
        "_booking_reservations.html", _booking_ctx(request, actor, db, error=error)
    )


# ── 店家自助：顧客管理（CRM + 標籤） ─────────────────────────────────────────


def _customers_admin_ctx(request: Request, actor: Actor, db: Session, **extra) -> dict:
    from saas_mvp.models.customer_tag_link import CustomerTagLink

    tid = actor.user.tenant_id
    tags = segments_svc.list_tags(db, tenant_id=tid)
    tag_by_id = {t.id: t for t in tags}
    tags_by_customer: dict[int, list] = {}
    for link in tenant_query(db, CustomerTagLink, tid).all():
        tag = tag_by_id.get(link.tag_id)
        if tag is not None:
            tags_by_customer.setdefault(link.customer_id, []).append(tag)
    return _ctx(
        request,
        actor,
        customers=customers_svc.list_customers(db, tenant_id=tid),
        tags=tags,
        tags_by_customer=tags_by_customer,
        **extra,
    )


# 註：本區段（顧客管理/標籤 CRUD/inline 編輯/刪除）與後方「顧客 CRM」區段
# （列表/搜尋/分頁/detail/匯入匯出/點數）在 upstream 合併時整併：
# GET /customers 主頁由 CRM 區段提供；本區段保留 /customers/list（標籤管理
# 檢視）與 tag 編輯/刪除、顧客 inline 編輯/刪除。建立標籤統一由 CRM 區段的
# POST /customers/tags 處理（支援帶/不帶 customer_id 兩種來源表單）。


@router.get("/customers/list", response_class=HTMLResponse)
def customers_list_partial(
    request: Request,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    """顧客管理 partial（標籤 CRUD + inline 編輯檢視；編輯列「取消」的目標）。"""
    return templates.TemplateResponse(
        "_customers.html", _customers_admin_ctx(request, actor, db)
    )


@router.get("/customers/tags/{tag_id}/edit", response_class=HTMLResponse)
def customers_edit_tag_form(
    request: Request,
    tag_id: int,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    return templates.TemplateResponse(
        "_customers.html",
        _customers_admin_ctx(request, actor, db, editing_tag_id=tag_id),
    )


@router.post("/customers/tags/{tag_id}/update", response_class=HTMLResponse)
def customers_update_tag(
    request: Request,
    tag_id: int,
    name: str = Form(...),
    color: str = Form(""),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    tid = actor.user.tenant_id
    error = None
    editing_tag_id = None
    try:
        segments_svc.update_tag(
            db, tenant_id=tid, tag_id=tag_id, name=name, color=color or None
        )
    except HTTPException as exc:
        error = str(exc.detail)
        editing_tag_id = tag_id
    return templates.TemplateResponse(
        "_customers.html",
        _customers_admin_ctx(
            request, actor, db, error=error, editing_tag_id=editing_tag_id
        ),
    )


@router.post("/customers/tags/{tag_id}/delete", response_class=HTMLResponse)
def customers_delete_tag(
    request: Request,
    tag_id: int,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    tid = actor.user.tenant_id
    error = None
    try:
        segments_svc.delete_tag(db, tenant_id=tid, tag_id=tag_id)
    except HTTPException as exc:
        error = str(exc.detail)
    return templates.TemplateResponse(
        "_customers.html", _customers_admin_ctx(request, actor, db, error=error)
    )


@router.get("/customers/{customer_id}/edit", response_class=HTMLResponse)
def customers_edit_form(
    request: Request,
    customer_id: int,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    return templates.TemplateResponse(
        "_customers.html",
        _customers_admin_ctx(request, actor, db, editing_customer_id=customer_id),
    )


@router.post("/customers/{customer_id}/update", response_class=HTMLResponse)
def customers_update(
    request: Request,
    customer_id: int,
    phone: str = Form(""),
    note: str = Form(""),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    tid = actor.user.tenant_id
    error = None
    editing_customer_id = None
    try:
        customers_svc.update_customer(
            db,
            tenant_id=tid,
            customer_id=customer_id,
            phone=phone,
            note=note,
        )
    except HTTPException as exc:
        error = str(exc.detail)
        editing_customer_id = customer_id
    return templates.TemplateResponse(
        "_customers.html",
        _customers_admin_ctx(
            request, actor, db, error=error, editing_customer_id=editing_customer_id
        ),
    )


@router.post("/customers/{customer_id}/delete", response_class=HTMLResponse)
def customers_delete(
    request: Request,
    customer_id: int,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    tid = actor.user.tenant_id
    error = None
    try:
        customers_svc.delete_customer(db, tenant_id=tid, customer_id=customer_id)
    except HTTPException as exc:
        error = str(exc.detail)
    return templates.TemplateResponse(
        "_customers.html", _customers_admin_ctx(request, actor, db, error=error)
    )


@router.post("/customers/{customer_id}/tags/attach", response_class=HTMLResponse)
def customers_attach_tag(
    request: Request,
    customer_id: int,
    tag_id: str = Form(""),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    tid = actor.user.tenant_id
    error = None
    try:
        if not tag_id.strip():
            error = "請先選擇標籤"
        else:
            segments_svc.attach_tag(
                db, tenant_id=tid, customer_id=customer_id, tag_id=int(tag_id)
            )
    except HTTPException as exc:
        error = str(exc.detail)
    except ValueError:
        error = "標籤格式錯誤"
    return templates.TemplateResponse(
        "_customers.html", _customers_admin_ctx(request, actor, db, error=error)
    )


@router.post(
    "/customers/{customer_id}/tags/{tag_id}/detach", response_class=HTMLResponse
)
def customers_detach_tag(
    request: Request,
    customer_id: int,
    tag_id: int,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    tid = actor.user.tenant_id
    # detach 冪等（未掛載為 no-op），不需錯誤處理
    segments_svc.detach_tag(db, tenant_id=tid, customer_id=customer_id, tag_id=tag_id)
    return templates.TemplateResponse(
        "_customers.html", _customers_admin_ctx(request, actor, db)
    )


# ── 店家自助：備註 ────────────────────────────────────────────────────────────


def _notes_ctx(request: Request, actor: Actor, db: Session, **extra) -> dict:
    tid = actor.user.tenant_id
    return _ctx(
        request,
        actor,
        notes=notes_svc.list_notes(db, tenant_id=tid),
        **extra,
    )


@router.get("/notes", response_class=HTMLResponse)
def notes_page(
    request: Request,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    return templates.TemplateResponse("notes.html", _notes_ctx(request, actor, db))


@router.get("/notes/list", response_class=HTMLResponse)
def notes_list_partial(
    request: Request,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    return templates.TemplateResponse("_notes.html", _notes_ctx(request, actor, db))


@router.post("/notes", response_class=HTMLResponse)
def notes_create(
    request: Request,
    title: str = Form(...),
    content: str = Form(""),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    tid = actor.user.tenant_id
    error = None
    try:
        notes_svc.create_note(
            db,
            tenant_id=tid,
            owner_id=actor.user.id,
            title=title,
            content=content,
        )
    except HTTPException as exc:
        error = str(exc.detail)
    return templates.TemplateResponse(
        "_notes.html", _notes_ctx(request, actor, db, error=error)
    )


@router.get("/notes/{note_id}/edit", response_class=HTMLResponse)
def notes_edit_form(
    request: Request,
    note_id: int,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    return templates.TemplateResponse(
        "_notes.html", _notes_ctx(request, actor, db, editing_id=note_id)
    )


@router.post("/notes/{note_id}/update", response_class=HTMLResponse)
def notes_update(
    request: Request,
    note_id: int,
    title: str = Form(...),
    content: str = Form(""),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    tid = actor.user.tenant_id
    error = None
    editing_id = None
    try:
        notes_svc.update_note(
            db,
            tenant_id=tid,
            note_id=note_id,
            title=title,
            content=content,
        )
    except HTTPException as exc:
        error = str(exc.detail)
        editing_id = note_id
    return templates.TemplateResponse(
        "_notes.html",
        _notes_ctx(request, actor, db, error=error, editing_id=editing_id),
    )


@router.post("/notes/{note_id}/delete", response_class=HTMLResponse)
def notes_delete(
    request: Request,
    note_id: int,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    tid = actor.user.tenant_id
    error = None
    try:
        notes_svc.delete_note(db, tenant_id=tid, note_id=note_id)
    except HTTPException as exc:
        error = str(exc.detail)
    return templates.TemplateResponse(
        "_notes.html", _notes_ctx(request, actor, db, error=error)
    )


# ── 店家自助：API 金鑰 ────────────────────────────────────────────────────────


def _api_keys_ctx(request: Request, actor: Actor, db: Session, **extra) -> dict:
    tid = actor.user.tenant_id
    return _ctx(
        request,
        actor,
        api_keys=api_keys_svc.list_keys(db, tenant_id=tid),
        **extra,
    )


@router.get("/api-keys", response_class=HTMLResponse)
def api_keys_page(
    request: Request,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    return templates.TemplateResponse(
        "api_keys.html", _api_keys_ctx(request, actor, db)
    )


@router.post("/api-keys", response_class=HTMLResponse)
def api_keys_create(
    request: Request,
    name: str = Form(...),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    tid = actor.user.tenant_id
    error = None
    created_plain_key = None
    created_name = None
    if not name.strip():
        error = "名稱不可為空"
    elif len(name) > 128:
        error = "名稱長度上限 128"
    else:
        _, created_plain_key = api_keys_svc.create_key(
            db, tenant_id=tid, user_id=actor.user.id, name=name.strip()
        )
        created_name = name.strip()
    # 明文 key 只出現在本次回應（created_plain_key），之後永遠無法再取得。
    return templates.TemplateResponse(
        "_api_keys.html",
        _api_keys_ctx(
            request,
            actor,
            db,
            error=error,
            created_plain_key=created_plain_key,
            created_name=created_name,
        ),
    )


@router.post("/api-keys/{key_id}/revoke", response_class=HTMLResponse)
def api_keys_revoke(
    request: Request,
    key_id: int,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    tid = actor.user.tenant_id
    error = None
    try:
        api_keys_svc.revoke_key(db, tenant_id=tid, key_id=key_id)
    except HTTPException as exc:
        error = str(exc.detail)
    return templates.TemplateResponse(
        "_api_keys.html", _api_keys_ctx(request, actor, db, error=error)
    )


# ── 店家自助：自動回覆規則 ────────────────────────────────────────────────────


def _auto_reply_ctx(request: Request, actor: Actor, db: Session, **extra) -> dict:
    tid = actor.user.tenant_id
    cfg = _line_config_or_none(db, tid)
    return _ctx(
        request,
        actor,
        rules=auto_reply_svc.list_rules(db, tenant_id=tid),
        flex_menus=flex_menu_svc.list_menus(db, tenant_id=tid),
        bot_mode=(cfg or {}).get("bot_mode", "translation"),
        **extra,
    )


def _auto_reply_form_kwargs(
    keyword: str,
    match_type: str,
    reply_type: str,
    reply_text: str,
    flex_menu_id: str,
    priority: str,
) -> dict:
    """表單值 → service 參數（空字串正規化為 None；驗證交給 service）。"""
    return {
        "keyword": keyword,
        "match_type": match_type,
        "reply_type": reply_type,
        "reply_text": reply_text.strip() or None,
        "flex_menu_id": int(flex_menu_id) if flex_menu_id.strip() else None,
        "priority": int(priority) if priority.strip() else 0,
    }


@router.get("/auto-reply", response_class=HTMLResponse)
def auto_reply_page(
    request: Request,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    return templates.TemplateResponse(
        "auto_reply.html", _auto_reply_ctx(request, actor, db)
    )


@router.get("/auto-reply/list", response_class=HTMLResponse)
def auto_reply_list_partial(
    request: Request,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    return templates.TemplateResponse(
        "_auto_reply.html", _auto_reply_ctx(request, actor, db)
    )


@router.post("/auto-reply", response_class=HTMLResponse)
def auto_reply_create(
    request: Request,
    keyword: str = Form(...),
    match_type: str = Form("contains"),
    reply_type: str = Form("text"),
    reply_text: str = Form(""),
    flex_menu_id: str = Form(""),
    priority: str = Form("0"),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    tid = actor.user.tenant_id
    error = None
    try:
        auto_reply_svc.create_rule(
            db,
            tenant_id=tid,
            **_auto_reply_form_kwargs(
                keyword, match_type, reply_type, reply_text, flex_menu_id, priority
            ),
        )
    except HTTPException as exc:
        error = str(exc.detail)
    except ValueError:
        error = "數字欄位格式錯誤"
    return templates.TemplateResponse(
        "_auto_reply.html", _auto_reply_ctx(request, actor, db, error=error)
    )


@router.get("/auto-reply/{rule_id}/edit", response_class=HTMLResponse)
def auto_reply_edit_form(
    request: Request,
    rule_id: int,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    return templates.TemplateResponse(
        "_auto_reply.html",
        _auto_reply_ctx(request, actor, db, editing_rule_id=rule_id),
    )


@router.post("/auto-reply/{rule_id}/update", response_class=HTMLResponse)
def auto_reply_update(
    request: Request,
    rule_id: int,
    keyword: str = Form(...),
    match_type: str = Form("contains"),
    reply_type: str = Form("text"),
    reply_text: str = Form(""),
    flex_menu_id: str = Form(""),
    priority: str = Form("0"),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    tid = actor.user.tenant_id
    error = None
    editing_rule_id = None
    try:
        auto_reply_svc.update_rule(
            db,
            tenant_id=tid,
            rule_id=rule_id,
            **_auto_reply_form_kwargs(
                keyword, match_type, reply_type, reply_text, flex_menu_id, priority
            ),
        )
    except HTTPException as exc:
        error = str(exc.detail)
        editing_rule_id = rule_id
    except ValueError:
        error = "數字欄位格式錯誤"
        editing_rule_id = rule_id
    return templates.TemplateResponse(
        "_auto_reply.html",
        _auto_reply_ctx(
            request, actor, db, error=error, editing_rule_id=editing_rule_id
        ),
    )


@router.post("/auto-reply/{rule_id}/toggle", response_class=HTMLResponse)
def auto_reply_toggle(
    request: Request,
    rule_id: int,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    tid = actor.user.tenant_id
    error = None
    try:
        rule = auto_reply_svc.get_rule(db, tenant_id=tid, rule_id=rule_id)
        auto_reply_svc.update_rule(
            db, tenant_id=tid, rule_id=rule_id, is_active=not rule.is_active
        )
    except HTTPException as exc:
        error = str(exc.detail)
    return templates.TemplateResponse(
        "_auto_reply.html", _auto_reply_ctx(request, actor, db, error=error)
    )


@router.post("/auto-reply/{rule_id}/delete", response_class=HTMLResponse)
def auto_reply_delete(
    request: Request,
    rule_id: int,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    tid = actor.user.tenant_id
    error = None
    try:
        auto_reply_svc.delete_rule(db, tenant_id=tid, rule_id=rule_id)
    except HTTPException as exc:
        error = str(exc.detail)
    return templates.TemplateResponse(
        "_auto_reply.html", _auto_reply_ctx(request, actor, db, error=error)
    )


@router.post("/booking/customers/{customer_id}/blacklist", response_class=HTMLResponse)
def booking_set_blacklist(
    request: Request,
    customer_id: int,
    blacklisted: str = Form(...),
    reason: str = Form(default=""),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    """加入/解除顧客黑名單（硬性阻擋線上預約），重新渲染顧客卡片。"""
    tid = actor.user.tenant_id
    try:
        customers_svc.set_blacklist(
            db,
            tenant_id=tid,
            customer_id=customer_id,
            blacklisted=(blacklisted == "true"),
            reason=(reason.strip() or None),
        )
    except HTTPException:
        pass  # 查無顧客（跨租戶/已刪）時靜默，照常回渲染目前清單
    return templates.TemplateResponse(
        "_booking_customers.html", _booking_ctx(request, actor, db)
    )


# ── 店家自助：商品銷售 ────────────────────────────────────────────────────────


def _shop_ctx(request: Request, actor: Actor, db: Session, **extra) -> dict:
    tid = actor.user.tenant_id
    return _ctx(
        request,
        actor,
        products=shop_svc.list_products(db, tenant_id=tid),
        orders=shop_svc.list_orders(db, tenant_id=tid),
        shop_payment_provider=platform_payment_svc.payment_provider(db, settings),
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
    if not _require_ui_feature(db, actor, features_svc.PRODUCT_SALES):
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
    if not _require_ui_feature(db, actor, features_svc.PRODUCT_SALES):
        return _feature_locked(request, actor, features_svc.PRODUCT_SALES, "商品銷售")
    tid = actor.user.tenant_id
    error = None
    try:
        shop_svc.create_product(
            db,
            tenant_id=tid,
            name=name,
            price_cents=price_cents,
            stock=int(stock) if stock.strip() else None,
        )
    except HTTPException as exc:
        error = str(exc.detail)
    except ValueError:
        error = "庫存需為整數"
    return templates.TemplateResponse(
        "_shop.html", _shop_ctx(request, actor, db, error=error)
    )


@router.post("/shop/products/{product_id}/deactivate", response_class=HTMLResponse)
def shop_deactivate_product(
    request: Request,
    product_id: int,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.PRODUCT_SALES):
        return _feature_locked(request, actor, features_svc.PRODUCT_SALES, "商品銷售")
    tid = actor.user.tenant_id
    error = None
    try:
        shop_svc.deactivate_product(db, tenant_id=tid, product_id=product_id)
    except HTTPException as exc:
        error = str(exc.detail)
    return templates.TemplateResponse(
        "_shop.html", _shop_ctx(request, actor, db, error=error)
    )


@router.get("/shop/products/{product_id}/edit", response_class=HTMLResponse)
def shop_edit_product_form(
    request: Request,
    product_id: int,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.PRODUCT_SALES):
        return _feature_locked(request, actor, features_svc.PRODUCT_SALES, "商品銷售")
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
    if not _require_ui_feature(db, actor, features_svc.PRODUCT_SALES):
        return _feature_locked(request, actor, features_svc.PRODUCT_SALES, "商品銷售")
    tid = actor.user.tenant_id
    error = None
    editing_id = None
    try:
        shop_svc.update_product(
            db,
            tenant_id=tid,
            product_id=product_id,
            name=name,
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
    if not _require_ui_feature(db, actor, features_svc.PRODUCT_SALES):
        return _feature_locked(request, actor, features_svc.PRODUCT_SALES, "商品銷售")
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
    if not _require_ui_feature(db, actor, features_svc.PRODUCT_SALES):
        return _feature_locked(request, actor, features_svc.PRODUCT_SALES, "商品銷售")
    tid = actor.user.tenant_id
    error = None
    try:
        shop_svc.mark_order_paid(db, tenant_id=tid, order_id=order_id)
    except shop_svc.OrderNotFound:
        error = "訂單不存在"
    return templates.TemplateResponse(
        "_shop.html", _shop_ctx(request, actor, db, error=error)
    )


@router.post("/shop/orders/{order_id}/cancel", response_class=HTMLResponse)
def shop_cancel_order(
    request: Request,
    order_id: int,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.PRODUCT_SALES):
        return _feature_locked(request, actor, features_svc.PRODUCT_SALES, "商品銷售")
    tid = actor.user.tenant_id
    error = None
    try:
        shop_svc.cancel_order(db, tenant_id=tid, order_id=order_id)
    except shop_svc.OrderNotFound:
        error = "訂單不存在"
    return templates.TemplateResponse(
        "_shop.html", _shop_ctx(request, actor, db, error=error)
    )


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
            request,
            actor,
            summary=analytics_svc.booking_summary(db, tenant_id=tid),
            utilization=analytics_svc.slot_utilization(db, tenant_id=tid),
            top=analytics_svc.top_customers(db, tenant_id=tid, limit=10),
            revenue=analytics_svc.revenue_summary(db, tenant_id=tid),
            trend=analytics_svc.trend_series(
                db, tenant_id=tid, period="week", periods=12
            ),
            staff_prod=reporting_svc.staff_performance(db, tenant_id=tid),
            service_prod=reporting_svc.popular_services(db, tenant_id=tid),
            retention=reporting_svc.return_rate(db, tenant_id=tid),
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


@router.get("/rich-menu/preview.png")
def rich_menu_preview(
    template: str = Query(...),
    theme: str = Query(...),
    actor: Actor = Depends(require_ui_user),
):
    """套用前預覽：即席產生選單圖(不動 DB、不打 LINE)。"""
    rich_menu_svc._validate(template, theme)
    _, image = rich_menu_svc.build_rich_menu_payload(template, theme)
    return Response(
        content=image,
        media_type="image/png",
        headers={"Cache-Control": "no-store"},
    )


# ── 店家自助：優惠券 ──────────────────────────────────────────────────────────


def _coupons_ctx(request: Request, actor: Actor, db: Session, **extra) -> dict:
    tid = actor.user.tenant_id
    return _ctx(
        request,
        actor,
        coupons=coupons_svc.list_coupons(db, tenant_id=tid),
        **extra,
    )


@router.get("/coupons", response_class=HTMLResponse)
def coupons_page(
    request: Request,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.COUPON_SYSTEM):
        return _feature_locked(
            request, actor, features_svc.COUPON_SYSTEM, "優惠券／會員"
        )
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
        return _feature_locked(
            request, actor, features_svc.COUPON_SYSTEM, "優惠券／會員"
        )
    tid = actor.user.tenant_id
    error = None
    try:
        coupons_svc.create_coupon(
            db,
            tenant_id=tid,
            code=code,
            name=name,
            discount_type=discount_type,
            discount_value=discount_value,
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
    error = None
    try:
        coupons_svc.deactivate_coupon(db, tenant_id=tid, coupon_id=coupon_id)
    except HTTPException as exc:
        error = str(exc.detail)
    return templates.TemplateResponse(
        "_coupons_list.html", _coupons_ctx(request, actor, db, error=error)
    )


@router.get("/coupons/{coupon_id}/edit", response_class=HTMLResponse)
def coupons_edit_form(
    request: Request,
    coupon_id: int,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.COUPON_SYSTEM):
        return _feature_locked(
            request, actor, features_svc.COUPON_SYSTEM, "優惠券／會員"
        )
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
        return _feature_locked(
            request, actor, features_svc.COUPON_SYSTEM, "優惠券／會員"
        )
    tid = actor.user.tenant_id
    error = None
    editing_id = None
    try:
        coupons_svc.update_coupon(
            db,
            tenant_id=tid,
            coupon_id=coupon_id,
            name=name,
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
        return _feature_locked(
            request, actor, features_svc.COUPON_SYSTEM, "優惠券／會員"
        )
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
    from saas_mvp.models.feature_subscription import FeatureSubscription
    from saas_mvp.models.subscription_charge import SubscriptionCharge

    tid = actor.user.tenant_id
    # 扣款紀錄（最新 20 筆,附 feature 名）
    charges = (
        db.query(SubscriptionCharge, FeatureSubscription.feature)
        .join(
            FeatureSubscription,
            SubscriptionCharge.subscription_id == FeatureSubscription.id,
        )
        .filter(SubscriptionCharge.tenant_id == tid)
        .order_by(SubscriptionCharge.id.desc())
        .limit(20)
        .all()
    )
    return _ctx(
        request,
        actor,
        features=features_svc.list_for_tenant(db, tid),
        charges=charges,
        feature_labels=features_svc._FEATURE_LABELS,
        **extra,
    )


@router.get("/features", response_class=HTMLResponse)
def features_page(
    request: Request,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    return templates.TemplateResponse(
        "features.html", _features_ctx(request, actor, db)
    )


@router.post("/features/{feature}/subscribe", response_class=HTMLResponse)
def features_subscribe(
    request: Request,
    feature: str,
    actor: Actor = Depends(require_ui_owner),
    db: Session = Depends(get_db),
):
    tenant = db.get(Tenant, actor.user.tenant_id)
    try:
        features_svc.validate_feature(feature)
        result = billing_svc.subscribe_feature(db, tenant, feature, actor.user.id)
        audit_svc.record_from_actor(
            db,
            actor,
            action="billing.feature.subscribe",
            target=f"tenant:{tenant.id}",
            detail={"feature": feature},
            request=request,
        )
        db.commit()
    except features_svc.UnknownFeatureError:
        result = None
    # ecpay 模式：尚未開通，導向綠界定期定額付款頁（首期授權成功後自動開通）。
    if result is not None and result.checkout_url:
        url = html.escape(result.checkout_url)
        return HTMLResponse(
            '<div class="card success">'
            f"<p>請完成綠界信用卡定期定額授權以開通「{html.escape(feature)}」。</p>"
            f'<a class="btn" href="{url}" target="_blank" rel="noopener">前往綠界付款</a>'
            '<p class="muted">完成首期授權後，功能將自動開通；可重新整理本頁查看狀態。</p>'
            "</div>"
        )
    return templates.TemplateResponse(
        "_features_list.html", _features_ctx(request, actor, db)
    )


@router.post("/features/{feature}/unsubscribe", response_class=HTMLResponse)
def features_unsubscribe(
    request: Request,
    feature: str,
    actor: Actor = Depends(require_ui_owner),
    db: Session = Depends(get_db),
):
    tenant = db.get(Tenant, actor.user.tenant_id)
    try:
        features_svc.validate_feature(feature)
        billing_svc.unsubscribe_feature(db, tenant, feature, actor.user.id)
        audit_svc.record_from_actor(
            db,
            actor,
            action="billing.feature.unsubscribe",
            target=f"tenant:{tenant.id}",
            detail={"feature": feature},
            request=request,
        )
        db.commit()
    except features_svc.UnknownFeatureError:
        pass
    return templates.TemplateResponse(
        "_features_list.html", _features_ctx(request, actor, db)
    )


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
    active_count = sum(1 for location in rows if location.is_active)
    return _ctx(
        request,
        actor,
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
    return templates.TemplateResponse(
        "locations.html", _locations_ctx(request, actor, db)
    )


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
            db,
            tenant_id=tid,
            name=name,
            address=address or None,
            phone=phone or None,
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
    error = None
    try:
        locations_svc.update_location(
            db,
            tenant_id=tid,
            location_id=location_id,
            name=name,
            address=address or None,
            phone=phone or None,
        )
    except HTTPException as exc:
        error = str(exc.detail)
    return templates.TemplateResponse(
        "_locations.html", _locations_ctx(request, actor, db, error=error)
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
    error = None
    try:
        locations_svc.update_location(
            db, tenant_id=tid, location_id=location_id, is_active=False
        )
    except HTTPException as exc:
        error = str(exc.detail)
    return templates.TemplateResponse(
        "_locations.html", _locations_ctx(request, actor, db, error=error)
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
    error = None
    try:
        locations_svc.update_location(
            db, tenant_id=tid, location_id=location_id, is_active=True
        )
    except HTTPException as exc:
        error = str(exc.detail)
    return templates.TemplateResponse(
        "_locations.html", _locations_ctx(request, actor, db, error=error)
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
    shifts = {
        s.id: staff_svc.list_shifts(db, tenant_id=tid, staff_id=s.id) for s in rows
    }
    leaves = {
        s.id: staff_svc.list_leaves(db, tenant_id=tid, staff_id=s.id) for s in rows
    }
    return _ctx(
        request,
        actor,
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
        return _feature_locked(
            request, actor, features_svc.STAFF_SCHEDULING, "員工排班"
        )
    return templates.TemplateResponse("staff.html", _staff_ctx(request, actor, db))


@router.get("/staff/list", response_class=HTMLResponse)
def staff_list_partial(
    request: Request,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    """員工列表 partial（班表/請假編輯列「取消」的 hx-get 目標）。"""
    if not _require_ui_feature(db, actor, features_svc.STAFF_SCHEDULING):
        return _feature_locked(
            request, actor, features_svc.STAFF_SCHEDULING, "員工排班"
        )
    return templates.TemplateResponse(
        "_staff_list.html", _staff_ctx(request, actor, db)
    )


@router.post("/staff", response_class=HTMLResponse)
def staff_create(
    request: Request,
    name: str = Form(...),
    role: str = Form(""),
    location_id: str = Form(""),
    booking_mode: str = Form("capacity"),
    actor: Actor = Depends(require_ui_owner),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.STAFF_SCHEDULING):
        return _feature_locked(
            request, actor, features_svc.STAFF_SCHEDULING, "員工排班"
        )
    tid = actor.user.tenant_id
    error = None
    try:
        staff_svc.create_staff(
            db,
            tenant_id=tid,
            name=name,
            role=role or None,
            location_id=_opt_int(location_id),
            booking_mode=booking_mode,
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
    actor: Actor = Depends(require_ui_owner),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.STAFF_SCHEDULING):
        return _feature_locked(
            request, actor, features_svc.STAFF_SCHEDULING, "員工排班"
        )
    tid = actor.user.tenant_id
    error = None
    try:
        staff_svc.update_staff(
            db,
            tenant_id=tid,
            staff_id=staff_id,
            name=name,
            role=role or None,
        )
    except HTTPException as exc:
        error = str(exc.detail)
    return templates.TemplateResponse(
        "_staff_list.html", _staff_ctx(request, actor, db, error=error)
    )


@router.post("/staff/{staff_id}/deactivate", response_class=HTMLResponse)
def staff_deactivate(
    request: Request,
    staff_id: int,
    actor: Actor = Depends(require_ui_owner),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.STAFF_SCHEDULING):
        return _feature_locked(
            request, actor, features_svc.STAFF_SCHEDULING, "員工排班"
        )
    tid = actor.user.tenant_id
    error = None
    try:
        staff_svc.update_staff(db, tenant_id=tid, staff_id=staff_id, is_active=False)
    except HTTPException as exc:
        error = str(exc.detail)
    return templates.TemplateResponse(
        "_staff_list.html", _staff_ctx(request, actor, db, error=error)
    )


@router.post("/staff/{staff_id}/activate", response_class=HTMLResponse)
def staff_activate(
    request: Request,
    staff_id: int,
    actor: Actor = Depends(require_ui_owner),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.STAFF_SCHEDULING):
        return _feature_locked(
            request, actor, features_svc.STAFF_SCHEDULING, "員工排班"
        )
    tid = actor.user.tenant_id
    error = None
    try:
        staff_svc.update_staff(db, tenant_id=tid, staff_id=staff_id, is_active=True)
    except HTTPException as exc:
        error = str(exc.detail)
    return templates.TemplateResponse(
        "_staff_list.html", _staff_ctx(request, actor, db, error=error)
    )


@router.post("/staff/{staff_id}/delete", response_class=HTMLResponse)
def staff_delete(
    request: Request,
    staff_id: int,
    actor: Actor = Depends(require_ui_owner),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.STAFF_SCHEDULING):
        return _feature_locked(
            request, actor, features_svc.STAFF_SCHEDULING, "員工排班"
        )
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
    actor: Actor = Depends(require_ui_owner),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.STAFF_SCHEDULING):
        return _feature_locked(
            request, actor, features_svc.STAFF_SCHEDULING, "員工排班"
        )
    tid = actor.user.tenant_id
    error = None
    try:
        staff_svc.rotate_token(db, tenant_id=tid, staff_id=staff_id)
    except HTTPException as exc:
        error = str(exc.detail)
    return templates.TemplateResponse(
        "_staff_list.html", _staff_ctx(request, actor, db, error=error)
    )


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
        return _feature_locked(
            request, actor, features_svc.STAFF_SCHEDULING, "員工排班"
        )
    tid = actor.user.tenant_id
    error = None
    try:
        staff_svc.create_shift(
            db,
            tenant_id=tid,
            staff_id=staff_id,
            start_time=start_time,
            end_time=end_time,
            weekday=_opt_int(weekday),
            rotation=rotation or None,
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
        return _feature_locked(
            request, actor, features_svc.STAFF_SCHEDULING, "員工排班"
        )
    tid = actor.user.tenant_id
    error = None
    saved = None
    try:
        wd = [int(w) for w in weekdays if w != ""]
        result = staff_svc.bulk_create_shifts_from_template(
            db,
            tenant_id=tid,
            staff_id=staff_id,
            template=template,
            weekdays=wd,
        )
        saved = f"已套用模板：新增 {result['created']} 筆、略過 {result['skipped']} 筆（已存在）。"
    except HTTPException as exc:
        error = str(exc.detail)
    except ValueError:
        error = "星期格式錯誤"
    return templates.TemplateResponse(
        "_staff_list.html", _staff_ctx(request, actor, db, error=error, bulk_msg=saved)
    )


@router.get("/staff/{staff_id}/shifts/{shift_id}/edit", response_class=HTMLResponse)
def staff_edit_shift_form(
    request: Request,
    staff_id: int,
    shift_id: int,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.STAFF_SCHEDULING):
        return _feature_locked(
            request, actor, features_svc.STAFF_SCHEDULING, "員工排班"
        )
    return templates.TemplateResponse(
        "_staff_list.html",
        _staff_ctx(request, actor, db, editing_shift_id=shift_id),
    )


@router.post("/staff/{staff_id}/shifts/{shift_id}/update", response_class=HTMLResponse)
def staff_update_shift(
    request: Request,
    staff_id: int,
    shift_id: int,
    start_time: str = Form(...),
    end_time: str = Form(...),
    weekday: str = Form(""),
    rotation: str = Form(""),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.STAFF_SCHEDULING):
        return _feature_locked(
            request, actor, features_svc.STAFF_SCHEDULING, "員工排班"
        )
    tid = actor.user.tenant_id
    error = None
    editing_shift_id = None
    try:
        # weekday 一律帶明確值（int 或 None=每日）——表單的 select 永遠有值。
        staff_svc.update_shift(
            db,
            tenant_id=tid,
            staff_id=staff_id,
            shift_id=shift_id,
            start_time=start_time,
            end_time=end_time,
            weekday=_opt_int(weekday),
            rotation=rotation or None,
        )
    except HTTPException as exc:
        error = str(exc.detail)
        editing_shift_id = shift_id
    except ValueError:
        error = "星期格式錯誤"
        editing_shift_id = shift_id
    return templates.TemplateResponse(
        "_staff_list.html",
        _staff_ctx(request, actor, db, error=error, editing_shift_id=editing_shift_id),
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
        return _feature_locked(
            request, actor, features_svc.STAFF_SCHEDULING, "員工排班"
        )
    tid = actor.user.tenant_id
    error = None
    try:
        staff_svc.delete_shift(db, tenant_id=tid, staff_id=staff_id, shift_id=shift_id)
    except HTTPException as exc:
        error = str(exc.detail)
    return templates.TemplateResponse(
        "_staff_list.html", _staff_ctx(request, actor, db, error=error)
    )


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
        return _feature_locked(
            request, actor, features_svc.STAFF_SCHEDULING, "員工排班"
        )
    tid = actor.user.tenant_id
    error = None
    try:
        staff_svc.create_leave(
            db,
            tenant_id=tid,
            staff_id=staff_id,
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


@router.get("/staff/{staff_id}/leaves/{leave_id}/edit", response_class=HTMLResponse)
def staff_edit_leave_form(
    request: Request,
    staff_id: int,
    leave_id: int,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.STAFF_SCHEDULING):
        return _feature_locked(
            request, actor, features_svc.STAFF_SCHEDULING, "員工排班"
        )
    return templates.TemplateResponse(
        "_staff_list.html",
        _staff_ctx(request, actor, db, editing_leave_id=leave_id),
    )


@router.post("/staff/{staff_id}/leaves/{leave_id}/update", response_class=HTMLResponse)
def staff_update_leave(
    request: Request,
    staff_id: int,
    leave_id: int,
    start_at: str = Form(...),
    end_at: str = Form(...),
    reason: str = Form(""),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.STAFF_SCHEDULING):
        return _feature_locked(
            request, actor, features_svc.STAFF_SCHEDULING, "員工排班"
        )
    tid = actor.user.tenant_id
    error = None
    editing_leave_id = None
    try:
        staff_svc.update_leave(
            db,
            tenant_id=tid,
            staff_id=staff_id,
            leave_id=leave_id,
            start_at=_parse_slot_start(start_at),
            end_at=_parse_slot_start(end_at),
            reason=reason or None,
        )
    except HTTPException as exc:
        error = str(exc.detail)
        editing_leave_id = leave_id
    except ValueError:
        error = "請假時間格式錯誤"
        editing_leave_id = leave_id
    return templates.TemplateResponse(
        "_staff_list.html",
        _staff_ctx(request, actor, db, error=error, editing_leave_id=editing_leave_id),
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
        return _feature_locked(
            request, actor, features_svc.STAFF_SCHEDULING, "員工排班"
        )
    tid = actor.user.tenant_id
    error = None
    try:
        staff_svc.delete_leave(db, tenant_id=tid, staff_id=staff_id, leave_id=leave_id)
    except HTTPException as exc:
        error = str(exc.detail)
    return templates.TemplateResponse(
        "_staff_list.html", _staff_ctx(request, actor, db, error=error)
    )


# ── 店家自助：服務項目（SERVICE_CATALOG） ───────────────────────────────────────


def _services_ctx(request: Request, actor: Actor, db: Session, **extra) -> dict:
    tid = actor.user.tenant_id
    services = catalog_svc.list_services(db, tenant_id=tid)
    staff_rows = staff_svc.list_staff(db, tenant_id=tid)
    staff_by_id = {s.id: s for s in staff_rows}
    svc_staff: dict[int, list] = {}
    for svc in services:
        links = catalog_svc.list_service_staff(db, tenant_id=tid, service_id=svc.id)
        svc_staff[svc.id] = [
            staff_by_id[ln.staff_id] for ln in links if ln.staff_id in staff_by_id
        ]
    return _ctx(
        request,
        actor,
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
    return templates.TemplateResponse(
        "services.html", _services_ctx(request, actor, db)
    )


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
            db,
            tenant_id=tid,
            category_id=category_id,
            name=name,
            sort_order=sort_order,
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
    error = None
    try:
        catalog_svc.delete_category(db, tenant_id=tid, category_id=category_id)
    except HTTPException as exc:
        error = str(exc.detail)
    return templates.TemplateResponse(
        "_services_list.html", _services_ctx(request, actor, db, error=error)
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
            db,
            tenant_id=tid,
            name=name,
            duration_minutes=duration_minutes,
            price_cents=price_cents,
            category_id=_opt_int(category_id),
            location_id=_opt_int(location_id),
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
            db,
            tenant_id=tid,
            service_id=service_id,
            name=name,
            duration_minutes=duration_minutes,
            price_cents=price_cents,
            category_id=_opt_int(category_id),
            location_id=_opt_int(location_id),
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
    error = None
    try:
        catalog_svc.delete_service(db, tenant_id=tid, service_id=service_id)
    except HTTPException as exc:
        error = str(exc.detail)
    return templates.TemplateResponse(
        "_services_list.html", _services_ctx(request, actor, db, error=error)
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


@router.post(
    "/services/{service_id}/staff/{staff_id}/unassign", response_class=HTMLResponse
)
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
    error = None
    try:
        catalog_svc.unassign_staff(
            db, tenant_id=tid, service_id=service_id, staff_id=staff_id
        )
    except HTTPException as exc:
        error = str(exc.detail)
    return templates.TemplateResponse(
        "_services_list.html", _services_ctx(request, actor, db, error=error)
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
        request,
        actor,
        customers=rows,
        q=q,
        page=page,
        pages=pages,
        total=total,
        **extra,
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
    packages_enabled = features_svc.is_enabled(db, tid, features_svc.SERVICE_PACKAGES)
    package_wallet = (
        packages_svc.customer_wallet(
            db, tenant_id=tid, customer_id=customer_id, include_empty=True
        )
        if packages_enabled
        else []
    )
    package_ledger = (
        packages_svc.ledger_for_customer(db, tenant_id=tid, customer_id=customer_id)
        if packages_enabled
        else []
    )
    package_services = {
        service.id: service for service in tenant_query(db, Service, tid).all()
    }
    gift_cards_enabled = features_svc.is_enabled(db, tid, features_svc.GIFT_CARDS)
    gift_card_wallet = (
        gift_cards_svc.customer_wallet(db, tenant_id=tid, customer_id=customer_id)
        if gift_cards_enabled
        else []
    )
    client_forms_enabled = features_svc.is_enabled(db, tid, features_svc.CLIENT_FORMS)
    client_form_requests = (
        client_forms_svc.for_customer(db, tenant_id=tid, customer_id=customer_id)
        if client_forms_enabled
        else []
    )
    return _ctx(
        request,
        actor,
        customer=customer,
        all_tags=all_tags,
        customer_tag_ids=customer_tag_ids,
        reservations=reservations,
        slots=slots,
        ledger=ledger,
        packages_enabled=packages_enabled,
        package_wallet=package_wallet,
        package_ledger=package_ledger,
        package_services=package_services,
        available_packages=(
            packages_svc.list_packages(db, tenant_id=tid, active_only=True)
            if packages_enabled
            else []
        ),
        can_issue_packages=(
            (getattr(actor.user, "role", None) or "owner") == "owner"
            or actor.user.is_admin
        ),
        package_issue_key=secrets.token_urlsafe(24),
        gift_cards_enabled=gift_cards_enabled,
        gift_card_wallet=gift_card_wallet,
        client_forms_enabled=client_forms_enabled,
        client_form_requests=client_form_requests,
        client_form_url=client_forms_svc.form_url,
        can_manage_client_forms=(
            (getattr(actor.user, "role", None) or "owner") == "owner"
            or actor.user.is_admin
        ),
        **extra,
    )


@router.post(
    "/customers/{customer_id}/reservations/{reservation_id}/client-forms",
    response_class=HTMLResponse,
)
def customer_attach_client_forms(
    request: Request,
    customer_id: int,
    reservation_id: int,
    actor: Actor = Depends(require_ui_owner),
    db: Session = Depends(get_db),
):
    from saas_mvp.models.reservation import Reservation

    if not _require_ui_feature(db, actor, features_svc.CLIENT_FORMS):
        return _feature_locked(
            request, actor, features_svc.CLIENT_FORMS, "顧客表單／同意書"
        )
    reservation = tenant_query(db, Reservation, actor.user.tenant_id).filter(
        Reservation.id == reservation_id,
        Reservation.customer_id == customer_id,
    ).one_or_none()
    if reservation is None:
        raise HTTPException(status_code=404, detail="reservation not found")
    rows = client_forms_svc.attach_to_reservation(db, reservation=reservation)
    audit_svc.record_from_actor(
        db,
        actor,
        action="client_forms.attach",
        target=f"reservation:{reservation.id}",
        detail={"forms": len(rows), "customer_id": customer_id},
        request=request,
    )
    db.commit()
    message = (
        f"已確認預約 #{reservation.id} 的適用表單，共 {len(rows)} 份。"
        if rows
        else "目前沒有已啟用且適用此服務的表單。"
    )
    return templates.TemplateResponse(
        "_customer_detail.html",
        _customer_detail_ctx(request, actor, db, customer_id, saved=message),
    )


@router.post("/customers/{customer_id}/gift-cards/claim", response_class=HTMLResponse)
def customer_claim_gift_card(
    request: Request,
    customer_id: int,
    code: str = Form(..., max_length=32),
    actor: Actor = Depends(require_ui_owner),
    db: Session = Depends(get_db),
):
    error = None
    saved = None
    if not _require_ui_feature(db, actor, features_svc.GIFT_CARDS):
        return _feature_locked(request, actor, features_svc.GIFT_CARDS, "電子禮物卡")
    try:
        card = gift_cards_svc.claim_card(
            db, tenant_id=actor.user.tenant_id, code=code, customer_id=customer_id
        )
        audit_svc.record_from_actor(
            db,
            actor,
            action="gift_cards.claim",
            target=f"gift_card:{card.id}",
            detail={"customer_id": customer_id},
            request=request,
        )
        db.commit()
        saved = "禮物卡已加入顧客錢包。"
    except gift_cards_svc.GiftCardError as exc:
        db.rollback()
        error = str(exc)
    return templates.TemplateResponse(
        "_customer_detail.html",
        _customer_detail_ctx(request, actor, db, customer_id, error=error, saved=saved),
    )


def _packages_ctx(request: Request, actor: Actor, db: Session, **extra) -> dict:
    tid = actor.user.tenant_id
    package_rows = packages_svc.list_packages(db, tenant_id=tid)
    services = catalog_svc.list_services(db, tenant_id=tid)
    return _ctx(
        request,
        actor,
        packages=package_rows,
        services=services,
        service_by_id={service.id: service for service in services},
        items_by_package={
            package.id: packages_svc.package_items(
                db, tenant_id=tid, package_id=package.id
            )
            for package in package_rows
        },
        can_manage=(
            (getattr(actor.user, "role", None) or "owner") == "owner"
            or actor.user.is_admin
        ),
        **extra,
    )


@router.get("/packages", response_class=HTMLResponse)
def packages_page(
    request: Request,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.SERVICE_PACKAGES):
        return _feature_locked(
            request, actor, features_svc.SERVICE_PACKAGES, "服務套票"
        )
    return templates.TemplateResponse(
        "packages.html", _packages_ctx(request, actor, db)
    )


@router.post("/packages", response_class=HTMLResponse)
def packages_create(
    request: Request,
    name: str = Form(..., max_length=128),
    description: str = Form("", max_length=2000),
    price_twd: int = Form(...),
    validity_days: int = Form(...),
    actor: Actor = Depends(require_ui_owner),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.SERVICE_PACKAGES):
        return _feature_locked(
            request, actor, features_svc.SERVICE_PACKAGES, "服務套票"
        )
    error = None
    try:
        row = packages_svc.create_package(
            db,
            tenant_id=actor.user.tenant_id,
            name=name,
            description=description,
            price_cents=price_twd * 100,
            validity_days=validity_days,
        )
        audit_svc.record_from_actor(
            db,
            actor,
            action="packages.create",
            target=f"package:{row.id}",
            detail={
                "price_cents": row.price_cents,
                "validity_days": row.validity_days,
            },
            request=request,
        )
        db.commit()
    except packages_svc.ServicePackageError as exc:
        db.rollback()
        error = str(exc)
    return templates.TemplateResponse(
        "_packages.html", _packages_ctx(request, actor, db, error=error)
    )


@router.post("/packages/{package_id}/items", response_class=HTMLResponse)
def packages_add_item(
    request: Request,
    package_id: int,
    service_id: int = Form(...),
    included_quantity: int = Form(...),
    actor: Actor = Depends(require_ui_owner),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.SERVICE_PACKAGES):
        return _feature_locked(
            request, actor, features_svc.SERVICE_PACKAGES, "服務套票"
        )
    error = None
    try:
        packages_svc.add_or_update_item(
            db,
            tenant_id=actor.user.tenant_id,
            package_id=package_id,
            service_id=service_id,
            included_quantity=included_quantity,
        )
        audit_svc.record_from_actor(
            db,
            actor,
            action="packages.item.update",
            target=f"package:{package_id}",
            detail={"service_id": service_id, "quantity": included_quantity},
            request=request,
        )
        db.commit()
    except packages_svc.ServicePackageError as exc:
        db.rollback()
        error = str(exc)
    return templates.TemplateResponse(
        "_packages.html", _packages_ctx(request, actor, db, error=error)
    )


@router.post("/packages/{package_id}/active", response_class=HTMLResponse)
def packages_set_active(
    request: Request,
    package_id: int,
    active: str = Form(...),
    actor: Actor = Depends(require_ui_owner),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.SERVICE_PACKAGES):
        return _feature_locked(
            request, actor, features_svc.SERVICE_PACKAGES, "服務套票"
        )
    error = None
    try:
        packages_svc.set_active(
            db,
            tenant_id=actor.user.tenant_id,
            package_id=package_id,
            active=active == "true",
        )
        audit_svc.record_from_actor(
            db,
            actor,
            action="packages.active",
            target=f"package:{package_id}",
            detail={"active": active == "true"},
            request=request,
        )
        db.commit()
    except packages_svc.ServicePackageError as exc:
        db.rollback()
        error = str(exc)
    return templates.TemplateResponse(
        "_packages.html", _packages_ctx(request, actor, db, error=error)
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


# ── 顧客 CSV 匯入 / 匯出 ──────────────────────────────────────────────────────
# 註：/customers/import 與 /customers/export.csv 必須宣告於
# /customers/{customer_id} 之前，否則 "import"/"export.csv" 會被當成 id。


@router.post("/customers/import", response_class=HTMLResponse)
async def customers_import(
    request: Request,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    """顧客 CSV 批次匯入（multipart；all-or-nothing，錯誤整批不寫）。"""
    # 注意:request.form() 產生的是 starlette 的 UploadFile(fastapi.UploadFile
    # 是其子類,isinstance 檢查必須用 starlette 基類)。
    from starlette.datastructures import UploadFile

    from saas_mvp.services import customer_import as import_svc

    tid = actor.user.tenant_id
    form = await request.form()
    upload = form.get("file")
    update_existing = bool(form.get("update_existing"))
    if upload is None or not isinstance(upload, UploadFile):
        report = import_svc.ImportReport(errors=["請選擇 CSV 檔案"])
    else:
        content = await upload.read()
        report = import_svc.import_customers(
            db, tenant_id=tid, content=content, update_existing=update_existing
        )
    ctx = _customers_ctx(request, actor, db, import_report=report)
    return templates.TemplateResponse("_customers_list.html", ctx)


def _csv_response(rows: list[dict], fieldnames: list[str], filename: str) -> Response:
    import csv as _csv
    import io as _io

    buf = _io.StringIO()
    writer = _csv.DictWriter(buf, fieldnames=fieldnames)
    writer.writeheader()
    for r in rows:
        writer.writerow(r)
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@router.get("/customers/export.csv")
def customers_export(
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
) -> Response:
    """顧客匯出（欄位為匯入格式超集，round-trip 相容）。"""
    rows = [
        {
            "display_name": c.display_name or "",
            "phone": c.phone or "",
            "birthday": c.birthday.isoformat() if c.birthday else "",
            "note": c.note or "",
            "line_user_id": c.line_user_id or "",
            "points_balance": c.points_balance,
            "tier": c.tier,
            "booking_count": c.booking_count,
            "last_booked_at": c.last_booked_at.isoformat() if c.last_booked_at else "",
            "created_at": c.created_at.isoformat() if c.created_at else "",
        }
        for c in customers_svc.list_customers(db, tenant_id=actor.user.tenant_id)
    ]
    return _csv_response(
        rows,
        [
            "display_name",
            "phone",
            "birthday",
            "note",
            "line_user_id",
            "points_balance",
            "tier",
            "booking_count",
            "last_booked_at",
            "created_at",
        ],
        "customers.csv",
    )


@router.get("/products/export.csv")
def products_export(
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
) -> Response:
    rows = [
        {
            "name": p.name,
            "price_cents": p.price_cents,
            "stock": "" if p.stock is None else p.stock,
            "is_active": "yes" if p.is_active else "no",
            "description": p.description or "",
        }
        for p in shop_svc.list_products(db, tenant_id=actor.user.tenant_id)
    ]
    return _csv_response(
        rows,
        ["name", "price_cents", "stock", "is_active", "description"],
        "products.csv",
    )


@router.get("/services/export.csv")
def services_export(
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
) -> Response:
    rows = [
        {
            "name": s.name,
            "duration_minutes": s.duration_minutes or "",
            "price_cents": s.price_cents or 0,
            "is_active": "yes" if s.is_active else "no",
        }
        for s in catalog_svc.list_services(db, tenant_id=actor.user.tenant_id)
    ]
    return _csv_response(
        rows,
        ["name", "duration_minutes", "price_cents", "is_active"],
        "services.csv",
    )


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
        return HTMLResponse(
            "<h1>找不到顧客</h1>", status_code=status.HTTP_404_NOT_FOUND
        )
    return templates.TemplateResponse("customer_detail.html", ctx)


@router.post("/customers/tags", response_class=HTMLResponse)
def customer_create_tag(
    request: Request,
    name: str = Form(..., max_length=64),
    color: str = Form("", max_length=16),
    customer_id: int | None = Form(None),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    """建立標籤——支援兩種來源表單：

    * 顧客 detail 頁（帶 customer_id）→ 回同一 detail partial。
    * 標籤管理檢視（_customers.html，無 customer_id）→ 回管理 partial。

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
    if customer_id is None:
        return templates.TemplateResponse(
            "_customers.html",
            _customers_admin_ctx(request, actor, db, error=error),
        )
    try:
        ctx = _customer_detail_ctx(request, actor, db, customer_id, error=error)
    except HTTPException:
        return HTMLResponse(
            "<h1>找不到顧客</h1>", status_code=status.HTTP_404_NOT_FOUND
        )
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
        return HTMLResponse(
            "<h1>找不到顧客</h1>", status_code=status.HTTP_404_NOT_FOUND
        )
    return templates.TemplateResponse(
        "_customer_detail.html",
        _customer_detail_ctx(
            request,
            actor,
            db,
            customer_id,
            error=error,
            saved="基本資料已更新",
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
    selected = {int(v) for v in form.getlist("tag_ids") if str(v).isdigit()}
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
        return HTMLResponse(
            "<h1>找不到顧客</h1>", status_code=status.HTTP_404_NOT_FOUND
        )
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
                    db,
                    tenant_id=tid,
                    customer=customer,
                    amount=-delta,
                    reason=reason,
                )
                db.commit()
                saved = f"已扣 {-delta} 點"
            except membership_svc.InsufficientPoints:
                db.rollback()
                error = "點數不足，無法扣點"
    except HTTPException:
        return HTMLResponse(
            "<h1>找不到顧客</h1>", status_code=status.HTTP_404_NOT_FOUND
        )
    return templates.TemplateResponse(
        "_customer_detail.html",
        _customer_detail_ctx(request, actor, db, customer_id, error=error, saved=saved),
    )


@router.post("/customers/{customer_id}/packages", response_class=HTMLResponse)
def customer_issue_package(
    request: Request,
    customer_id: int,
    package_id: int = Form(...),
    issuance_key: str = Form(..., max_length=64),
    actor: Actor = Depends(require_ui_owner),
    db: Session = Depends(get_db),
):
    error = None
    saved = None
    if not _require_ui_feature(db, actor, features_svc.SERVICE_PACKAGES):
        error = "服務套票功能尚未開通。"
    else:
        try:
            owned = packages_svc.issue_package(
                db,
                tenant_id=actor.user.tenant_id,
                customer_id=customer_id,
                package_id=package_id,
                actor_user_id=actor.user.id,
                issuance_key=issuance_key,
            )
            audit_svc.record_from_actor(
                db,
                actor,
                action="packages.issue",
                target=f"customer_package:{owned.id}",
                detail={
                    "customer_id": customer_id,
                    "package_id": package_id,
                    "price_cents": owned.price_cents_snapshot,
                },
                request=request,
            )
            db.commit()
            saved = f"已發行「{owned.package_name_snapshot}」"
        except packages_svc.ServicePackageError as exc:
            db.rollback()
            error = str(exc)
    return templates.TemplateResponse(
        "_customer_detail.html",
        _customer_detail_ctx(request, actor, db, customer_id, error=error, saved=saved),
    )


@router.post(
    "/customers/{customer_id}/packages/{customer_package_id}/cancel",
    response_class=HTMLResponse,
)
def customer_cancel_package(
    request: Request,
    customer_id: int,
    customer_package_id: int,
    note: str = Form("", max_length=255),
    actor: Actor = Depends(require_ui_owner),
    db: Session = Depends(get_db),
):
    error = None
    saved = None
    try:
        owned = packages_svc.cancel_customer_package(
            db,
            tenant_id=actor.user.tenant_id,
            customer_id=customer_id,
            customer_package_id=customer_package_id,
            actor_user_id=actor.user.id,
            note=note,
        )
        audit_svc.record_from_actor(
            db,
            actor,
            action="packages.customer.cancel",
            target=f"customer_package:{owned.id}",
            detail={"customer_id": customer_id, "note": note.strip()[:255]},
            request=request,
        )
        db.commit()
        saved = (
            f"已作廢「{owned.package_name_snapshot}」未用次數；款項請另行退款／對帳。"
        )
    except packages_svc.ServicePackageError as exc:
        db.rollback()
        error = str(exc)
    return templates.TemplateResponse(
        "_customer_detail.html",
        _customer_detail_ctx(request, actor, db, customer_id, error=error, saved=saved),
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
            db,
            tenant_id=tid,
            status=status_filter or None,
            limit=_NOTIF_PAGE_SIZE,
            offset=offset,
        )
    elif tab == "campaign":
        rows, total = notif_history_svc.list_campaign_sends(
            db,
            tenant_id=tid,
            status=status_filter or None,
            limit=_NOTIF_PAGE_SIZE,
            offset=offset,
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
        request,
        actor,
        tab=tab,
        status_filter=status_filter,
        rows=rows,
        total=total,
        page=min(page, pages),
        pages=pages,
        campaign_names=campaign_names,
        usage_history=usage_history,
        push_status=push_status,
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
    segment_chips = {c.id: _describe_segment(c.segment_json, tag_names) for c in rows}
    return _ctx(
        request,
        actor,
        campaigns=rows,
        tags=tags,
        locations=locations,
        segment_chips=segment_chips,
        **extra,
    )


def _campaign_or_none(db: Session, tenant_id: int, campaign_id: int) -> Campaign | None:
    return (
        tenant_query(db, Campaign, tenant_id).filter(Campaign.id == campaign_id).first()
    )


@router.get("/campaigns", response_class=HTMLResponse)
def campaigns_page(
    request: Request,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.MARKETING_AUTO):
        return _feature_locked(
            request, actor, features_svc.MARKETING_AUTO, "行銷自動化"
        )
    return templates.TemplateResponse(
        "campaigns.html", _campaigns_ctx(request, actor, db)
    )


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
    message_type: str = Form("text"),
    flex_menu_id: str = Form(""),
    image_url: str = Form(""),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    import json as _json

    if not _require_ui_feature(db, actor, features_svc.MARKETING_AUTO):
        return _feature_locked(
            request, actor, features_svc.MARKETING_AUTO, "行銷自動化"
        )
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
                int(v) for v in form.getlist("segment_tag_ids") if str(v).isdigit()
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
        # 訊息型別（A3.2）：白名單外一律 text;image 需 https URL。
        mt = message_type if message_type in ("text", "flex", "image") else "text"
        img = image_url.strip() or None
        if mt == "image" and (img is None or not img.startswith("https://")):
            mt = "text"
        campaign = Campaign(
            tenant_id=tid,
            name=name,
            type=type,
            message_template=message_template,
            schedule_at=schedule,
            segment_json=seg or None,
            reward_type=reward_type or None,
            reward_value=_opt_int(reward_value),
            message_type=mt,
            flex_menu_id=_opt_int(flex_menu_id),
            image_url=img,
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
        return _feature_locked(
            request, actor, features_svc.MARKETING_AUTO, "行銷自動化"
        )
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
        return _feature_locked(
            request, actor, features_svc.MARKETING_AUTO, "行銷自動化"
        )
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
        return _feature_locked(
            request, actor, features_svc.MARKETING_AUTO, "行銷自動化"
        )
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
        return _feature_locked(
            request, actor, features_svc.MARKETING_AUTO, "行銷自動化"
        )
    tid = actor.user.tenant_id
    error = None
    editing_id = None
    try:
        marketing_svc.update_campaign(
            db,
            tenant_id=tid,
            campaign_id=campaign_id,
            name=name,
            message_template=message_template,
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
        return _feature_locked(
            request, actor, features_svc.MARKETING_AUTO, "行銷自動化"
        )
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
        request,
        actor,
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


@router.post("/flex-menu/delete", response_class=HTMLResponse)
def flex_menu_delete(
    request: Request,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    """刪除整個選單（含所有卡片）；重繪時自動重建空選單＝重設。"""
    if not _require_ui_feature(db, actor, features_svc.FLEX_MENU):
        return _feature_locked(request, actor, features_svc.FLEX_MENU, "圖文選單卡片")
    tid = actor.user.tenant_id
    error = None
    menu = _get_or_create_flex_menu(db, tid)
    try:
        flex_menu_svc.delete_menu(db, tenant_id=tid, menu_id=menu.id)
    except HTTPException as exc:
        error = str(exc.detail)
    return templates.TemplateResponse(
        "_flex_menu.html", _flex_ctx(request, actor, db, error=error)
    )


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
            db,
            tenant_id=tid,
            menu_id=menu.id,
            title=title,
            action_type=action_type,
            action_data=action_data,
            subtitle=subtitle or None,
            image_url=image_url or None,
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
    error = None
    try:
        flex_menu_svc.delete_card(db, tenant_id=tid, menu_id=menu.id, card_id=card_id)
    except HTTPException as exc:
        error = str(exc.detail)
    return templates.TemplateResponse(
        "_flex_menu.html", _flex_ctx(request, actor, db, error=error)
    )


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
            db,
            tenant_id=tid,
            menu_id=menu.id,
            card_id=card_id,
            title=title,
            action_type=action_type,
            action_data=action_data,
            subtitle=subtitle,
            image_url=image_url,
            bg_color=bg_color,
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
        request,
        actor,
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
        return _feature_locked(
            request, actor, features_svc.PUBLIC_PROFILE, "公開店家頁"
        )
    return templates.TemplateResponse(
        "portfolio.html", _portfolio_ctx(request, actor, db)
    )


@router.post("/portfolio/categories", response_class=HTMLResponse)
def portfolio_create_category(
    request: Request,
    name: str = Form(...),
    sort_order: int = Form(0),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.PUBLIC_PROFILE):
        return _feature_locked(
            request, actor, features_svc.PUBLIC_PROFILE, "公開店家頁"
        )
    tid = actor.user.tenant_id
    error = None
    try:
        portfolio_svc.create_category(
            db, tenant_id=tid, name=name, sort_order=sort_order
        )
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
        return _feature_locked(
            request, actor, features_svc.PUBLIC_PROFILE, "公開店家頁"
        )
    tid = actor.user.tenant_id
    error = None
    try:
        portfolio_svc.delete_category(db, tenant_id=tid, category_id=category_id)
    except HTTPException as exc:
        error = str(exc.detail)
    return templates.TemplateResponse(
        "_portfolio.html", _portfolio_ctx(request, actor, db, error=error)
    )


@router.get("/portfolio/categories/{category_id}/edit", response_class=HTMLResponse)
def portfolio_edit_category_form(
    request: Request,
    category_id: int,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.PUBLIC_PROFILE):
        return _feature_locked(
            request, actor, features_svc.PUBLIC_PROFILE, "公開店家頁"
        )
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
        return _feature_locked(
            request, actor, features_svc.PUBLIC_PROFILE, "公開店家頁"
        )
    tid = actor.user.tenant_id
    error = None
    editing_category_id = None
    try:
        portfolio_svc.update_category(
            db,
            tenant_id=tid,
            category_id=category_id,
            name=name,
            sort_order=sort_order,
        )
    except HTTPException as exc:
        error = str(exc.detail)
        editing_category_id = category_id
    return templates.TemplateResponse(
        "_portfolio.html",
        _portfolio_ctx(
            request, actor, db, error=error, editing_category_id=editing_category_id
        ),
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
        return _feature_locked(
            request, actor, features_svc.PUBLIC_PROFILE, "公開店家頁"
        )
    tid = actor.user.tenant_id
    error = None
    try:
        portfolio_svc.create_item(
            db,
            tenant_id=tid,
            image_url=image_url,
            caption=caption or None,
            category_id=_opt_int(category_id),
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
        return _feature_locked(
            request, actor, features_svc.PUBLIC_PROFILE, "公開店家頁"
        )
    tid = actor.user.tenant_id
    error = None
    try:
        portfolio_svc.delete_item(db, tenant_id=tid, item_id=item_id)
    except HTTPException as exc:
        error = str(exc.detail)
    return templates.TemplateResponse(
        "_portfolio.html", _portfolio_ctx(request, actor, db, error=error)
    )


@router.get("/portfolio/items/{item_id}/edit", response_class=HTMLResponse)
def portfolio_edit_item_form(
    request: Request,
    item_id: int,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.PUBLIC_PROFILE):
        return _feature_locked(
            request, actor, features_svc.PUBLIC_PROFILE, "公開店家頁"
        )
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
        return _feature_locked(
            request, actor, features_svc.PUBLIC_PROFILE, "公開店家頁"
        )
    tid = actor.user.tenant_id
    error = None
    editing_item_id = None
    try:
        portfolio_svc.update_item(
            db,
            tenant_id=tid,
            item_id=item_id,
            image_url=image_url,
            caption=caption,
            sort_order=sort_order,
        )
    except HTTPException as exc:
        error = str(exc.detail)
        editing_item_id = item_id
    return templates.TemplateResponse(
        "_portfolio.html",
        _portfolio_ctx(
            request, actor, db, error=error, editing_item_id=editing_item_id
        ),
    )


# ── 店家自助：公開店家頁（PUBLIC_PROFILE） ──────────────────────────────────────


def _profile_ctx(request: Request, actor: Actor, db: Session, **extra) -> dict:
    tid = actor.user.tenant_id
    return _ctx(
        request,
        actor,
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
        return _feature_locked(
            request, actor, features_svc.PUBLIC_PROFILE, "公開店家頁"
        )
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
        return _feature_locked(
            request, actor, features_svc.PUBLIC_PROFILE, "公開店家頁"
        )
    tid = actor.user.tenant_id
    error = None
    saved = False
    try:
        profile_svc.upsert(
            db,
            tid,
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


# ── 店家自助：房間／設備資源（BOOKABLE_RESOURCES） ───────────────────────────


def _resources_ctx(request: Request, actor: Actor, db: Session, **extra) -> dict:
    tid = actor.user.tenant_id
    resource_types = resources_svc.list_types(db, tenant_id=tid)
    resources = resources_svc.list_resources(db, tenant_id=tid)
    services = catalog_svc.list_services(db, tenant_id=tid)
    locations = locations_svc.list_locations(db, tenant_id=tid)
    windows = resources_svc.list_availability(db, tenant_id=tid)
    blocks = resources_svc.list_blocks(db, tenant_id=tid)
    windows_by_resource: dict[int, list] = {}
    blocks_by_resource: dict[int, list] = {}
    for window in windows:
        windows_by_resource.setdefault(window.resource_id, []).append(window)
    for block in blocks:
        blocks_by_resource.setdefault(block.resource_id, []).append(block)
    return _ctx(
        request,
        actor,
        resource_types=resource_types,
        resources=resources,
        services=services,
        locations=locations,
        requirements=resources_svc.list_requirements(db, tenant_id=tid),
        windows_by_resource=windows_by_resource,
        blocks_by_resource=blocks_by_resource,
        upcoming_allocations=resources_svc.list_upcoming_allocations(
            db, tenant_id=tid
        ),
        type_names={row.id: row.name for row in resource_types},
        resource_names={row.id: row.name for row in resources},
        service_names={row.id: row.name for row in services},
        location_names={row.id: row.name for row in locations},
        weekday_names=("週一", "週二", "週三", "週四", "週五", "週六", "週日"),
        can_manage_resources=(
            (getattr(actor.user, "role", None) or "owner") == "owner"
            or actor.user.is_admin
        ),
        **extra,
    )


def _resources_response(
    request: Request, actor: Actor, db: Session, *, error: str | None = None
):
    return templates.TemplateResponse(
        "_resources.html", _resources_ctx(request, actor, db, error=error)
    )


def _resources_enabled(db: Session, actor: Actor):
    return _require_ui_feature(db, actor, features_svc.BOOKABLE_RESOURCES)


@router.get("/resources", response_class=HTMLResponse)
def resources_page(
    request: Request,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _resources_enabled(db, actor):
        return _feature_locked(
            request, actor, features_svc.BOOKABLE_RESOURCES, "房間／設備資源"
        )
    return templates.TemplateResponse(
        "resources.html", _resources_ctx(request, actor, db)
    )


@router.post("/resources/types", response_class=HTMLResponse)
def resources_create_type(
    request: Request,
    name: str = Form(..., max_length=128),
    description: str = Form("", max_length=2000),
    actor: Actor = Depends(require_ui_owner),
    db: Session = Depends(get_db),
):
    if not _resources_enabled(db, actor):
        return _feature_locked(
            request, actor, features_svc.BOOKABLE_RESOURCES, "房間／設備資源"
        )
    error = None
    try:
        row = resources_svc.create_type(
            db, tenant_id=actor.user.tenant_id, name=name, description=description
        )
        audit_svc.record_from_actor(
            db,
            actor,
            action="resources.type.create",
            target=f"resource_type:{row.id}",
            request=request,
        )
        db.commit()
    except resources_svc.BookableResourceError as exc:
        db.rollback()
        error = str(exc)
    return _resources_response(request, actor, db, error=error)


@router.post("/resources/types/{resource_type_id}/active", response_class=HTMLResponse)
def resources_type_active(
    request: Request,
    resource_type_id: int,
    active: str = Form(...),
    actor: Actor = Depends(require_ui_owner),
    db: Session = Depends(get_db),
):
    if not _resources_enabled(db, actor):
        return _feature_locked(
            request, actor, features_svc.BOOKABLE_RESOURCES, "房間／設備資源"
        )
    error = None
    try:
        row = resources_svc.set_type_active(
            db,
            tenant_id=actor.user.tenant_id,
            resource_type_id=resource_type_id,
            active=active == "true",
        )
        audit_svc.record_from_actor(
            db,
            actor,
            action="resources.type.active",
            target=f"resource_type:{row.id}",
            detail={"active": row.is_active},
            request=request,
        )
        db.commit()
    except resources_svc.BookableResourceError as exc:
        db.rollback()
        error = str(exc)
    return _resources_response(request, actor, db, error=error)


@router.post("/resources", response_class=HTMLResponse)
def resources_create(
    request: Request,
    resource_type_id: int = Form(...),
    name: str = Form(..., max_length=128),
    description: str = Form("", max_length=2000),
    internal_code: str = Form("", max_length=64),
    capacity: int = Form(1),
    location_id: str = Form(""),
    available_from: str = Form(""),
    available_until: str = Form(""),
    actor: Actor = Depends(require_ui_owner),
    db: Session = Depends(get_db),
):
    if not _resources_enabled(db, actor):
        return _feature_locked(
            request, actor, features_svc.BOOKABLE_RESOURCES, "房間／設備資源"
        )
    error = None
    try:
        row = resources_svc.create_resource(
            db,
            tenant_id=actor.user.tenant_id,
            resource_type_id=resource_type_id,
            name=name,
            description=description,
            internal_code=internal_code,
            capacity=capacity,
            location_id=_opt_int(location_id),
            available_from=(
                datetime.date.fromisoformat(available_from) if available_from else None
            ),
            available_until=(
                datetime.date.fromisoformat(available_until)
                if available_until
                else None
            ),
        )
        audit_svc.record_from_actor(
            db,
            actor,
            action="resources.create",
            target=f"resource:{row.id}",
            request=request,
        )
        db.commit()
    except (resources_svc.BookableResourceError, ValueError) as exc:
        db.rollback()
        error = str(exc) or "日期格式不正確。"
    return _resources_response(request, actor, db, error=error)


@router.post("/resources/{resource_id}", response_class=HTMLResponse)
def resources_update(
    request: Request,
    resource_id: int,
    name: str = Form(..., max_length=128),
    description: str = Form("", max_length=2000),
    internal_code: str = Form("", max_length=64),
    capacity: int = Form(1),
    location_id: str = Form(""),
    available_from: str = Form(""),
    available_until: str = Form(""),
    actor: Actor = Depends(require_ui_owner),
    db: Session = Depends(get_db),
):
    if not _resources_enabled(db, actor):
        return _feature_locked(
            request, actor, features_svc.BOOKABLE_RESOURCES, "房間／設備資源"
        )
    error = None
    try:
        row = resources_svc.update_resource(
            db,
            tenant_id=actor.user.tenant_id,
            resource_id=resource_id,
            name=name,
            description=description,
            internal_code=internal_code,
            capacity=capacity,
            location_id=_opt_int(location_id),
            available_from=(
                datetime.date.fromisoformat(available_from) if available_from else None
            ),
            available_until=(
                datetime.date.fromisoformat(available_until)
                if available_until
                else None
            ),
        )
        audit_svc.record_from_actor(
            db,
            actor,
            action="resources.update",
            target=f"resource:{row.id}",
            request=request,
        )
        db.commit()
    except (resources_svc.BookableResourceError, ValueError) as exc:
        db.rollback()
        error = str(exc) or "日期格式不正確。"
    return _resources_response(request, actor, db, error=error)


@router.post("/resources/{resource_id}/active", response_class=HTMLResponse)
def resources_active(
    request: Request,
    resource_id: int,
    active: str = Form(...),
    actor: Actor = Depends(require_ui_owner),
    db: Session = Depends(get_db),
):
    if not _resources_enabled(db, actor):
        return _feature_locked(
            request, actor, features_svc.BOOKABLE_RESOURCES, "房間／設備資源"
        )
    error = None
    try:
        row = resources_svc.set_resource_active(
            db,
            tenant_id=actor.user.tenant_id,
            resource_id=resource_id,
            active=active == "true",
        )
        audit_svc.record_from_actor(
            db,
            actor,
            action="resources.active",
            target=f"resource:{row.id}",
            detail={"active": row.is_active},
            request=request,
        )
        db.commit()
    except resources_svc.BookableResourceError as exc:
        db.rollback()
        error = str(exc)
    return _resources_response(request, actor, db, error=error)


@router.post("/resource-requirements", response_class=HTMLResponse)
def resources_set_requirement(
    request: Request,
    service_id: int = Form(...),
    resource_type_id: int = Form(...),
    quantity: int = Form(1),
    actor: Actor = Depends(require_ui_owner),
    db: Session = Depends(get_db),
):
    if not _resources_enabled(db, actor):
        return _feature_locked(
            request, actor, features_svc.BOOKABLE_RESOURCES, "房間／設備資源"
        )
    error = None
    try:
        row = resources_svc.set_requirement(
            db,
            tenant_id=actor.user.tenant_id,
            service_id=service_id,
            resource_type_id=resource_type_id,
            quantity=quantity,
        )
        audit_svc.record_from_actor(
            db,
            actor,
            action="resources.requirement.set",
            target=f"requirement:{row.id}",
            request=request,
        )
        db.commit()
    except resources_svc.BookableResourceError as exc:
        db.rollback()
        error = str(exc)
    return _resources_response(request, actor, db, error=error)


@router.post("/resource-requirements/{requirement_id}/delete", response_class=HTMLResponse)
def resources_remove_requirement(
    request: Request,
    requirement_id: int,
    actor: Actor = Depends(require_ui_owner),
    db: Session = Depends(get_db),
):
    if not _resources_enabled(db, actor):
        return _feature_locked(
            request, actor, features_svc.BOOKABLE_RESOURCES, "房間／設備資源"
        )
    error = None
    try:
        resources_svc.remove_requirement(
            db, tenant_id=actor.user.tenant_id, requirement_id=requirement_id
        )
        audit_svc.record_from_actor(
            db,
            actor,
            action="resources.requirement.delete",
            target=f"requirement:{requirement_id}",
            request=request,
        )
        db.commit()
    except resources_svc.BookableResourceError as exc:
        db.rollback()
        error = str(exc)
    return _resources_response(request, actor, db, error=error)


@router.post("/resources/{resource_id}/availability", response_class=HTMLResponse)
def resources_add_availability(
    request: Request,
    resource_id: int,
    weekday: int = Form(...),
    start_time: str = Form(...),
    end_time: str = Form(...),
    actor: Actor = Depends(require_ui_owner),
    db: Session = Depends(get_db),
):
    if not _resources_enabled(db, actor):
        return _feature_locked(
            request, actor, features_svc.BOOKABLE_RESOURCES, "房間／設備資源"
        )
    error = None
    try:
        resources_svc.add_availability(
            db,
            tenant_id=actor.user.tenant_id,
            resource_id=resource_id,
            weekday=weekday,
            start_time=datetime.time.fromisoformat(start_time),
            end_time=datetime.time.fromisoformat(end_time),
        )
        audit_svc.record_from_actor(
            db,
            actor,
            action="resources.availability.add",
            target=f"resource:{resource_id}",
            request=request,
        )
        db.commit()
    except (resources_svc.BookableResourceError, ValueError) as exc:
        db.rollback()
        error = str(exc) or "時間格式不正確。"
    return _resources_response(request, actor, db, error=error)


@router.post("/resources/availability/{availability_id}/delete", response_class=HTMLResponse)
def resources_remove_availability(
    request: Request,
    availability_id: int,
    actor: Actor = Depends(require_ui_owner),
    db: Session = Depends(get_db),
):
    if not _resources_enabled(db, actor):
        return _feature_locked(
            request, actor, features_svc.BOOKABLE_RESOURCES, "房間／設備資源"
        )
    error = None
    try:
        resources_svc.remove_availability(
            db, tenant_id=actor.user.tenant_id, availability_id=availability_id
        )
        db.commit()
    except resources_svc.BookableResourceError as exc:
        db.rollback()
        error = str(exc)
    return _resources_response(request, actor, db, error=error)


@router.post("/resources/{resource_id}/blocks", response_class=HTMLResponse)
def resources_add_block(
    request: Request,
    resource_id: int,
    starts_at: str = Form(...),
    ends_at: str = Form(...),
    reason: str = Form("", max_length=255),
    actor: Actor = Depends(require_ui_owner),
    db: Session = Depends(get_db),
):
    if not _resources_enabled(db, actor):
        return _feature_locked(
            request, actor, features_svc.BOOKABLE_RESOURCES, "房間／設備資源"
        )
    error = None
    try:
        resources_svc.add_block(
            db,
            tenant_id=actor.user.tenant_id,
            resource_id=resource_id,
            starts_at=_parse_slot_start(starts_at),
            ends_at=_parse_slot_start(ends_at),
            reason=reason,
        )
        audit_svc.record_from_actor(
            db,
            actor,
            action="resources.block.add",
            target=f"resource:{resource_id}",
            request=request,
        )
        db.commit()
    except (resources_svc.BookableResourceError, ValueError) as exc:
        db.rollback()
        error = str(exc) or "日期時間格式不正確。"
    return _resources_response(request, actor, db, error=error)


@router.post("/resources/blocks/{block_id}/delete", response_class=HTMLResponse)
def resources_remove_block(
    request: Request,
    block_id: int,
    actor: Actor = Depends(require_ui_owner),
    db: Session = Depends(get_db),
):
    if not _resources_enabled(db, actor):
        return _feature_locked(
            request, actor, features_svc.BOOKABLE_RESOURCES, "房間／設備資源"
        )
    error = None
    try:
        resources_svc.remove_block(
            db, tenant_id=actor.user.tenant_id, block_id=block_id
        )
        db.commit()
    except resources_svc.BookableResourceError as exc:
        db.rollback()
        error = str(exc)
    return _resources_response(request, actor, db, error=error)


# ── 店家自助：顧客諮詢表／同意書（CLIENT_FORMS） ──────────────────────────────


def _client_forms_ctx(request: Request, actor: Actor, db: Session, **extra) -> dict:
    tid = actor.user.tenant_id
    rows = client_forms_svc.list_templates(db, tenant_id=tid)
    question_map = {
        row.id: client_forms_svc.questions(db, tenant_id=tid, template_id=row.id)
        for row in rows
    }
    option_map = {
        question.id: (
            json.loads(question.options_json) if question.options_json else []
        )
        for form_questions in question_map.values()
        for question in form_questions
    }
    services = catalog_svc.list_services(db, tenant_id=tid)
    return _ctx(
        request,
        actor,
        form_templates=rows,
        questions_by_template=question_map,
        question_options=option_map,
        services=services,
        service_names={service.id: service.name for service in services},
        **extra,
    )


@router.get("/client-forms", response_class=HTMLResponse)
def client_forms_page(
    request: Request,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.CLIENT_FORMS):
        return _feature_locked(
            request, actor, features_svc.CLIENT_FORMS, "顧客表單／同意書"
        )
    return templates.TemplateResponse(
        "client_forms.html", _client_forms_ctx(request, actor, db)
    )


@router.post("/client-forms", response_class=HTMLResponse)
def client_forms_create(
    request: Request,
    name: str = Form(..., max_length=128),
    intro: str = Form("", max_length=4000),
    consent_text: str = Form(..., max_length=4000),
    service_id: str = Form(""),
    require_signature: str = Form(""),
    actor: Actor = Depends(require_ui_owner),
    db: Session = Depends(get_db),
):
    error = None
    if not _require_ui_feature(db, actor, features_svc.CLIENT_FORMS):
        return _feature_locked(
            request, actor, features_svc.CLIENT_FORMS, "顧客表單／同意書"
        )
    try:
        row = client_forms_svc.create_template(
            db,
            tenant_id=actor.user.tenant_id,
            name=name,
            intro=intro,
            consent_text=consent_text,
            service_id=_opt_int(service_id),
            require_signature=require_signature == "true",
        )
        audit_svc.record_from_actor(
            db,
            actor,
            action="client_forms.create",
            target=f"form:{row.id}",
            request=request,
        )
        db.commit()
    except client_forms_svc.ClientFormError as exc:
        db.rollback()
        error = str(exc)
    return templates.TemplateResponse(
        "_client_forms.html", _client_forms_ctx(request, actor, db, error=error)
    )


@router.post("/client-forms/{template_id}/questions", response_class=HTMLResponse)
def client_forms_add_question(
    request: Request,
    template_id: int,
    label: str = Form(..., max_length=255),
    field_type: str = Form(...),
    required: str = Form(""),
    options: str = Form("", max_length=6000),
    actor: Actor = Depends(require_ui_owner),
    db: Session = Depends(get_db),
):
    error = None
    if not _require_ui_feature(db, actor, features_svc.CLIENT_FORMS):
        return _feature_locked(
            request, actor, features_svc.CLIENT_FORMS, "顧客表單／同意書"
        )
    try:
        client_forms_svc.add_question(
            db,
            tenant_id=actor.user.tenant_id,
            template_id=template_id,
            label=label,
            field_type=field_type,
            required=required == "true",
            options=options,
        )
        audit_svc.record_from_actor(
            db,
            actor,
            action="client_forms.question.add",
            target=f"form:{template_id}",
            request=request,
        )
        db.commit()
    except client_forms_svc.ClientFormError as exc:
        db.rollback()
        error = str(exc)
    return templates.TemplateResponse(
        "_client_forms.html", _client_forms_ctx(request, actor, db, error=error)
    )


@router.post("/client-forms/{template_id}/active", response_class=HTMLResponse)
def client_forms_active(
    request: Request,
    template_id: int,
    active: str = Form(...),
    actor: Actor = Depends(require_ui_owner),
    db: Session = Depends(get_db),
):
    error = None
    if not _require_ui_feature(db, actor, features_svc.CLIENT_FORMS):
        return _feature_locked(
            request, actor, features_svc.CLIENT_FORMS, "顧客表單／同意書"
        )
    try:
        row = client_forms_svc.set_active(
            db,
            tenant_id=actor.user.tenant_id,
            template_id=template_id,
            active=active == "true",
        )
        audit_svc.record_from_actor(
            db,
            actor,
            action="client_forms.active",
            target=f"form:{row.id}",
            detail={"active": row.is_active},
            request=request,
        )
        db.commit()
    except client_forms_svc.ClientFormError as exc:
        db.rollback()
        error = str(exc)
    return templates.TemplateResponse(
        "_client_forms.html", _client_forms_ctx(request, actor, db, error=error)
    )


# ── 店家自助：電子禮物卡（GIFT_CARDS） ────────────────────────────────────────


def _gift_cards_ctx(request: Request, actor: Actor, db: Session, **extra) -> dict:
    tid = actor.user.tenant_id
    return _ctx(
        request,
        actor,
        cards=gift_cards_svc.recent_cards(db, tenant_id=tid),
        customers=customers_svc.list_customers(db, tenant_id=tid, limit=300),
        issuance_key=secrets.token_urlsafe(24),
        **extra,
    )


@router.get("/gift-cards", response_class=HTMLResponse)
def gift_cards_page(
    request: Request,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.GIFT_CARDS):
        return _feature_locked(request, actor, features_svc.GIFT_CARDS, "電子禮物卡")
    return templates.TemplateResponse(
        "gift_cards.html", _gift_cards_ctx(request, actor, db)
    )


@router.post("/gift-cards", response_class=HTMLResponse)
def gift_cards_issue(
    request: Request,
    amount_twd: int = Form(...),
    fulfillment_guarantee: str = Form(..., max_length=2000),
    issuance_key: str = Form(..., max_length=64),
    recipient_customer_id: str = Form(""),
    purchaser_name: str = Form("", max_length=128),
    recipient_name: str = Form("", max_length=128),
    message: str = Form("", max_length=500),
    compliance_ack: str = Form(""),
    actor: Actor = Depends(require_ui_owner),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.GIFT_CARDS):
        return _feature_locked(request, actor, features_svc.GIFT_CARDS, "電子禮物卡")
    error = None
    issued_code = None
    issued_card = None
    saved = None
    try:
        if compliance_ack != "true":
            raise gift_cards_svc.GiftCardError("請確認已核對履約保障與禮券法規資訊。")
        result = gift_cards_svc.issue_card(
            db,
            tenant_id=actor.user.tenant_id,
            amount_cents=amount_twd * 100,
            fulfillment_guarantee=fulfillment_guarantee,
            issuance_key=issuance_key,
            issued_by_user_id=actor.user.id,
            recipient_customer_id=_opt_int(recipient_customer_id),
            purchaser_name=purchaser_name,
            recipient_name=recipient_name,
            message=message,
        )
        audit_svc.record_from_actor(
            db,
            actor,
            action="gift_cards.issue",
            target=f"gift_card:{result.card.id}",
            detail={
                "amount_cents": result.card.initial_value_cents,
                "recipient_customer_id": result.card.recipient_customer_id,
            },
            request=request,
        )
        db.commit()
        issued_code = result.code
        issued_card = result.card if result.created else None
        saved = (
            "禮物卡已發行。" if result.created else "此筆發行已處理，未重複建立禮物卡。"
        )
    except gift_cards_svc.GiftCardError as exc:
        db.rollback()
        error = str(exc)
    return templates.TemplateResponse(
        "_gift_cards.html",
        _gift_cards_ctx(
            request,
            actor,
            db,
            error=error,
            saved=saved,
            issued_code=issued_code,
            issued_card=issued_card,
        ),
    )


@router.post("/gift-cards/{gift_card_id}/void", response_class=HTMLResponse)
def gift_cards_void(
    request: Request,
    gift_card_id: int,
    note: str = Form(..., min_length=2, max_length=255),
    actor: Actor = Depends(require_ui_owner),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.GIFT_CARDS):
        return _feature_locked(request, actor, features_svc.GIFT_CARDS, "電子禮物卡")
    error = None
    try:
        card = gift_cards_svc.void_card(
            db,
            tenant_id=actor.user.tenant_id,
            gift_card_id=gift_card_id,
            note=note,
            actor_user_id=actor.user.id,
        )
        audit_svc.record_from_actor(
            db,
            actor,
            action="gift_cards.void",
            target=f"gift_card:{card.id}",
            detail={"reason": note},
            request=request,
        )
        db.commit()
    except gift_cards_svc.GiftCardError as exc:
        db.rollback()
        error = str(exc)
    return templates.TemplateResponse(
        "_gift_cards.html", _gift_cards_ctx(request, actor, db, error=error)
    )


# ── 店家自助：POS 結帳（PRODUCT_SALES） ─────────────────────────────────────────


def _pos_ctx(request: Request, actor: Actor, db: Session, **extra) -> dict:
    tid = actor.user.tenant_id
    from saas_mvp.models.booking_slot import BookingSlot
    from saas_mvp.models.order import Order
    from saas_mvp.models.reservation import RESERVATION_CONFIRMED, Reservation

    active_staff = [row for row in staff_svc.list_staff(db, tenant_id=tid) if row.is_active]
    now = datetime.datetime.now(datetime.timezone.utc)
    reservation_rows = db.execute(
        select(Reservation, BookingSlot, Service)
        .join(BookingSlot, BookingSlot.id == Reservation.slot_id)
        .outerjoin(Service, Service.id == Reservation.service_id)
        .outerjoin(Order, Order.reservation_id == Reservation.id)
        .where(
            Reservation.tenant_id == tid,
            Reservation.status == RESERVATION_CONFIRMED,
            BookingSlot.slot_start >= now - datetime.timedelta(days=30),
            Order.id.is_(None),
        )
        .order_by(BookingSlot.slot_start.desc())
        .limit(100)
    ).all()
    return _ctx(
        request,
        actor,
        products=shop_svc.list_products(db, tenant_id=tid),
        gift_cards_enabled=features_svc.is_enabled(db, tid, features_svc.GIFT_CARDS),
        staff=active_staff,
        staff_by_id={row.id: row for row in active_staff},
        pos_reservations=reservation_rows,
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
            gift_card_balance_cents=result["gift_card_balance_cents"],
        )
    return templates.TemplateResponse(
        "_pos.html", _pos_ctx(request, actor, db, **extra)
    )


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
    gift_card_code = (form.get("gift_card_code") or "").strip() or None
    reservation_id = _opt_int(form.get("reservation_id") or "")
    staff_id = _opt_int(form.get("staff_id") or "")
    payment_method = (form.get("payment_method") or "").strip() or None
    mark_paid = form.get("mark_paid") == "true"
    try:
        tip_cents = int(
            (Decimal(str(form.get("tip_twd") or "0")) * 100).quantize(
                Decimal("1"), rounding=ROUND_HALF_UP
            )
        )
    except (InvalidOperation, ValueError):
        tip_cents = -1
    try:
        points_to_redeem = int(form.get("points_to_redeem") or 0)
    except ValueError:
        points_to_redeem = 0

    # 從 qty_<product_id> 欄位組裝結帳明細（數量 > 0 才納入）。
    items: list[dict] = []
    submitted_qty: dict[int, int] = {}
    for key, value in form.items():
        if not key.startswith("qty_"):
            continue
        try:
            product_id = int(key[4:])
            qty = int(value)
        except (TypeError, ValueError):
            continue
        submitted_qty[product_id] = max(0, qty)
        if qty > 0:
            items.append({"product_id": product_id, "qty": qty})

    error = None
    order = None
    if not items and reservation_id is None:
        error = "請選擇一筆預約服務或至少一項商品。"
    else:
        try:
            order = pos_svc.checkout(
                db,
                tenant_id=tid,
                customer_id=customer_id,
                items=items,
                coupon_code=coupon_code,
                points_to_redeem=points_to_redeem,
                gift_card_code=gift_card_code,
                reservation_id=reservation_id,
                staff_id=staff_id,
                payment_method=payment_method,
                tip_cents=tip_cents,
                mark_paid=mark_paid,
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
        except gift_cards_svc.GiftCardError as exc:
            error = str(exc)
        except pos_svc.ReservationNotFound:
            error = "找不到該預約，或預約已取消。"
        except pos_svc.ReservationAlreadyCheckedOut:
            error = "這筆預約已經結帳，請勿重複收款。"
        except pos_svc.StaffNotFound:
            error = "找不到指定員工，或員工已停用。"
        except pos_svc.StaffRequired:
            error = "此店已啟用員工抽成，完成收款前請選擇銷售／服務員工。"
        except HTTPException as exc:
            error = str(exc.detail)

    if error is not None:
        # checkout 會先鎖庫存／點數再驗後續條件；任何錯誤都必須整筆回滾。
        db.rollback()

    extra = {"phone": phone}
    if error is not None:
        extra.update(
            selected_reservation_id=reservation_id,
            selected_staff_id=staff_id,
            submitted_qty=submitted_qty,
            submitted_coupon_code=coupon_code or "",
            submitted_points=points_to_redeem,
            submitted_tip_twd=(form.get("tip_twd") or "0"),
            submitted_payment_method=payment_method or "cash",
            submitted_mark_paid=mark_paid,
        )
    if customer_id is not None:
        result = (
            pos_svc.lookup_by_phone(db, tenant_id=tid, phone=phone) if phone else None
        )
        if result is not None:
            extra.update(
                lookup_done=True,
                customer=result["customer"],
                points_balance=result["points_balance"],
                active_coupons=result["active_coupons"],
                gift_card_balance_cents=result["gift_card_balance_cents"],
            )
    return templates.TemplateResponse(
        "_pos.html", _pos_ctx(request, actor, db, order=order, error=error, **extra)
    )


# ── 店家 owner：員工抽成與薪資結算 ───────────────────────────────────────────


def _commissions_ctx(
    request: Request,
    actor: Actor,
    db: Session,
    *,
    pay_run_id: int | None = None,
    **extra,
) -> dict:
    tid = actor.user.tenant_id
    staff = staff_svc.list_staff(db, tenant_id=tid)
    runs = commissions_svc.list_pay_runs(db, tenant_id=tid)
    selected = None
    selected_items = []
    if pay_run_id is not None:
        try:
            selected = commissions_svc.get_pay_run(
                db, tenant_id=tid, pay_run_id=pay_run_id
            )
            selected_items = commissions_svc.pay_run_items(
                db, tenant_id=tid, pay_run_id=selected.id
            )
        except commissions_svc.CommissionError:
            pass
    today = datetime.datetime.now(datetime.timezone.utc).date()
    rules = commissions_svc.latest_rules(db, tenant_id=tid)
    tier_map = {
        rule.id: commissions_svc.rule_tiers(db, tenant_id=tid, rule_id=rule.id)
        for rule in rules.values()
        if rule.structure == "tiered"
    }
    return _ctx(
        request,
        actor,
        staff=staff,
        staff_by_id={row.id: row for row in staff},
        commission_rules=rules,
        commission_tiers=tier_map,
        goal_progress=commissions_svc.sales_goal_progress(
            db, tenant_id=tid, on_date=today
        ),
        pay_runs=runs,
        selected_pay_run=selected,
        selected_pay_run_items=selected_items,
        recent_earnings=commissions_svc.recent_earnings(db, tenant_id=tid),
        today=today,
        month_start=today.replace(day=1),
        **extra,
    )


def _commission_feature_or_locked(request: Request, actor: Actor, db: Session):
    if not _require_ui_feature(db, actor, features_svc.STAFF_COMMISSIONS):
        return _feature_locked(
            request, actor, features_svc.STAFF_COMMISSIONS, "員工抽成／薪資結算"
        )
    return None


def _money_to_cents(raw: str, *, allow_negative: bool = False) -> int:
    try:
        value = Decimal(raw.strip())
    except (InvalidOperation, AttributeError):
        raise commissions_svc.CommissionError("金額格式不正確。") from None
    if not value.is_finite():
        raise commissions_svc.CommissionError("金額格式不正確。")
    if not allow_negative and value < 0:
        raise commissions_svc.CommissionError("金額不可為負數。")
    return int((value * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


@router.get("/commissions", response_class=HTMLResponse)
def commissions_page(
    request: Request,
    pay_run_id: int | None = Query(None),
    saved: int = Query(0),
    actor: Actor = Depends(require_ui_owner),
    db: Session = Depends(get_db),
):
    locked = _commission_feature_or_locked(request, actor, db)
    if locked:
        return locked
    return templates.TemplateResponse(
        "commissions.html",
        _commissions_ctx(
            request, actor, db, pay_run_id=pay_run_id, saved=bool(saved)
        ),
    )


@router.post("/commissions/rules", response_class=HTMLResponse)
def commissions_rule_save(
    request: Request,
    staff_id: int = Form(...),
    item_type: str = Form(...),
    method: str = Form(...),
    value: str = Form(..., max_length=32),
    calculation_basis: str = Form("net"),
    effective_from: datetime.date = Form(...),
    actor: Actor = Depends(require_ui_owner),
    db: Session = Depends(get_db),
):
    locked = _commission_feature_or_locked(request, actor, db)
    if locked:
        return locked
    try:
        if method == "percent":
            decimal_value = Decimal(value.strip())
            if not decimal_value.is_finite():
                raise commissions_svc.CommissionError("抽成數值格式不正確。")
            stored_value = int(
                (decimal_value * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
            )
        else:
            stored_value = _money_to_cents(value)
        row = commissions_svc.save_rule(
            db,
            tenant_id=actor.user.tenant_id,
            staff_id=staff_id,
            item_type=item_type,
            method=method,
            value=stored_value,
            calculation_basis=calculation_basis,
            effective_from=effective_from,
            actor_user_id=actor.user.id,
        )
        audit_svc.record_from_actor(
            db,
            actor,
            action="commissions.rule.create",
            target=f"commission_rule:{row.id}",
            detail={"staff_id": staff_id, "item_type": item_type, "effective_from": effective_from.isoformat()},
            request=request,
        )
        db.commit()
    except (InvalidOperation, commissions_svc.CommissionError) as exc:
        db.rollback()
        message = "抽成數值格式不正確。" if isinstance(exc, InvalidOperation) else str(exc)
        return templates.TemplateResponse(
            "commissions.html",
            _commissions_ctx(request, actor, db, error=message),
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    return RedirectResponse("/ui/commissions?saved=1", status_code=303)


@router.post("/commissions/tiered-rules", response_class=HTMLResponse)
async def commissions_tiered_rule_save(
    request: Request,
    actor: Actor = Depends(require_ui_owner),
    db: Session = Depends(get_db),
):
    locked = _commission_feature_or_locked(request, actor, db)
    if locked:
        return locked
    form = await request.form()
    try:
        staff_id = int(form.get("staff_id") or 0)
        method = str(form.get("method") or "")
        tiers: list[tuple[int, int]] = []
        for index in range(10):
            threshold_raw = str(form.get(f"threshold_{index}") or "").strip()
            value_raw = str(form.get(f"tier_value_{index}") or "").strip()
            if not threshold_raw and not value_raw:
                continue
            if not threshold_raw or not value_raw:
                raise commissions_svc.CommissionError(
                    "每個級距都必須填寫門檻與抽成值。"
                )
            threshold = _money_to_cents(threshold_raw)
            if method == "percent":
                decimal_value = Decimal(value_raw)
                if not decimal_value.is_finite():
                    raise commissions_svc.CommissionError("抽成數值格式不正確。")
                stored_value = int(
                    (decimal_value * 100).quantize(
                        Decimal("1"), rounding=ROUND_HALF_UP
                    )
                )
            else:
                stored_value = _money_to_cents(value_raw)
            tiers.append((threshold, stored_value))
        row = commissions_svc.save_tiered_rule(
            db,
            tenant_id=actor.user.tenant_id,
            staff_id=staff_id,
            item_type=str(form.get("item_type") or ""),
            method=method,
            tiers=tiers,
            calculation_basis=str(form.get("calculation_basis") or "net"),
            sales_period=str(form.get("sales_period") or "monthly"),
            effective_from=datetime.date.fromisoformat(
                str(form.get("effective_from") or "")
            ),
            actor_user_id=actor.user.id,
        )
        audit_svc.record_from_actor(
            db,
            actor,
            action="commissions.tiered_rule.create",
            target=f"commission_rule:{row.id}",
            detail={"staff_id": staff_id, "tiers": len(tiers)},
            request=request,
        )
        db.commit()
    except (ValueError, InvalidOperation, commissions_svc.CommissionError) as exc:
        db.rollback()
        message = (
            str(exc)
            if isinstance(exc, commissions_svc.CommissionError)
            else "階梯抽成格式不正確。"
        )
        return templates.TemplateResponse(
            "commissions.html",
            _commissions_ctx(request, actor, db, error=message),
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    return RedirectResponse("/ui/commissions?saved=1", status_code=303)


@router.post("/commissions/goals", response_class=HTMLResponse)
def commissions_goal_save(
    request: Request,
    staff_id: int = Form(...),
    item_type: str = Form("all"),
    target_twd: str = Form(..., max_length=32),
    sales_period: str = Form("monthly"),
    effective_from: datetime.date = Form(...),
    actor: Actor = Depends(require_ui_owner),
    db: Session = Depends(get_db),
):
    locked = _commission_feature_or_locked(request, actor, db)
    if locked:
        return locked
    try:
        goal = commissions_svc.save_sales_goal(
            db,
            tenant_id=actor.user.tenant_id,
            staff_id=staff_id,
            item_type=item_type,
            target_cents=_money_to_cents(target_twd),
            sales_period=sales_period,
            effective_from=effective_from,
            actor_user_id=actor.user.id,
        )
        audit_svc.record_from_actor(
            db,
            actor,
            action="commissions.goal.create",
            target=f"staff_sales_goal:{goal.id}",
            detail={"staff_id": staff_id, "target_cents": goal.target_cents},
            request=request,
        )
        db.commit()
    except commissions_svc.CommissionError as exc:
        db.rollback()
        return templates.TemplateResponse(
            "commissions.html",
            _commissions_ctx(request, actor, db, error=str(exc)),
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    return RedirectResponse("/ui/commissions?saved=1", status_code=303)


def _commission_csv_response(rows: list[list], filename: str) -> Response:
    def safe_cell(value):
        # 避免員工名稱／商品名稱被 Excel 當成公式執行。
        if isinstance(value, str) and value.startswith(
            ("=", "+", "-", "@", "\t", "\r")
        ):
            return "'" + value
        return value

    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerows([[safe_cell(cell) for cell in row] for row in rows])
    return Response(
        content=output.getvalue().encode("utf-8-sig"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/commissions/pay-runs/{pay_run_id}/export.csv")
def commissions_pay_run_export(
    pay_run_id: int,
    request: Request,
    actor: Actor = Depends(require_ui_owner),
    db: Session = Depends(get_db),
):
    locked = _commission_feature_or_locked(request, actor, db)
    if locked:
        return locked
    try:
        run, data = commissions_svc.pay_run_export_data(
            db, tenant_id=actor.user.tenant_id, pay_run_id=pay_run_id
        )
    except commissions_svc.CommissionError as exc:
        return Response(str(exc), status_code=status.HTTP_404_NOT_FOUND)
    rows: list[list] = [[
        "結算單", "期間開始", "期間結束", "狀態", "員工",
        "抽成", "小費", "加減項", "應付", "說明",
    ]]
    status_labels = {"draft": "草稿", "finalized": "已確認", "paid": "已付款"}
    for item, staff in data:
        rows.append([
            run.id,
            run.period_start.isoformat(),
            run.period_end.isoformat(),
            status_labels.get(run.status, run.status),
            staff.name if staff else f"員工 #{item.staff_id}",
            f"{item.commission_cents / 100:.2f}",
            f"{item.tip_cents / 100:.2f}",
            f"{item.adjustment_cents / 100:.2f}",
            f"{item.total_cents / 100:.2f}",
            item.adjustment_note or "",
        ])
    return _commission_csv_response(rows, f"pay-run-{run.id}.csv")


@router.get("/commissions/activity.csv")
def commissions_activity_export(
    request: Request,
    period_start: datetime.date = Query(...),
    period_end: datetime.date = Query(...),
    staff_id: str = Query("", max_length=32),
    item_type: str | None = Query(None),
    actor: Actor = Depends(require_ui_owner),
    db: Session = Depends(get_db),
):
    locked = _commission_feature_or_locked(request, actor, db)
    if locked:
        return locked
    try:
        parsed_staff_id = _opt_int(staff_id)
    except ValueError:
        return Response("員工篩選值不正確。", status_code=status.HTTP_400_BAD_REQUEST)
    try:
        earnings = commissions_svc.activity_export_data(
            db,
            tenant_id=actor.user.tenant_id,
            period_start=period_start,
            period_end=period_end,
            staff_id=parsed_staff_id,
            item_type=item_type,
        )
    except commissions_svc.CommissionError as exc:
        return Response(str(exc), status_code=status.HTTP_400_BAD_REQUEST)
    staff = staff_svc.list_staff(db, tenant_id=actor.user.tenant_id)
    staff_by_id = {row.id: row.name for row in staff}
    rows: list[list] = [[
        "成交時間", "員工", "類型", "項目", "原價",
        "淨額", "抽成／小費", "結算單", "沖銷狀態",
    ]]
    for earning in earnings:
        rows.append([
            earning.earned_at.isoformat(),
            staff_by_id.get(earning.staff_id, f"員工 #{earning.staff_id}"),
            earning.item_type,
            earning.item_name_snapshot,
            f"{earning.gross_cents / 100:.2f}",
            f"{earning.net_cents / 100:.2f}",
            f"{earning.commission_cents / 100:.2f}",
            earning.pay_run_id or "",
            "已沖銷" if earning.reversed_at else "",
        ])
    return _commission_csv_response(
        rows,
        f"commission-activity-{period_start.isoformat()}-{period_end.isoformat()}.csv",
    )


@router.post("/commissions/pay-runs", response_class=HTMLResponse)
def commissions_pay_run_create(
    request: Request,
    period_start: datetime.date = Form(...),
    period_end: datetime.date = Form(...),
    actor: Actor = Depends(require_ui_owner),
    db: Session = Depends(get_db),
):
    locked = _commission_feature_or_locked(request, actor, db)
    if locked:
        return locked
    try:
        run = commissions_svc.create_pay_run(
            db,
            tenant_id=actor.user.tenant_id,
            period_start=period_start,
            period_end=period_end,
            actor_user_id=actor.user.id,
        )
        audit_svc.record_from_actor(
            db,
            actor,
            action="commissions.pay_run.create",
            target=f"pay_run:{run.id}",
            detail={"period_start": period_start.isoformat(), "period_end": period_end.isoformat()},
            request=request,
        )
        db.commit()
    except commissions_svc.CommissionError as exc:
        db.rollback()
        return templates.TemplateResponse(
            "commissions.html",
            _commissions_ctx(request, actor, db, error=str(exc)),
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    return RedirectResponse(f"/ui/commissions?pay_run_id={run.id}", status_code=303)


@router.post("/commissions/pay-runs/{pay_run_id}/adjust", response_class=HTMLResponse)
def commissions_pay_run_adjust(
    pay_run_id: int,
    request: Request,
    staff_id: int = Form(...),
    adjustment_twd: str = Form("0", max_length=32),
    note: str = Form("", max_length=500),
    actor: Actor = Depends(require_ui_owner),
    db: Session = Depends(get_db),
):
    locked = _commission_feature_or_locked(request, actor, db)
    if locked:
        return locked
    try:
        row = commissions_svc.update_adjustment(
            db,
            tenant_id=actor.user.tenant_id,
            pay_run_id=pay_run_id,
            staff_id=staff_id,
            adjustment_cents=_money_to_cents(adjustment_twd, allow_negative=True),
            note=note,
        )
        audit_svc.record_from_actor(
            db,
            actor,
            action="commissions.pay_run.adjust",
            target=f"pay_run:{pay_run_id}",
            detail={"staff_id": staff_id, "adjustment_cents": row.adjustment_cents},
            request=request,
        )
        db.commit()
    except commissions_svc.CommissionError as exc:
        db.rollback()
        return templates.TemplateResponse(
            "commissions.html",
            _commissions_ctx(request, actor, db, pay_run_id=pay_run_id, error=str(exc)),
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    return RedirectResponse(f"/ui/commissions?pay_run_id={pay_run_id}", status_code=303)


def _pay_run_transition(
    request: Request,
    actor: Actor,
    db: Session,
    pay_run_id: int,
    action: str,
):
    locked = _commission_feature_or_locked(request, actor, db)
    if locked:
        return locked
    try:
        if action == "finalize":
            commissions_svc.finalize_pay_run(
                db, tenant_id=actor.user.tenant_id, pay_run_id=pay_run_id, actor_user_id=actor.user.id
            )
        elif action == "paid":
            commissions_svc.mark_pay_run_paid(
                db, tenant_id=actor.user.tenant_id, pay_run_id=pay_run_id, actor_user_id=actor.user.id
            )
        else:
            commissions_svc.delete_draft(
                db, tenant_id=actor.user.tenant_id, pay_run_id=pay_run_id
            )
        audit_svc.record_from_actor(
            db,
            actor,
            action=f"commissions.pay_run.{action}",
            target=f"pay_run:{pay_run_id}",
            request=request,
        )
        db.commit()
    except commissions_svc.CommissionError as exc:
        db.rollback()
        return templates.TemplateResponse(
            "commissions.html",
            _commissions_ctx(request, actor, db, pay_run_id=pay_run_id, error=str(exc)),
            status_code=status.HTTP_409_CONFLICT,
        )
    target = (
        "/ui/commissions"
        if action == "delete"
        else f"/ui/commissions?pay_run_id={pay_run_id}"
    )
    return RedirectResponse(target, status_code=303)


@router.post("/commissions/pay-runs/{pay_run_id}/finalize", response_class=HTMLResponse)
def commissions_pay_run_finalize(
    pay_run_id: int,
    request: Request,
    actor: Actor = Depends(require_ui_owner),
    db: Session = Depends(get_db),
):
    return _pay_run_transition(request, actor, db, pay_run_id, "finalize")


@router.post("/commissions/pay-runs/{pay_run_id}/paid", response_class=HTMLResponse)
def commissions_pay_run_paid(
    pay_run_id: int,
    request: Request,
    actor: Actor = Depends(require_ui_owner),
    db: Session = Depends(get_db),
):
    return _pay_run_transition(request, actor, db, pay_run_id, "paid")


@router.post("/commissions/pay-runs/{pay_run_id}/delete", response_class=HTMLResponse)
def commissions_pay_run_delete(
    pay_run_id: int,
    request: Request,
    actor: Actor = Depends(require_ui_owner),
    db: Session = Depends(get_db),
):
    return _pay_run_transition(request, actor, db, pay_run_id, "delete")


# ── 店家自助：AI 客服 / FAQ（AI_ASSISTANT） ─────────────────────────────────────


def _faq_ctx(request: Request, actor: Actor, db: Session, **extra) -> dict:
    tid = actor.user.tenant_id
    extra.setdefault("editing_id", None)
    return _ctx(
        request,
        actor,
        faqs=faq_svc.list_faqs(db, tenant_id=tid),
        unanswered=faq_svc.list_unanswered(db, tenant_id=tid),
        **extra,
    )


@router.post("/faq/unanswered/{unanswered_id}/convert", response_class=HTMLResponse)
def faq_unanswered_convert(
    unanswered_id: int,
    request: Request,
    answer: str = Form(...),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.AI_ASSISTANT):
        return _feature_locked(request, actor, features_svc.AI_ASSISTANT, "AI 客服")
    error = None
    try:
        faq_svc.convert_unanswered(
            db,
            tenant_id=actor.user.tenant_id,
            unanswered_id=unanswered_id,
            answer=answer,
        )
    except HTTPException as exc:
        error = str(exc.detail)
    return templates.TemplateResponse(
        "faq.html", _faq_ctx(request, actor, db, error=error)
    )


@router.post("/faq/unanswered/{unanswered_id}/dismiss", response_class=HTMLResponse)
def faq_unanswered_dismiss(
    unanswered_id: int,
    request: Request,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.AI_ASSISTANT):
        return _feature_locked(request, actor, features_svc.AI_ASSISTANT, "AI 客服")
    faq_svc.dismiss_unanswered(
        db, tenant_id=actor.user.tenant_id, unanswered_id=unanswered_id
    )
    return templates.TemplateResponse("faq.html", _faq_ctx(request, actor, db))


@router.get("/faq", response_class=HTMLResponse)
def faq_page(
    request: Request,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.AI_ASSISTANT):
        return _feature_locked(request, actor, features_svc.AI_ASSISTANT, "AI 客服")
    return templates.TemplateResponse("faq.html", _faq_ctx(request, actor, db))


@router.get("/faq/list", response_class=HTMLResponse)
def faq_list_partial(
    request: Request,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    """FAQ 清單 partial；供編輯列取消時還原，避免把完整頁面嵌進卡片。"""
    if not _require_ui_feature(db, actor, features_svc.AI_ASSISTANT):
        return _feature_locked(request, actor, features_svc.AI_ASSISTANT, "AI 客服")
    return templates.TemplateResponse("_faq_list.html", _faq_ctx(request, actor, db))


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
    error = None
    try:
        faq_svc.delete_faq(db, tenant_id=tid, faq_id=faq_id)
    except HTTPException as exc:
        error = str(exc.detail)
    return templates.TemplateResponse(
        "_faq_list.html", _faq_ctx(request, actor, db, error=error)
    )


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
    if not _require_ui_feature(db, actor, features_svc.AI_ASSISTANT):
        error = "AI 客服未開通（專業版功能）。"
    elif len(question) > _AI_QUESTION_MAX_LEN:
        error = f"問題過長（上限 {_AI_QUESTION_MAX_LEN} 字），請精簡後再試。"
    else:
        assistant = get_assistant(db)
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
            _ctx(
                request,
                actor,
                question=question,
                answer=answer,
                source=source,
                error=error,
            ),
        )
    assistant = get_assistant(db)
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
        _ctx(
            request, actor, question=question, answer=answer, source=source, error=error
        ),
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
        messages = line_chat_svc.list_messages(db, tenant_id=tid, line_user_id=selected)
        for c in conversations:
            if c["line_user_id"] == selected:
                selected_name = c["display_name"]
                break
    return templates.TemplateResponse(
        "line_chat.html",
        _ctx(
            request,
            actor,
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
                    tid,
                    "line_message",
                    line_user_id=line_user_id,
                    text=text,
                    direction="out",
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
    gcal_retry_queued: int = Query(default=0, ge=0),
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

    # E1:GCal 連結狀態(指引卡用)。
    from saas_mvp.models.tenant_gcal_credential import TenantGcalCredential

    gcal_cred = db.execute(
        select(TenantGcalCredential).where(TenantGcalCredential.tenant_id == tid)
    ).scalar_one_or_none()
    from saas_mvp.services import gcal as gcal_svc

    gcal_sync_summary = gcal_svc.summary(db, tid)

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
            request,
            actor,
            gcal_cred=gcal_cred,
            gcal_sync_summary=gcal_sync_summary,
            gcal_retry_queued=gcal_retry_queued,
            view=view,
            mode=mode,
            today=today,
            month_data=month_data,
            week_data=week_data,
            staff_grid=staff_grid,
        ),
    )
