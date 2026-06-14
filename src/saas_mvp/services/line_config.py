"""LINE Channel Config 服務層 — Admin 管理端點使用。

設計原則
--------
* 回傳格式不含明文 secret/token，以 has_channel_secret / has_access_token 遮罩。
* upsert 語意：tenant 已有設定時更新、無時新建，call site 不需先 GET。
* 找不到 tenant 回 404；解密失敗回 500（不應發生，屬金鑰輪換場景）。
"""

from __future__ import annotations

import logging

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from saas_mvp.line_client import LineBotInfoClient
from saas_mvp.models.tenant import Tenant
from saas_mvp.models.line_channel_config import (
    LineChannelConfig,
    validate_target_lang,
    InvalidTargetLangError,
)

logger = logging.getLogger(__name__)


def _to_response(cfg: LineChannelConfig) -> dict:
    """將 ORM 物件轉為 API 回應（遮罩 secret/token）。"""
    return {
        "tenant_id": cfg.tenant_id,
        "has_channel_secret": bool(cfg.channel_secret_enc),
        "has_access_token": bool(cfg.access_token_enc),
        "default_target_lang": cfg.default_target_lang,
        "created_at": cfg.created_at.isoformat() if cfg.created_at else None,
        "updated_at": cfg.updated_at.isoformat() if cfg.updated_at else None,
    }


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
    bot_info_client: LineBotInfoClient | None = None,
) -> dict:
    """新建或覆寫租戶 LINE 設定；回傳遮罩版 response。

    若提供 ``bot_info_client``，在設定 commit 成功後自動呼叫 LINE
    ``GET /v2/bot/info`` 取 bot userId 並回填 ``line_bot_user_id``。
    bot/info 失敗（網路/離線）或 userId 已被他租戶佔用（IntegrityError）時，
    僅記 warning、rollback，不阻擋 upsert——設定仍儲存成功、欄位留原值。

    Raises
    ------
    404 tenant not found
    400 invalid target_lang
    """
    tenant = db.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="tenant not found")

    # BCP-47 驗證
    try:
        validate_target_lang(default_target_lang)
    except InvalidTargetLangError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    cfg = tenant.line_channel_config
    if cfg is None:
        cfg = LineChannelConfig(tenant_id=tenant_id)
        db.add(cfg)

    cfg.channel_secret = channel_secret
    cfg.access_token = access_token
    cfg.default_target_lang = default_target_lang

    db.commit()
    db.refresh(cfg)

    # bot/info 自動回填 userId：失敗不阻擋 upsert（離線相容）。
    # get_user_id 失敗與二次 commit 的 IntegrityError 共用同一 try/except，
    # rollback 必須顯式呼叫，防止 session 髒狀態污染後續請求。
    if bot_info_client is not None:
        try:
            uid = bot_info_client.get_user_id(cfg.access_token)
            if uid:
                cfg.line_bot_user_id = uid
                db.commit()
        except Exception:
            logger.warning("line bot info fetch or uid commit failed, skipping")
            db.rollback()

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
