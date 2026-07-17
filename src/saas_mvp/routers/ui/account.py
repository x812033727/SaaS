"""UI 子模組(P4 純搬移自 routers/ui.py):成員管理(RBAC)+ Google Calendar 連結 + 店家自助 dashboard + 開店精靈 + 帳號/變更密碼。"""
from __future__ import annotations

import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import Depends, Form, Query, Request, status
from fastapi.responses import (
    HTMLResponse,
    RedirectResponse,
)
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
from saas_mvp.auth.security import create_access_token, hash_password, verify_password
from saas_mvp.line_client import (
    LineBotInfoClient,
    LineWebhookAdminClient,
    LineWebhookAdminError,
    get_bot_info_client,
    get_webhook_admin_client,
)
from saas_mvp.models.tenant import Tenant, normalize_store_type
from saas_mvp.models.user import User
from saas_mvp.quota import get_quota_status
from saas_mvp.services import audit as audit_svc
from saas_mvp.services import line_config as line_config_svc
from saas_mvp.services import onboarding as onboarding_svc
from saas_mvp.services import oauth as oauth_svc
from saas_mvp.services import platform_oauth_config as platform_oauth_svc
from saas_mvp.services import plans as plans_svc
from saas_mvp.services import push_quota as push_quota_svc
from saas_mvp.services import members as members_svc
from saas_mvp.services import totp as totp_svc
from saas_mvp.auth.ratelimit import otp_limiter
from fastapi import HTTPException

from saas_mvp.routers.ui._shared import (
    router, templates, _ctx, _line_config_or_none, _line_webhook_url_for, _log,
    _set_auth_cookie, _CSRF_COOKIE_NAME,
)
from saas_mvp.routers.ui.billing import _plan_info, _line_insights

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


def _render_members(request, actor, db, *, notice=None, error=None, status_code=200):
    return templates.TemplateResponse(
        "members.html",
        _ctx(
            request, actor,
            members=members_svc.list_members(db, actor.user.tenant_id),
            invite_url=None,
            member_notice=notice,
            member_error=error,
        ),
        status_code=status_code,
    )


@router.post("/members/{user_id}/role", response_class=HTMLResponse)
def members_set_role(
    user_id: int,
    request: Request,
    role: str = Form(...),
    actor: Actor = Depends(require_ui_owner),
    db: Session = Depends(get_db),
):
    try:
        target = members_svc.set_role(db, actor.user, user_id, role)
    except members_svc.MemberActionError as exc:
        return _render_members(request, actor, db, error=str(exc), status_code=400)
    audit_svc.record_from_actor(
        db, actor, action="member.role", target=f"user:{user_id}",
        detail={"role": role}, request=request,
    )
    db.commit()
    return _render_members(
        request, actor, db, notice=f"已將 {target.email} 設為{'負責人' if role == 'owner' else '員工'}。"
    )


@router.post("/members/{user_id}/disable", response_class=HTMLResponse)
def members_disable(
    user_id: int,
    request: Request,
    actor: Actor = Depends(require_ui_owner),
    db: Session = Depends(get_db),
):
    try:
        target = members_svc.disable_member(db, actor.user, user_id)
    except members_svc.MemberActionError as exc:
        return _render_members(request, actor, db, error=str(exc), status_code=400)
    audit_svc.record_from_actor(
        db, actor, action="member.disable", target=f"user:{user_id}", request=request,
    )
    db.commit()
    return _render_members(request, actor, db, notice=f"已停用 {target.email},該成員已被登出。")


@router.post("/members/{user_id}/enable", response_class=HTMLResponse)
def members_enable(
    user_id: int,
    request: Request,
    actor: Actor = Depends(require_ui_owner),
    db: Session = Depends(get_db),
):
    try:
        target = members_svc.enable_member(db, actor.user, user_id)
    except members_svc.MemberActionError as exc:
        return _render_members(request, actor, db, error=str(exc), status_code=400)
    audit_svc.record_from_actor(
        db, actor, action="member.enable", target=f"user:{user_id}", request=request,
    )
    db.commit()
    return _render_members(request, actor, db, notice=f"已啟用 {target.email}。")


@router.post("/members/{user_id}/remove", response_class=HTMLResponse)
def members_remove(
    user_id: int,
    request: Request,
    actor: Actor = Depends(require_ui_owner),
    db: Session = Depends(get_db),
):
    # audit 先記(target 刪除後就查不到 email);移除成功才 commit audit。
    try:
        members_svc.remove_member(db, actor.user, user_id)
    except members_svc.MemberActionError as exc:
        return _render_members(request, actor, db, error=str(exc), status_code=400)
    audit_svc.record_from_actor(
        db, actor, action="member.remove", target=f"user:{user_id}", request=request,
    )
    db.commit()
    return _render_members(request, actor, db, notice="已移除該成員。")


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

    jwt_token = create_access_token(
        user_id=user.id, tenant_id=user.tenant_id,
        token_version=user.token_version,
    )
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
    # R4-B3:Google 日曆漂移(店家在 Google 端改/刪同步事件)未處理筆數,揭露於卡片。
    from sqlalchemy import func
    from saas_mvp.models.reservation import RESERVATION_CONFIRMED, Reservation
    gcal_drift_count = db.execute(
        select(func.count())
        .select_from(Reservation)
        .where(
            Reservation.tenant_id == tid,
            Reservation.gcal_drift_detected_at.is_not(None),
            Reservation.status == RESERVATION_CONFIRMED,
        )
    ).scalar_one()
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
            gcal_drift_count=gcal_drift_count,
            email_unverified=actor.user.email_verified_at is None,
            verification_resent=bool(verification_resent),
            verification_error=bool(verification_error),
            verification_queued=bool(verification_queued),
            verification_rate_limited=bool(verification_rate_limited),
            line_insights=_line_insights(db, tid),
        ),
    )


# ── R4-B4:開店精靈 + 一鍵示範資料 ───────────────────────────────────────────

@router.get("/onboarding", response_class=HTMLResponse)
def onboarding_wizard(
    request: Request,
    demo_loaded: int = Query(0),
    demo_cleared: int = Query(0),
    demo_error: int = Query(0),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    from saas_mvp.services import demo_data as demo_svc

    tenant = db.get(Tenant, actor.user.tenant_id)
    checklist = onboarding_svc.checklist(db, tenant=tenant, user=actor.user)
    return templates.TemplateResponse(
        "onboarding.html",
        _ctx(
            request,
            actor,
            tenant=tenant,
            onboarding=checklist,
            onboarding_done=onboarding_svc.all_done(checklist),
            has_demo=demo_svc.has_demo(db, tenant.id),
            demo_loaded=bool(demo_loaded),
            demo_cleared=bool(demo_cleared),
            demo_error=bool(demo_error),
        ),
    )


@router.post("/onboarding/demo-data")
def onboarding_demo_load(
    actor: Actor = Depends(require_ui_owner),
    db: Session = Depends(get_db),
):
    from saas_mvp.services import demo_data as demo_svc

    try:
        demo_svc.load_demo(db, actor.user.tenant_id)
    except Exception:  # noqa: BLE001 — 示範資料失敗不可讓精靈頁 500
        _log.warning("demo data load failed tenant=%s", actor.user.tenant_id, exc_info=True)
        return RedirectResponse(
            "/ui/onboarding?demo_error=1", status_code=status.HTTP_303_SEE_OTHER
        )
    return RedirectResponse(
        "/ui/onboarding?demo_loaded=1", status_code=status.HTTP_303_SEE_OTHER
    )


@router.post("/onboarding/demo-data/clear")
def onboarding_demo_clear(
    actor: Actor = Depends(require_ui_owner),
    db: Session = Depends(get_db),
):
    from saas_mvp.services import demo_data as demo_svc

    try:
        demo_svc.clear_demo(db, actor.user.tenant_id)
    except Exception:  # noqa: BLE001
        _log.warning("demo data clear failed tenant=%s", actor.user.tenant_id, exc_info=True)
        return RedirectResponse(
            "/ui/onboarding?demo_error=1", status_code=status.HTTP_303_SEE_OTHER
        )
    return RedirectResponse(
        "/ui/onboarding?demo_cleared=1", status_code=status.HTTP_303_SEE_OTHER
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
    logged_out_all: str | None = Query(default=None),
):
    # 綁定結果（由 /auth/oauth/.../callback 導回時帶 query 參數）轉成可顯示文案。
    linked_label = _OAUTH_PROVIDER_LABELS.get(linked or "")
    provider_label = _OAUTH_PROVIDER_LABELS.get(actor.user.oauth_provider or "")
    # 上次登入（R5-D1）：DB 存 UTC（SQLite 取出為 naive），顯示轉台北時間。
    last_login_display = None
    if actor.user.last_login_at is not None:
        _dt = actor.user.last_login_at
        if _dt.tzinfo is None:
            _dt = _dt.replace(tzinfo=datetime.timezone.utc)
        last_login_display = _dt.astimezone(ZoneInfo("Asia/Taipei")).strftime(
            "%Y-%m-%d %H:%M"
        )
    return templates.TemplateResponse(
        "account.html",
        _ctx(
            request,
            actor,
            last_login_display=last_login_display,
            last_login_ip=actor.user.last_login_ip,
            totp_on=actor.user.totp_enabled,
            remaining_codes=(
                totp_svc.remaining_recovery_codes(db, actor.user)
                if actor.user.totp_enabled else 0
            ),
            logged_out_all=logged_out_all,
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


# ── TOTP 2FA 註冊/停用(R5-D2;HTMX partial 比照密碼卡)──────────────────────


def _totp_partial(request: Request, actor: Actor, db: Session, **extra):
    base = {
        "totp_on": actor.user.totp_enabled,
        "remaining_codes": (
            totp_svc.remaining_recovery_codes(db, actor.user)
            if actor.user.totp_enabled else 0
        ),
    }
    base.update(extra)
    return templates.TemplateResponse(
        "_account_totp.html", _ctx(request, actor, **base)
    )


@router.post("/account/totp/start", response_class=HTMLResponse)
def account_totp_start(
    request: Request,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if actor.user.totp_enabled:
        return _totp_partial(request, actor, db)
    secret = totp_svc.start_enrollment(db, actor.user)
    uri = totp_svc.provisioning_uri(actor.user, secret)
    return _totp_partial(
        request, actor, db,
        totp_pending=True,
        qr_svg=totp_svc.qr_svg(uri),
        totp_secret_manual=secret,
    )


@router.post("/account/totp/confirm", response_class=HTMLResponse)
def account_totp_confirm(
    request: Request,
    otp: str = Form(...),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if actor.user.totp_enabled:
        return _totp_partial(request, actor, db)
    if not actor.user.totp_secret_enc:
        return _totp_partial(request, actor, db, error="請先點「啟用兩步驟驗證」。")
    codes = totp_svc.confirm_enrollment(db, actor.user, otp)
    if codes is None:
        uri = totp_svc.provisioning_uri(actor.user)
        return _totp_partial(
            request, actor, db,
            totp_pending=True,
            qr_svg=totp_svc.qr_svg(uri),
            totp_secret_manual=actor.user.totp_secret,
            error="驗證碼錯誤，請確認 App 時間同步後重試。",
        )
    audit_svc.record_from_actor(
        db, actor, action="auth.mfa.enable", request=request
    )
    db.commit()
    return _totp_partial(request, actor, db, recovery_codes=codes)


@router.post("/account/totp/disable", response_class=HTMLResponse)
def account_totp_disable(
    request: Request,
    otp: str = Form(...),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not actor.user.totp_enabled:
        return _totp_partial(request, actor, db)
    if settings.rate_limit_enabled:
        try:
            otp_limiter._check_rate_limit(f"user:{actor.user.id}")
        except HTTPException as exc:
            if exc.status_code != status.HTTP_429_TOO_MANY_REQUESTS:
                raise
            return _totp_partial(
                request, actor, db, error="嘗試次數過多，請 5 分鐘後再試。"
            )
    if not totp_svc.disable(db, actor.user, otp):
        return _totp_partial(request, actor, db, error="驗證碼錯誤，未停用。")
    audit_svc.record_from_actor(
        db, actor, action="auth.mfa.disable", request=request
    )
    db.commit()
    return _totp_partial(request, actor, db)


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
    # R5-D3:改密碼 = token_version+1 撤銷所有既有票。本裝置例外——重簽新 cookie
    # 讓操作者留在登入態(否則自己剛改完密碼下一個請求就被踢登出)。
    user.token_version = (user.token_version or 0) + 1
    db.add(user)
    db.commit()
    resp = templates.TemplateResponse(
        "_account_password.html",
        _ctx(request, actor, saved=True),
    )
    # csrf_value 沿用舊值不輪替:本回應是 HTMX partial,不會重繪頁面既有表單的
    # csrf hidden field / hx-headers;輪替會讓同頁下個動作 CSRF 不符被 403。
    _set_auth_cookie(
        resp,
        create_access_token(
            user_id=user.id, tenant_id=user.tenant_id,
            token_version=user.token_version,
        ),
        csrf_value=request.cookies.get(_CSRF_COOKIE_NAME) or None,
    )
    return resp


@router.post("/account/logout-all", response_class=HTMLResponse)
def account_logout_all(
    request: Request,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    """登出所有裝置:token_version+1 撤銷所有既有票;本裝置重簽新 cookie 續留。"""
    user = db.get(User, actor.user.id)
    members_svc.logout_all_devices(db, user)
    audit_svc.record_from_actor(
        db, actor, action="auth.logout_all", request=request,
    )
    db.commit()
    resp = RedirectResponse("/ui/account?logged_out_all=1", status_code=status.HTTP_303_SEE_OTHER)
    _set_auth_cookie(
        resp,
        create_access_token(
            user_id=user.id, tenant_id=user.tenant_id,
            token_version=user.token_version,
        ),
    )
    return resp


