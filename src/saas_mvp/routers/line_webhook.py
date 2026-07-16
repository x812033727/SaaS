"""LINE Webhook 端點 — /line/webhook/{tenant_id}

設計決策（架構師確認）
---------------------
* raw body 用 Request.body() 取得，再 JSON decode，確保 HMAC 比對用原始 bytes。
* X-Line-Signature 驗章失敗 → 400（符合 LINE 文件建議）。
* 非文字事件靜默略過，回 200 OK。
* 重送去重：以 webhookEventId 持久化 claim 狀態；processed / pending
  重複 ID 略過，failed 且尚未送出 reply 者允許重試；LINE 的重送提示欄位
  僅作診斷 log。
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
  後，先把 event payload 持久化進 DB outbox，再把「處理 events 鏈」丟進
  FastAPI ``BackgroundTasks``，自身立即回
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
* 持久交付：BackgroundTasks 仍負責低延遲處理，但 event 已在回 200 前落盤。
  該 worker crash / restart 時，scheduler 會跨 process 以 CAS 認領 pending/processing row
  重放；重試用盡後保留 failed dead-letter 供監控與人工排查。
* M2 技術債（不修 — 詳見下方錨點）：
  - async 化（真方向）：``HttpLineReplyClient`` 改用 ``httpx.AsyncClient``
    （lifespan 管理單一 instance）或 LINE Bot SDK v3 ``AsyncMessagingApi``；
    ``LineReplyClient.reply`` 改為 async 方法；``_process_events`` 改
    ``async def``；``Session(bind=bind)`` 改 ``AsyncSession`` +
    ``async with async_engine.begin()`` 整套重寫；fake / spy mock 全要動。
    **1 個獨立 PR 的工作量**。M1 流量下 Starlette 預設 40 thread 的
    threadpool 不是瓶頸，提早開工 ROI 為負。
  - task queue 已以現有 PostgreSQL outbox + scheduler recovery 完成，
    不另引入 ARQ/Celery broker 與重複的部署面。
  - ``asyncio.to_thread`` 包裝**不再列入技術債**——理由見步驟 6c 註解
    （canonical 說明位置）：「sync 函式已在 threadpool」再包一層屬冗餘
    雙重包裝（anyio 反模式，淨效果為零、反而多佔一條 thread）。

  追蹤：
  - async 化：[M2-LINE-WEBHOOK-ASYNC](../../../docs/M2_ISSUES.md#m2-line-webhook-async)
  - task queue 化：[M2-LINE-WEBHOOK-QUEUE](../../../docs/M2_ISSUES.md#m2-line-webhook-queue)
  - event hardening：[M2-LINE-WEBHOOK-001](../../../docs/M2_ISSUES.md#m2-line-webhook-001)
    / [M2-LINE-WEBHOOK-002](../../../docs/M2_ISSUES.md#m2-line-webhook-002)
    / [M2-LINE-WEBHOOK-003](../../../docs/M2_ISSUES.md#m2-line-webhook-003)
    / [M2-LINE-WEBHOOK-004](../../../docs/M2_ISSUES.md#m2-line-webhook-004)
"""

from __future__ import annotations

import base64
import datetime
import hashlib
import hmac
import json
import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from saas_mvp.config import settings
from saas_mvp.db import get_db
from saas_mvp.line_client import (
    LineProfileClient,
    LineReplyClient,
    get_line_client,
    get_profile_client,
)
from saas_mvp.models.line_channel_config import (
    InvalidTargetLangError,
    LineChannelConfig,
    LineConfigDecryptionError,
    validate_target_lang,
)
from saas_mvp.models.line_webhook_event import (
    LineWebhookEvent,
    LineWebhookEventStage,
    LineWebhookEventStatus,
)
from saas_mvp.models.customer import Customer, upsert_customer_from_line
from saas_mvp.models.line_user_lang import get_user_lang, upsert_user_lang
from saas_mvp.models.tenant import Tenant
from saas_mvp.quota import has_char_quota, has_quota, increment_usage
from saas_mvp.translation import TranslationResult, Translator, get_translator
from saas_mvp.translation.commands import parse_lang_command
from saas_mvp.booking.commands import parse_booking_command, parse_postback_data
from saas_mvp.services import booking as booking_svc
from saas_mvp.services import booking_form as booking_form_svc
from saas_mvp.services import waitlist as waitlist_svc
from saas_mvp.services import auto_reply as auto_reply_svc
from saas_mvp.services import catalog as catalog_svc
from saas_mvp.services import coupons as coupons_svc
from saas_mvp.services import features as features_svc
from saas_mvp.services import flex_menu as flex_menu_svc
from saas_mvp.services import line_chat as line_chat_svc
from saas_mvp.services import shop as shop_svc
from saas_mvp.services import slots as slots_svc
from saas_mvp.services import staff as staff_svc
from saas_mvp.services.payment import get_payment_provider
from saas_mvp.models.auto_reply_rule import REPLY_TYPE_FLEX, REPLY_TYPE_TEXT

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
    profile_client: LineProfileClient = Depends(get_profile_client),
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
    # 把 for-loop 整段（webhookEventId claim + 雙閘 quota + translate + reply +
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
    # bot_mode 為純字串，現在（request session 仍活）讀出後當資料傳入背景。
    bot_mode = cfg.bot_mode or "translation"
    # 配額用 effective_plan（含試用）；純字串，request session 活著時算出。
    from saas_mvp.services.plans import effective_plan

    # 交付保證邊界：在回 200 前將每筆 webhookEventId + payload commit
    # 進 DB。即使 worker 在 Starlette 開始 BackgroundTasks 前 crash，
    # scheduler 仍能從 pending row 重放，事件不會靜默遺失。
    # 無 webhookEventId 的舊格式 event 無法建立冪等 key，維持原本
    # background 直接處理的相容行為（LINE 正式 event 會帶 ID）。
    claimed_row_ids: list[int | None] = []
    queued_events: list[dict] = []
    for event in events:
        event_row, should_process = _claim_webhook_event(db, tenant_id, event)
        if not should_process:
            continue
        queued_events.append(event)
        claimed_row_ids.append(event_row.id if event_row is not None else None)

    background_tasks.add_task(
        _process_events,
        tenant_id,
        effective_plan(tenant),
        cfg.default_target_lang,
        access_token,
        queued_events,
        translator,
        line_client,
        bind,
        bot_mode,
        profile_client,
        claimed_row_ids,
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
    bot_mode: str = "translation",
    profile_client: LineProfileClient | None = None,
    claimed_row_ids: list[int | None] | None = None,
) -> None:
    """在 background 內依序處理每個 event，並以 webhookEventId 做冪等去重。

    處理順序：
      webhookEventId claim → event type 過濾 → /lang 解析 → 雙閘 quota
      → translate → same-language skip → reply → increment_usage

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
            event_row_id: int | None = None
            stage = LineWebhookEventStage.CLAIMED.value
            stage_holder = [stage]
            try:
                if claimed_row_ids is None:
                    # 向後相容：直接呼叫的測試／內部工具仍可在此 claim。
                    event_row, should_process = _claim_webhook_event(db, tenant_id, event)
                    if not should_process:
                        continue
                    event_row_id = event_row.id if event_row is not None else None
                else:
                    event_row_id = claimed_row_ids[event_idx]

                stage = _handle_line_event(
                    db,
                    tenant_id,
                    plan,
                    default_target_lang,
                    access_token,
                    event,
                    event_idx,
                    translator,
                    line_client,
                    stage_holder,
                    bot_mode,
                    profile_client,
                )
                _mark_webhook_event_processed(db, event_row_id, stage)
            except Exception as exc:
                # 單筆 event 失敗不可污染同批後續 event；rollback 必須先於 log。
                db.rollback()
                _mark_webhook_event_failed(db, event_row_id, stage_holder[0], exc)
                _log.exception(
                    "background _process_events failed for tenant %d event_idx=%d (events=%d)",
                    tenant_id,
                    event_idx,
                    len(events),
                )
                continue


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


_FAILED_RETRYABLE_STAGES_BEFORE_REPLY = (
    LineWebhookEventStage.CLAIMED.value,
    LineWebhookEventStage.QUOTA_CHECKED.value,
    LineWebhookEventStage.TRANSLATED.value,
)


def _claim_webhook_event(
    db: Session,
    tenant_id: int,
    event: dict,
) -> tuple[LineWebhookEvent | None, bool]:
    """以 webhookEventId claim 單筆 event；缺 ID 時退化為直接處理。"""
    webhook_event_id = event.get("webhookEventId")
    if not webhook_event_id:
        return None, True

    row = LineWebhookEvent(
        tenant_id=tenant_id,
        webhook_event_id=webhook_event_id,
        status=LineWebhookEventStatus.PENDING.value,
        last_stage=LineWebhookEventStage.CLAIMED.value,
        # A0.2 outbox：原始 event 隨 claim 落盤 — worker 在處理中死掉時，
        # ops/retry_stuck_webhook_events 有完整素材可重放（LINE 已收到 200
        # 不會重送，此前這類 in-flight 任務直接蒸發）。
        payload_json=json.dumps(event, ensure_ascii=False),
        event_type=(event.get("type") or "")[:32] or None,
    )
    db.add(row)
    try:
        db.commit()
        db.refresh(row)
        return row, True
    except IntegrityError:
        db.rollback()
        retry_claimed = _claim_failed_webhook_event_for_retry(
            db,
            tenant_id,
            webhook_event_id,
        )
        if retry_claimed:
            existing = _get_webhook_event(db, tenant_id, webhook_event_id)
            _log.info(
                "retry failed LINE webhook event tenant=%d webhook_event_id=%s",
                tenant_id,
                webhook_event_id,
            )
            return existing, True

        existing = db.execute(
            select(LineWebhookEvent).where(
                LineWebhookEvent.tenant_id == tenant_id,
                LineWebhookEvent.webhook_event_id == webhook_event_id,
            )
        ).scalar_one_or_none()
        _log.info(
            "skip duplicate LINE webhook event tenant=%d webhook_event_id=%s status=%s",
            tenant_id,
            webhook_event_id,
            existing.status if existing is not None else "unknown",
        )
        return existing, False


def _claim_failed_webhook_event_for_retry(
    db: Session,
    tenant_id: int,
    webhook_event_id: str,
) -> bool:
    """把 reply 前失敗的 row 原子改回 pending；成功者才可重跑。

    attempt_count 達 settings.webhook_max_attempts 後不再 claim，
    LINE 的後續重送落入 duplicate-skip 分支（回 200 吞掉），
    避免持續失敗的事件被無限重處理。
    """
    now = _utcnow()
    result = db.execute(
        update(LineWebhookEvent)
        .where(
            LineWebhookEvent.tenant_id == tenant_id,
            LineWebhookEvent.webhook_event_id == webhook_event_id,
            LineWebhookEvent.status == LineWebhookEventStatus.FAILED.value,
            LineWebhookEvent.attempt_count < settings.webhook_max_attempts,
            or_(
                LineWebhookEvent.last_stage.is_(None),
                LineWebhookEvent.last_stage.in_(
                    _FAILED_RETRYABLE_STAGES_BEFORE_REPLY
                ),
            ),
        )
        .values(
            status=LineWebhookEventStatus.PENDING.value,
            attempt_count=LineWebhookEvent.attempt_count + 1,
            last_error=None,
            last_stage=LineWebhookEventStage.CLAIMED.value,
            processed_at=None,
            updated_at=now,
        )
    )
    if result.rowcount != 1:
        db.rollback()
        return False
    db.commit()
    return True


def _get_webhook_event(
    db: Session,
    tenant_id: int,
    webhook_event_id: str,
) -> LineWebhookEvent | None:
    return db.execute(
        select(LineWebhookEvent).where(
            LineWebhookEvent.tenant_id == tenant_id,
            LineWebhookEvent.webhook_event_id == webhook_event_id,
        )
    ).scalar_one_or_none()


def _mark_webhook_event_processed(
    db: Session,
    event_row_id: int | None,
    stage: str,
) -> None:
    if event_row_id is None:
        return
    row = db.get(LineWebhookEvent, event_row_id)
    if row is None:  # pragma: no cover - defensive only
        return
    now = _utcnow()
    row.status = LineWebhookEventStatus.PROCESSED.value
    row.last_stage = stage
    row.last_error = None
    row.processed_at = now
    row.updated_at = now
    db.commit()


def _mark_webhook_event_failed(
    db: Session,
    event_row_id: int | None,
    stage: str,
    exc: Exception,
) -> None:
    if event_row_id is None:
        return
    row = db.get(LineWebhookEvent, event_row_id)
    if row is None:  # pragma: no cover - defensive only
        return
    now = _utcnow()
    row.status = LineWebhookEventStatus.FAILED.value
    row.last_stage = stage
    # 類名 + 例外訊息（截斷至欄位上限），供事後診斷；純類名資訊量不足。
    row.last_error = f"{type(exc).__name__}: {exc}"[:255]
    # F3/M2-003:遮罩後 traceback 摘要供診斷(不含 locals、截 4000 字)。
    from saas_mvp.obs.errors import safe_traceback

    row.error_detail = safe_traceback(exc)
    row.updated_at = now
    db.commit()


def _handle_line_event(
    db: Session,
    tenant_id: int,
    plan: str,
    default_target_lang: str,
    access_token: str,
    event: dict,
    event_idx: int,
    translator: Translator,
    line_client: LineReplyClient,
    stage_holder: list[str] | None = None,
    bot_mode: str = "translation",
    profile_client: LineProfileClient | None = None,
) -> str:
    """處理單筆 LINE event，回傳最後完成的處理階段。"""
    stage = LineWebhookEventStage.CLAIMED.value
    if stage_holder is not None:
        stage_holder[0] = stage

    delivery_ctx = event.get("deliveryContext") or {}
    if delivery_ctx.get("isRedelivery") is True:
        _log.info(
            "LINE event redelivery flag observed for tenant %d event_idx=%d; using webhookEventId for idempotency",
            tenant_id,
            event_idx,
        )

    # ── 好友事件：三種 bot_mode 一體處理（unfollow 的顧客檔回寫不分模式） ────
    early_etype = event.get("type")
    if early_etype == "follow":
        return _handle_follow_event(
            db,
            tenant_id,
            access_token,
            event,
            line_client,
            stage_holder,
            profile_client,
            bot_mode,
        )
    if early_etype == "unfollow":
        return _handle_unfollow_event(db, tenant_id, event, stage_holder)

    # ── bot_mode 分流：booking/auto_reply 各自處理；translation（預設）維持現狀 ─
    if bot_mode == "booking":
        return _handle_booking_event(
            db,
            tenant_id,
            access_token,
            event,
            line_client,
            stage_holder,
            profile_client,
        )
    if bot_mode == "auto_reply":
        return _handle_auto_reply_event(
            db,
            tenant_id,
            access_token,
            event,
            line_client,
            stage_holder,
        )

    event_type = event.get("type")
    if event_type != "message":
        return stage

    message = event.get("message", {})
    if message.get("type") != "text":
        return stage

    text = message.get("text", "")
    reply_token = event.get("replyToken", "")
    line_user_id = event.get("source", {}).get("userId", "")

    lang_code, remaining_text = parse_lang_command(text)

    if lang_code:
        try:
            validate_target_lang(lang_code)
        except InvalidTargetLangError:
            line_client.reply(
                reply_token,
                f"無效的語言代碼：{lang_code!r}，請使用 BCP-47 格式（例如：ja、en、zh-TW）",
                access_token=access_token,
            )
            return stage

        if not remaining_text:
            if line_user_id:
                upsert_user_lang(db, tenant_id, line_user_id, lang_code)
            line_client.reply(
                reply_token,
                f"語言已切換為：{lang_code}",
                access_token=access_token,
            )
            return stage

    if lang_code:
        target_lang = lang_code
    elif line_user_id:
        target_lang = get_user_lang(db, tenant_id, line_user_id) or default_target_lang
    else:
        target_lang = default_target_lang

    translate_text = remaining_text if lang_code else text

    stage = LineWebhookEventStage.QUOTA_CHECKED.value
    if stage_holder is not None:
        stage_holder[0] = stage
    if not has_quota(db, tenant_id, plan):
        line_client.reply(reply_token, _QUOTA_EXCEEDED_MSG, access_token=access_token)
        return stage

    if not has_char_quota(db, tenant_id, plan):
        line_client.reply(reply_token, _QUOTA_EXCEEDED_MSG, access_token=access_token)
        return stage

    result = _translate_sync(translator, translate_text, target_lang)
    stage = LineWebhookEventStage.TRANSLATED.value
    if stage_holder is not None:
        stage_holder[0] = stage
    if result.skipped:
        _log.info(
            "skip same-language LINE event for tenant %d event_idx=%d detected=%s target=%s",
            tenant_id,
            event_idx,
            result.detected_lang,
            target_lang,
        )
        return stage

    # ── 6c. 回覆（失敗會向上拋；此時尚未計量，不會白扣） ────────────────────
    # reply 是阻塞 I/O，但 _process_events 是 sync 函式，BackgroundTasks 會透過
    # run_in_threadpool 放到 threadpool 執行，已移出 event loop；不需要再包
    # asyncio.to_thread。sync 函式內再包一層屬冗餘雙重包裝，反而多佔 thread。
    line_client.reply(reply_token, result.text, access_token=access_token)
    stage = LineWebhookEventStage.REPLY_SENT.value
    if stage_holder is not None:
        stage_holder[0] = stage

    increment_usage(db, tenant_id, plan, chars=len(result.text))
    stage = LineWebhookEventStage.USAGE_INCREMENTED.value
    if stage_holder is not None:
        stage_holder[0] = stage
    return stage


def _handle_follow_event(
    db: Session,
    tenant_id: int,
    access_token: str,
    event: dict,
    line_client: LineReplyClient,
    stage_holder: list[str] | None = None,
    profile_client: LineProfileClient | None = None,
    bot_mode: str = "translation",
) -> str:
    """follow（加好友/解除封鎖）：建/更新顧客檔 + 回歡迎訊息。

    顧客檔 upsert 為冪等（LINE redelivery / 重複 follow 安全）；歡迎訊息
    文案取租戶自訂 welcome_message，NULL/空白時依 bot_mode 用內建預設。
    booking 模式附「開始預約/查看時段/我的預約」quick-reply 降低第一步門檻。
    """
    stage = LineWebhookEventStage.CLAIMED.value
    if stage_holder is not None:
        stage_holder[0] = stage

    line_user_id = event.get("source", {}).get("userId", "")
    reply_token = event.get("replyToken", "")

    if line_user_id:
        # follow 可取 profile（好友狀態必然成立）；失敗降級 None 不阻擋建檔。
        display_name = _resolve_display_name(profile_client, line_user_id, access_token)
        customer = upsert_customer_from_line(
            db,
            tenant_id=tenant_id,
            line_user_id=line_user_id,
            display_name=display_name,
            bump_booking=False,
        )
        now = datetime.datetime.now(datetime.timezone.utc)
        customer.line_followed = True
        customer.line_followed_at = now
        db.commit()

    if not reply_token:
        return stage

    cfg = db.execute(
        select(LineChannelConfig).where(LineChannelConfig.tenant_id == tenant_id)
    ).scalar_one_or_none()
    custom = (cfg.welcome_message or "").strip() if cfg is not None else ""

    quick_reply = None
    if bot_mode == "booking":
        text = custom or _DEFAULT_WELCOME_BOOKING
        quick_reply = _WELCOME_QUICK_REPLY
    elif bot_mode == "auto_reply":
        text = custom or _DEFAULT_WELCOME_GENERIC
    else:
        text = custom or _DEFAULT_WELCOME_TRANSLATION

    # 顧客檔已 commit；先標 REPLY_SENT 再 reply（reply 失敗不重試，避免
    # redelivery 重複轟炸歡迎訊息——與 booking 路徑同語意）。
    stage = LineWebhookEventStage.REPLY_SENT.value
    if stage_holder is not None:
        stage_holder[0] = stage
    line_client.reply(reply_token, text, access_token=access_token, quick_reply=quick_reply)
    return stage


def _handle_unfollow_event(
    db: Session,
    tenant_id: int,
    event: dict,
    stage_holder: list[str] | None = None,
) -> str:
    """unfollow（封鎖/解除好友）：標記顧客不可推播。

    商業關鍵：行銷推播（marketing.run_campaign）藉 line_followed=False 跳過
    此顧客，不對推不到的人白扣推播額度。unfollow 事件無 replyToken，不回覆。
    """
    stage = LineWebhookEventStage.CLAIMED.value
    if stage_holder is not None:
        stage_holder[0] = stage

    line_user_id = event.get("source", {}).get("userId", "")
    if not line_user_id:
        return stage

    customer = db.execute(
        select(Customer).where(
            Customer.tenant_id == tenant_id,
            Customer.line_user_id == line_user_id,
        )
    ).scalar_one_or_none()
    if customer is not None:
        customer.line_followed = False
        customer.line_followed_at = datetime.datetime.now(datetime.timezone.utc)
        db.commit()
    return stage


def replay_stored_event(
    db: Session,
    row: LineWebhookEvent,
    *,
    line_client: LineReplyClient | None = None,
    profile_client: LineProfileClient | None = None,
    translator: Translator | None = None,
) -> str:
    """重放一筆卡住的 pending event（A0.2；供 ops/retry_stuck_webhook_events）。

    以 row.payload_json 重建 event，重新載入租戶 LINE 設定與各 client 後走
    ``_handle_line_event``。回傳 processed / failed / skipped。

    注意：replyToken 只有 5 分鐘壽命，重放時多半已過期 — reply 失敗會把
    event 標 failed（比照既有語意），但**副作用（建單等）已在服務層 commit**，
    這正是重放的價值：預約不再蒸發，只是顧客少收到一句回覆。
    """
    from saas_mvp.line_client import (
        HttpLineProfileClient,
        HttpLineReplyClient,
    )
    from saas_mvp.services.plans import effective_plan
    from saas_mvp.translation import get_translator

    if not row.payload_json:
        return "skipped"
    try:
        event = json.loads(row.payload_json)
    except ValueError:
        return "skipped"

    cfg = db.execute(
        select(LineChannelConfig).where(
            LineChannelConfig.tenant_id == row.tenant_id
        )
    ).scalar_one_or_none()
    tenant = db.get(Tenant, row.tenant_id)
    if cfg is None or tenant is None:
        return "skipped"

    stage_holder = [LineWebhookEventStage.CLAIMED.value]
    try:
        stage = _handle_line_event(
            db,
            row.tenant_id,
            effective_plan(tenant),
            cfg.default_target_lang,
            cfg.access_token,
            event,
            0,
            translator or get_translator(),
            line_client or HttpLineReplyClient(),
            stage_holder,
            cfg.bot_mode or "translation",
            profile_client or HttpLineProfileClient(),
        )
        _mark_webhook_event_processed(db, row.id, stage)
        return "processed"
    except Exception as exc:  # noqa: BLE001 — 單筆失敗不中斷批次
        db.rollback()
        _mark_webhook_event_failed(db, row.id, stage_holder[0], exc)
        return "failed"


def _record_line_message_best_effort(
    db: Session,
    *,
    tenant_id: int,
    line_user_id: str,
    text: str,
    direction: str,
) -> None:
    if not line_user_id or not text:
        return
    try:
        if direction == "in":
            line_chat_svc.record_inbound(
                db, tenant_id=tenant_id, line_user_id=line_user_id, text=text
            )
        else:
            line_chat_svc.record_outbound(
                db, tenant_id=tenant_id, line_user_id=line_user_id, text=text
            )
    except Exception:  # noqa: BLE001 - 對話紀錄不得阻斷 webhook 主流程
        db.rollback()
        _log.warning(
            "failed to record LINE %s message for tenant %d",
            direction,
            tenant_id,
            exc_info=True,
        )


def _handle_auto_reply_event(
    db: Session,
    tenant_id: int,
    access_token: str,
    event: dict,
    line_client: LineReplyClient,
    stage_holder: list[str] | None = None,
) -> str:
    """auto_reply 模式：關鍵字規則命中才回覆；未命中靜默。"""
    stage = LineWebhookEventStage.CLAIMED.value
    if stage_holder is not None:
        stage_holder[0] = stage

    if event.get("type") != "message":
        return stage

    message = event.get("message", {})
    if message.get("type") != "text":
        return stage

    text = message.get("text", "")
    reply_token = event.get("replyToken", "")
    line_user_id = event.get("source", {}).get("userId", "")

    _record_line_message_best_effort(
        db,
        tenant_id=tenant_id,
        line_user_id=line_user_id,
        text=text,
        direction="in",
    )

    rules = auto_reply_svc.list_rules(db, tenant_id=tenant_id, active_only=True)
    rule = auto_reply_svc.match(rules, text)
    if rule is None:
        return stage

    if rule.reply_type == REPLY_TYPE_TEXT:
        reply_text = rule.reply_text or ""
        if not reply_text:
            return stage
        line_client.reply(reply_token, reply_text, access_token=access_token)
        _record_line_message_best_effort(
            db,
            tenant_id=tenant_id,
            line_user_id=line_user_id,
            text=reply_text,
            direction="out",
        )
    elif rule.reply_type == REPLY_TYPE_FLEX and rule.flex_menu_id is not None:
        menu = flex_menu_svc.get_menu(
            db, tenant_id=tenant_id, menu_id=rule.flex_menu_id
        )
        cards = flex_menu_svc.list_cards(
            db, tenant_id=tenant_id, menu_id=rule.flex_menu_id
        )
        payload = flex_menu_svc.build_flex_payload(menu, cards)
        alt_text = payload.get("altText", "選單")
        line_client.reply_flex(
            reply_token,
            alt_text,
            payload["contents"],
            access_token=access_token,
        )
        _record_line_message_best_effort(
            db,
            tenant_id=tenant_id,
            line_user_id=line_user_id,
            text=alt_text,
            direction="out",
        )
    else:
        return stage

    stage = LineWebhookEventStage.REPLY_SENT.value
    if stage_holder is not None:
        stage_holder[0] = stage
    return stage


_BOOKING_HELP = (
    "可用指令：\n"
    "・時段 — 查看並選擇可預約時段\n"
    "・預約 — 引導式預約（或：預約 <時段編號> <人數>）\n"
    "・我的預約 — 查看我的預約\n"
    "・改期 <預約編號> — 引導改到其他時段\n"
    "・取消 <預約編號> — 例：取消 7\n"
    "・候補 — 查看/取消我的額滿候補"
    "\n・套票 — 查看可用服務次數與到期日"
    "\n・禮物卡 — 查看餘額；領取禮物卡 <卡號> — 加入錢包"
)
# follow（加好友）預設歡迎文案：租戶未自訂（welcome_message NULL/空白）時依 bot_mode 選用。
_DEFAULT_WELCOME_BOOKING = (
    "感謝加入好友！🎉\n"
    "點下方按鈕即可開始預約，或輸入「時段」查看可預約時段、「我的預約」管理既有預約。"
)
_DEFAULT_WELCOME_TRANSLATION = (
    "感謝加入好友！直接傳訊息即可自動翻譯；輸入 /lang <語言代碼> 可切換目標語言（例：/lang ja）。"
)
_DEFAULT_WELCOME_GENERIC = "感謝加入好友！有任何問題歡迎直接留言。"
# 歡迎訊息／非文字訊息引導的 quick-reply（booking 模式）。
_WELCOME_QUICK_REPLY = [
    ("開始預約", "action=book"),
    ("查看時段", "action=slots"),
    ("我的預約", "action=my"),
]
# 引導式人數上限（quick-reply 按鈕數）
_PARTY_CHOICES_MAX = 6
# 列給使用者選的時段上限（LINE quick-reply 最多 13 筆）
_SLOT_CHOICES_MAX = 12


def _booking_intent(event: dict) -> tuple[str | None, dict]:
    """由 message(text) 或 postback 取出 (action, params)。"""
    etype = event.get("type")
    if etype == "message" and event.get("message", {}).get("type") == "text":
        return parse_booking_command(event["message"].get("text", ""))
    if etype == "postback":
        action, params = parse_postback_data(event.get("postback", {}).get("data", ""))
        # datetimepicker（A1.3）：LINE 把選定日期放 postback.params.date，
        # 併入 params（data 內已帶 date 者優先，不覆蓋）。
        picker = event.get("postback", {}).get("params") or {}
        if action is not None and picker.get("date"):
            params.setdefault("date", picker["date"])
        return action, params
    return None, {}


def _available_slots(db: Session, tenant_id: int) -> list:
    return [
        s
        for s in slots_svc.list_slots(db, tenant_id=tenant_id, active_only=True)
        if s.online_available > 0
    ][:_SLOT_CHOICES_MAX]


def _available_slots_on_date(db: Session, tenant_id: int, date: str | None) -> list:
    """指定日期（'YYYY-MM-DD'）的可預約時段；date 缺/不合法時退回全部（安全降級）。"""
    base = [
        s
        for s in slots_svc.list_slots(db, tenant_id=tenant_id, active_only=True)
        if s.online_available > 0
    ]
    if date:
        base = [s for s in base if s.slot_start.date().isoformat() == date]
    return base[:_SLOT_CHOICES_MAX]


def _available_dates(db: Session, tenant_id: int, limit: int = 10) -> list[str]:
    """有可預約時段（online_available>0）的日期，去重 + 升冪排序 + 取前 limit 筆。"""
    seen: set[str] = set()
    dates: list[str] = []
    for s in slots_svc.list_slots(db, tenant_id=tenant_id, active_only=True):
        if s.online_available <= 0:
            continue
        d = s.slot_start.date().isoformat()
        if d not in seen:
            seen.add(d)
            dates.append(d)
    return sorted(dates)[:limit]


# 日期 quick-reply 上限（LINE quick-reply 最多 13 筆）
_DATE_CHOICES_MAX = 13


def _date_choice_buttons(service_id: int, dates: list[str]) -> list:
    """日期 → quick-reply 按鈕（postback action=pick_date，攜帶 service_id + date）。

    末位附 datetimepicker（A1.3）：日期多於按鈕上限或想選較遠日期時，
    可用原生日曆挑日；選定值由 LINE 放在 postback.params.date
    （_booking_intent 併入 params）。
    """
    _weekday_zh = ("一", "二", "三", "四", "五", "六", "日")
    buttons: list = []
    for d in dates[: _DATE_CHOICES_MAX - 1]:
        try:
            dt = datetime.date.fromisoformat(d)
            label = f"{dt.strftime('%m/%d')} (週{_weekday_zh[dt.weekday()]})"
        except ValueError:
            label = d
        buttons.append(
            (label, f"action=pick_date&service_id={service_id}&date={d}")
        )
    if dates:
        buttons.append({
            "type": "datetimepicker",
            "label": "📅 挑其他日期",
            "data": f"action=pick_date&service_id={service_id}",
            "mode": "date",
            "initial": dates[0],
            "min": dates[0],
            "max": dates[-1],
        })
    return buttons


def _slot_choice_buttons(slots: list) -> list[tuple[str, str]]:
    """時段 → quick-reply 按鈕（postback action=pick_slot）。"""
    return [
        (s.slot_start.strftime("%m/%d %H:%M"), f"action=pick_slot&slot_id={s.id}")
        for s in slots
    ]


def _party_choice_buttons(slot_id: int, max_party: int) -> list[tuple[str, str]]:
    """人數 → quick-reply 按鈕（postback action=book）。"""
    upper = max(1, min(_PARTY_CHOICES_MAX, max_party))
    return [
        (f"{n} 位", f"action=book&slot_id={slot_id}&party={n}")
        for n in range(1, upper + 1)
    ]


def _prompt_choose_slot(db: Session, tenant_id: int) -> tuple[str, list | None]:
    slots = _available_slots(db, tenant_id)
    if not slots:
        return "目前沒有可預約的時段。", None
    return "請選擇時段：", _slot_choice_buttons(slots)


# ── 引導式對話：服務 → 日期 → 員工 → 時段 → 確認（stateless，狀態以 postback 攜帶） ──

def _active_services(db: Session, tenant_id: int) -> list:
    """上架中的服務項目（供引導式第一步）。最多 12（carousel 上限）。"""
    return [
        s
        for s in catalog_svc.list_services(db, tenant_id=tenant_id)
        if s.is_active
    ][:flex_menu_svc.MAX_CARDS]


def _service_carousel(services: list) -> dict:
    """服務清單 → LINE Flex carousel（每張卡片一個「選擇」postback 按鈕）。"""
    bubbles = []
    for s in services:
        subtitle_parts = []
        if s.duration_minutes:
            subtitle_parts.append(f"{s.duration_minutes} 分鐘")
        if s.price_cents:
            subtitle_parts.append(f"${s.price_cents}")
        subtitle = "・".join(subtitle_parts) or "點選預約"
        bubbles.append(
            {
                "type": "bubble",
                "body": {
                    "type": "box",
                    "layout": "vertical",
                    "contents": [
                        {"type": "text", "text": s.name, "weight": "bold",
                         "size": "lg", "wrap": True},
                        {"type": "text", "text": subtitle, "size": "sm",
                         "color": "#888888", "wrap": True},
                    ],
                },
                "footer": {
                    "type": "box",
                    "layout": "vertical",
                    "contents": [
                        {
                            "type": "button",
                            "style": "primary",
                            "action": {
                                "type": "postback",
                                "label": "選擇",
                                "data": f"action=pick_service&service_id={s.id}",
                                "displayText": f"選擇 {s.name}"[:300],
                            },
                        }
                    ],
                },
            }
        )
    return {
        "type": "flex",
        "altText": "請選擇服務項目",
        "contents": {"type": "carousel", "contents": bubbles},
    }


def _my_reservations_carousel(db: Session, tenant_id: int, rows: list) -> dict:
    """「我的預約」清單 → LINE Flex carousel（每張卡片附「取消預約」按鈕，上限 12）。"""
    from saas_mvp.models.booking_slot import BookingSlot

    rows = rows[:12]  # carousel 上限 12 張
    slot_ids = [r.slot_id for r in rows if r.slot_id is not None]
    slots = {}
    if slot_ids:
        slots = {
            s.id: s
            for s in db.query(BookingSlot)
            .filter(BookingSlot.tenant_id == tenant_id, BookingSlot.id.in_(slot_ids))
            .all()
        }
    bubbles = []
    for r in rows:
        slot = slots.get(r.slot_id)
        when = slot.slot_start.strftime("%m/%d %H:%M") if slot is not None else "—"
        bubbles.append({
            "type": "bubble",
            "body": {
                "type": "box",
                "layout": "vertical",
                "contents": [
                    {"type": "text", "text": f"預約 #{r.id}", "weight": "bold",
                     "size": "lg"},
                    {"type": "text", "text": f"時間：{when}", "size": "sm",
                     "color": "#555555", "wrap": True},
                    {"type": "text", "text": f"人數：{r.party_size} 位", "size": "sm",
                     "color": "#888888"},
                ],
            },
            "footer": {
                "type": "box",
                "layout": "vertical",
                "spacing": "sm",
                "contents": [
                    {
                        "type": "button",
                        "style": "primary",
                        "action": {
                            "type": "postback",
                            "label": "改期",
                            "data": f"action=reschedule&reservation_id={r.id}",
                            "displayText": f"改期 #{r.id}",
                        },
                    },
                    {
                        "type": "button",
                        "style": "secondary",
                        "action": {
                            "type": "postback",
                            "label": "取消預約",
                            "data": f"action=cancel&reservation_id={r.id}",
                            "displayText": f"取消預約 #{r.id}",
                        },
                    },
                ],
            },
        })
    return {
        "type": "flex",
        "altText": "你的預約",
        "contents": {"type": "carousel", "contents": bubbles},
    }


def _slots_fitting_service(db: Session, tenant_id: int, slots: list, service_id):
    """依服務時長過濾時段：slot 長度不足者剔除。

    slot_end 為 NULL 的舊資料一律放行（降級不擋，資訊不足不誤殺）；
    服務不存在或未設時長也放行。
    """
    if service_id is None:
        return slots
    try:
        service = catalog_svc.get_service(
            db, tenant_id=tenant_id, service_id=service_id
        )
    except Exception:  # noqa: BLE001 — 服務查無：不套過濾
        return slots
    duration = getattr(service, "duration_minutes", None)
    if duration:
        needed = datetime.timedelta(minutes=duration)
        slots = [
            s
            for s in slots
            if s.slot_end is None or (s.slot_end - s.slot_start) >= needed
        ]
    if features_svc.is_enabled(db, tenant_id, features_svc.BOOKABLE_RESOURCES):
        from saas_mvp.services import bookable_resources as resources_svc

        slots = [
            slot
            for slot in slots
            if resources_svc.slot_has_required_resources(
                db,
                tenant_id=tenant_id,
                service_id=service_id,
                slot=slot,
            )
        ]
    return slots


def _waitlist_join_buttons(
    slot_id: int | None,
    party_size: int,
    service_id: int | None = None,
    staff_id: int | None = None,
) -> list[tuple[str, str]] | None:
    """額滿回覆的「加入候補」quick-reply 按鈕。"""
    if slot_id is None:
        return None
    data = f"action=waitlist_join&slot_id={slot_id}&party={party_size}"
    if service_id is not None:
        data += f"&service_id={service_id}"
    if staff_id is not None:
        data += f"&staff_id={staff_id}"
    return [("加入候補", data)]


def _my_waitlist_reply(
    db: Session, tenant_id: int, line_user_id: str
) -> tuple[str, list | None]:
    """「候補」指令：列出有效候補 + 取消按鈕。"""
    if not line_user_id:
        return "無法識別使用者，請從 LINE 操作。", None
    entries = waitlist_svc.list_my_waitlist(
        db, tenant_id=tenant_id, line_user_id=line_user_id
    )
    if not entries:
        return "你目前沒有候補。時段額滿時可點「加入候補」登記。", None
    from saas_mvp.models.booking_slot import BookingSlot

    slot_ids = [e.slot_id for e in entries]
    slots = {
        s.id: s
        for s in db.query(BookingSlot)
        .filter(BookingSlot.tenant_id == tenant_id, BookingSlot.id.in_(slot_ids))
        .all()
    }
    lines = []
    buttons: list[tuple[str, str]] = []
    for e in entries[:13]:
        slot = slots.get(e.slot_id)
        when = slot.slot_start.strftime("%m/%d %H:%M") if slot else "—"
        state = "已通知" if e.status == "notified" else "等候中"
        lines.append(f"・{when}（{e.party_size} 位，{state}）")
        buttons.append(
            (f"取消候補 {when}"[:20], f"action=waitlist_cancel&entry_id={e.id}")
        )
    return "你的候補：\n" + "\n".join(lines), buttons


def _resched_date_buttons(
    reservation_id: int, dates: list[str]
) -> list[tuple[str, str]]:
    """改期：日期 → quick-reply（action=resched_date，前向攜帶 reservation_id）。"""
    _weekday_zh = ("一", "二", "三", "四", "五", "六", "日")
    buttons: list[tuple[str, str]] = []
    for d in dates[:_DATE_CHOICES_MAX]:
        try:
            dt = datetime.date.fromisoformat(d)
            label = f"{dt.strftime('%m/%d')} (週{_weekday_zh[dt.weekday()]})"
        except ValueError:
            label = d
        buttons.append(
            (label, f"action=resched_date&reservation_id={reservation_id}&date={d}")
        )
    return buttons


def _resched_slot_buttons(
    reservation_id: int, slots: list
) -> list[tuple[str, str]]:
    """改期：時段 → quick-reply（action=resched_slot，前向攜帶 reservation_id）。"""
    return [
        (
            s.slot_start.strftime("%m/%d %H:%M"),
            f"action=resched_slot&reservation_id={reservation_id}&slot_id={s.id}",
        )
        for s in slots
    ]


def _owned_confirmed_reservation(
    db: Session, tenant_id: int, reservation_id: int, line_user_id: str
):
    """取自己的 confirmed 預約；查無/他人/已取消回 (None, 錯誤訊息)。"""
    from saas_mvp.models.reservation import RESERVATION_CONFIRMED, Reservation

    resv = (
        db.query(Reservation)
        .filter(
            Reservation.tenant_id == tenant_id,
            Reservation.id == reservation_id,
        )
        .first()
    )
    if resv is None:
        return None, f"找不到預約 #{reservation_id}。"
    if resv.line_user_id != line_user_id:
        return None, "無法改期其他人的預約。"
    if resv.status != RESERVATION_CONFIRMED:
        return None, f"預約 #{reservation_id} 已取消，無法改期。"
    return resv, None


def _staff_choice_buttons(
    service_id: int, staff_list: list, date: str | None = None
) -> list[tuple[str, str]]:
    """員工 → quick-reply 按鈕（postback action=pick_staff）；首項為「不指定」。

    date（'YYYY-MM-DD'）若有則前向攜帶至每個按鈕（含「不指定」）。
    """
    suffix = f"&date={date}" if date else ""
    buttons: list[tuple[str, str]] = [
        ("不指定", f"action=pick_staff&service_id={service_id}{suffix}")
    ]
    for st in staff_list:
        buttons.append(
            (
                st.name[:20],
                f"action=pick_staff&service_id={service_id}"
                f"&staff_id={st.id}{suffix}",
            )
        )
    return buttons[:13]


def _service_staff(db: Session, tenant_id: int, service_id: int) -> list:
    """指派到該服務的 active 員工清單。"""
    links = catalog_svc.list_service_staff(
        db, tenant_id=tenant_id, service_id=service_id
    )
    out = []
    for link in links:
        try:
            st = staff_svc.get_staff(db, tenant_id=tenant_id, staff_id=link.staff_id)
        except Exception:  # noqa: BLE001 — 指派但員工已刪：略過
            continue
        if st.is_active:
            out.append(st)
    return out


def _slot_buttons_with_state(
    slots: list, service_id: int, staff_id: int | None
) -> list[tuple[str, str]]:
    """時段 → quick-reply，data 攜帶 service_id / staff_id 前向狀態。"""
    buttons = []
    for s in slots:
        data = f"action=pick_slot&service_id={service_id}&slot_id={s.id}"
        if staff_id is not None:
            data += f"&staff_id={staff_id}"
        buttons.append((s.slot_start.strftime("%m/%d %H:%M"), data))
    return buttons


def _confirm_text(db: Session, tenant_id: int, resv, slot_id: int) -> str:
    """建單成功確認文字 + 加入 Google 行事曆連結。"""
    from saas_mvp.services import calendar_ics

    base = (
        f"預約成功！\n預約編號：{resv.id}\n人數：{resv.party_size} 位\n"
        f"如需取消請輸入：取消 {resv.id}"
    )
    # 定金（C4）:需付定金時置頂提示 + 付款連結。
    if getattr(resv, "deposit_status", None) == "pending" and settings.public_base_url:
        from saas_mvp.models.tenant import Tenant as _Tenant
        from saas_mvp.services import deposit as deposit_svc

        _t = db.get(_Tenant, tenant_id)
        if _t is not None:
            base = (
                "預約成功！⚠️ " + deposit_svc.deposit_prompt(resv, _t)
                + f"\n付款連結：{deposit_svc.payment_url(resv)}"
                + f"\n預約編號：{resv.id}(人數 {resv.party_size} 位)"
            )
    from saas_mvp.services import client_forms as client_forms_svc
    form_rows = client_forms_svc.for_reservation(
        db, tenant_id=tenant_id, reservation_id=resv.id
    )
    pending_forms = [row for row in form_rows if row.status == "pending"]
    if pending_forms:
        base += "\n預約前請完成：" + "；".join(
            f"{row.template_name_snapshot} {client_forms_svc.form_url(row)}"
            for row in pending_forms
        )
    # 取時段時間組「加入 Google 行事曆」連結。
    from saas_mvp.models.booking_slot import BookingSlot

    slot_obj = (
        db.query(BookingSlot)
        .filter(BookingSlot.tenant_id == tenant_id, BookingSlot.id == slot_id)
        .first()
    )
    if slot_obj is not None:
        start = slot_obj.slot_start
        end = slot_obj.slot_end or start
        url = calendar_ics.google_calendar_url(
            title="預約", start=start, end=end
        )
        base += f"\n加入 Google 行事曆：{url}"
    return base


def _try_conversational(
    db: Session,
    tenant_id: int,
    action: str | None,
    params: dict,
    line_user_id: str,
    display_name: str | None = None,
    source_webhook_event_id: str | None = None,
) -> tuple[str | None, list | None, dict | None] | None:
    """引導式對話步驟機（服務→日期→員工→時段→確認），以 postback 攜帶狀態。

    回傳 (text, quick_reply, flex) 表示「已由本流程處理」；回 None 表示本流程
    不接手，交回既有 _dispatch_booking（向後相容：無服務時退回原始時段流程）。

    優雅降級：沒有任何上架服務時，'book'（無 slot_id）不接手，由既有
    _prompt_choose_slot 處理，使既有 raw-slot 預約測試不受影響。
    """
    # /menu 或「選單」：推送租戶 active FlexMenu（圖文選單卡片）。
    if action == "menu":
        if not features_svc.is_enabled(db, tenant_id, features_svc.FLEX_MENU):
            return "本店尚未開放圖文選單。", None, None
        menu = flex_menu_svc.get_active_menu(db, tenant_id=tenant_id)
        if menu is None:
            return "目前沒有可用的選單。", None, None
        cards = flex_menu_svc.list_cards(db, tenant_id=tenant_id, menu_id=menu.id)
        if not cards:
            return "目前沒有可用的選單。", None, None
        return None, None, flex_menu_svc.build_flex_payload(menu, cards)

    # 「我的預約」→ Flex carousel（每張附取消按鈕）；無預約則回文字提示。
    if action == "my":
        rows = booking_svc.list_my_reservations(
            db, tenant_id=tenant_id, line_user_id=line_user_id
        )
        if not rows:
            return "你目前沒有預約。輸入「時段」開始預約。", None, None
        return None, None, _my_reservations_carousel(db, tenant_id, rows)

    # ── 改期三步（reschedule → resched_date → resched_slot）────────────────
    # 第一步：驗擁有者 → 日期 quick-reply（前向攜帶 reservation_id）。
    if action == "reschedule":
        reservation_id = params.get("reservation_id")
        if reservation_id is None:
            return "請指定預約編號，例：改期 7", None, None
        _resv, err = _owned_confirmed_reservation(
            db, tenant_id, reservation_id, line_user_id
        )
        if err is not None:
            return err, None, None
        dates = _available_dates(db, tenant_id)
        if not dates:
            return "目前沒有可改期的日期。", None, None
        return (
            f"改期預約 #{reservation_id}，請選擇新日期：",
            _resched_date_buttons(reservation_id, dates),
            None,
        )

    # 第二步：選定新日期 → 該日可預約時段 quick-reply。
    if action == "resched_date":
        reservation_id = params.get("reservation_id")
        if reservation_id is None:
            return "請重新輸入「改期 <預約編號>」開始。", None, None
        date = params.get("date")
        slots = _available_slots_on_date(db, tenant_id, date)
        if not slots:
            return "該日期目前沒有可預約的時段，請改選其他日期。", None, None
        return (
            "請選擇新時段：",
            _resched_slot_buttons(reservation_id, slots),
            None,
        )

    # 第三步：選定新時段 → 原子換 slot（服務層鎖雙 slot、單一 commit）。
    if action == "resched_slot":
        reservation_id = params.get("reservation_id")
        slot_id = params.get("slot_id")
        if reservation_id is None or slot_id is None:
            return "請重新輸入「改期 <預約編號>」開始。", None, None
        try:
            resv = booking_svc.reschedule_reservation(
                db,
                tenant_id=tenant_id,
                reservation_id=reservation_id,
                new_slot_id=slot_id,
                line_user_id=line_user_id,
            )
        except booking_svc.ReservationPermissionError:
            return "無法改期其他人的預約。", None, None
        except booking_svc.ReservationNotFoundError:
            return f"找不到可改期的預約 #{reservation_id}。", None, None
        except booking_svc.SlotNotFoundError:
            return f"找不到時段 #{slot_id}，請重新輸入「改期 {reservation_id}」。", None, None
        except booking_svc.SlotFullError:
            # 原預約保留；可候補新時段（名額釋出通知後再改期）。
            return (
                f"時段 #{slot_id} 已額滿，可加入候補或改選其他時段"
                f"（原預約 #{reservation_id} 仍保留）。",
                _waitlist_join_buttons(slot_id, 1),
                None,
            )
        except booking_svc.ResourceUnavailableError:
            return (
                "此時段所需的房間或設備已被預約，請改選其他時段。"
                f"（原預約 #{reservation_id} 仍保留）",
                None,
                None,
            )
        from saas_mvp.models.booking_slot import BookingSlot

        new_slot = (
            db.query(BookingSlot)
            .filter(
                BookingSlot.tenant_id == tenant_id, BookingSlot.id == resv.slot_id
            )
            .first()
        )
        when = (
            new_slot.slot_start.strftime("%m/%d %H:%M")
            if new_slot is not None
            else "—"
        )
        return (
            f"改期成功！\n預約 #{resv.id} 已改至 {when}\n"
            f"人數：{resv.party_size} 位",
            None,
            None,
        )

    # 引導式第一步：'book'（無 slot_id）且有上架服務 → 服務 carousel。
    if action == "book" and params.get("slot_id") is None:
        services = _active_services(db, tenant_id)
        if not services:
            return None  # 退回既有時段流程（優雅降級）
        return None, None, _service_carousel(services)

    # 第二步：選定服務 → 日期 quick-reply（只列有可預約時段的日期）。
    if action == "pick_service":
        service_id = params.get("service_id")
        if service_id is None:
            return None
        try:
            catalog_svc.get_service(db, tenant_id=tenant_id, service_id=service_id)
        except Exception:  # noqa: BLE001 — 服務不存在/跨租戶
            return "找不到該服務，請重新輸入「預約」。", None, None
        dates = _available_dates(db, tenant_id)
        if not dates:
            return "目前沒有可預約的日期。", None, None
        return (
            "請選擇日期：",
            _date_choice_buttons(service_id, dates),
            None,
        )

    # 第三步：選定（服務 + 日期）→ 員工 quick-reply（含「不指定」，攜帶日期）。
    if action == "pick_date":
        service_id = params.get("service_id")
        if service_id is None:
            return None
        date = params.get("date")
        staff_list = _service_staff(db, tenant_id, service_id)
        return (
            "請選擇服務人員：",
            _staff_choice_buttons(service_id, staff_list, date),
            None,
        )

    # 第四步：選定（服務 + 員工 + 日期）→ 該日期可預約時段 quick-reply（攜帶狀態）。
    if action == "pick_staff":
        service_id = params.get("service_id")
        if service_id is None:
            return None
        staff_id = params.get("staff_id")
        date = params.get("date")
        slots = _available_slots_on_date(db, tenant_id, date)
        # 依服務時長過濾（slot_end 為 NULL 的舊時段放行）。
        slots = _slots_fitting_service(db, tenant_id, slots, service_id)
        if not slots:
            return "該日期目前沒有時長足夠的可預約時段。", None, None
        return (
            "請選擇時段：",
            _slot_buttons_with_state(slots, service_id, staff_id),
            None,
        )

    # 第四步：選定時段（帶 service_id）→ 建單 + 確認。
    if action == "pick_slot" and params.get("service_id") is not None:
        service_id = params.get("service_id")
        staff_id = params.get("staff_id")
        slot_id = params.get("slot_id")
        party_size = params.get("party_size", 1)
        if slot_id is None:
            return _prompt_choose_slot(db, tenant_id) + (None,)
        try:
            resv = booking_svc.book_slot(
                db,
                tenant_id=tenant_id,
                slot_id=slot_id,
                party_size=party_size,
                line_user_id=line_user_id,
                display_name=display_name,
                staff_id=staff_id,
                service_id=service_id,
                source_webhook_event_id=source_webhook_event_id,
            )
        except booking_svc.CustomerBlacklistedError:
            return "很抱歉，您目前無法在線上預約，請直接與店家聯繫。", None, None
        except booking_svc.CrossTenantReferenceError:
            return "預約資料有誤，請重新輸入「預約」開始。", None, None
        except booking_svc.SlotNotFoundError:
            return f"找不到時段 #{slot_id}，請重新輸入「預約」查看。", None, None
        except booking_svc.SlotFullError:
            return (
                f"時段 #{slot_id} 已額滿，可加入候補（名額釋出時通知您）"
                f"或改選其他時段。",
                _waitlist_join_buttons(slot_id, party_size, service_id, staff_id),
                None,
            )
        except booking_svc.ResourceUnavailableError:
            return "此時段所需的房間或設備已被預約，請改選其他時段。", None, None
        return _confirm_text(db, tenant_id, resv, slot_id), None, None

    return None


def _dispatch_booking(
    db: Session,
    tenant_id: int,
    action: str | None,
    params: dict,
    line_user_id: str,
    raw_text: str = "",
    display_name: str | None = None,
    source_webhook_event_id: str | None = None,
) -> tuple[str, list | None]:
    """執行預約指令；回傳 (回覆文字, quick_reply 按鈕或 None)。預期錯誤轉友善訊息。"""
    # 引導式第一步：選時段（「時段」或「預約」無參數）
    if action == "slots" or (action == "book" and params.get("slot_id") is None):
        return _prompt_choose_slot(db, tenant_id)

    # 引導式第二步：已選時段，選人數
    if action == "pick_slot":
        slot_id = params.get("slot_id")
        slot = None
        if slot_id is not None:
            slot = next(
                (s for s in _available_slots(db, tenant_id) if s.id == slot_id), None
            )
        if slot is None:
            return _prompt_choose_slot(db, tenant_id)
        return (
            f"時段 {slot.slot_start.strftime('%m/%d %H:%M')}，請選擇人數：",
            _party_choice_buttons(slot_id, slot.online_available),
        )

    # 第三步 / 一次性：建單
    if action == "book":
        slot_id = params.get("slot_id")
        party_size = params.get("party_size", 1)
        service_id = params.get("service_id")
        staff_id = params.get("staff_id")
        try:
            resv = booking_svc.book_slot(
                db,
                tenant_id=tenant_id,
                slot_id=slot_id,
                party_size=party_size,
                line_user_id=line_user_id,
                display_name=display_name,
                service_id=service_id,
                staff_id=staff_id,
                source_webhook_event_id=source_webhook_event_id,
            )
        except booking_svc.CustomerBlacklistedError:
            return "很抱歉，您目前無法在線上預約，請直接與店家聯繫。", None
        except booking_svc.SlotNotFoundError:
            return f"找不到時段 #{slot_id}，請重新輸入「時段」查看。", None
        except booking_svc.SlotFullError:
            return (
                f"時段 #{slot_id} 已額滿，可加入候補（名額釋出時通知您）"
                f"或改選其他時段。",
                _waitlist_join_buttons(slot_id, party_size, service_id, staff_id),
            )
        except booking_svc.ResourceUnavailableError:
            return "此時段所需的房間或設備已被預約，請改選其他時段。", None
        # 統一走 _confirm_text（含行事曆連結與定金提示）。
        return _confirm_text(db, tenant_id, resv, slot_id), None

    if action == "my":
        rows = booking_svc.list_my_reservations(
            db, tenant_id=tenant_id, line_user_id=line_user_id
        )
        if not rows:
            return "你目前沒有預約。輸入「時段」開始預約。", None
        return "你的預約：\n" + "\n".join(
            f"#{r.id} {r.party_size} 位" for r in rows
        ), None

    if action == "confirm":
        reservation_id = params.get("reservation_id")
        if reservation_id is None or not line_user_id:
            return "請從提醒訊息的「確認出席」按鈕操作。", None
        try:
            booking_svc.confirm_reservation(
                db,
                tenant_id=tenant_id,
                reservation_id=reservation_id,
                line_user_id=line_user_id,
            )
        except booking_svc.ReservationNotFoundError:
            return f"找不到有效的預約 #{reservation_id}。", None
        except booking_svc.ReservationPermissionError:
            return "無法確認其他人的預約。", None
        return f"已為您確認預約 #{reservation_id}，期待您的光臨！", None

    if action == "rate":
        # 滿意度調查（A3.3）：問卷 quick-reply 的 1–5 分按鈕。
        reservation_id = params.get("reservation_id")
        score = params.get("score")
        if reservation_id is None or score is None or not line_user_id:
            return "請從調查訊息的評分按鈕操作。", None
        from saas_mvp.services import feedback as feedback_svc

        row = feedback_svc.record_score(
            db,
            tenant_id=tenant_id,
            reservation_id=reservation_id,
            line_user_id=line_user_id,
            score=score,
        )
        if row is None:
            return "找不到對應的調查，感謝您的回饋！", None
        if score <= 3:
            return (
                "非常抱歉這次的體驗未達期待 😔 您的意見已轉達店家，"
                "我們會持續改進，期待下次給您更好的服務。",
                None,
            )
        thanks = f"感謝您的 {score} 分好評！🎉 期待再次為您服務。"
        if features_svc.is_enabled(db, tenant_id, features_svc.COUPON_SYSTEM):
            thanks += "\n輸入「優惠券」看看本店的回饋活動！"
        return thanks, None

    if action == "cancel":
        reservation_id = params.get("reservation_id")
        if reservation_id is None:
            return "請指定預約編號，例：取消 7", None
        try:
            booking_svc.cancel_reservation(
                db,
                tenant_id=tenant_id,
                reservation_id=reservation_id,
                line_user_id=line_user_id,
            )
        except booking_svc.ReservationNotFoundError:
            return f"找不到預約 #{reservation_id}。", None
        except booking_svc.ReservationPermissionError:
            return "無法取消其他人的預約。", None
        return f"預約 #{reservation_id} 已取消。", None

    if action in ("coupons", "redeem"):
        if not features_svc.is_enabled(db, tenant_id, features_svc.COUPON_SYSTEM):
            return "本店尚未開放優惠券功能。", None
        if action == "coupons":
            return _list_coupons_reply(db, tenant_id)
        return _redeem_coupon_reply(db, tenant_id, params.get("code"), line_user_id), None

    if action == "points":
        return _points_reply(db, tenant_id, line_user_id), None

    if action == "packages":
        return _packages_reply(db, tenant_id, line_user_id), None

    if action in ("gift_cards", "claim_gift_card"):
        return _gift_cards_reply(
            db, tenant_id, line_user_id,
            claim_code=params.get("code") if action == "claim_gift_card" else None,
        ), None

    # 顧客自助留聯絡資料：PRIVACY_MODE 開通時回 tokenized PII 表單連結
    # （不在聊天室索取個資）；未開通回引導文案。
    if action == "contact":
        if not line_user_id:
            return "無法識別使用者，請從 LINE 操作。", None
        if not features_svc.is_enabled(db, tenant_id, features_svc.PRIVACY_MODE):
            return (
                "本店未開放線上填寫個資，如需留下聯絡方式請直接告知店家。",
                None,
            )
        from saas_mvp.services import pii as pii_svc

        return (
            pii_svc.push_form_link(
                db, tenant_id=tenant_id, line_user_id=line_user_id
            ),
            None,
        )

    # ── 額滿候補 ────────────────────────────────────────────────────────────
    if action == "waitlist":
        return _my_waitlist_reply(db, tenant_id, line_user_id)

    if action == "waitlist_join":
        slot_id = params.get("slot_id")
        if slot_id is None or not line_user_id:
            return "請從額滿時段的「加入候補」按鈕登記。", None
        try:
            entry = waitlist_svc.join_waitlist(
                db,
                tenant_id=tenant_id,
                slot_id=slot_id,
                line_user_id=line_user_id,
                party_size=params.get("party_size", 1),
                display_name=display_name,
                service_id=params.get("service_id"),
                staff_id=params.get("staff_id"),
            )
        except waitlist_svc.WaitlistSlotNotFound:
            return f"找不到時段 #{slot_id}。", None
        except waitlist_svc.SlotNotFullError:
            return (
                f"時段 #{slot_id} 目前有名額，可直接預約！",
                [("立即預約", f"action=pick_slot&slot_id={slot_id}")],
            )
        return (
            f"已加入候補！時段釋出 {entry.party_size} 位以上名額時會通知您。\n"
            f"輸入「候補」可查看或取消。",
            None,
        )

    if action == "waitlist_cancel":
        entry_id = params.get("entry_id")
        if entry_id is None:
            return "請輸入「候補」查看後，點選要取消的候補。", None
        try:
            waitlist_svc.cancel_waitlist(
                db,
                tenant_id=tenant_id,
                entry_id=entry_id,
                line_user_id=line_user_id,
            )
        except waitlist_svc.WaitlistEntryNotFound:
            return "找不到該筆候補。", None
        return "候補已取消。", None

    # AI 預約 agent（A2）：無法辨識的純文字先給 agent 補槽（AI_BOOKING_AGENT
    # 開通時）；agent 只填槽，建單走既有 pick_slot postback 確定性路徑。
    # 未開通 / LLM 失敗回 None → 落回下方既有 AI_ASSISTANT QA / 說明。
    if action is None and raw_text and line_user_id:
        from saas_mvp.services import ai_conversation as ai_conversation_svc

        agent_out = ai_conversation_svc.handle_free_text(
            db, tenant_id, line_user_id, raw_text
        )
        if agent_out is not None:
            return agent_out

    # AI 客服 fallback：無法辨識的純文字訊息，若租戶開通 AI_ASSISTANT，
    # 以 get_assistant() 回答（context 由 faq.match 注入）。surgical、behind flag。
    if action is None and raw_text and features_svc.is_enabled(
        db, tenant_id, features_svc.AI_ASSISTANT
    ):
        return _ai_reply(db, tenant_id, raw_text), None

    if action in ("shop", "buy", "my_orders"):
        if not features_svc.is_enabled(db, tenant_id, features_svc.PRODUCT_SALES):
            return "本店尚未開放商品購買功能。", None
        if action == "shop":
            return _list_products_reply(db, tenant_id)
        if action == "buy":
            return _buy_reply(db, tenant_id, params.get("product_id"), params.get("qty", 1), line_user_id, params.get("coupon")), None
        return _my_orders_reply(db, tenant_id, line_user_id), None

    # help 或無法辨識
    return _BOOKING_HELP, None


def _ai_reply(db: Session, tenant_id: int, text: str) -> str:
    """以 AI 助手回答自由文字（context 由 faq.build_context 注入）。失敗回退說明。"""
    from saas_mvp.ai import AIError, get_assistant
    from saas_mvp.services import faq as faq_svc

    assistant = get_assistant(db)
    context = faq_svc.build_context(
        db, tenant_id, text, max_entries=assistant.context_max_entries
    )
    # D4:無 FAQ 命中 → 記為「AI 答不好的問題」(upsert 去重,永不拋)
    if not context:
        faq_svc.record_unanswered(db, tenant_id=tenant_id, question=text)
    try:
        return assistant.answer(text, context).answer
    except AIError:
        faq_svc.record_unanswered(db, tenant_id=tenant_id, question=text)
        return _BOOKING_HELP


def _list_coupons_reply(db: Session, tenant_id: int) -> tuple[str, list | None]:
    """列出有效券，附 quick-reply 兌換按鈕。"""
    coupons = [c for c in coupons_svc.list_coupons(db, tenant_id=tenant_id) if c.is_active][:12]
    if not coupons:
        return "目前沒有可用的優惠券。", None
    buttons = [(f"兌換 {c.name}"[:20], f"action=redeem&code={c.code}") for c in coupons]
    return "可用優惠券：\n" + "\n".join(f"・{c.name}（{c.code}）" for c in coupons), buttons


def _redeem_coupon_reply(
    db: Session, tenant_id: int, code: str | None, line_user_id: str
) -> str:
    if not code:
        return "請輸入券碼，例：兌換 ABC123"
    if not line_user_id:
        return "無法識別使用者，請從 LINE 操作。"
    try:
        coupons_svc.redeem_coupon(
            db, tenant_id=tenant_id, code=code, line_user_id=line_user_id
        )
    except coupons_svc.CouponNotFound:
        return f"找不到券碼 {code}。"
    except coupons_svc.CouponInactive:
        return f"券碼 {code} 已停用。"
    except coupons_svc.CouponExpired:
        return f"券碼 {code} 不在有效期間。"
    except coupons_svc.CouponExhausted:
        return f"券碼 {code} 已被領完。"
    except coupons_svc.AlreadyRedeemed:
        return f"你已兌換過券碼 {code}。"
    return f"兌換成功！券碼 {code} 已套用。"


def _points_reply(db: Session, tenant_id: int, line_user_id: str) -> str:
    from saas_mvp.models.customer import Customer

    customer = (
        db.query(Customer)
        .filter(Customer.tenant_id == tenant_id, Customer.line_user_id == line_user_id)
        .first()
    )
    if customer is None:
        return "你目前沒有會員資料，完成預約後即可累積點數。"
    return f"你的點數：{customer.points_balance or 0}\n會員等級：{customer.tier or 'regular'}"


def _packages_reply(db: Session, tenant_id: int, line_user_id: str) -> str:
    if not features_svc.is_enabled(db, tenant_id, features_svc.SERVICE_PACKAGES):
        return "本店尚未開放服務套票功能。"
    from saas_mvp.models.customer import Customer
    from saas_mvp.services import service_packages as packages_svc

    customer = (
        db.query(Customer)
        .filter(Customer.tenant_id == tenant_id, Customer.line_user_id == line_user_id)
        .first()
    )
    if customer is None:
        return "你目前沒有服務套票。"
    wallet = packages_svc.customer_wallet(
        db, tenant_id=tenant_id, customer_id=customer.id
    )
    if not wallet:
        return "你目前沒有可用的服務套票（可能已用完或過期）。"
    lines = ["你的服務套票："]
    for credit in wallet[:20]:
        expires = credit.customer_package.expires_at.strftime("%Y-%m-%d")
        lines.append(
            f"・{credit.customer_package.package_name_snapshot}／{credit.service.name}："
            f"剩 {credit.remaining} 次（{expires} 到期）"
        )
    lines.append("網頁預約時可勾選「使用服務套票」自動扣次。")
    return "\n".join(lines)


def _gift_cards_reply(
    db: Session, tenant_id: int, line_user_id: str, claim_code: str | None = None
) -> str:
    if not features_svc.is_enabled(db, tenant_id, features_svc.GIFT_CARDS):
        return "本店尚未開放電子禮物卡功能。"
    from saas_mvp.models.customer import Customer
    from saas_mvp.services import gift_cards as gift_cards_svc

    customer = db.query(Customer).filter(
        Customer.tenant_id == tenant_id, Customer.line_user_id == line_user_id
    ).first()
    if customer is None:
        return "你目前沒有會員資料，完成一次預約後即可領取禮物卡。"
    if claim_code:
        try:
            gift_cards_svc.claim_card(
                db, tenant_id=tenant_id, code=claim_code, customer_id=customer.id
            )
            db.commit()
        except gift_cards_svc.GiftCardError as exc:
            db.rollback()
            return str(exc)
    wallet = gift_cards_svc.customer_wallet(
        db, tenant_id=tenant_id, customer_id=customer.id
    )
    if not wallet:
        return "你目前沒有可用的禮物卡。收到卡號後輸入：領取禮物卡 <卡號>"
    lines = ["你的禮物卡（永久有效）："]
    for item in wallet[:20]:
        lines.append(f"・末四碼 {item.card.code_last4}：NT$ {item.balance_cents // 100}")
    lines.append("可在店內結帳時出示卡號，餘額可分次使用。")
    return "\n".join(lines)


def _list_products_reply(db: Session, tenant_id: int) -> tuple[str, list | None]:
    products = shop_svc.list_products(db, tenant_id=tenant_id, active_only=True)
    products = [p for p in products if p.stock is None or p.stock > 0][:12]
    if not products:
        return "目前沒有可購買的商品。", None
    buttons = [
        (f"購買 {p.name}"[:20], f"action=buy&product_id={p.id}&qty=1") for p in products
    ]
    lines = "\n".join(f"・{p.name}（{p.price_cents} {p.currency}）" for p in products)
    return "可購買商品：\n" + lines, buttons


def _buy_reply(
    db: Session, tenant_id: int, product_id: int | None, qty: int, line_user_id: str,
    coupon_code: str | None = None,
) -> str:
    if product_id is None:
        return "請指定商品，例：購買 1 2（先輸入「商品」查看）"
    try:
        order = shop_svc.create_order(
            db,
            tenant_id=tenant_id,
            items=[(product_id, qty)],
            line_user_id=line_user_id or None,
            coupon_code=coupon_code or None,
        )
    except shop_svc.ProductNotFound:
        return f"找不到商品 #{product_id}。"
    except shop_svc.ProductInactive:
        return f"商品 #{product_id} 已下架。"
    except shop_svc.OutOfStock:
        return f"商品 #{product_id} 庫存不足。"
    except shop_svc.CouponApplyError as exc:
        return f"優惠券無法套用：{exc}"
    checkout = get_payment_provider(db).create_checkout(
        order_id=order.id, amount_cents=order.total_cents, currency=order.currency
    )
    # 有折抵（會員等級 / 優惠券）時附上折抵金額，讓顧客看到優惠。
    discount_line = (
        f"已折抵：{order.discount_cents} {order.currency}\n"
        if (order.discount_cents or 0) > 0 else ""
    )
    return (
        f"已建立訂單 #{order.id}\n"
        f"{discount_line}"
        f"應付：{order.total_cents} {order.currency}\n"
        f"付款連結：{checkout}"
    )


def _my_orders_reply(db: Session, tenant_id: int, line_user_id: str) -> str:
    if not line_user_id:
        return "無法識別使用者。"
    orders = [
        o for o in shop_svc.list_orders(db, tenant_id=tenant_id)
        if o.line_user_id == line_user_id
    ]
    if not orders:
        return "你目前沒有訂單。輸入「商品」開始購買。"
    return "你的訂單：\n" + "\n".join(
        f"#{o.id} {o.total_cents} {o.currency}（{o.status}）" for o in orders
    )


# 會實際建單的 booking 動作；只有這些動作才需向 LINE 取使用者 displayName，
# 避免「時段/我的預約/取消」等查詢類訊息也多打一次 profile API。
_BOOKING_CREATE_ACTIONS = {"book", "pick_slot"}


def _resolve_display_name(
    profile_client: LineProfileClient | None,
    line_user_id: str,
    access_token: str,
) -> str | None:
    """向 LINE 取使用者 displayName 供建單回填；任何失敗皆降級為 None，不阻擋建單。

    webhook event.source 只給 userId，displayName 需另呼叫 profile API 取得。
    profile API 僅對「已加 bot 好友」者回名字，非好友/封鎖回 404；網路/憑證失敗
    亦同——一律吞掉並回 None，由 book_slot 照常以 line_user_id 建單。
    """
    if not line_user_id or profile_client is None:
        return None
    try:
        profile = profile_client.get_profile(line_user_id, access_token=access_token)
    except Exception:  # noqa: BLE001 - profile 失敗不得中斷建單
        _log.warning(
            "LINE profile fetch failed for user %s; proceeding without display_name",
            line_user_id,
        )
        return None
    return profile.display_name if profile else None


def _handle_booking_event(
    db: Session,
    tenant_id: int,
    access_token: str,
    event: dict,
    line_client: LineReplyClient,
    stage_holder: list[str] | None = None,
    profile_client: LineProfileClient | None = None,
) -> str:
    """booking 模式事件處理：解析指令 → 執行 → reply（含引導式 quick-reply 按鈕）。

    冪等性：mutating 動作（book/cancel）在 _dispatch_booking 內 commit 後，
    才把 stage 標到 REPLY_SENT 並 reply；若 reply 失敗，event 記 FAILED@REPLY_SENT
    → 不重試 → 不會因重送而重複建單/取消（與翻譯路徑「已送出不重扣」同類語意）。
    """
    stage = LineWebhookEventStage.CLAIMED.value
    etype = event.get("type")
    # A0.2 冪等鍵:建單時掛上此 webhook 事件 id,重放同一事件不重複建單。
    webhook_event_id = event.get("webhookEventId")
    if etype not in ("message", "postback"):
        return stage  # 其他事件靜默略過（follow/unfollow 等）

    reply_token = event.get("replyToken", "")
    line_user_id = event.get("source", {}).get("userId", "")

    action, params = _booking_intent(event)
    # 取出原始文字（供無法辨識時的 AI 客服 fallback）。
    raw_text = ""
    if etype == "message" and event.get("message", {}).get("type") == "text":
        raw_text = event["message"].get("text", "")

    # 非文字訊息（貼圖/圖片/位置/語音等）：友善引導 + 預約 quick-reply。
    # 原本落到通用說明文字牆，對點錯/傳貼圖的顧客不友善。
    if etype == "message" and event.get("message", {}).get("type") != "text":
        stage = LineWebhookEventStage.REPLY_SENT.value
        if stage_holder is not None:
            stage_holder[0] = stage
        if reply_token:
            line_client.reply(
                reply_token,
                "收到您的訊息！需要預約服務嗎？點下方按鈕即可開始：",
                access_token=access_token,
                quick_reply=_WELCOME_QUICK_REPLY,
            )
        return stage

    # 後台客服：存檔顧客傳入的文字訊息 + SSE 推播到後台（best-effort，不影響預約）。
    if raw_text and line_user_id:
        try:
            from saas_mvp.services import line_chat as line_chat_svc
            from saas_mvp.services.events import publish_event

            line_chat_svc.record_inbound(
                db, tenant_id=tenant_id, line_user_id=line_user_id, text=raw_text
            )
            publish_event(
                tenant_id, "line_message",
                line_user_id=line_user_id, text=raw_text, direction="in",
            )
        except Exception:  # noqa: BLE001 — 客服存檔失敗不得影響預約主流程
            db.rollback()

    # 僅在會建單的動作向 LINE 取 displayName，供顧客檔回填（可核對是誰預約）。
    display_name = None
    if action in _BOOKING_CREATE_ACTIONS and line_user_id:
        display_name = _resolve_display_name(profile_client, line_user_id, access_token)

    # 引導式對話（服務→日期→員工→時段→確認）優先攔截；未接手者交回既有 dispatcher。
    conv = _try_conversational(
        db, tenant_id, action, params, line_user_id, display_name,
        source_webhook_event_id=webhook_event_id,
    )
    if conv is not None:
        reply_text, quick_reply, flex = conv
    else:
        # message 但非文字（圖片/貼圖）→ action 為 None；回說明
        reply_text, quick_reply = _dispatch_booking(
            db, tenant_id, action, params, line_user_id, raw_text, display_name,
            source_webhook_event_id=webhook_event_id,
        )
        flex = None

    # 網頁預約入口（A1.1）：WEB_BOOKING 開通時，booking 模式所有文字回覆
    # （含無 quick-reply 者）一律附「用網頁預約」URI 按鈕 — 通用入口，
    # token 深連結 TTL 30 分（token 便宜，每次回覆發一枚可接受）。
    # public_base_url 未設（dev）不附，避免無效 URI 讓 LINE 整則回覆被拒。
    if (
        flex is None
        and line_user_id
        and settings.public_base_url
        and features_svc.is_enabled(db, tenant_id, features_svc.WEB_BOOKING)
    ):
        try:
            form_row = booking_form_svc.issue_token(
                db,
                tenant_id=tenant_id,
                line_user_id=line_user_id,
                display_name=display_name,
            )
            quick_reply = list(quick_reply or [])[:12] + [{
                "type": "uri",
                "label": "🌐 用網頁預約",
                "uri": booking_form_svc.form_url(form_row),
            }]
        except Exception:  # noqa: BLE001 — 表單入口失敗不得阻擋主回覆
            db.rollback()

    # 副作用（若有）已於 dispatcher 內 commit；標記不可重試後再 reply。
    stage = LineWebhookEventStage.REPLY_SENT.value
    if stage_holder is not None:
        stage_holder[0] = stage
    if reply_token:
        if flex is not None:
            line_client.reply_flex(
                reply_token,
                flex.get("altText", "選單"),
                flex["contents"],
                access_token=access_token,
            )
        else:
            line_client.reply(
                reply_token,
                reply_text,
                access_token=access_token,
                quick_reply=quick_reply,
            )
    return stage


def _translate_sync(
    translator: Translator,
    text: str,
    target_lang: str,
) -> TranslationResult:
    """同步呼叫翻譯介面（背景任務內執行）。

    helper 封裝是為了維持單一翻譯呼叫點，未來換 async SDK 只改這裡。
    為何可 sync 直呼：見步驟 6c 註解（canonical 說明位置）。
    """
    return translator.translate(text, target_lang)
