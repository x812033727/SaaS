"""Password hashing (bcrypt via passlib) and JWT signing (PyJWT)."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import jwt
from jwt.exceptions import PyJWTError  # noqa: F401 — re-exported for callers
from passlib.context import CryptContext

from saas_mvp.config import settings

# bcrypt cost factor。生產維持安全預設 12；測試環境可用 SAAS_BCRYPT_ROUNDS 降到
# 最低值（4）以大幅加速整批雜湊，且不影響演算法本身（verify 由 hash 內嵌的 rounds 決定）。
_BCRYPT_ROUNDS = max(4, min(31, int(os.getenv("SAAS_BCRYPT_ROUNDS", "12"))))

_pwd_ctx = CryptContext(
    schemes=["bcrypt"],
    deprecated="auto",
    bcrypt__rounds=_BCRYPT_ROUNDS,
)


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
