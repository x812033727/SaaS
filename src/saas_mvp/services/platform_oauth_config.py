"""平台 OAuth 設定：加密落庫、環境變數備援、遮罩狀態。"""

from __future__ import annotations

import re

from sqlalchemy.orm import Session

from saas_mvp.models.platform_oauth_config import PlatformOAuthConfig

_LINE_CHANNEL_ID_RE = re.compile(r"^[0-9]{5,20}$")


class PlatformOAuthConfigError(ValueError):
    """管理員輸入的 OAuth 設定不合法。"""


def _line_row(db: Session) -> PlatformOAuthConfig | None:
    return db.query(PlatformOAuthConfig).filter_by(provider="line").one_or_none()


def effective_line_credentials(db: Session | None, settings) -> tuple[str, str] | None:
    """資料庫優先，否則回退環境變數；不回傳半套設定。"""
    if db is not None:
        row = _line_row(db)
        if row is not None:
            return row.client_id, row.client_secret
    channel_id = (settings.line_login_channel_id or "").strip()
    channel_secret = (settings.line_login_channel_secret or "").strip()
    if channel_id and channel_secret:
        return channel_id, channel_secret
    return None


def line_status(db: Session, settings) -> dict:
    row = _line_row(db)
    if row is not None:
        return {
            "configured": True,
            "source": "database",
            "client_id": row.client_id,
            "secret_mask": "••••••••",
            "updated_at": row.updated_at,
        }
    credentials = effective_line_credentials(None, settings)
    if credentials:
        return {
            "configured": True,
            "source": "environment",
            "client_id": credentials[0],
            "secret_mask": "••••••••",
            "updated_at": None,
        }
    return {
        "configured": False,
        "source": "unconfigured",
        "client_id": "",
        "secret_mask": "",
        "updated_at": None,
    }


def save_line_credentials(
    db: Session,
    *,
    channel_id: str,
    channel_secret: str,
    actor_user_id: int,
) -> PlatformOAuthConfig:
    channel_id = channel_id.strip()
    channel_secret = channel_secret.strip()
    row = _line_row(db)
    if not _LINE_CHANNEL_ID_RE.fullmatch(channel_id):
        raise PlatformOAuthConfigError("Channel ID 應為 5–20 位數字。")
    if not channel_secret and row is None:
        raise PlatformOAuthConfigError("首次設定必須輸入 Channel Secret。")
    if channel_secret and (len(channel_secret) < 16 or len(channel_secret) > 255):
        raise PlatformOAuthConfigError("Channel Secret 長度不正確。")
    if any(ch.isspace() for ch in channel_secret):
        raise PlatformOAuthConfigError("Channel Secret 不可包含空白。")

    if row is None:
        row = PlatformOAuthConfig(provider="line", client_id=channel_id)
        db.add(row)
    row.client_id = channel_id
    if channel_secret:
        row.client_secret = channel_secret
    row.updated_by_user_id = actor_user_id
    db.flush()
    return row


def clear_line_override(db: Session) -> bool:
    row = _line_row(db)
    if row is None:
        return False
    db.delete(row)
    db.flush()
    return True
