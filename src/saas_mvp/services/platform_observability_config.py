"""Platform error-monitoring settings: encrypted database override + env fallback."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

from sqlalchemy.orm import Session

from saas_mvp.models.platform_observability_config import PlatformObservabilityConfig


class PlatformObservabilityConfigError(ValueError):
    pass


@dataclass(frozen=True)
class EffectiveObservabilityConfig:
    sentry_dsn: str
    source: str


def _row(db: Session) -> PlatformObservabilityConfig | None:
    return db.query(PlatformObservabilityConfig).order_by(
        PlatformObservabilityConfig.id
    ).first()


def effective_observability_config(
    db: Session | None, settings
) -> EffectiveObservabilityConfig | None:
    if db is not None:
        row = _row(db)
        if row is not None:
            return EffectiveObservabilityConfig(row.sentry_dsn, "database")
    dsn = (settings.sentry_dsn or "").strip()
    return EffectiveObservabilityConfig(dsn, "environment") if dsn else None


def observability_status(db: Session, settings) -> dict:
    config = effective_observability_config(db, settings)
    row = _row(db) if config and config.source == "database" else None
    if config is None:
        return {
            "configured": False,
            "source": "unconfigured",
            "dsn_mask": "",
            "updated_at": None,
        }
    parsed = urlparse(config.sentry_dsn)
    project = parsed.path.rstrip("/").rsplit("/", 1)[-1]
    return {
        "configured": True,
        "source": config.source,
        "dsn_mask": f"{parsed.scheme}://•••@{parsed.hostname}/•••{project[-4:]}",
        "updated_at": row.updated_at if row is not None else None,
    }


def _validate_dsn(dsn: str) -> str:
    value = dsn.strip()
    parsed = urlparse(value)
    if (
        len(value) > 1024
        or parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or not parsed.username
        or not parsed.path.strip("/")
        or any(ch.isspace() for ch in value)
    ):
        raise PlatformObservabilityConfigError("Sentry DSN 格式不正確。")
    return value


def save_observability_config(
    db: Session, *, sentry_dsn: str, actor_user_id: int
) -> PlatformObservabilityConfig:
    value = _validate_dsn(sentry_dsn)
    row = _row(db)
    if row is None:
        row = PlatformObservabilityConfig()
        db.add(row)
    row.sentry_dsn = value
    row.updated_by_user_id = actor_user_id
    db.flush()
    return row


def clear_observability_override(db: Session) -> bool:
    row = _row(db)
    if row is None:
        return False
    db.delete(row)
    db.flush()
    return True


def apply_effective_observability_config(db: Session | None, settings) -> bool:
    from saas_mvp.obs.alerts import init_sentry

    config = effective_observability_config(db, settings)
    return init_sentry(config.sentry_dsn if config else "")


def send_test_event(db: Session, settings) -> None:
    """Initialize the effective DSN and synchronously flush a harmless test event."""
    config = effective_observability_config(db, settings)
    if config is None:
        raise PlatformObservabilityConfigError("請先設定 Sentry DSN。")
    if not apply_effective_observability_config(db, settings):
        raise PlatformObservabilityConfigError("Sentry 無法初始化，請確認 DSN 與套件設定。")
    try:
        import sentry_sdk

        sentry_sdk.capture_message("SaaS platform observability connection test")
        sentry_sdk.flush(timeout=5)
    except Exception as exc:  # noqa: BLE001
        raise PlatformObservabilityConfigError("Sentry 測試事件送出失敗。") from exc
