"""LINE Channel Config 服務層 — Admin 管理端點使用。

設計原則
--------
* 回傳格式不含明文 secret/token，以 has_channel_secret / has_access_token 遮罩。
* upsert 語意：tenant 已有設定時更新、無時新建，call site 不需先 GET。
* 找不到 tenant 回 404；解密失敗回 500（不應發生，屬金鑰輪換場景）。
"""

from __future__ import annotations

import datetime
import logging

from fastapi import HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from saas_mvp.line_client import (
    LineBotInfoClient,
    LineBotInfoCredentialError,
    LineBotInfoError,
    LineBotInfoNetworkError,
    LineBotInfoParseError,
)
from saas_mvp.models.tenant import Tenant
from saas_mvp.models.line_channel_config import (
    DEFAULT_BOT_MODE,
    InvalidBotModeError,
    InvalidTargetLangError,
    LineChannelConfig,
    LineConfigDecryptionError,
    validate_bot_mode,
    validate_target_lang,
)

logger = logging.getLogger(__name__)

_STATUS_UNCHECKED = "unchecked"
_STATUS_VALID = "valid"
_STATUS_INVALID = "invalid"
_STATUS_ERROR = "error"
_STATUS_CONFLICT = "conflict"
_ERROR_MAX_LEN = 255


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _clip_error(message: str) -> str:
    return message[:_ERROR_MAX_LEN]


def _normalize_credential_status(value: str | None) -> str:
    return value or _STATUS_UNCHECKED


def _to_response(cfg: LineChannelConfig) -> dict:
    """將 ORM 物件轉為 API 回應（遮罩 secret/token）。"""
    return {
        "tenant_id": cfg.tenant_id,
        "has_channel_secret": bool(cfg.channel_secret_enc),
        "has_access_token": bool(cfg.access_token_enc),
        "default_target_lang": cfg.default_target_lang,
        "bot_mode": cfg.bot_mode or DEFAULT_BOT_MODE,
        "welcome_message": cfg.welcome_message,
        "credential_status": _normalize_credential_status(cfg.credential_status),
        "credential_last_error": cfg.credential_last_error,
        "credential_checked_at": (
            cfg.credential_checked_at.isoformat() if cfg.credential_checked_at else None
        ),
        "created_at": cfg.created_at.isoformat() if cfg.created_at else None,
        "updated_at": cfg.updated_at.isoformat() if cfg.updated_at else None,
    }


def _access_token_changed(cfg: LineChannelConfig, access_token: str) -> bool:
    if cfg.access_token_enc is None:
        return True
    try:
        return cfg.access_token != access_token
    except LineConfigDecryptionError:
        return True


def _set_unchecked_for_token_change(cfg: LineChannelConfig) -> None:
    cfg.line_bot_user_id = None
    cfg.credential_status = _STATUS_UNCHECKED
    cfg.credential_last_error = None
    cfg.credential_checked_at = None


def _mark_credential_status(
    db: Session,
    cfg: LineChannelConfig,
    *,
    status_value: str,
    error: str | None,
) -> None:
    cfg.credential_status = status_value
    cfg.credential_last_error = _clip_error(error) if error else None
    cfg.credential_checked_at = _utcnow()
    db.commit()
    db.refresh(cfg)


def _verify_and_mark_bot_info(
    db: Session,
    cfg: LineChannelConfig,
    *,
    tenant_id: int,
    bot_info_client: LineBotInfoClient,
) -> None:
    try:
        uid = bot_info_client.get_user_id(cfg.access_token)
        if not uid:
            _mark_credential_status(
                db,
                cfg,
                status_value=_STATUS_INVALID,
                error="LINE bot/info did not return a valid userId",
            )
            return

        cfg.line_bot_user_id = uid
        cfg.credential_status = _STATUS_VALID
        cfg.credential_last_error = None
        cfg.credential_checked_at = _utcnow()
        db.commit()
        db.refresh(cfg)
    except IntegrityError:
        logger.warning(
            "bot/info uid conflict for tenant %s, marking credential conflict",
            tenant_id,
            exc_info=True,
        )
        db.rollback()
        db.refresh(cfg)
        _mark_credential_status(
            db,
            cfg,
            status_value=_STATUS_CONFLICT,
            error="LINE bot userId is already connected to another tenant",
        )
    except (LineBotInfoCredentialError, LineBotInfoParseError) as exc:
        logger.warning(
            "bot/info credential invalid for tenant %s: %s",
            tenant_id,
            type(exc).__name__,
        )
        db.rollback()
        db.refresh(cfg)
        _mark_credential_status(
            db,
            cfg,
            status_value=_STATUS_INVALID,
            error=f"{type(exc).__name__}: {exc}",
        )
    except (LineBotInfoNetworkError, LineBotInfoError) as exc:
        logger.warning(
            "bot/info check failed for tenant %s: %s",
            tenant_id,
            type(exc).__name__,
        )
        db.rollback()
        db.refresh(cfg)
        _mark_credential_status(
            db,
            cfg,
            status_value=_STATUS_ERROR,
            error=f"{type(exc).__name__}: {exc}",
        )
    except Exception as exc:  # noqa: BLE001 - legacy/fake clients may raise arbitrary errors
        logger.warning(
            "bot/info uid fetch failed for tenant %s, marking credential error",
            tenant_id,
            exc_info=True,
        )
        db.rollback()
        db.refresh(cfg)
        _mark_credential_status(
            db,
            cfg,
            status_value=_STATUS_ERROR,
            error=f"{type(exc).__name__}: {exc}",
        )


def get_line_config(db: Session, tenant_id: int) -> dict:
    """取得租戶 LINE 設定（遮罩版）；不存在回 404。"""
    tenant = db.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="tenant not found")

    cfg = tenant.line_channel_config
    if cfg is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="line channel config not found for this tenant",
        )
    return _to_response(cfg)


def upsert_line_config(
    db: Session,
    tenant_id: int,
    channel_secret: str,
    access_token: str,
    default_target_lang: str = "zh-TW",
    bot_mode: str | None = None,
    bot_info_client: LineBotInfoClient | None = None,
) -> dict:
    """新建或覆寫租戶 LINE 設定；回傳遮罩版 response。

    若提供 ``bot_info_client``，在設定 commit 成功後自動呼叫 LINE
    ``GET /v2/bot/info`` 取 bot userId 並回填 ``line_bot_user_id``。
    bot/info 失敗或 userId 已被他租戶佔用時，不阻擋 upsert，而是寫入
    credential_status 供 API 回應揭露。

    ``bot_mode``：None 時不更動（新建留預設 translation）；提供時驗證白名單。

    Raises
    ------
    404 tenant not found
    400 invalid target_lang / bot_mode
    """
    tenant = db.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="tenant not found")

    # BCP-47 驗證
    try:
        validate_target_lang(default_target_lang)
    except InvalidTargetLangError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    # bot_mode 白名單驗證（提供時）
    if bot_mode is not None:
        try:
            validate_bot_mode(bot_mode)
        except InvalidBotModeError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
            ) from exc

    cfg = tenant.line_channel_config
    if cfg is None:
        cfg = LineChannelConfig(tenant_id=tenant_id)
        db.add(cfg)
        token_changed = True
    else:
        token_changed = _access_token_changed(cfg, access_token)

    cfg.channel_secret = channel_secret
    cfg.access_token = access_token
    cfg.default_target_lang = default_target_lang
    if bot_mode is not None:
        cfg.bot_mode = bot_mode
    if token_changed:
        _set_unchecked_for_token_change(cfg)
    elif cfg.credential_status is None:
        cfg.credential_status = _STATUS_UNCHECKED

    db.commit()
    db.refresh(cfg)

    if bot_info_client is not None:
        _verify_and_mark_bot_info(
            db,
            cfg,
            tenant_id=tenant_id,
            bot_info_client=bot_info_client,
        )

    return _to_response(cfg)


def verify_line_config(
    db: Session,
    tenant_id: int,
    *,
    bot_info_client: LineBotInfoClient,
) -> dict:
    """重新驗證租戶 LINE 憑證並回填狀態；回傳遮罩版 response。

    複用 ``_verify_and_mark_bot_info``：呼叫 LINE ``GET /v2/bot/info`` 取
    bot userId，更新 ``credential_status`` / ``line_bot_user_id``。任何
    credential / network / conflict 錯誤皆由該函式吸收並寫入 credential_status，
    不向外拋 5xx。

    Raises
    ------
    404 tenant or line config not found
    """
    tenant = db.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="tenant not found")

    cfg = tenant.line_channel_config
    if cfg is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="line channel config not found for this tenant",
        )

    _verify_and_mark_bot_info(
        db,
        cfg,
        tenant_id=tenant_id,
        bot_info_client=bot_info_client,
    )
    return _to_response(cfg)


def set_bot_mode(db: Session, tenant_id: int, bot_mode: str) -> dict:
    """僅切換 bot_mode（不需重輸憑證）；回傳遮罩版 response。

    Raises
    ------
    404 tenant or line config not found
    400 invalid bot_mode
    """
    try:
        validate_bot_mode(bot_mode)
    except InvalidBotModeError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc

    tenant = db.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="tenant not found")
    cfg = tenant.line_channel_config
    if cfg is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="line channel config not found for this tenant",
        )
    cfg.bot_mode = bot_mode
    db.commit()
    db.refresh(cfg)
    return _to_response(cfg)


# 歡迎訊息長度上限：LINE text message 上限 5000 字，取保守值防灌爆。
WELCOME_MESSAGE_MAX_LEN = 1000


def set_welcome_message(db: Session, tenant_id: int, welcome_message: str | None) -> dict:
    """僅更新 follow 歡迎訊息（不需重輸憑證）；空白/None 清空＝回內建預設文案。

    Raises
    ------
    404 tenant or line config not found
    400 too long
    """
    normalized = (welcome_message or "").strip() or None
    if normalized is not None and len(normalized) > WELCOME_MESSAGE_MAX_LEN:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"welcome_message too long (max {WELCOME_MESSAGE_MAX_LEN} chars)",
        )

    tenant = db.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="tenant not found")
    cfg = tenant.line_channel_config
    if cfg is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="line channel config not found for this tenant",
        )
    cfg.welcome_message = normalized
    db.commit()
    db.refresh(cfg)
    return _to_response(cfg)


def delete_line_config(db: Session, tenant_id: int) -> dict:
    """刪除租戶 LINE 設定；找不到回 404。"""
    tenant = db.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="tenant not found")

    cfg = tenant.line_channel_config
    if cfg is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="line channel config not found for this tenant",
        )

    db.delete(cfg)
    db.commit()
    return {"detail": "deleted", "tenant_id": tenant_id}
