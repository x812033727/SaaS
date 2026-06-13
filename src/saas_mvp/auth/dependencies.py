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

from fastapi import Depends, HTTPException, status
from fastapi.security import APIKeyHeader, OAuth2PasswordBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from saas_mvp.auth.security import PyJWTError, decode_access_token
from saas_mvp.db import get_db
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
    """已驗證的請求者：一個 User，外加可選的 API key ID。"""
    user: User
    api_key_id: Optional[int] = None


def _resolve_api_key(key_str: str, db: Session) -> Actor:
    """以 prefix 縮候選集 + SHA-256 比對，驗證 API key 並回傳 Actor。"""
    from saas_mvp.models.api_key import ApiKey  # 避免頂層循環 import

    from saas_mvp.models.api_key import _KEY_PREFIX  # 避免頂層循環 import
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

    user = db.get(User, row.user_id)
    if user is None:
        raise _401

    return Actor(user=user, api_key_id=row.id)


def get_current_actor(
    x_api_key: Optional[str] = Depends(_api_key_header),
    bearer_token: Optional[str] = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
) -> Actor:
    """核心 dependency：解析請求憑證，回傳 Actor。"""
    # Path 1: X-API-Key header
    if x_api_key:
        if not x_api_key.startswith("myapp_"):
            raise _401
        return _resolve_api_key(x_api_key, db)

    # Path 2 & 3: Authorization: Bearer <token>
    if bearer_token:
        if bearer_token.startswith("myapp_"):
            # Bearer <api_key>
            return _resolve_api_key(bearer_token, db)
        # JWT 路徑
        try:
            payload = decode_access_token(bearer_token)
            user_id_str: str | None = payload.get("sub")
            if not user_id_str:
                raise _401
            user_id = int(user_id_str)
        except (PyJWTError, ValueError):
            raise _401

        user = db.get(User, user_id)
        if user is None:
            raise _401
        return Actor(user=user, api_key_id=None)

    # Path 4: 無憑證
    raise _401


def get_current_user(
    actor: Actor = Depends(get_current_actor),
) -> User:
    """向下相容包裝：從 Actor 中取出 User，現有 router 零改動。"""
    return actor.user
