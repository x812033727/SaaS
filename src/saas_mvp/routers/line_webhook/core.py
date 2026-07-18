"""line_webhook 套件:webhook 端點 + 事件冪等機制 + replay(純搬移自 175-596,844-905)。"""
import base64
import datetime
import hashlib
import hmac
import json

from fastapi import BackgroundTasks, Depends, HTTPException, Request, status
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
    LineChannelConfig,
    LineConfigDecryptionError,
)
from saas_mvp.models.line_webhook_event import (
    LineWebhookEvent,
    LineWebhookEventStage,
    LineWebhookEventStatus,
)
from saas_mvp.models.tenant import Tenant
from saas_mvp.translation import Translator, get_translator


from saas_mvp.routers.line_webhook._shared import (
    _INVALID_SIGNATURE_DETAIL,
    _log,
    _WEBHOOK_ROUTE,
    router,
)
from saas_mvp.routers.line_webhook.events import _handle_line_event


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
