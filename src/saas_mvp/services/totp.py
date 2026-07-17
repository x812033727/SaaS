"""TOTP 2FA(R5-D2)— 註冊/確認/驗證/停用 + 一次性恢復碼。

設計:
* secret Fernet 加密存 users.totp_secret_enc;``totp_enabled_at`` 為 NULL
  時未生效(註冊到一半的暫存 secret 不影響登入)。
* 恢復碼只存 sha256,產生時一次性顯示;用過標記 used_at 不可重用。
* 驗證視窗 ±1 step(30 秒),容忍手機時鐘小幅漂移。
"""

from __future__ import annotations

import datetime
import hashlib
import secrets

import pyotp
import segno
from sqlalchemy.orm import Session

from saas_mvp.models.totp_recovery_code import TotpRecoveryCode
from saas_mvp.models.user import User

ISSUER_NAME = "LINE 預約平台"
RECOVERY_CODE_COUNT = 10
_VALID_WINDOW = 1  # ±1 step = ±30s


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _hash_code(code: str) -> str:
    return hashlib.sha256(_normalize(code).encode()).hexdigest()


def _normalize(code: str) -> str:
    return (code or "").strip().upper().replace("-", "").replace(" ", "")


def start_enrollment(db: Session, user: User) -> str:
    """產生新 secret 暫存(未啟用)。重複呼叫覆蓋舊暫存值。commit。"""
    secret = pyotp.random_base32()
    user.totp_secret = secret
    user.totp_enabled_at = None
    db.commit()
    return secret


def provisioning_uri(user: User, secret: str | None = None) -> str:
    return pyotp.TOTP(secret or user.totp_secret).provisioning_uri(
        name=user.email, issuer_name=ISSUER_NAME
    )


def qr_svg(uri: str) -> str:
    """伺服器端渲染 QR 為 inline SVG(免任何前端依賴)。"""
    return segno.make(uri, error="m").svg_inline(scale=4)


def _totp_matches(user: User, code: str) -> bool:
    code = _normalize(code)
    if not code or not code.isdigit() or len(code) != 6:
        return False
    if not user.totp_secret_enc:
        return False
    return bool(
        pyotp.TOTP(user.totp_secret).verify(code, valid_window=_VALID_WINDOW)
    )


def confirm_enrollment(db: Session, user: User, code: str) -> list[str] | None:
    """驗一次 TOTP 後正式啟用;回傳 10 組恢復碼明文(僅此一次)。

    驗證失敗回 None、不啟用。commit。
    """
    if not _totp_matches(user, code):
        return None
    user.totp_enabled_at = _utcnow()
    db.query(TotpRecoveryCode).filter(
        TotpRecoveryCode.user_id == user.id
    ).delete(synchronize_session=False)
    codes: list[str] = []
    for _ in range(RECOVERY_CODE_COUNT):
        raw = secrets.token_hex(4).upper()  # 8 hex chars
        display = f"{raw[:4]}-{raw[4:]}"
        codes.append(display)
        db.add(TotpRecoveryCode(user_id=user.id, code_hash=_hash_code(display)))
    db.commit()
    return codes


def verify_code(db: Session, user: User, code: str) -> bool:
    """登入第二步/停用驗證:TOTP 或未用過的恢復碼(用掉即失效)。commit(恢復碼)。"""
    if not user.totp_enabled:
        return False
    if _totp_matches(user, code):
        return True
    normalized = _normalize(code)
    if len(normalized) != 8:
        return False
    row = (
        db.query(TotpRecoveryCode)
        .filter(
            TotpRecoveryCode.user_id == user.id,
            TotpRecoveryCode.code_hash == _hash_code(normalized),
            TotpRecoveryCode.used_at.is_(None),
        )
        .first()
    )
    if row is None:
        return False
    row.used_at = _utcnow()
    db.commit()
    return True


def disable(db: Session, user: User, code: str) -> bool:
    """停用 2FA:需通過一次 TOTP/恢復碼驗證。commit。"""
    if not verify_code(db, user, code):
        return False
    user.totp_secret = ""
    user.totp_enabled_at = None
    db.query(TotpRecoveryCode).filter(
        TotpRecoveryCode.user_id == user.id
    ).delete(synchronize_session=False)
    db.commit()
    return True


def remaining_recovery_codes(db: Session, user: User) -> int:
    return (
        db.query(TotpRecoveryCode)
        .filter(
            TotpRecoveryCode.user_id == user.id,
            TotpRecoveryCode.used_at.is_(None),
        )
        .count()
    )
