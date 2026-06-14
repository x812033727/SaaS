"""LINE Webhook 端點 — /line/webhook/{tenant_id}

設計決策（架構師確認）
---------------------
* raw body 用 Request.body() 取得，再 JSON decode，確保 HMAC 比對用原始 bytes。
* X-Line-Signature 驗章失敗 → 400（符合 LINE 文件建議）。
* 非文字事件靜默略過，回 200 OK。
* quota 超量 → 不翻譯、以明確訊息 reply（不拋 500）。
* 跨租戶隔離：用 path `tenant_id` 查 DB LineChannelConfig，找不到 → 404。
* Translator / LineReplyClient 由 FastAPI dependency 注入，測試可 override。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from saas_mvp.db import get_db
from saas_mvp.line_client import LineReplyClient, get_line_client
from saas_mvp.models.line_channel_config import LineChannelConfig
from saas_mvp.models.tenant import Tenant
from saas_mvp.quota import check_and_increment
from saas_mvp.translation import Translator, get_translator
from saas_mvp.translation.commands import parse_lang_command

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
    cfg = db.execute(
        select(LineChannelConfig).where(LineChannelConfig.tenant_id == tenant_id)
    ).scalar_one_or_none()

    if cfg is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="LINE channel config not found for this tenant",
        )

    # ── 3. 驗章 X-Line-Signature ───────────────────────────────────────────────
    signature = request.headers.get("X-Line-Signature", "")
    if not signature:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing X-Line-Signature header",
        )

    if not _verify_signature(body, cfg.channel_secret, signature):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid X-Line-Signature",
        )

    # ── 4. 解析 JSON payload ───────────────────────────────────────────────────
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

    access_token = cfg.access_token

    # ── 5. 處理每個 event ──────────────────────────────────────────────────────
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

        # 解析 /lang 指令
        lang_code, remaining_text = parse_lang_command(text)
        target_lang = lang_code if lang_code else cfg.default_target_lang

        if lang_code and not remaining_text:
            # 純粹的 /lang xx 指令（無後續文字） → 回覆確認，不計 quota
            line_client.reply(
                reply_token,
                f"語言已切換為：{target_lang}",
                access_token=access_token,
            )
            continue

        translate_text = remaining_text if lang_code else text

        # ── 5a. 配額檢查（超量 → 回覆明確訊息，不翻譯） ───────────────────────
        try:
            check_and_increment(db, tenant_id, tenant.plan)
        except HTTPException as exc:
            if exc.status_code == 429:
                line_client.reply(reply_token, _QUOTA_EXCEEDED_MSG, access_token=access_token)
                continue
            raise

        # ── 5b. 翻譯 ──────────────────────────────────────────────────────────
        translated = translator.translate(translate_text, target_lang)

        # ── 5c. 回覆 ──────────────────────────────────────────────────────────
        line_client.reply(reply_token, translated, access_token=access_token)

    return {"status": "ok"}
