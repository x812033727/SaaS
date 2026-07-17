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


def unusable_password_hash() -> str:
    """Return a valid bcrypt hash of a random secret no caller will ever know.

    For OAuth-only users we still satisfy the NOT NULL ``hashed_password``
    column (the security model is never relaxed) — but no password will ever
    verify against it, so password login is effectively disabled for them.
    """
    import secrets

    return hash_password(secrets.token_urlsafe(32))


# ──────────────────────────── JWT helpers ─────────────────────────────────────

def create_access_token(
    user_id: int,
    tenant_id: int,
    *,
    expires_delta: timedelta | None = None,
    impersonator_id: int | None = None,
    original_auth_ts: int | None = None,
    mfa_pending: bool = False,
    login_method: str | None = None,
    token_version: int = 0,
) -> str:
    """Sign a JWT with sub=<user_id>, tenant_id, exp claims.

    impersonator_id（F2 代管）:有值時 payload 加 ``imp`` claim,且 exp
    **強制縮短為 30 分鐘**(代管票短命,降低外洩風險)。

    original_auth_ts(R4-C1 滑動續期):首次登入的 unix 秒,續期時原樣帶入
    ``oa`` claim — /auth/renew 以此限制滑動視窗總長(勿無限續命)。
    向後相容:未帶則不加 claim,舊 token 照常驗。

    token_version(R5-D3 撤銷):簽發時的 ``user.token_version``,永遠寫入
    ``tv`` claim。decode 端(get_current_actor/get_ui_actor_optional)與 DB
    現值比對,不符即失效 —— 改密碼/停用/登出全部只需 +1 即撤銷所有在外票。
    舊票(無 tv)decode 端視為 0,故 token_version 仍為 0 者零中斷。

    mfa_pending(R5-D2 2FA):密碼/OAuth 已過、TOTP 未驗的中繼票 —— 加
    ``mfa="pending"`` claim 且 exp **強制縮短為 5 分鐘**。此票**不可**當
    access token 用:``decode_access_token`` 預設拒收(所有驗證路徑共用該
    入口)。login_method 隨票攜帶(``lm`` claim),供 MFA 完成後稽核正確的
    登入方式。
    """
    delta = expires_delta or timedelta(minutes=settings.access_token_expire_minutes)
    if impersonator_id is not None:
        delta = min(delta, timedelta(minutes=30))
    if mfa_pending:
        delta = min(delta, timedelta(minutes=5))
    expire = datetime.now(timezone.utc) + delta
    payload: dict = {
        "sub": str(user_id),
        "tenant_id": tenant_id,
        "exp": expire,
        "tv": int(token_version or 0),
    }
    if impersonator_id is not None:
        payload["imp"] = impersonator_id
    if original_auth_ts is not None:
        payload["oa"] = int(original_auth_ts)
    if mfa_pending:
        payload["mfa"] = "pending"
        if login_method:
            payload["lm"] = login_method
    return jwt.encode(payload, settings.secret_key, algorithm=settings.algorithm)


def decode_access_token(token: str, *, allow_mfa_pending: bool = False) -> dict:
    """Decode & verify JWT. Raises jwt.PyJWTError on invalid/expired token.

    MFA pending 中繼票(``mfa="pending"``)預設一律拒收 —— 唯一豁免是
    2FA 第二步驗證端點(明確帶 allow_mfa_pending=True)。
    """
    payload = jwt.decode(token, settings.secret_key, algorithms=[settings.algorithm])
    if not allow_mfa_pending and payload.get("mfa") == "pending":
        raise jwt.InvalidTokenError("MFA pending token is not an access token")
    return payload
