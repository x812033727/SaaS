"""Password hashing (bcrypt via passlib) and JWT signing (PyJWT)."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import jwt
from jwt.exceptions import PyJWTError  # noqa: F401 — re-exported for callers
from passlib.context import CryptContext

from saas_mvp.config import settings


def _build_pwd_ctx() -> CryptContext:
    """建立 bcrypt CryptContext。

    生產環境使用 passlib 預設 cost（安全強度）。僅當 ``SAAS_BCRYPT_ROUNDS``
    env 顯式設定時才覆寫 rounds——測試環境可設低值（如 4）大幅加速；env 未設
    時行為與原本完全一致，不影響生產安全性。bcrypt 合法區間為 4~31，超界則忽略。
    """
    raw = os.environ.get("SAAS_BCRYPT_ROUNDS")
    if raw:
        try:
            rounds = int(raw)
        except ValueError:
            rounds = 0
        if 4 <= rounds <= 31:
            return CryptContext(
                schemes=["bcrypt"], deprecated="auto", bcrypt__rounds=rounds
            )
    return CryptContext(schemes=["bcrypt"], deprecated="auto")


_pwd_ctx = _build_pwd_ctx()


# ──────────────────────────── password helpers ────────────────────────────────

def hash_password(plain: str) -> str:
    """Return a bcrypt hash of *plain*. Never store the plain-text value."""
    return _pwd_ctx.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    """Return True iff *plain* matches *hashed* (constant-time compare)."""
    return _pwd_ctx.verify(plain, hashed)


# ──────────────────────────── JWT helpers ─────────────────────────────────────

def create_access_token(
    user_id: int,
    tenant_id: int,
    *,
    expires_delta: timedelta | None = None,
) -> str:
    """Sign a JWT with sub=<user_id>, tenant_id, exp claims."""
    delta = expires_delta or timedelta(minutes=settings.access_token_expire_minutes)
    expire = datetime.now(timezone.utc) + delta
    payload: dict = {
        "sub": str(user_id),
        "tenant_id": tenant_id,
        "exp": expire,
    }
    return jwt.encode(payload, settings.secret_key, algorithm=settings.algorithm)


def decode_access_token(token: str) -> dict:
    """Decode & verify JWT. Raises jwt.PyJWTError on invalid/expired token."""
    return jwt.decode(token, settings.secret_key, algorithms=[settings.algorithm])
