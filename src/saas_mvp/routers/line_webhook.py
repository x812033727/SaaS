"""LINE Webhook 端點 — /line/webhook/{tenant_id}

設計決策（架構師確認）
---------------------
* raw body 用 Request.body() 取得，再 JSON decode，確保 HMAC 比對用原始 bytes。
* X-Line-Signature 驗章失敗 → 400（符合 LINE 文件建議）。
* 非文字事件靜默略過，回 200 OK。
* 重送去重：deliveryContext.isRedelivery=true 的 event 一律略過（不翻譯、不計量、回 200），
  避免 LINE 重投造成重複翻譯與重複扣 quota。判定採無狀態旗標，缺欄位視為首投。
* quota 超量 → 不翻譯、以明確訊息 reply（不拋 500）。
* quota 計費點後移：先做非遞增檢查放行，待 translate 與 reply 皆成功後才
  increment_usage(+1)；下游任一失敗則不計量，消除「白扣」。increment_usage
  傳入 plan 於鎖內重驗 limit，消除 has_quota→increment 的 TOCTOU 超賣。
* 跨租戶隔離：用 path `tenant_id` 查 DB LineChannelConfig。
* 租戶列舉防護：所有驗章失敗——無 config、缺 X-Line-Signature header、簽章錯——
  一律回相同的 400 + 相同 detail，外部無法藉狀態碼或回應內容區分租戶是否已設定；
  無 config 路徑並做等量 HMAC 計算對齊 timing。
* Translator / LineReplyClient 由 FastAPI dependency 注入，測試可 override。
"""

from __future__ import annotations

import asyncio
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

# 統一的「驗章失敗」回應 detail。無 config 與簽章錯共用，避免外部藉 detail 區分租戶是否已設定。
_INVALID_SIGNATURE_DETAIL = "Invalid X-Line-Signature"


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
        # 消除租戶列舉 oracle：未設定 config 的 tenant_id 與「簽章錯誤」回應一致
        # （同 400、同 detail），外部無法藉狀態碼或回應內容區分「未設定」與「簽章錯」。
        # 先做一次等量 HMAC 計算，使兩路徑時間特徵相近，降低 timing side-channel。
        hmac.new(b"\x00" * 32, body, hashlib.sha256).digest()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=_INVALID_SIGNATURE_DETAIL,
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
    # 列舉防護：缺 header、簽章錯、無 config 三種失敗一律回相同的 400 + detail，
    # 任何分支都不可洩漏「該 tenant 是否已設定 LINE」。缺 header 不可單獨給
    # 「Missing ...」訊息，否則攻擊者送無 header 請求即可逐一列舉已設定租戶。
    signature = request.headers.get("X-Line-Signature", "")
    if not signature or not _verify_signature(body, channel_secret, signature):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=_INVALID_SIGNATURE_DETAIL,
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
        # 重送去重：LINE 在前次未收到 2xx 時會重投同一 event，
        # deliveryContext.isRedelivery=true。對重送 event 一律略過——不翻譯、
        # 不回覆、不計 quota——避免重複翻譯與重複扣量。判定鍵採無狀態旗標。
        # 缺 deliveryContext（或為 null）/isRedelivery 非 True 時，視為首投，行為不變。
        delivery_ctx = event.get("deliveryContext") or {}
        if delivery_ctx.get("isRedelivery") is True:
            _log.info(
                "skip redelivered LINE event for tenant %d (isRedelivery=true)",
                tenant_id,
            )
            continue

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
        # 計費點後移：此處僅「檢查」不「遞增」，避免下游翻譯／回覆失敗時白扣。
        if not has_quota(db, tenant_id, tenant.plan):
            line_client.reply(reply_token, _QUOTA_EXCEEDED_MSG, access_token=access_token)
            continue

        # ── 6b. 翻譯（失敗會向上拋；此時尚未計量，不會白扣） ────────────────────
        # translator.translate 為阻塞 I/O（urllib），handler 為 async；用 to_thread
        # 移出 event loop，避免高並發時阻塞其他請求。介面不變，向後兼容。
        translated = await asyncio.to_thread(
            translator.translate, translate_text, target_lang
        )

        # ── 6c. 回覆（失敗會向上拋；此時尚未計量，不會白扣） ────────────────────
        # NOTE: line_client.reply 同為阻塞 I/O — 高流量下應 wrap in asyncio.to_thread (M2)
        line_client.reply(reply_token, translated, access_token=access_token)

        # ── 6d. 翻譯與回覆皆成功後才計量 +1（消除下游失敗白扣） ────────────────
        # 傳入 plan 啟用鎖內重驗 limit，消除 has_quota→increment 的 TOCTOU 超賣。
        increment_usage(db, tenant_id, tenant.plan)

    return {"status": "ok"}
