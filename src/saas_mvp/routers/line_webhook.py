"""LINE Webhook 端點 — /line/webhook/{tenant_id}

設計決策（架構師確認）
---------------------
* raw body 用 Request.body() 取得，再 JSON decode，確保 HMAC 比對用原始 bytes。
* X-Line-Signature 驗章失敗 → 400（符合 LINE 文件建議）。
* 非文字事件靜默略過，回 200 OK。
* quota 超量 → 不翻譯、以明確訊息 reply（不拋 500）。
* quota 計費點後移：先做非遞增檢查放行，待 translate 與 reply 皆成功後才
  increment_usage(+1)；下游任一失敗則不計量，消除「白扣」。
* 跨租戶隔離：用 path `tenant_id` 查 DB LineChannelConfig，找不到 → 404。
* Translator / LineReplyClient 由 FastAPI dependency 注入，測試可 override。

已知 tradeoff（資安審查確認可接受）
-----------------------------------
* DB 查詢必須早於簽章驗證，因為 channel_secret 存在 DB。
  副作用：攻擊者可藉 404（無 config）vs 400（簽章錯）區分哪些 tenant_id
  已設定 LINE config。MVP 已接受此風險，生產前可改為統一回 400。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from saas_mvp.db import get_db
from saas_mvp.line_client import LineReplyClient, get_line_client
from saas_mvp.models.line_channel_config import (
    InvalidTargetLangError,
    LineChannelConfig,
    LineConfigDecryptionError,
    validate_target_lang,
)
from saas_mvp.models.line_user_lang import get_user_lang, upsert_user_lang
from saas_mvp.models.tenant import Tenant
from saas_mvp.quota import has_quota, increment_usage
from saas_mvp.translation import Translator, get_translator
from saas_mvp.translation.commands import parse_lang_command

_log = logging.getLogger(__name__)

router = APIRouter(
    prefix="/line",
    tags=["line-webhook"],
)

_QUOTA_EXCEEDED_MSG = (
    "翻譯配額已超過今日上限，請明日再試或升級方案。"
)


def _verify_signature(body: bytes, channel_secret: str, signature: str) -> bool:
    """驗證 X-Line-Signature（HMAC-SHA256 + base64）。

    LINE 文件：
      signature = base64( HMAC-SHA256(channel_secret, body) )
    """
    mac = hmac.new(
        channel_secret.encode("utf-8"),
        body,
        hashlib.sha256,
    )
    expected = base64.b64encode(mac.digest()).decode("utf-8")
    return hmac.compare_digest(expected, signature)


@router.post("/webhook/{tenant_id}", summary="LINE Webhook — 接收事件、翻譯並回覆")
async def line_webhook(
    tenant_id: int,
    request: Request,
    db: Session = Depends(get_db),
    translator: Translator = Depends(get_translator),
    line_client: LineReplyClient = Depends(get_line_client),
):
    # ── 1. 取得 raw body（HMAC 驗章必須用原始 bytes） ──────────────────────────
    body = await request.body()

    # ── 2. 查詢租戶 LINE 設定（跨租戶隔離：用 tenant_id 查 DB） ───────────────
    # 注意：因為 channel_secret 存在 DB，必須先查 DB 才能做簽章驗證，
    # 所以 DB 查詢早於簽章驗證是結構限制，非邏輯錯誤。
    cfg = db.execute(
        select(LineChannelConfig).where(LineChannelConfig.tenant_id == tenant_id)
    ).scalar_one_or_none()

    if cfg is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="LINE channel config not found for this tenant",
        )

    # ── 3. 解密 channel credentials（金鑰輪換或資料損壞時提前返回 200 以阻止 LINE 重試） ──
    try:
        channel_secret = cfg.channel_secret
        access_token = cfg.access_token
    except LineConfigDecryptionError:
        _log.error(
            "LINE channel config decryption failed for tenant %d — returning 200 to stop LINE retry",
            tenant_id,
        )
        return {"status": "ok"}

    # ── 4. 驗章 X-Line-Signature ───────────────────────────────────────────────
    signature = request.headers.get("X-Line-Signature", "")
    if not signature:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing X-Line-Signature header",
        )

    if not _verify_signature(body, channel_secret, signature):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid X-Line-Signature",
        )

    # ── 5. 解析 JSON payload ───────────────────────────────────────────────────
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Request body is not valid JSON",
        )

    events = payload.get("events", [])

    # 查租戶 plan（quota 計算用）
    tenant = db.get(Tenant, tenant_id)
    # tenant 不可能為 None（cfg 已確認 tenant_id 存在），防衛性保留
    if tenant is None:  # pragma: no cover
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="tenant not found")

    # ── 6. 處理每個 event ──────────────────────────────────────────────────────
    for event in events:
        event_type = event.get("type")

        # 非 message event → 略過
        if event_type != "message":
            continue

        message = event.get("message", {})
        # 非文字訊息 → 略過
        if message.get("type") != "text":
            continue

        text = message.get("text", "")
        reply_token = event.get("replyToken", "")
        line_user_id = event.get("source", {}).get("userId", "")

        # 解析 /lang 指令
        lang_code, remaining_text = parse_lang_command(text)

        if lang_code:
            # BCP-47 格式驗證（防注入，已明確拒絕含特殊字元的惡意輸入）
            try:
                validate_target_lang(lang_code)
            except InvalidTargetLangError:
                line_client.reply(
                    reply_token,
                    f"無效的語言代碼：{lang_code!r}，請使用 BCP-47 格式（例如：ja、en、zh-TW）",
                    access_token=access_token,
                )
                continue

            if not remaining_text:
                # 純 /lang xx 指令 → 持久化偏好 + 回覆確認，不計 quota
                if line_user_id:
                    upsert_user_lang(db, tenant_id, line_user_id, lang_code)
                line_client.reply(
                    reply_token,
                    f"語言已切換為：{lang_code}",
                    access_token=access_token,
                )
                continue

        # 決定翻譯目標語言
        # 優先序：/lang 行內指定 > 使用者持久化偏好 > channel 預設
        if lang_code:
            target_lang = lang_code  # 同一則訊息含 /lang xx + 文字
        elif line_user_id:
            target_lang = (
                get_user_lang(db, tenant_id, line_user_id) or cfg.default_target_lang
            )
        else:
            target_lang = cfg.default_target_lang

        translate_text = remaining_text if lang_code else text

        # ── 6a. 配額檢查（非遞增；超量 → 回覆明確訊息，不翻譯、不計量） ───────
        if not has_quota(db, tenant_id, tenant.plan):
            line_client.reply(reply_token, _QUOTA_EXCEEDED_MSG, access_token=access_token)
            continue

        # ── 6b. 翻譯 ──────────────────────────────────────────────────────────
        translated = translator.translate(translate_text, target_lang)

        # ── 6c. 回覆 ──────────────────────────────────────────────────────────
        line_client.reply(reply_token, translated, access_token=access_token)

        # ── 6d. 翻譯與回覆皆成功後才計量 +1（消除下游失敗白扣） ────────────────
        increment_usage(db, tenant_id)

    return {"status": "ok"}
