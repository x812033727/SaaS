# SaaS MVP — Multi-tenant REST API

多租戶 SaaS REST API，支援帳號管理、資源 CRUD、配額計量與 API Key 認證。

## 快速啟動

```bash
pip install -e .
python -m saas_mvp        # 或 saas-mvp
# 預設 http://127.0.0.1:8000
```

## 管理 UI（`/ui`）

伺服器渲染的管理介面（Jinja2 + HTMX，與 API 同源），啟動後瀏覽
`http://127.0.0.1:8000/ui/login`。涵蓋兩個層級：

- **店家自助**：註冊/登入、儀表板（bot 狀態 + 今日用量）、LINE 憑證設定與
  連線測試、店家類型設定。
- **平台管理員**（`is_admin=True`）：跨店家 bot 總覽（依類型/狀態篩選）、
  單一租戶管理（方案、停啟用、店家類型、代管 LINE 設定與驗證）。

設計重點：

- 登入後 JWT 存於 **httpOnly cookie**（`SameSite=Lax`，prod 加 `Secure`）。
  UI 路由用獨立的 cookie 認證，**不影響 API 路徑**（API 仍只認 header；
  cookie-only 請求一律 401）。
- HTML 永不輸出明文 `channel_secret` / `access_token`，只揭露 `has_*` 與
  `credential_status`。
- CSRF 目前僅依賴 `SameSite=Lax` + 同源（見 `KNOWN_LIMITATIONS.md`）。

## 認證方式

所有受保護端點支援以下三種認證（互斥選一）：

| 方式 | 標頭範例 |
|------|---------|
| Session JWT | `Authorization: Bearer eyJ...` |
| API Key（X-API-Key） | `X-API-Key: myapp_xxxxxxxx...` |
| API Key（Bearer） | `Authorization: Bearer myapp_xxxxxxxx...` |

## 主要端點

### 帳號 `/auth`

| 方法 | 路徑 | 說明 |
|------|------|------|
| POST | `/auth/register` | 註冊（需 tenant_name），回傳 access_token |
| POST | `/auth/token` | 登入，回傳 access_token |

### 租戶 `/tenants`

| 方法 | 路徑 | 說明 |
|------|------|------|
| GET | `/tenants/me` | 取得當前租戶資訊（含 plan、`store_type`） |
| PUT | `/tenants/me` | 自助更新租戶（目前僅 `store_type` 標籤；plan/is_active 歸 admin/billing） |
| GET | `/tenants/me/dashboard` | 店家自助總覽：租戶資訊 + LINE bot 狀態（遮罩）+ 今日用量 |
| POST | `/tenants/me/line-config/verify` | 測試自己 LINE bot 連線（重新驗證憑證） |

> `store_type` 為「分類標籤 + 篩選」用途，**不影響任何 bot 行為**。採軟驗證：
> 值會 strip + lowercase，空字串轉 NULL（未分類），未知值仍接受（自由標籤），
> 僅上限 32 字元。建議值：`restaurant` / `retail` / `service` / `other`。

### 筆記 `/notes`（受 quota 管控）

| 方法 | 路徑 | 說明 |
|------|------|------|
| POST | `/notes/` | 建立筆記 |
| GET | `/notes/` | 列出租戶所有筆記 |
| GET | `/notes/{id}` | 取得單筆 |
| PUT | `/notes/{id}` | 更新 |
| DELETE | `/notes/{id}` | 刪除 |

### API Key 管理 `/api-keys`

| 方法 | 路徑 | 說明 |
|------|------|------|
| POST | `/api-keys/` | 建立新 API key（**明文 plain_key 只回傳一次**） |
| GET | `/api-keys/` | 列出租戶 keys（只顯示 key_prefix，不含明文或 hash） |
| DELETE | `/api-keys/{id}` | 撤銷 key（軟刪除，usage 歷史保留） |

#### 建立 key 範例

```bash
curl -X POST http://localhost:8000/api-keys/ \
  -H "Authorization: Bearer <jwt>" \
  -H "Content-Type: application/json" \
  -d '{"name": "my-service-key"}'
```

回應（**plain_key 只此一次，請妥善保存**）：
```json
{
  "id": 1,
  "name": "my-service-key",
  "key_prefix": "aB3cD4eF",
  "plain_key": "myapp_aB3cD4eFgHiJkLmNoPqRsTuVwXyZ01234567890ABCD",
  "created_at": "2026-06-14T10:00:00"
}
```

#### 撤銷 key 範例

```bash
curl -X DELETE http://localhost:8000/api-keys/1 \
  -H "Authorization: Bearer <jwt>"
# 204 No Content
```

### 配額查詢 `/quota`

| 方法 | 路徑 | 說明 |
|------|------|------|
| GET | `/quota/status` | 查詢今日 tenant-level 用量狀態 |

### 用量明細 `/usage`

| 方法 | 路徑 | 說明 |
|------|------|------|
| GET | `/usage/` | 回傳租戶總量 + per-key 明細 |

#### `/usage/` 回傳欄位說明

```json
{
  "tenant": {
    "plan": "free",
    "daily_limit": 100,
    "used_today": 42,
    "remaining": 58,
    "period": "2026-06-14"
  },
  "api_keys": [
    {
      "api_key_id": 1,
      "name": "my-service-key",
      "key_prefix": "aB3cD4eF",
      "used_today": 15,
      "remaining": 85,
      "period": "2026-06-14"
    }
  ]
}
```

| 欄位 | 說明 |
|------|------|
| `tenant.plan` | 目前方案（`free` / `pro`） |
| `tenant.daily_limit` | 每日 API 呼叫上限（free=100, pro=10000） |
| `tenant.used_today` | 今日已使用次數（含所有認證方式） |
| `tenant.remaining` | 剩餘可用次數（`max(0, limit - used)`) |
| `tenant.period` | 計量日期（UTC，ISO 8601） |
| `api_keys[].api_key_id` | API key 的 DB ID |
| `api_keys[].name` | key 名稱 |
| `api_keys[].key_prefix` | 隨機部分前 8 字元（用於識別） |
| `api_keys[].used_today` | 今日透過該 key 的呼叫次數 |
| `api_keys[].remaining` | 今日透過該 key 的剩餘額度（`max(0, daily_limit - used_today)`）；per-key 共享租戶配額，無獨立上限 |
| `api_keys[].period` | 計量日期（ISO 8601） |

## 配額規則

| Plan | 每日上限 |
|------|---------|
| free | 100 次 |
| pro  | 10,000 次 |

超量回 HTTP 429：
```json
{"detail": "Quota exceeded for today. Upgrade to pro for higher limits."}
```

## 執行測試

### 推薦方式（跨環境一鍵）

```bash
bash run_tests.sh
```

`run_tests.sh` 會自動：
1. 偵測可用的 Python（優先 `/opt/ti/.venv/bin/python`，後備 `python3`）
2. 嘗試安裝依賴（`pip install -e ".[test]"`）
3. 設定 `SAAS_DATABASE_URL=sqlite:///:memory:` 避免沙盒寫入限制
4. 以 `PYTHONPATH=src` 執行 `pytest -q`

### 手動方式

```bash
# 驗收指令（綁定專案 venv，排除系統 Python 干擾）
/opt/ti/.venv/bin/python -m pytest -q
```

> **注意**：系統 `python` / `python3` 可能缺少 sqlalchemy 等依賴，請一律使用上方絕對路徑。
> `pyproject.toml` 已設定 `pythonpath = ["src"]`，不需額外 `PYTHONPATH=src`。

乾淨環境安裝步驟：

```bash
pip install -e ".[test]"
/opt/ti/.venv/bin/python -m pytest -q
```

所有測試使用 in-memory SQLite，無需外部網路。

> **注意**：此環境無 `python` 命令（只有 `python3`），請使用 `bash run_tests.sh` 或明確指定 `python3` / venv 路徑。

---

## LINE Messaging API 整合

### 環境變數

| 變數 | 說明 | 必填 |
|------|------|------|
| `SAAS_LINE_CHANNEL_ENCRYPT_KEY` | 加密 channel secret/token 用的 Fernet key（44 字元 URL-safe base64）<br>開發/測試有預設值，**生產環境必填** | 生產環境必填 |

產生 Fernet key：
```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

### 設定租戶 LINE Channel（Admin 端點）

以 admin 帳號設定或更新租戶的 LINE channel secret 與 access token：

```bash
# PUT（冪等 upsert）：建立或覆寫設定
curl -X PUT http://localhost:8000/admin/line-configs/{tenant_id} \
  -H "Authorization: Bearer <admin_token>" \
  -H "Content-Type: application/json" \
  -d '{
    "channel_secret": "你的 channel secret",
    "access_token": "你的 channel access token",
    "default_target_lang": "zh-TW"
  }'

# GET：查詢設定（secret/token 以遮罩形式回傳，不含明文）
curl http://localhost:8000/admin/line-configs/{tenant_id} \
  -H "Authorization: Bearer <admin_token>"

# DELETE：刪除設定
curl -X DELETE http://localhost:8000/admin/line-configs/{tenant_id} \
  -H "Authorization: Bearer <admin_token>"

# POST verify：測試 bot 連線（重新驗證憑證，回填 credential_status / line_bot_user_id）
curl -X POST http://localhost:8000/admin/line-configs/{tenant_id}/verify \
  -H "Authorization: Bearer <admin_token>"
```

**回應範例**（secret/token 不含明文）：
```json
{
  "tenant_id": 1,
  "has_channel_secret": true,
  "has_access_token": true,
  "default_target_lang": "zh-TW",
  "credential_status": "valid",
  "created_at": "2026-06-14T10:00:00+00:00",
  "updated_at": "2026-06-14T10:00:00+00:00"
}
```

### 跨店家 LINE bot 總覽（Admin 端點）

平台營運方可一站式總覽所有店家的 LINE bot 狀態與今日用量，並依店家類型 / 啟用狀態篩選：

```bash
# 列出所有店家 bot（遮罩憑證，永不回傳明文 secret/token）
curl "http://localhost:8000/admin/line-bots" \
  -H "Authorization: Bearer <admin_token>"

# 依店家類型篩選 + 僅啟用中
curl "http://localhost:8000/admin/line-bots?store_type=restaurant&is_active=true" \
  -H "Authorization: Bearer <admin_token>"

# 僅列出未分類（store_type 為 NULL）
curl "http://localhost:8000/admin/line-bots?uncategorized=true" \
  -H "Authorization: Bearer <admin_token>"
```

| 查詢參數 | 說明 |
|------|------|
| `skip` / `limit` | 分頁（預設 0 / 50，limit 上限 200） |
| `store_type` | 依店家類型篩選 |
| `is_active` | 依啟用狀態篩選 |
| `uncategorized` | `true` 時僅列出未分類（`store_type` 為 NULL）的店家 |

**單列回應**（遮罩，僅 `has_*` 布林 + 狀態，無明文憑證）：
```json
{
  "tenant_id": 1,
  "name": "acme",
  "store_type": "restaurant",
  "plan": "free",
  "is_active": true,
  "has_line_config": true,
  "has_channel_secret": true,
  "has_access_token": true,
  "credential_status": "valid",
  "line_bot_user_id": "Uxxxxxxxx...",
  "default_target_lang": "zh-TW",
  "today_count": 12,
  "today_chars": 340
}
```

> 尚未設定 LINE bot 的店家也會出現在列表中：`has_line_config=false`、`credential_status=null`。

此外 `PATCH /admin/tenants/{tenant_id}` 可順帶設定店家類型（`store_type`）：
送 `{"store_type": "retail"}` 設定、送 `{"store_type": null}` 清空、不送則不動。

### 租戶自助設定 LINE Channel（租戶端點）

租戶可用自己的登入 token 管理**自己**的 LINE 設定，無需 admin。`tenant_id` 一律取自登入身分，
端點路徑無 `{tenant_id}` 參數，租戶**無法**讀取或修改其他租戶的設定。

```bash
# PUT（冪等 upsert）：建立或更新自己的設定
curl -X PUT http://localhost:8000/tenants/me/line-config \
  -H "Authorization: Bearer <tenant_token>" \
  -H "Content-Type: application/json" \
  -d '{
    "channel_secret": "你的 channel secret",
    "access_token": "你的 channel access token",
    "default_target_lang": "zh-TW"
  }'

# GET：查詢自己的設定（含 webhook_url 引導欄位）
curl http://localhost:8000/tenants/me/line-config \
  -H "Authorization: Bearer <tenant_token>"

# DELETE：刪除自己的設定（成功回 204 No Content）
curl -X DELETE http://localhost:8000/tenants/me/line-config \
  -H "Authorization: Bearer <tenant_token>"

# POST verify：測試自己 bot 連線（重新驗證憑證）
curl -X POST http://localhost:8000/tenants/me/line-config/verify \
  -H "Authorization: Bearer <tenant_token>"

# GET dashboard：一站式總覽（租戶資訊 + bot 狀態 + 今日用量）
curl http://localhost:8000/tenants/me/dashboard \
  -H "Authorization: Bearer <tenant_token>"
```

**GET 回應範例**（含 `webhook_url`，secret/token 不含明文）：
```json
{
  "tenant_id": 1,
  "has_channel_secret": true,
  "has_access_token": true,
  "default_target_lang": "zh-TW",
  "created_at": "2026-06-14T10:00:00+00:00",
  "updated_at": "2026-06-14T10:00:00+00:00",
  "webhook_url": "/line/webhook/1"
}
```

> **`webhook_url` 用途**：此為**相對路徑**。請自行拼接你的服務 host
> （例如 `https://your-domain.com` + `/line/webhook/1`），填入 LINE Developer Console
> 的 Webhook URL 欄位，即可讓該租戶的 LINE channel 將訊息投遞到本服務。
> host 因部署環境而異，故不硬編碼於回應中。

### Webhook 端點

```
POST /line/webhook/{tenant_id}
```

**必要標頭**：`X-Line-Signature`（由 LINE Platform 自動附加）

**LINE Developer Console 設定**：
1. Webhook URL：`https://your-domain.com/line/webhook/{tenant_id}`
2. Use webhook：啟用
3. Auto-reply messages：停用（由此服務處理回覆）

**事件處理邏輯**：

| 條件 | 行為 |
|------|------|
| 文字訊息 | 翻譯為 `default_target_lang`，透過 reply API 回覆 |
| `/lang ja 你好` | 翻譯為指定語言（`ja`），回覆譯文 |
| `/lang ja`（無後續文字） | 回覆語言切換確認，不計 quota |
| 圖片/貼圖/其他非文字訊息 | 略過（回 200，不回覆） |
| follow/unfollow/其他 event | 略過（回 200） |
| X-Line-Signature 缺漏或不符 | 400 Bad Request |
| quota 超量 | 回覆明確超量訊息（不拋 5xx） |

**支援的目標語言**（BCP-47 格式）：`zh-TW`、`en`、`ja`、`ko` 等（後端需支援對應語言）。

### 回填既有 LINE bot userId（維運腳本）

既有 `line_channel_configs.line_bot_user_id IS NULL` 的資料，可用一次性腳本回填。
腳本沿用現有 `HttpLineBotInfoClient`，會解密 DB 內的 access token 後呼叫
LINE `GET /v2/bot/info`；stdout 不輸出 access token、channel secret 或 LINE userId。

```bash
# 先 dry-run：會呼叫 LINE bot/info，但不寫入 DB
PYTHONPATH=src python3 -m saas_mvp.ops.backfill_line_bot_user_id --dry-run --limit 50

# 確認結果後再 apply；每筆獨立 commit，可重跑
PYTHONPATH=src python3 -m saas_mvp.ops.backfill_line_bot_user_id --apply --limit 50

# 單一租戶排查；若已回填會輸出 skipped reason=already_set
PYTHONPATH=src python3 -m saas_mvp.ops.backfill_line_bot_user_id --dry-run --tenant-id 123
```

輸出格式為穩定 `key=value` 行：

```text
mode=dry_run
tenant_id=123 status=updated reason=dry_run
summary total=1 updated=1 skipped=0 failed=0 conflict=0
```

狀態說明：

| status | 說明 |
|--------|------|
| `updated` | `--dry-run` 表示可回填，`--apply` 表示已寫入 |
| `skipped` | 找不到設定或該租戶已回填，不覆寫既有值 |
| `failed` | bot/info 失敗、回應缺 userId 或 commit 失敗 |
| `conflict` | 回傳的 bot userId 已被其他租戶使用 |

`--dry-run` 不是純本機檢查，仍會呼叫 LINE API，可能受 token、網路與 rate limit 影響。

### 翻譯後端

| 設定 | 行為 |
|------|------|
| 未設定 `SAAS_DEEPL_API_KEY` | 使用 StubTranslator（離線，格式：`[LANG] 原文`） |
| 設定 `SAAS_DEEPL_API_KEY` | 使用真實 DeepL API |

StubTranslator 輸出範例：`[ZH-TW] Hello` → 供開發測試用，不需外部 API。

---

## LINE 預約系統（Booking）

比照 VibeAI 的 LINE 預約 SaaS 功能：時段容量管理、顧客自助預約/查詢/取消、顧客 CRM 自動建檔、
預約前自動提醒。**與翻譯並存**——每店家以 `bot_mode` 切換 bot 行為。

### bot 模式切換（`bot_mode`）

`LineChannelConfig.bot_mode`：`translation`（預設）/ `booking`。webhook 依此分流；
既有翻譯店家不受影響（migration 對既有列回填 `translation`）。可於 admin 或自助 line-config
端點設定：

```bash
curl -X PUT http://localhost:8000/tenants/me/line-config \
  -H "Authorization: Bearer <tenant_token>" -H "Content-Type: application/json" \
  -d '{"channel_secret":"...","access_token":"...","bot_mode":"booking"}'
```

> 不送 `bot_mode` 時維持既有值；送無效值回 400。

### 時段／容量 `/booking/slots`

| 方法 | 路徑 | 說明 |
|------|------|------|
| POST | `/booking/slots/` | 建時段（`slot_start`,`slot_end?`,`max_capacity`,`walkin_reserved?`） |
| GET | `/booking/slots/` | 列出（可帶 `date_from`/`date_to`/`active_only`）；回傳含 `online_available` |
| GET | `/booking/slots/{id}` | 取得單一（跨租戶 404） |
| PUT | `/booking/slots/{id}` | 調 `max_capacity`/`walkin_reserved`/`is_active`；下修低於已訂量 → 409 |
| DELETE | `/booking/slots/{id}` | 軟刪（`is_active=False`，保留既有預約） |

> 線上可用名額 = `max_capacity - walkin_reserved - booked_count`。
> `walkin_reserved` 保留現場名額（例：20 桌保留 5 → `max_capacity=20, walkin_reserved=5`，線上最多 15）。

### 預約 `/booking/reservations`

| 方法 | 路徑 | 說明 |
|------|------|------|
| POST | `/booking/reservations/` | 建單（容量不足 409、時段不存在 404）；原子容量控管 |
| GET | `/booking/reservations/` | 列出（可帶 `status`/`slot_id`） |
| GET | `/booking/reservations/{id}` | 取得單一（跨租戶 404） |
| POST | `/booking/reservations/{id}/cancel` | 取消（回補容量、待發提醒標 skipped） |

### 顧客 CRM `/booking/customers`

| 方法 | 路徑 | 說明 |
|------|------|------|
| GET | `/booking/customers/` | 列出（含 `booking_count`、`last_booked_at`） |
| GET | `/booking/customers/{id}` | 取得單一 |
| PATCH | `/booking/customers/{id}` | 補 `phone`/`note` |

> 顧客檔由 LINE 預約流程自動建立／更新（唯一鍵 `(tenant_id, line_user_id)`）。

### LINE 預約對話（`bot_mode=booking` 時）

webhook 接受文字指令與 postback（Rich Menu／quick-reply 按鈕）：

| 輸入 | 行為 |
|------|------|
| `預約` / `時段` | **引導式**：回時段 quick-reply 按鈕（點選即進入下一步） |
| 點時段按鈕（postback `action=pick_slot&slot_id=N`） | 回人數 quick-reply 按鈕 |
| 點人數按鈕（postback `action=book&slot_id=N&party=K`） | 建單；額滿婉拒 |
| `預約 <時段編號> <人數>` / `/book 12 2` | 一次性文字建單（不需逐步點選） |
| `我的預約` / `/my` | 列出自己的預約 |
| `取消 <預約編號>` / `/cancel 7` | 取消（驗證 line_user_id） |

> **引導式對話**全靠 postback 攜帶上下文（slot_id → slot_id+party），無需伺服器
> 對話狀態表。顧客不必記時段編號，逐步點按即可完成。
> 預約互動與回覆**不計入翻譯 quota**（quota 為翻譯字數/次數計量表）。

### 自動提醒（cron + ops 腳本）

建單時自動入列 `day_before`（前一天）與 `day_of`（當天提前 `SAAS_REMINDER_DAY_OF_LEAD_MINUTES`
分鐘）兩筆提醒。由 cron 定時跑 ops 腳本派送（LINE push）：

```bash
# 每 10–15 分鐘跑一次、單一實例（避免多 worker 重送）
*/10 * * * * python -m saas_mvp.ops.send_due_reminders --apply

# 先 dry-run 預覽（不推播、不寫入）
python -m saas_mvp.ops.send_due_reminders --dry-run --limit 200
```

冪等三層：`UNIQUE(reservation_id, kind)` + 逐筆 `SELECT … FOR UPDATE` 重驗 pending + 推播成功後才標 sent。
取消的預約、非 booking 模式或無 LINE 設定的租戶一律跳過。

| 環境變數 | 說明 | 預設 |
|------|------|------|
| `SAAS_REMINDER_ENABLED` | 建單是否入列提醒 | `true` |
| `SAAS_REMINDER_DAY_OF_LEAD_MINUTES` | 當天提醒提前分鐘數 | `180` |
| `SAAS_REMINDER_MAX_PER_RUN` | ops 單次最多派送筆數 | `500` |

### 圖文選單（Rich Menu）

店家可在 `/ui/rich-menu` 一鍵套用預設圖文選單模板 + 主題配色，選單按鈕直接對應預約指令
（預約／我的預約／時段／說明），顧客點按即觸發對話流程。

- **模板**：`booking3`（三宮格）、`booking4`（四宮格含說明）。
- **主題配色**：`line_green`、`ocean_blue`、`royal_purple`、`sunset_orange`、`dark`。
- **背景圖**：以**純 stdlib（zlib）產生純色 PNG**，零影像函式庫依賴。
- **套用流程**：（刪舊）→ 建立選單結構 → 上傳背景圖 → 設為預設；`richMenuId` 存回
  `LineChannelConfig`。LINE API 經 `LineRichMenuClient`（ABC / Http / Fake）。

> 預約管理與圖文選單皆已整合進伺服器渲染管理 UI（見上方「管理 UI」），
> 導覽列新增「預約管理」「圖文選單」。

### 優惠券 + 會員集點/等級（P3）

**優惠券** `/booking/coupons`（店家端 CRUD）：

| 方法 | 路徑 | 說明 |
|------|------|------|
| POST | `/booking/coupons/` | 建券（`code`,`name`,`discount_type`=percent/amount,`discount_value`,`max_redemptions?`,有效期?） |
| GET | `/booking/coupons/` `· /{id}` | 列出 / 單一 |
| PUT | `/booking/coupons/{id}` | 改名/上限/有效期/停用 |
| DELETE | `/booking/coupons/{id}` | 停用（軟刪） |
| GET | `/booking/coupons/{id}/redemptions` | 核銷紀錄 |

- **核銷原子性**：`SELECT … FOR UPDATE` 鎖券列、鎖內重驗（啟用/有效期/`redeemed_count < max_redemptions`）後遞增。
- **一人一券**：`UNIQUE(coupon_id, line_user_id)` 於 DB 層擋重複核銷。
- LINE 指令：`優惠券`（列券 + quick-reply 兌換鈕）、`兌換 <券碼>`（postback `action=redeem&code=X`）。
- UI：`/ui/coupons` 建立/停用。

**會員集點/等級**：

- 每完成一筆預約自動集點（`SAAS_POINTS_PER_BOOKING`，預設 10），集點與建單**同一交易**。
- 點數彙總於 `Customer.points_balance`，每筆異動寫 `PointTransaction`（append-only 帳本）。
- 等級由點數即時重算：`regular`(0) / `silver`(100) / `gold`(500)。
- REST：`GET /booking/customers/{id}/points`（帳本）、`POST /booking/customers/{id}/points`（店家手動加/扣點，扣點不足回 409）；`CustomerResponse` 含 `points_balance`/`tier`。
- LINE 指令：`點數` / `我的點數`（顯示點數與等級）。

| 環境變數 | 說明 | 預設 |
|------|------|------|
| `SAAS_POINTS_PER_BOOKING` | 每筆預約集點數（0=停用集點） | `10` |

### 報表分析（P5）

`/booking/analytics`（店家端）：

| 方法 | 路徑 | 說明 |
|------|------|------|
| GET | `/booking/analytics/summary?date_from&date_to` | 總單/已確認/取消率/總人數/不重複顧客/爽約率 |
| GET | `/booking/analytics/utilization` | 依「小時」聚合時段使用率（已訂/容量/使用率） |
| GET | `/booking/analytics/customers?limit` | 常客排行（依訂位次數） |
| GET | `/booking/analytics/export.csv` | 預約明細 CSV 匯出（stdlib `csv`） |

- 聚合於單一查詢取出後 Python 計算（避免 DB 方言差異、無 N+1），租戶隔離。
- **爽約率需標記到場**：`POST /booking/reservations/{id}/attendance`（`{attended: bool}`）標記到場/未到，
  `Reservation.attended`（nullable）；未標記則 `no_show_rate` 回 `null`，報表以取消率為主並明示限制。
- UI：`/ui/reports`（摘要卡 + 時段使用率表 + 常客 Top10 + CSV 下載）；`/ui/booking` 預約列加
  「到場/未到」標記按鈕。導覽列加「報表」。
