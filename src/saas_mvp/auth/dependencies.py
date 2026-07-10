"""FastAPI dependency: 解析 Bearer/X-API-Key → Actor（含 User + api_key_id）。

三路認證（互斥、窮舉）：
1. X-API-Key header 有值                → API key 路徑
2. Authorization: Bearer myapp_*        → API key 路徑
3. Authorization: Bearer eyJ* 或其他   → JWT 路徑
4. 三者皆無                             → 401

FastAPI 在同一 request 內快取 dependency 結果，
get_current_actor 只呼叫一次（即使被多個子 dependency 依賴）。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Optional

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import APIKeyHeader, OAuth2PasswordBearer
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from saas_mvp.auth.security import PyJWTError, decode_access_token
from saas_mvp.db import get_db
from saas_mvp.models.api_key import ApiKey, _KEY_PREFIX  # 頂層 import，統一常數來源
from saas_mvp.models.user import User

# Bearer scheme — auto_error=False 讓我們自行處理 fallback
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/token", auto_error=False)

# X-API-Key header scheme
_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

_401 = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Could not validate credentials",
    headers={"WWW-Authenticate": "Bearer"},
)


@dataclass
class Actor:
    """已驗證的請求者：一個 User，外加可選的 API key ID。

    impersonator_user_id（F2 代管）:UI cookie 帶合法 ``imp`` claim 時 =
    admin 的 user id;稽核統一由此取得,代管期間所有操作自動記 admin 身分。
    """
    user: User
    api_key_id: Optional[int] = None
    impersonator_user_id: Optional[int] = None


def _resolve_api_key(key_str: str, db: Session) -> Actor:
    """以 prefix 縮候選集 + SHA-256 比對，驗證 API key 並回傳 Actor。"""
    # 格式防衛：key 至少需要 prefix（len(_KEY_PREFIX)字元）+ 8 字元隨機部分
    if len(key_str) < len(_KEY_PREFIX) + 8:
        raise _401

    key_hash = hashlib.sha256(key_str.encode()).hexdigest()
    # P3: 用 _KEY_PREFIX 長度而非 hardcode 6，避免未來 prefix 改長度時靜默偏移
    key_prefix = key_str[len(_KEY_PREFIX):len(_KEY_PREFIX) + 8]

    row = db.execute(
        select(ApiKey).where(
            ApiKey.key_prefix == key_prefix,
            ApiKey.key_hash == key_hash,
            ApiKey.is_active == True,  # noqa: E712
        )
    ).scalar_one_or_none()

    if row is None:
        raise _401

    user = db.execute(
        select(User).where(User.id == row.user_id).options(joinedload(User.tenant))
    ).scalar_one_or_none()
    if user is None:
        raise _401

    return Actor(user=user, api_key_id=row.id)


def get_current_actor(
    x_api_key: Optional[str] = Depends(_api_key_header),
    bearer_token: Optional[str] = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
) -> Actor:
    """核心 dependency：解析請求憑證，回傳 Actor。

    三路認證後統一執行租戶停用檢查（is_active=False → 403，不豁免 admin）。
    """
    # Path 1: X-API-Key header
    if x_api_key:
        if not x_api_key.startswith(_KEY_PREFIX):
            raise _401
        return _check_tenant_active(_resolve_api_key(x_api_key, db))

    # Path 2 & 3: Authorization: Bearer <token>
    if bearer_token:
        if bearer_token.startswith(_KEY_PREFIX):
            # Bearer <api_key>
            return _check_tenant_active(_resolve_api_key(bearer_token, db))
        # JWT 路徑
        try:
            payload = decode_access_token(bearer_token)
            user_id_str: str | None = payload.get("sub")
            if not user_id_str:
                raise _401
            user_id = int(user_id_str)
        except (PyJWTError, ValueError):
            raise _401

        user = db.execute(
            select(User).where(User.id == user_id).options(joinedload(User.tenant))
        ).scalar_one_or_none()
        if user is None:
            raise _401
        return _check_tenant_active(Actor(user=user, api_key_id=None))

    # Path 4: 無憑證
    raise _401


def _check_tenant_active(actor: Actor) -> Actor:
    """租戶停用攔截：is_active=False → 403（不豁免 admin）。"""
    if actor.user.tenant and not actor.user.tenant.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="tenant disabled",
        )
    return actor


def get_current_user(
    actor: Actor = Depends(get_current_actor),
) -> User:
    """向下相容包裝：從 Actor 中取出 User，現有 router 零改動。"""
    return actor.user


# ─────────────────── 伺服器渲染 UI 的 cookie 認證（與 API 路徑隔離） ───────────────────
# 設計：刻意「不」碰 get_current_actor（安全敏感、已窮舉測試）。UI 改用獨立的
# cookie 讀取 dependency，僅重用相同基礎元件（decode_access_token、同一套 User
# 查詢、租戶停用檢查）。如此 API 路徑仍只認 header（cookie-only 請求一律 401），
# 不會把瀏覽器的 CSRF 面引入 API。
#
# 例外用於把「需登入 / 無權限 / 租戶停用」轉成 HTML 行為（重導 / 403 頁），
# 而非 API 的 JSON 401/403；對應的 handler 在 app.py 註冊。

_UI_COOKIE_NAME = "access_token"


class UILoginRequired(Exception):
    """UI 路由未帶有效 cookie → 由 app 層 handler 重導至 /ui/login（303）。"""


class UIForbidden(Exception):
    """已登入但無權限（例如非 admin）→ app 層 handler 回 403 HTML 頁。"""


class UITenantDisabled(Exception):
    """cookie 有效但租戶已停用 → app 層 handler 回 403 停用頁（非重導登入）。"""


def get_ui_actor_optional(
    request: Request,
    db: Session = Depends(get_db),
) -> Optional[Actor]:
    """從 cookie 讀 JWT 解析 Actor；缺失/無效/過期一律回 None（永不拋認證錯）。

    重用 get_current_actor 的 JWT 分支同款 User 查詢（joinedload tenant）。
    租戶停用為「憑證有效但不可用」，故以 UITenantDisabled 表達（非 None）。
    """
    token = request.cookies.get(_UI_COOKIE_NAME)
    if not token:
        return None
    try:
        payload = decode_access_token(token)
        user_id_str: str | None = payload.get("sub")
        if not user_id_str:
            return None
        user_id = int(user_id_str)
    except (PyJWTError, ValueError):
        return None

    user = db.execute(
        select(User).where(User.id == user_id).options(joinedload(User.tenant))
    ).scalar_one_or_none()
    if user is None:
        return None
    if user.tenant and not user.tenant.is_active:
        raise UITenantDisabled()

    # F2 代管:imp claim 需指向「存在且仍是 admin」的 user,否則整張票失效
    # (admin 被降權/刪除即刻切斷所有在外代管票;fail-closed)。
    impersonator_user_id: Optional[int] = None
    imp_raw = payload.get("imp")
    if imp_raw is not None:
        try:
            imp_id = int(imp_raw)
        except (TypeError, ValueError):
            return None
        imp_user = db.get(User, imp_id)
        if imp_user is None or not imp_user.is_admin:
            return None
        impersonator_user_id = imp_id
    return Actor(
        user=user, api_key_id=None, impersonator_user_id=impersonator_user_id
    )


def require_ui_user(
    actor: Optional[Actor] = Depends(get_ui_actor_optional),
) -> Actor:
    """受保護 UI 頁：無有效 cookie → UILoginRequired（→ 303 /ui/login）。"""
    if actor is None:
        raise UILoginRequired()
    return actor


def require_ui_admin(
    actor: Actor = Depends(require_ui_user),
) -> Actor:
    """管理 UI 頁：未登入 → 重導登入；已登入但非 admin → 403 HTML 頁。"""
    if not actor.user.is_admin:
        raise UIForbidden()
    return actor


def require_ui_owner(
    actor: Actor = Depends(require_ui_user),
) -> Actor:
    """店內 owner 限定頁（B5）：帳務/LINE 設定/成員管理。

    平台 admin 豁免（跨租戶維運需要）；staff → 403。
    舊資料 role 由 0011 server_default 回填 owner，NULL 防禦性視為 owner。
    """
    role = getattr(actor.user, "role", None) or "owner"
    if role != "owner" and not actor.user.is_admin:
        raise UIForbidden()
    return actor
