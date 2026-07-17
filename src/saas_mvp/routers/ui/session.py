"""UI 子模組(P4 純搬移自 routers/ui.py):公開:登入/註冊/登出 + Email 驗證/忘記密碼。"""
from __future__ import annotations


from fastapi import Depends, Form, Request, status
from fastapi.responses import (
    HTMLResponse,
    RedirectResponse,
)
from sqlalchemy.orm import Session

from saas_mvp.config import settings
from saas_mvp.deps import (
    Actor,
    get_db,
    require_ui_user,
)
from saas_mvp.auth.dependencies import _UI_COOKIE_NAME
from saas_mvp.auth.security import create_access_token, hash_password, verify_password
from saas_mvp.models.tenant import Tenant
from saas_mvp.models.user import User
from saas_mvp.services import account_email as account_email_svc
from saas_mvp.services import login_audit
from saas_mvp.services import oauth as oauth_svc
from saas_mvp.services import plans as plans_svc
from saas_mvp.services.mailer import Mailer, get_mailer
from saas_mvp.auth.ratelimit import (
    email_identity_limiter,
    email_ip_limiter,
    email_user_limiter,
)
from fastapi import HTTPException

from saas_mvp.routers.ui._shared import (
    router, templates, _CSRF_COOKIE_NAME,
    _ctx, _set_auth_cookie,
)

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
    mailer: Mailer = Depends(get_mailer),
):
    user = db.query(User).filter(User.email == email).first()
    if not user or not verify_password(password, user.hashed_password):
        login_audit.on_login_failure(db, email=email, request=request)
        # 統一錯誤訊息，避免帳號列舉
        return templates.TemplateResponse(
            "login.html",
            _ctx(request, error="電子郵件或密碼錯誤"),
            status_code=status.HTTP_401_UNAUTHORIZED,
        )
    login_audit.on_login_success(db, user, request, mailer=mailer)
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


