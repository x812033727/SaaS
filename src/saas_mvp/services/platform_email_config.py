"""平台 SMTP 設定：資料庫優先、環境變數備援。"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from saas_mvp.models.platform_email_config import PlatformEmailConfig


class PlatformEmailConfigError(ValueError):
    pass


@dataclass(frozen=True)
class EffectiveEmailConfig:
    host: str
    port: int
    user: str
    password: str
    from_address: str
    source: str


def _row(db: Session) -> PlatformEmailConfig | None:
    return db.query(PlatformEmailConfig).order_by(PlatformEmailConfig.id).first()


def effective_email_config(db: Session | None, settings) -> EffectiveEmailConfig | None:
    if db is not None:
        row = _row(db)
        if row is not None:
            return EffectiveEmailConfig(
                host=row.smtp_host,
                port=row.smtp_port,
                user=row.smtp_user,
                password=row.smtp_password,
                from_address=row.smtp_from,
                source="database",
            )
    host = (settings.smtp_host or "").strip()
    if not host:
        return None
    return EffectiveEmailConfig(
        host=host,
        port=int(settings.smtp_port),
        user=(settings.smtp_user or "").strip(),
        password=settings.smtp_password or "",
        from_address=(settings.smtp_from or settings.smtp_user or "").strip(),
        source="environment",
    )


def email_status(db: Session, settings) -> dict:
    config = effective_email_config(db, settings)
    if config is None:
        return {
            "configured": False,
            "source": "unconfigured",
            "host": "",
            "port": 587,
            "user": "",
            "from_address": "",
        }
    return {
        "configured": True,
        "source": config.source,
        "host": config.host,
        "port": config.port,
        "user": config.user,
        "from_address": config.from_address,
    }


def save_email_config(
    db: Session,
    *,
    host: str,
    port: int,
    user: str,
    password: str,
    from_address: str,
    actor_user_id: int,
) -> PlatformEmailConfig:
    host = host.strip()
    user = user.strip()
    password = password.strip()
    from_address = from_address.strip()
    row = _row(db)
    if not host or len(host) > 255 or any(ch.isspace() for ch in host):
        raise PlatformEmailConfigError("SMTP 主機格式不正確。")
    if port < 1 or port > 65535:
        raise PlatformEmailConfigError("SMTP 連接埠必須介於 1–65535。")
    if not from_address or "@" not in from_address or len(from_address) > 255:
        raise PlatformEmailConfigError("寄件人 Email 格式不正確。")
    if not password and row is None:
        raise PlatformEmailConfigError("首次設定必須輸入 SMTP 密碼或應用程式密碼。")
    if len(user) > 255 or len(password) > 255:
        raise PlatformEmailConfigError("SMTP 帳號或密碼過長。")

    if row is None:
        row = PlatformEmailConfig()
        db.add(row)
    row.smtp_host = host
    row.smtp_port = port
    row.smtp_user = user
    row.smtp_from = from_address
    if password:
        row.smtp_password = password
    row.updated_by_user_id = actor_user_id
    db.flush()
    return row


def clear_email_override(db: Session) -> bool:
    row = _row(db)
    if row is None:
        return False
    db.delete(row)
    db.flush()
    return True
