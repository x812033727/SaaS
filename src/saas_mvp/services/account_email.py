"""帳號 Email 流程（B3）— 驗證信 + 忘記密碼。

* token 明文只出現在信件連結；DB 存 SHA-256（models/email_token.py）。
* 防帳號列舉：request_password_reset 對「查無 email」靜默成功（回一樣的訊息）。
* 驗證信 best-effort：寄失敗不阻擋註冊（dashboard banner 可重寄）。
"""

from __future__ import annotations

import datetime
import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from saas_mvp.auth.security import hash_password
from saas_mvp.config import settings
from saas_mvp.models.email_token import (
    PURPOSE_RESET,
    PURPOSE_VERIFY,
    EmailToken,
    generate_token,
    hash_token,
)
from saas_mvp.models.user import User
from saas_mvp.services.mailer import Mailer, MailerError

_log = logging.getLogger(__name__)


class TokenInvalid(Exception):
    """token 不存在 / 已用 / 已過期 / 用途不符。"""


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _issue(db: Session, user_id: int, purpose: str) -> str:
    """發 token（回明文；DB 存雜湊）並 commit。"""
    token = generate_token()
    db.add(EmailToken(
        user_id=user_id,
        purpose=purpose,
        token_hash=hash_token(token),
        expires_at=_utcnow() + datetime.timedelta(minutes=settings.email_token_ttl_minutes),
    ))
    db.commit()
    return token


def _consume(db: Session, token: str, purpose: str) -> EmailToken:
    """驗 token（用途/一次性/期限），標記 used（不 commit，由呼叫端一併提交）。"""
    row = db.execute(
        select(EmailToken).where(EmailToken.token_hash == hash_token(token))
    ).scalar_one_or_none()
    if row is None or row.purpose != purpose or row.used_at is not None:
        raise TokenInvalid("token invalid")
    exp = row.expires_at
    now = _utcnow()
    if exp.tzinfo is None:  # SQLite naive
        now = now.replace(tzinfo=None)
    if now > exp:
        raise TokenInvalid("token expired")
    row.used_at = _utcnow()
    return row


def send_verification_email(db: Session, user: User, mailer: Mailer) -> bool:
    """寄驗證信；best-effort（失敗記 log 回 False，不拋）。"""
    token = _issue(db, user.id, PURPOSE_VERIFY)
    base = settings.public_base_url.rstrip("/") or "http://127.0.0.1:8000"
    url = f"{base}/ui/verify-email/{token}"
    try:
        mailer.send(
            to=user.email,
            subject="請驗證您的 Email — LINE 預約平台",
            body=(
                "您好！\n\n請點擊以下連結完成 Email 驗證：\n"
                f"{url}\n\n"
                f"連結 {settings.email_token_ttl_minutes // 60} 小時內有效。"
                "若這不是您的操作，請忽略本信。"
            ),
        )
        return True
    except MailerError:
        _log.warning("verification email send failed for user %d", user.id, exc_info=True)
        return False


def verify_email(db: Session, token: str) -> User:
    """驗證連結：consume token → 標記 user.email_verified_at。"""
    row = _consume(db, token, PURPOSE_VERIFY)
    user = db.get(User, row.user_id)
    if user is None:  # pragma: no cover - FK CASCADE 防禦
        raise TokenInvalid("user gone")
    if user.email_verified_at is None:
        user.email_verified_at = _utcnow()
    db.commit()
    return user


def request_password_reset(db: Session, email: str, mailer: Mailer) -> None:
    """寄重設密碼信；查無 email 靜默成功（防帳號列舉）。寄送失敗拋 MailerError。"""
    user = db.execute(
        select(User).where(User.email == email)
    ).scalar_one_or_none()
    if user is None:
        return
    token = _issue(db, user.id, PURPOSE_RESET)
    base = settings.public_base_url.rstrip("/") or "http://127.0.0.1:8000"
    url = f"{base}/ui/reset-password/{token}"
    mailer.send(
        to=user.email,
        subject="重設密碼 — LINE 預約平台",
        body=(
            "您好！\n\n請點擊以下連結重設密碼：\n"
            f"{url}\n\n"
            f"連結 {settings.email_token_ttl_minutes // 60} 小時內有效。"
            "若這不是您的操作，請忽略本信（密碼不會被更改）。"
        ),
    )


def reset_password(db: Session, token: str, new_password: str) -> User:
    """重設密碼：consume token → 寫入新 bcrypt hash。密碼長度由呼叫端先驗。"""
    row = _consume(db, token, PURPOSE_RESET)
    user = db.get(User, row.user_id)
    if user is None:  # pragma: no cover
        raise TokenInvalid("user gone")
    user.hashed_password = hash_password(new_password)
    db.commit()
    return user
