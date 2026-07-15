"""平台 AI 設定：資料庫優先、環境變數備援。"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from saas_mvp.models.platform_ai_config import PlatformAIConfig


class PlatformAIConfigError(ValueError):
    pass


@dataclass(frozen=True)
class EffectiveAIConfig:
    provider: str
    api_key: str
    model: str
    source: str


def _row(db: Session) -> PlatformAIConfig | None:
    return db.query(PlatformAIConfig).order_by(PlatformAIConfig.id).first()


def effective_ai_config(db: Session | None, settings) -> EffectiveAIConfig | None:
    if db is not None:
        row = _row(db)
        if row is not None:
            return EffectiveAIConfig(
                provider=row.provider,
                api_key=row.api_key,
                model=row.model,
                source="database",
            )
    api_key = (settings.anthropic_api_key or "").strip()
    if not api_key:
        return None
    return EffectiveAIConfig(
        provider="anthropic",
        api_key=api_key,
        model=(settings.ai_model or "").strip(),
        source="environment",
    )


def ai_status(db: Session, settings) -> dict:
    config = effective_ai_config(db, settings)
    if config is None:
        return {
            "configured": False,
            "source": "unconfigured",
            "provider": "anthropic",
            "model": (settings.ai_model or "").strip(),
            "key_mask": "",
            "updated_at": None,
        }
    row = _row(db) if config.source == "database" else None
    return {
        "configured": True,
        "source": config.source,
        "provider": config.provider,
        "model": config.model,
        "key_mask": "••••" + config.api_key[-4:] if len(config.api_key) >= 4 else "••••",
        "updated_at": row.updated_at if row is not None else None,
    }


def save_ai_config(
    db: Session,
    *,
    api_key: str,
    model: str,
    actor_user_id: int,
) -> PlatformAIConfig:
    api_key = api_key.strip()
    model = model.strip()
    row = _row(db)
    if not api_key and row is None:
        raise PlatformAIConfigError("首次設定必須輸入 Anthropic API Key。")
    if api_key and (
        len(api_key) < 20
        or len(api_key) > 255
        or any(ch.isspace() for ch in api_key)
    ):
        raise PlatformAIConfigError("Anthropic API Key 格式不正確。")
    if (
        not model
        or len(model) > 128
        or any(ch.isspace() for ch in model)
        or not model.startswith("claude-")
    ):
        raise PlatformAIConfigError("模型 ID 必須是 claude- 開頭且不可包含空白。")

    if row is None:
        row = PlatformAIConfig(provider="anthropic")
        db.add(row)
    row.provider = "anthropic"
    row.model = model
    if api_key:
        row.api_key = api_key
    row.updated_by_user_id = actor_user_id
    db.flush()
    return row


def clear_ai_override(db: Session) -> bool:
    row = _row(db)
    if row is None:
        return False
    db.delete(row)
    db.flush()
    return True


def test_ai_config(db: Session, settings) -> None:
    """用極小輸出呼叫驗證 API key 與模型，不保存任何測試內容。"""
    config = effective_ai_config(db, settings)
    if config is None:
        raise PlatformAIConfigError("AI 尚未設定。")
    try:
        import anthropic

        client = anthropic.Anthropic(api_key=config.api_key, timeout=15.0)
        client.messages.create(
            model=config.model,
            max_tokens=8,
            system="Connection check. Reply OK.",
            messages=[{"role": "user", "content": "OK"}],
        )
    except Exception as exc:  # noqa: BLE001 - 對後台提供安全且不洩密的錯誤
        name = type(exc).__name__
        if name in {"AuthenticationError", "PermissionDeniedError"}:
            detail = "API Key 無效或沒有權限。"
        elif name == "NotFoundError":
            detail = "找不到指定模型，請確認模型 ID。"
        elif name == "RateLimitError":
            detail = "Anthropic 額度不足或已達速率限制。"
        else:
            detail = "無法連線 Anthropic，請稍後再試。"
        raise PlatformAIConfigError(detail) from exc
