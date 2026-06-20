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
* destination 二次驗證：驗簽通過後，若 cfg.line_bot_user_id 已設定且
  payload.destination 不符 → 回 400（共用 _INVALID_SIGNATURE_DETAIL，不洩漏租戶存在性）；
  舊 config（line_bot_user_id=NULL）略過此 check，向後相容。
* 租戶列舉防護：所有驗章失敗——無 config、缺 X-Line-Signature header、簽章錯、
  destination 不符——一律回相同的 400 + 相同 detail，外部無法藉狀態碼或回應內容
  區分租戶是否已設定。
  * 四條拒絕路徑皆收斂到同一等量時間驗簽 helper `_constant_time_verify`（含完整
    `hmac.new → digest → b64encode → hmac.compare_digest`），使「無 config」「缺
    header」「簽章錯」三條路徑的 CPU 開銷強制對等，消除 timing side-channel。
  * 對外回應字串完全相同；伺服器端 log 仍以 `reason=no_config / missing_header /
    bad_signature / bad_destination` 區分，僅供監控/告警，不對外暴露。
* Translator / LineReplyClient 由 FastAPI dependency 注入，測試可 override。

背景任務語意（Task #1 切片）
---------------------
* handler 在「驗章 / 解密 / JSON parse / destination 二次驗證」**全部通過**
  後，把「處理 events 鏈」丟進 FastAPI ``BackgroundTasks``，自身立即回
  ``{"status": "ok"}`` 200，**不再同步執行 translate / reply / increment**。
  符合 LINE 官方「We recommend processing events asynchronously」建議
  （LINE 暫停推送條件是「fail to receive」= 沒回 2xx；只要回 200 就不會被
  暫停，與回應延遲無關）。
* 切片邊界：「要不要回 200」的所有判斷**必須留 handler 主體**——列舉防護
  的四條拒絕路徑（無 config / 缺 header / 簽章錯 / destination 不符）絕不
  丟背景，否則攻擊者趁 background 延遲即可繞過同 400 + 同 detail 的
  timing 收斂。
* DB session：handler 用 ``Depends(get_db)`` 拿 request-scoped session
  僅做「查 cfg + 查 tenant plan」；**丟背景前先抓出綁定 engine**
  ``bind = db.get_bind()``，把 ``bind`` 傳進 ``_process_events``。
  背景任務內部以 ``Session(bind=bind)`` **自管 session**——request-scoped
  session 在 response 後由 FastAPI 依賴收尾關閉，背景任務跨 await 邊界
  仍持有會觸發「session 已關閉」錯誤。為什麼不傳 handler 的 ``db``
  進去？同樣理由（已關閉 session）。為什麼不讓背景用模組全域
  ``SessionLocal()``？production 綁 production engine、測試
  ``dependency_overrides[get_db]`` 綁的是另一顆 in-memory
  ``StaticPool`` engine——兩顆 :memory: 是各自獨立的空 DB，背景寫到
  錯的庫，測試端讀不到副作用。用 ``db.get_bind()`` 從 request session
  抓出當下綁定的 engine，**測試端**自然拿測試的 engine（StaticPool
  跨執行緒共享同一顆 in-memory DB → 背景看得到資料、零改測試）、
  **production 端**拿真實 engine。``increment_usage`` 的
  SELECT FOR UPDATE 鎖、commit、close 全部在獨立交易內完成。
* 錯誤處理：``_process_events`` 以單一 event 為例外邊界；每個 event
  各自 ``try/except Exception``，失敗時先 ``db.rollback()`` 重置 session、
  再 ``log.exception`` 記錄含 ``event_idx`` 的錯誤並繼續下一筆。單一 event
  失敗不得中斷同批其他 events；不保留外層 ``except`` 吃掉整批。
* in-process 假設：BackgroundTasks 是 Starlette 內建、in-process 機制，
  跨 worker process **不**傳遞任務。``uvicorn --workers N`` 部署時，
  handler 收到的 task 只在接收當下的 worker 跑；該 worker crash / restart
  時任務丟失，由 LINE redelivery 補救。M1 單 worker 部署無此問題。
* M2 技術債（不修 — 詳見下方錨點）：
  - async 化（真方向）：``HttpLineReplyClient`` 改用 ``httpx.AsyncClient``
    （lifespan 管理單一 instance）或 LINE Bot SDK v3 ``AsyncMessagingApi``；
    ``LineReplyClient.reply`` 改為 async 方法；``_process_events`` 改
    ``async def``；``Session(bind=bind)`` 改 ``AsyncSession`` +
    ``async with async_engine.begin()`` 整套重寫；fake / spy mock 全要動。
    **1 個獨立 PR 的工作量**。M1 流量下 Starlette 預設 40 thread 的
    threadpool 不是瓶頸，提早開工 ROI 為負。
  - task queue 化：換 ARQ / Celery 支援跨 worker process、加入重試與
    dead-letter queue、補背景任務監控指標——與 async 化**無因果**的
    獨立子任務，可分開排程。
  - ``asyncio.to_thread`` 包裝**不再列入技術債**——理由見步驟 6c 註解
    （canonical 說明位置）：「sync 函式已在 threadpool」再包一層屬冗餘
    雙重包裝（anyio 反模式，淨效果為零、反而多佔一條 thread）。

  詳見 issue #XXX / 2026-Q2 async 化重構追蹤（TODO: 開 issue、替換 #XXX）。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
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
from saas_mvp.quota import has_char_quota, has_quota, increment_usage
from saas_mvp.translation import Translator, get_translator
from saas_mvp.translation.commands import parse_lang_command

_log = logging.getLogger(__name__)

# ── Webhook 路徑單一真相來源 ──────────────────────────────────────────────────
# router 掛載路徑與「對外公告的 webhook_url」共用同一組常數，避免兩處各自硬碼、
# 路由改名後靜默脫節。tenants router 的自助端點 import webhook_url_for() 組裝回應，
# 並有測試斷言「webhook_url 與 app 實際註冊的 route 一致」作保底。
LINE_WEBHOOK_PREFIX = "/line"
_WEBHOOK_ROUTE = "/webhook/{tenant_id}"
# 完整對外路徑模板，例：/line/webhook/{tenant_id}
LINE_WEBHOOK_PATH_TEMPLATE = LINE_WEBHOOK_PREFIX + _WEBHOOK_ROUTE


def webhook_url_for(tenant_id: int) -> str:
    """組裝租戶專屬 webhook 相對路徑（host 由部署端拼接）。"""
    return LINE_WEBHOOK_PATH_TEMPLATE.format(tenant_id=tenant_id)


router = APIRouter(
    prefix=LINE_WEBHOOK_PREFIX,
    tags=["line-webhook"],
)

_QUOTA_EXCEEDED_MSG = (
    "翻譯配額已超過今日上限，請明日再試或升級方案。"
)

# 統一的「驗章失敗」回應 detail。四條拒絕路徑（無 config / 缺 header / 簽章錯 /
# destination 不符）共用，避免外部藉 detail 區分租戶是否已設定。
_INVALID_SIGNATURE_DETAIL = "Invalid X-Line-Signature"

# 等量時間驗簽用的固定 dummy secret（32 bytes 對應 SHA-256 block size）。
# cfg 缺失路徑會把這個 dummy 餵進 helper，確保與「簽章錯」分支跑完全相同的
# HMAC + b64encode + compare_digest 鏈，消除 timing side-channel。
_DUMMY_SECRET: bytes = b"\x00" * 32


def _constant_time_verify(body: bytes, signature: str, secret: bytes) -> bool:
    """等量時間驗證 X-Line-Signature（HMAC-SHA256 + base64 + compare_digest）。

    介面刻意收 `secret: bytes`：cfg 缺失路徑可傳 `_DUMMY_SECRET`，與正常路徑
    跑完全相同的計算鏈，保證分支間的 CPU 開銷對等。helper 是單一實作點——
    未來若換 signature scheme 或加 nonce，只需改這一處。

    LINE 文件：
        signature = base64( HMAC-SHA256(channel_secret, body) )
    """
    mac = hmac.new(secret, body, hashlib.sha256)
    expected = base64.b64encode(mac.digest()).decode("utf-8")
    return hmac.compare_digest(expected, signature)


@router.post(_WEBHOOK_ROUTE, summary="LINE Webhook — 接收事件、翻譯並回覆")
async def line_webhook(
    tenant_id: int,
    request: Request,
    background_tasks: BackgroundTasks,
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
        # 走完整等量驗簽鏈（new → digest → b64encode → compare_digest）對齊 timing，
        # 不再 short-circuit：secret 用 _DUMMY_SECRET，結果必 False。
        header_signature = request.headers.get("X-Line-Signature", "")
        _log.warning(
            "webhook rejected reason=%s tenant=%d",
            "no_config",
            tenant_id,
        )
        _constant_time_verify(body, header_signature, _DUMMY_SECRET)
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
    # 三條路徑皆走同一 helper（_constant_time_verify），缺 header 也不再
    # short-circuit——把空字串餵進 helper 讓 compare_digest 自然回 False，
    # 與「簽章錯」分支的 CPU 開銷完全對等。
    header_signature = request.headers.get("X-Line-Signature", "")
    if not _constant_time_verify(
        body, header_signature, channel_secret.encode("utf-8")
    ):
        reason = "missing_header" if not header_signature else "bad_signature"
        _log.warning(
            "webhook rejected reason=%s tenant=%d",
            reason,
            tenant_id,
        )
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

    # ── 5a. destination 二次驗證（驗簽通過後才信任 payload 內容） ──────────────
    # 防 LINE Console 錯配：租戶 A 的 bot 事件被打到租戶 B 的 webhook URL。
    # 僅當 cfg.line_bot_user_id 已回填（經 bot/info）才比對；舊 config（NULL）略過，
    # 行為與現況一致（向後相容）。失敗回應與簽章錯誤「完全一致」（同 400、同 detail），
    # 不洩漏租戶存在性；log 不含 destination 值與 tenant_id，避免 log 側資訊洩漏。
    if cfg.line_bot_user_id and payload.get("destination") != cfg.line_bot_user_id:
        # 對外回應與簽章失敗完全一致（同 400、同 detail）；log 區分 reason 供監控。
        # log 不含 destination 值，避免側通道；tenant_id 在 log 端用於定位，無列舉風險。
        _log.warning(
            "webhook rejected reason=%s tenant=%d",
            "bad_destination",
            tenant_id,
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=_INVALID_SIGNATURE_DETAIL,
        )

    events = payload.get("events", [])

    # 查租戶 plan（quota 計算用）
    tenant = db.get(Tenant, tenant_id)
    # tenant 不可能為 None（cfg 已確認 tenant_id 存在），防衛性保留
    if tenant is None:  # pragma: no cover
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="tenant not found")

    # ── 6. 把 events 處理鏈丟背景，handler 立即回 200 ─────────────────────────
    # 把 for-loop 整段（redelivery 去重 + 雙閘 quota + translate + reply +
    # increment_usage）原封搬入 _process_events；handler 自身**不再同步**
    # 執行任何 translate / reply。Starlette TestClient 內部 await
    # self.background() 才 return response，測試端可繼續用既有 spy 斷言
    # （response 返回時 background 已跑完）。
    #
    # 輸入契約只接「純資料 + engine handle」：tenant_id / plan /
    # default_target_lang / access_token / events / engine (= db.get_bind())。
    # 不傳 Request、不傳 request-scoped db（response 後由 FastAPI 收尾
    # 關閉、背景持有會炸）、不傳 cfg 物件（channel_secret 背景無用途，
    # 縮窄暴露面）。傳 engine 而非工廠，理由見模組 docstring：測試端
    # `dependency_overrides[get_db]` 注入的是綁在測試 in-memory engine
    # 的 session，這個 engine 必須跟著帶進背景，否則背景寫到錯的庫。
    # 取 bind 的時機是 request session 還活著的「現在」，response 後
    # db 已關閉、bind 屬性依然可讀（engine 物件獨立於 session 生命週期）。
    bind = db.get_bind()
    background_tasks.add_task(
        _process_events,
        tenant_id,
        tenant.plan,
        cfg.default_target_lang,
        access_token,
        events,
        translator,
        line_client,
        bind,
    )

    return {"status": "ok"}


# ── 背景任務：events 處理鏈（handler 同步段切片） ──────────────────────────────
def _process_events(
    tenant_id: int,
    plan: str,
    default_target_lang: str,
    access_token: str,
    events: list,
    translator: Translator,
    line_client: LineReplyClient,
    bind,
) -> None:
    """在 background 內依序處理每個 event（行為與順序與背景化前完全一致）。

    從 line_webhook handler 同步段整段剪下、零行為改動：
      redelivery 去重 → event type 過濾 → /lang 解析 → 雙閘 quota
      → translate → reply → increment_usage

    DB session 自管：每個 event 進入處理邊界時以
    ``with Session(bind=bind) as db`` 新開 session，離開該筆 event
    自動 close；不跨 event 共用 session，也不保留外層 ``except``。
    ``bind`` 由 handler 在丟背景前以 ``db.get_bind()`` 抓出——傳 engine 而非
    factory，是為了對齊測試的 ``dependency_overrides[get_db]`` 機制：
    request session 綁的是測試自己的 in-memory StaticPool engine，背景
    用同一 engine 開 session 才能看到資料（若改用模組全域 ``SessionLocal()``
    會綁到 production 引擎，兩顆 :memory: 互相獨立、測試端永遠讀不到副作用）。
    request-scoped session（``Depends(get_db)``）在 response 後由 FastAPI
    收尾關閉，**不可**傳進背景任務——否則 SELECT FOR UPDATE / commit 會在
    已關閉 session 上跑、報「this Session's transaction has been rolled
    back due to a previous exception」。

    例外處理：每個 event 各自 ``try/except Exception``。單筆失敗時先
    ``db.rollback()`` 重置該筆 event 的 SQLAlchemy session，再記錄含
    ``event_idx`` 的 ``log.exception``，然後關閉該 session 並繼續下一個
    event；單一 event 失敗不會中斷同批其他 events。
    """
    for event_idx, event in enumerate(events):
        with Session(bind=bind) as db:
            try:
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
                        get_user_lang(db, tenant_id, line_user_id) or default_target_lang
                    )
                else:
                    target_lang = default_target_lang

                translate_text = remaining_text if lang_code else text

                # ── 6a-1. 次數配額檢查（非遞增；超量 → 回覆明確訊息，不翻譯、不計量） ─
                # 計費點後移：此處僅「檢查」不「遞增」，避免下游翻譯／回覆失敗時白扣。
                if not has_quota(db, tenant_id, plan):
                    line_client.reply(reply_token, _QUOTA_EXCEEDED_MSG, access_token=access_token)
                    continue

                # ── 6a-2. 字數配額檢查（譯文字數，與次數軸並列；任一不通即擋下） ─────
                # 採譯文字數（len(translated)），理由：譯文是後端可控、語意單一的字串點，
                # 與後扣骨架自然對齊，源文多語混雜與表情字元歧義可避開。
                # 兩道閘各自獨立查詢、獨立擋下，沿用既有「單次溢出可接受」語意。
                if not has_char_quota(db, tenant_id, plan):
                    line_client.reply(reply_token, _QUOTA_EXCEEDED_MSG, access_token=access_token)
                    continue

                # ── 6b. 翻譯（失敗會向上拋；此時尚未計量，不會白扣） ────────────────────
                # 為何 sync 直呼：見步驟 6c 註解（BackgroundTasks 自動
                # run_in_threadpool 已將 I/O 移出 event loop，無需 to_thread）。
                translated = _translate_sync(translator, translate_text, target_lang)

                # ── 6c. 回覆（失敗會向上拋；此時尚未計量，不會白扣） ────────────────────
                # 為何 sync 直呼：reply 為阻塞 I/O（urllib.request.urlopen），但
                # _process_events 是 sync 函式、由 BackgroundTasks 自動
                # run_in_threadpool 移出 event loop，等同 asyncio.to_thread 效果。
                # sync 函式內亦無法 await，再包一層 asyncio.to_thread 屬冗餘雙重
                # 包裝（anyio 反模式，淨效果為零、反而多佔一條 thread）。本註解
                # 是「sync 函式無需 to_thread 包裝」的 canonical 說明位置——模組
                # docstring M2 段、_translate_sync helper 與 6b 步驟都指向這裡。
                # threadpool 線程佔用改善路徑見模組 docstring M2 段。
                line_client.reply(reply_token, translated, access_token=access_token)

                # ── 6d. 翻譯與回覆皆成功後才計量（消除下游失敗白扣） ─────────────────
                # 單一 ``increment_usage(plan, chars=N)`` 一次 SELECT FOR UPDATE、
                # 一次 commit 完成 ``count += 1; char_count += len(translated)``——
                # 翻案自舊版「兩並列函式各自鎖 + 各自 commit」：少一輪 DB 往返 +
                # 少一次 commit，鎖窗口由兩次壓成一次。
                #
                # 重驗語意（翻案重點）：count 達 limit 不 +1（沿用舊邏輯，極罕見
                # TOCTOU）；char_count 達/超 char_limit 時**真實累計**寫入
                # ``current + chars``（不 saturate、停在原值）——避免舊版
                # ``saturate + 嚴格 <`` 造成的結構性死閘：下次 has_char_quota
                # 在「達/超 limit」時正確擋下，閘真實有效。代價是「單次溢出」
                # ——同次數軸「單次溢出可接受」語意對齊，舊版「永不超賣計費」
                # 期待被明確捨棄（PR 描述點名）。
                increment_usage(db, tenant_id, plan, chars=len(translated))
            except Exception:
                # 單筆 event 失敗不可污染同批後續 event；rollback 必須先於 log。
                db.rollback()
                _log.exception(
                    "background _process_events failed for tenant %d event_idx=%d (events=%d)",
                    tenant_id,
                    event_idx,
                    len(events),
                )
                continue


def _translate_sync(translator: Translator, text: str, target_lang: str) -> str:
    """同步呼叫翻譯介面（背景任務內執行）。

    helper 封裝是為了維持單一翻譯呼叫點，未來換 async SDK 只改這裡。
    為何可 sync 直呼：見步驟 6c 註解（canonical 說明位置）。
    """
    return translator.translate(text, target_lang)
