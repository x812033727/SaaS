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

# R7-A 純搬移拆分:原 2379 行單檔 → _shared/replies/events/core 四模組
# (acyclic:_shared ← replies ← events ← core)。此處依原始順序 re-export,
# 對外引用面(app.py/tenants/ui/_shared/ops/tests)完全不變。
from saas_mvp.routers.line_webhook._shared import (  # noqa: F401
    LINE_WEBHOOK_PATH_TEMPLATE,
    LINE_WEBHOOK_PREFIX,
    _INVALID_SIGNATURE_DETAIL,
    _QUOTA_EXCEEDED_MSG,
    _WEBHOOK_ROUTE,
    _log,
    router,
    webhook_url_for,
)
from saas_mvp.routers.line_webhook.core import (  # noqa: F401
    _DUMMY_SECRET,
    _claim_failed_webhook_event_for_retry,
    _claim_webhook_event,
    _constant_time_verify,
    _get_webhook_event,
    _mark_webhook_event_failed,
    _mark_webhook_event_processed,
    _process_events,
    _utcnow,
    _FAILED_RETRYABLE_STAGES_BEFORE_REPLY,
    line_webhook,
    replay_stored_event,
)
from saas_mvp.routers.line_webhook.events import (  # noqa: F401
    _handle_auto_reply_event,
    _handle_follow_event,
    _handle_line_event,
    _handle_unfollow_event,
    _record_line_message_best_effort,
)
from saas_mvp.routers.line_webhook.replies import (  # noqa: F401
    _BOOKING_CREATE_ACTIONS,
    _BOOKING_HELP,
    _DATE_CHOICES_MAX,
    _DEFAULT_WELCOME_BOOKING,
    _DEFAULT_WELCOME_GENERIC,
    _DEFAULT_WELCOME_TRANSLATION,
    _PARTY_CHOICES_MAX,
    _SLOT_CHOICES_MAX,
    _WELCOME_QUICK_REPLY,
    _active_services,
    _ai_reply,
    _available_dates,
    _available_slots,
    _available_slots_on_date,
    _booking_intent,
    _buy_reply,
    _confirm_text,
    _date_choice_buttons,
    _dispatch_booking,
    _gift_cards_reply,
    _handle_booking_event,
    _list_coupons_reply,
    _list_products_reply,
    _my_orders_reply,
    _my_reservations_carousel,
    _my_waitlist_reply,
    _owned_confirmed_reservation,
    _packages_reply,
    _party_choice_buttons,
    _points_reply,
    _prompt_choose_slot,
    _redeem_coupon_reply,
    _resched_date_buttons,
    _resched_slot_buttons,
    _resolve_display_name,
    _service_carousel,
    _service_staff,
    _slot_buttons_with_state,
    _slot_choice_buttons,
    _slots_fitting_service,
    _staff_choice_buttons,
    _translate_sync,
    _try_conversational,
    _waitlist_join_buttons,
)
