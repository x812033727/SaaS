"""UI 共用基座(P4 純搬移自 routers/ui.py):imports/templates/CSRF/共用工具。

router 與 templates 為整個 ui package 的唯一實例;各子模組 import 本模組後
對同一 router 掛路由,__init__ 按原區段順序 import 保證路由註冊順序不變。
"""
from __future__ import annotations

import hmac
import secrets
import logging
from pathlib import Path

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import (
    Response,
)
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from saas_mvp.config import settings
from saas_mvp.deps import (
    Actor,
)
from saas_mvp.auth.dependencies import UILoginRequired, _UI_COOKIE_NAME
from saas_mvp.routers.line_webhook import webhook_url_for
from saas_mvp.services import features as features_svc
from saas_mvp.services import line_config as line_config_svc
from fastapi import HTTPException

_PKG_DIR = Path(__file__).resolve().parent.parent.parent  # src/saas_mvp（P4:多一層 ui/）
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


_log = logging.getLogger(__name__)


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



# ── 共用：選填整數解析 ──────────────────────────────────────────────────────────


def _opt_int(value: str) -> int | None:
    """空字串 → None；否則轉 int（非法拋 ValueError，由呼叫端轉 error）。"""
    value = (value or "").strip()
    return int(value) if value else None


def _require_ui_feature(db: Session, actor: Actor, feature: str) -> bool:
    return features_svc.is_enabled(db, actor.user.tenant_id, feature)

