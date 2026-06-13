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

```bash
python -m pytest -q
```

所有測試使用 in-memory SQLite，無需外部網路。
