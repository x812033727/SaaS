# SaaS MVP — Multi-tenant REST API

多租戶 SaaS REST API，支援帳號管理、資源 CRUD、配額計量與 API Key 認證。

## 快速啟動

```bash
pip install -e .
python -m saas_mvp        # 或 saas-mvp
# 預設 http://127.0.0.1:8000
```

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
| GET | `/tenants/me` | 取得當前租戶資訊（含 plan） |

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
```

**回應範例**（secret/token 不含明文）：
```json
{
  "tenant_id": 1,
  "has_channel_secret": true,
  "has_access_token": true,
  "default_target_lang": "zh-TW",
  "created_at": "2026-06-14T10:00:00+00:00",
  "updated_at": "2026-06-14T10:00:00+00:00"
}
```

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

### 翻譯後端

| 設定 | 行為 |
|------|------|
| 未設定 `SAAS_DEEPL_API_KEY` | 使用 StubTranslator（離線，格式：`[LANG] 原文`） |
| 設定 `SAAS_DEEPL_API_KEY` | 使用真實 DeepL API |

StubTranslator 輸出範例：`[ZH-TW] Hello` → 供開發測試用，不需外部 API。
