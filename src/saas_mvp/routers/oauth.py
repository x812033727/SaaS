"""OAuth 登入 router（PHASE 3：LINE Login + Google），以 email 不分大小寫連結帳號。

流程：
  GET /auth/oauth/{provider}/login    → 設 state cookie（CSRF）+ 302 導向授權頁。
  GET /auth/oauth/{provider}/callback → 驗 state、exchange_code、連結/登入、導回 /ui/。

帳號連結規則（CRITICAL — 維持既有安全模型）：
  callback 以 email（不分大小寫）查使用者。
    * 已存在  → 補上 oauth_provider/oauth_subject（若尚未設）並登入（設與 ui.py
                同一個 httpOnly cookie）。
    * 不存在  → **不**自動建立租戶（會違反 routers/auth.py 的「租戶名為建立者
                專屬」規則，等同任何 Google 帳號都能憑空開新店家）。改回 403，
                要求該 email 須先註冊或受邀。

provider ∈ {line, google}；未知 provider 回 404。
"""

from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from saas_mvp.auth.security import create_access_token
from saas_mvp.config import settings
from saas_mvp.db import get_db
from saas_mvp.models.user import User
from saas_mvp.routers.ui import _set_auth_cookie
from saas_mvp.services import oauth as oauth_svc

router = APIRouter(prefix="/auth/oauth", tags=["oauth"], include_in_schema=False)

_STATE_COOKIE_NAME = "oauth_state"


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
def oauth_login(provider: str):
    _validate_provider(provider)
    state = secrets.token_urlsafe(24)
    redirect_uri = _redirect_uri(provider)
    p = oauth_svc.get_provider(provider, settings=settings)
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
    return resp


@router.get("/{provider}/callback")
def oauth_callback(
    provider: str,
    request: Request,
    code: str | None = None,
    state: str | None = None,
    db: Session = Depends(get_db),
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

    p = oauth_svc.get_provider(provider, settings=settings)
    try:
        identity = p.exchange_code(code, _redirect_uri(provider))
    except oauth_svc.OAuthError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="OAuth exchange failed"
        )

    email = identity["email"]
    subject = identity["subject"]

    # 以 email 不分大小寫查使用者。
    user = (
        db.query(User)
        .filter(func.lower(User.email) == email.lower())
        .first()
    )

    # 帳號連結規則（見模組 docstring）：找不到使用者時，**絕不**自動建立租戶，
    # 否則任何 OAuth 帳號都能憑空開新店家、繞過 routers/auth.py 的租戶名專屬保護。
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="此電子郵件尚未註冊，請先以該信箱註冊或請店家邀請後再使用社群登入。",
        )

    # 補綁外部身分（僅在尚未設定時），不覆寫既有連結。
    if not user.oauth_provider:
        user.oauth_provider = provider
        user.oauth_subject = subject
        db.commit()

    token = create_access_token(user_id=user.id, tenant_id=user.tenant_id)
    resp = RedirectResponse("/ui/", status_code=status.HTTP_303_SEE_OTHER)
    _set_auth_cookie(resp, token)
    resp.delete_cookie(_STATE_COOKIE_NAME, path="/")
    return resp
