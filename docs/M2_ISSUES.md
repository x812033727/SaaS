# M2 Issue Tracker

本檔只追蹤已確認但不混入 M1 webhook 驗收的技術債；每項需另開 PR 實作。

## M2-LINE-WEBHOOK-ASYNC

標題：LINE webhook 背景處理整套 async 化

範圍：
- `HttpLineReplyClient` 改用 `httpx.AsyncClient` 或 LINE Bot SDK v3 `AsyncMessagingApi`。
- `LineReplyClient.reply` 改為 async 方法。
- `_process_events` 改 `async def`。
- SQLAlchemy 同步 session 改 `AsyncSession`，相關 DB 操作一併 await 化。
- 更新 fake / spy 測試介面。

驗收：
- webhook async 化後，既有簽章、destination、redelivery 冪等、timing、event model 測試全綠。
- 不再把 `asyncio.to_thread` 包裝列為待辦。

## M2-LINE-WEBHOOK-QUEUE

標題：LINE webhook 背景任務升級為持久化 queue

範圍：
- 評估 ARQ / Celery 或同等 queue。
- 支援跨 worker process 任務交付。
- 加入 retry、dead-letter queue 與基本任務監控。

驗收：
- worker crash / restart 後，已接收但未處理的 event 不會靜默遺失。
- retry 次數、dead-letter 數量可被監控。

## M2-LINE-WEBHOOK-001

標題：webhook event 加入 `MAX_ATTEMPTS` 守衛

範圍：
- 為同一 `webhookEventId` 的 failed-before-reply 重試設定上限，初始建議 `MAX_ATTEMPTS = 5`。
- 超過上限時維持 `failed` 並產生告警或可查詢狀態。

驗收：
- 同一 event 超過上限後不再被改回 `pending`。
- 不影響已 `processed` 或 reply 後失敗的去重語意。

## M2-LINE-WEBHOOK-002

標題：webhook event TTL 清理 job

範圍：
- 刪除 `created_at < now - 7 days` 且已完成的 `line_webhook_events` rows。
- 清理方式可先用 ops command 或排程 job，不在 request path 內執行。

驗收：
- 7 天前的 `processed` rows 可被清理。
- `pending` / `failed` rows 預設不清，避免吃掉診斷資料。

## M2-LINE-WEBHOOK-003

標題：webhook event 失敗診斷資料補強

範圍：
- 保留目前 `last_error` 只存 exception class 的安全預設。
- 若需要完整 message / traceback，另建受控診斷表或受限 log pipeline。
- 避免 access token、channel secret、使用者文字等敏感資料落入 DB。

驗收：
- 失敗排查可取得足夠上下文。
- 自動化測試覆蓋敏感資訊不落 DB。

## M2-LINE-WEBHOOK-004

標題：webhook event pending 超時監控

範圍：
- 建立 `pending` 超過 5 分鐘未轉 `processed` / `failed` 的查詢或監控指標。
- 設定告警門檻與排查 runbook。

驗收：
- 可查出卡住超過 5 分鐘的 rows。
- 告警內容包含 tenant_id、webhook_event_id、last_stage、attempt_count。
