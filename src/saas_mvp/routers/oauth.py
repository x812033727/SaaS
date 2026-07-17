"""OAuth 登入 router（PHASE 3：LINE Login + Google），支援登入與「綁定既有帳號」。

流程：
  GET /auth/oauth/{provider}/login    → 設 state cookie（CSRF）+ 302 導向授權頁。
                                         帶 ?link=1 時另設 intent cookie，callback
                                         會把外部身分綁到「目前已登入」的使用者。
  GET /auth/oauth/{provider}/callback → 驗 state、exchange_code、連結/登入、導回。

兩種模式：
  1. 登入模式（未帶 link，或 callback 當下未登入）：
       先以 (provider, subject) 查使用者（支援「LINE 註冊信箱 ≠ 後台帳號信箱」者
       仍能用 LINE 登入），查無再以 email（不分大小寫）查。
         * 命中  → 補上 oauth_provider/oauth_subject（若尚未設）並登入（設與 ui.py
                   同一個 httpOnly cookie），導回 /ui/。
         * 查無  → **不**自動建立租戶（會違反 routers/auth.py 的「租戶名為建立者
                   專屬」規則，等同任何外部帳號都能憑空開新店家）。改回 403，
                   要求該 email 須先註冊或受邀。
  2. 綁定模式（callback 當下已登入，且 login 時帶過 ?link=1）：
       直接把外部身分綁到「目前登入的使用者」（不靠 email 配對，因 LINE 的
       email 可能與後台帳號不同）。導回 /ui/account。
       帳號接管防護：若該外部身分已被「其他」使用者綁定，拒絕並回 account 頁
       顯示錯誤（避免一個 LINE 身分對應多個後台帳號、登入時語意不明）。

provider ∈ {line, google}；未知 provider 回 404。
"""

from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from saas_mvp.auth.dependencies import get_ui_actor_optional
from saas_mvp.auth.security import create_access_token
from saas_mvp.config import settings
from saas_mvp.db import get_db
from saas_mvp.models.user import User
from saas_mvp.routers.ui import _set_auth_cookie
from saas_mvp.services import login_audit
from saas_mvp.services import oauth as oauth_svc
from saas_mvp.services.mailer import Mailer, get_mailer

router = APIRouter(prefix="/auth/oauth", tags=["oauth"], include_in_schema=False)

_STATE_COOKIE_NAME = "oauth_state"
# 綁定意圖 cookie：login 時帶 ?link=1 設下，callback 讀到且使用者已登入即進綁定模式。
_INTENT_COOKIE_NAME = "oauth_intent"


def _current_ui_user(request: Request, db: Session) -> User | None:
    """讀目前 UI 登入使用者；無 cookie / 無效 / 租戶停用一律回 None（不拋）。"""
    try:
        actor = get_ui_actor_optional(request, db)
    except Exception:  # noqa: BLE001 — 停用租戶等情形視為「未登入」，走登入模式。
        return None
    return actor.user if actor else None


def _validate_provider(provider: str) -> None:
    if provider not in oauth_svc.VALID_PROVIDERS:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Unknown OAuth provider"
        )


def _redirect_uri(provider: str) -> str:
    """組 callback 絕對網址（oauth_redirect_base 優先，否則 public_base_url）。"""
    base = (settings.oauth_redirect_base or settings.public_base_url or "").rstrip("/")
    return f"{base}/auth/oauth/{provider}/callback"


@router.get("/{provider}/login")
def oauth_login(
    provider: str,
    link: int = 0,
    db: Session = Depends(get_db),
):
    _validate_provider(provider)
    state = secrets.token_urlsafe(24)
    redirect_uri = _redirect_uri(provider)
    try:
        p = oauth_svc.get_provider(provider, settings=settings, db=db)
    except oauth_svc.OAuthNotConfigured:
        if link:
            return _account_redirect(oauth_error="not_configured")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"{provider.upper()} OAuth is not configured",
        )
    url = p.authorize_url(state, redirect_uri)

    resp = RedirectResponse(url, status_code=status.HTTP_302_FOUND)
    # state cookie 為 CSRF 防護：callback 比對 query.state 與 cookie。
    resp.set_cookie(
        key=_STATE_COOKIE_NAME,
        value=state,
        httponly=True,
        samesite="lax",
        secure=settings.env not in ("dev", "test"),
        max_age=600,
        path="/",
    )
    # ?link=1（後台「連結 LINE 帳戶」按鈕）：記下綁定意圖，callback 綁到目前登入者。
    if link:
        resp.set_cookie(
            key=_INTENT_COOKIE_NAME,
            value="link",
            httponly=True,
            samesite="lax",
            secure=settings.env not in ("dev", "test"),
            max_age=600,
            path="/",
        )
    return resp


@router.get("/{provider}/callback")
def oauth_callback(
    provider: str,
    request: Request,
    code: str | None = None,
    state: str | None = None,
    db: Session = Depends(get_db),
    mailer: Mailer = Depends(get_mailer),
):
    _validate_provider(provider)

    # CSRF：query.state 必須與先前種下的 state cookie 相符。
    cookie_state = request.cookies.get(_STATE_COOKIE_NAME)
    if not state or not cookie_state or not secrets.compare_digest(state, cookie_state):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid OAuth state"
        )
    if not code:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Missing authorization code"
        )

    try:
        p = oauth_svc.get_provider(provider, settings=settings, db=db)
    except oauth_svc.OAuthNotConfigured:
        if request.cookies.get(_INTENT_COOKIE_NAME) == "link":
            return _account_redirect(oauth_error="not_configured")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"{provider.upper()} OAuth is not configured",
        )
    try:
        identity = p.exchange_code(code, _redirect_uri(provider))
    except oauth_svc.OAuthError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="OAuth exchange failed"
        )

    email = identity.get("email")
    subject = identity["subject"]

    # 綁定模式：login 時帶過 ?link=1，且 callback 當下確實已登入 → 綁到目前使用者。
    intent = request.cookies.get(_INTENT_COOKIE_NAME)
    link_user = _current_ui_user(request, db) if intent == "link" else None
    if link_user is not None:
        # 帳號接管防護：同一外部身分不得綁到多個後台帳號。
        owner = (
            db.query(User)
            .filter(User.oauth_provider == provider, User.oauth_subject == subject)
            .first()
        )
        if owner is not None and owner.id != link_user.id:
            return _account_redirect(oauth_error="in_use")
        link_user.oauth_provider = provider
        link_user.oauth_subject = subject
        db.commit()
        return _account_redirect(linked=provider)

    # 登入模式：先以 (provider, subject) 查（支援 LINE 信箱 ≠ 後台信箱者），再以 email 查。
    user = (
        db.query(User)
        .filter(User.oauth_provider == provider, User.oauth_subject == subject)
        .first()
    )
    if user is None and email:
        user = db.query(User).filter(func.lower(User.email) == email.lower()).first()

    # 帳號連結規則（見模組 docstring）：找不到使用者時，**絕不**自動建立租戶，
    # 否則任何 OAuth 帳號都能憑空開新店家、繞過 routers/auth.py 的租戶名專屬保護。
    if user is None:
        login_audit.on_login_failure(
            db, email=email or subject, request=request, method=f"oauth:{provider}"
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "LINE 未提供可驗證的電子郵件，請先登入後台並從帳號設定連結 LINE 帳戶。"
                if provider == "line" and not email
                else "此電子郵件尚未註冊，請先以該信箱註冊或請店家邀請後再使用社群登入。"
            ),
        )

    # 補綁外部身分（僅在尚未設定時），不覆寫既有連結。
    if not user.oauth_provider:
        user.oauth_provider = provider
        user.oauth_subject = subject
        db.commit()

    login_audit.on_login_success(
        db, user, request, method=f"oauth:{provider}", mailer=mailer
    )
    token = create_access_token(user_id=user.id, tenant_id=user.tenant_id)
    resp = RedirectResponse("/ui/", status_code=status.HTTP_303_SEE_OTHER)
    _set_auth_cookie(resp, token)
    resp.delete_cookie(_STATE_COOKIE_NAME, path="/")
    resp.delete_cookie(_INTENT_COOKIE_NAME, path="/")
    return resp


def _account_redirect(*, linked: str | None = None, oauth_error: str | None = None):
    """綁定模式結束導回 /ui/account（帶結果參數），並清掉 state/intent cookie。"""
    if oauth_error:
        target = f"/ui/account?oauth_error={oauth_error}"
    else:
        target = f"/ui/account?linked={linked}"
    resp = RedirectResponse(target, status_code=status.HTTP_303_SEE_OTHER)
    resp.delete_cookie(_STATE_COOKIE_NAME, path="/")
    resp.delete_cookie(_INTENT_COOKIE_NAME, path="/")
    return resp
