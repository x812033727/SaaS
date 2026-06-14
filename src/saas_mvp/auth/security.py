"""Password hashing (bcrypt via passlib) and JWT signing (PyJWT)."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import jwt
from jwt.exceptions import PyJWTError  # noqa: F401 — re-exported for callers
from passlib.context import CryptContext

from saas_mvp.config import settings

# bcrypt cost factor。生產維持安全預設 12。測試環境可用 SAAS_BCRYPT_ROUNDS 降到
# 最低值以大幅加速整批雜湊（不影響演算法本身；verify 由 hash 內嵌的 rounds 決定），
# 但必須同時設 SAAS_TESTING=1 明確表態，否則低於 10 的值一律拒絕啟動（fail-closed），
# 防止 CI/CD 環境變數污染把弱雜湊帶進生產。
_BCRYPT_ROUNDS = max(4, min(31, int(os.getenv("SAAS_BCRYPT_ROUNDS", "12"))))
if _BCRYPT_ROUNDS < 10 and not os.getenv("SAAS_TESTING"):
    raise RuntimeError(
        f"SAAS_BCRYPT_ROUNDS={_BCRYPT_ROUNDS} is unsafe for production "
        "(minimum secure cost is 10); set SAAS_TESTING=1 to allow a lower value in tests."
    )

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
