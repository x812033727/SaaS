## 2026-06-14 00:17

現有程式碼已有足夠的脈絡。整理調研結果如下：

---

## 調研彙整

### 🔑 API Key 生成與儲存

重點: `secrets.token_urlsafe(32)` 產生 256-bit URL-safe Base64 key，是 Python 官方推薦的 CSPRNG，勝過 `random` 模組。來源：[Python docs](https://runebook.dev/en/docs/python/library/secrets/generating-tokens)

重點: **API key 用 SHA-256，不用 bcrypt**。bcrypt 設計用來慢（密碼防暴力），每次 request 驗證會加 100ms+，會拖垮吞吐量。API key 本身高熵，SHA-256 已足夠安全。來源：[cybersierra - bcrypt kills API perf](https://cybersierra.co/blog/bcrypt-performance-issues-api/)

重點: 業界標準（Stripe/GitHub/OpenAI）：key 只在建立時明文回傳一次，DB 只存 hash；顯示時只顯示 prefix（前 8 字元）供使用者識別。來源：[Stripe Auth docs](https://docs.stripe.com/api/authentication)

重點: Key 格式建議帶 prefix（例：`myapp_`），方便 regex 掃描洩漏、便於除錯。來源：[vjay15 API key design](https://vjay15.github.io/blog/apikeys/)

重點: 快速 lookup 技巧：儲存 key 的前 8 字元（`key_prefix`）作為索引，查詢時先用 prefix 縮小候選集，再比對完整 hash，避免全表 hash 掃描。

---

### 🔐 FastAPI 多認證並存

重點: FastAPI `APIKeyHeader(auto_error=False)` + `OAuth2PasswordBearer(auto_error=False)` 可並存，讓單一 dependency 先試 JWT、再試 API Key，任一有效即通過。來源：[Hash Block Medium](https://medium.com/@connect.hashblock/securing-fastapi-with-api-keys-oauth2-and-role-scopes-all-together-4cf6aafa6600)

重點: 既有 `get_current_user` 只處理 JWT；新增 `get_current_actor` dependency，內部 fallback：`X-API-Key` header 有值走 key 驗證，無值走既有 JWT 路徑，兩者回傳相同 `User` 物件，下游 router 不需改動。來源：[PropelAuth - FastAPI DI](https://www.propelauth.com/post/fastapi-auth-with-dependency-injection)

重點: FastAPI 官方支援 `APIKeyHeader`、`APIKeyQuery` 兩種 Security scheme，直接整合 OpenAPI docs。來源：[FastAPI security](https://fastapi.tiangolo.com/tutorial/security/first-steps/)

---

### 📊 用量計量與 Quota

重點: 既有 `check_and_increment`（`SELECT FOR UPDATE` + `flush/commit`）已是正確的原子操作模式，可直接複用；只需在 `ApiUsage` 加一欄 `api_key_id`（nullable FK）來支援 per-key 維度。來源：現有 `quota.py`

重點: 純 DB 計量（無 Redis）在中等流量完全可行；需要橫向擴展時才換 Redis INCR。目前 SQLite/Postgres 的 FOR UPDATE 方案已有正確的鎖。來源：[Atomic SQL operations](https://blog.pjam.me/posts/atomic-operations-in-sql/)

重點: SlowAPI（slowapi）適合快速套用 decorator 限流，但需 Redis 做跨 process 共享；本專案已有 DB 計量邏輯，**不建議引入 SlowAPI**，維持在 SQLAlchemy 層處理，保持架構一致性。來源：[SlowAPI docs](https://slowapi.readthedocs.io/)

---

### ⚠️ 常見坑

重點: key_hash 不可加 random salt（否則無法由輸入 key 反推 hash 查 DB）；直接 `hashlib.sha256(key.encode()).hexdigest()` 即可，高熵 key 不需 salt。

重點: `ApiUsage` 現有 `UniqueConstraint("tenant_id", "period")` ——擴充 per-key 維度時，約束要改為 `("tenant_id", "api_key_id", "period")`，否則 `api_key_id=NULL` 的舊列與新列會衝突，需謹慎處理 migration。

重點: 撤銷（revoke）用 `is_active=False` 軟刪除，不要實體刪除，保留 usage 歷史記錄的外鍵完整性。

---

## 建議

建議: **新增 `ApiKey` model**：欄位 `id / user_id / tenant_id / name / key_prefix(8字) / key_hash(sha256) / is_active / created_at`，FK 到 `users` 與 `tenants`，`key_hash` 加 unique index。

建議: **`ApiUsage` 加 `api_key_id` nullable FK**，同時修改 UniqueConstraint 為 `(tenant_id, api_key_id, period)`；`NULL` 視為 session 認證的計量。

建議: **統一 dependency** 命名 `get_current_user`（沿用命名），內部改為 JWT fallback → API Key fallback，現有 router 不需改動 `Depends(get_current_user)` 呼叫。

建議: **Key 格式**：`myapp_` + `secrets.token_urlsafe(32)`，建立時只傳一次完整 key，DB 存 `key_prefix = key[:8]`、`key_hash = sha256(key)`。

建議: **Quota 複用**：`require_quota` dependency 裡的 `check_and_increment` 傳入 `api_key_id` 參數，區分 per-key 計量；`/usage` 端點同時回傳 tenant 總量與 per-key 明細。

建議: **測試**：沿用既有 `conftest.py` 的 SQLite in-memory fixture，新增 fixture 建立 test API key，所有新測試保持純離線。

---

Sources:
- [secrets module - Python docs](https://runebook.dev/en/docs/python/library/secrets/generating-tokens)
- [Why bcrypt kills API performance](https://cybersierra.co/blog/bcrypt-performance-issues-api/)
- [Stripe API Authentication](https://docs.stripe.com/api/authentication)
- [My adventure in designing API keys](https://vjay15.github.io/blog/apikeys/)
- [FastAPI + API Keys + OAuth2 + RBAC](https://medium.com/@connect.hashblock/securing-fastapi-with-api-keys-oauth2-and-role-scopes-all-together-4cf6aafa6600)
- [PropelAuth - FastAPI DI](https://www.propelauth.com/post/fastapi-auth-with-dependency-injection)
- [SlowAPI docs](https://slowapi.readthedocs.io/)
- [Atomic SQL operations](https://blog.pjam.me/posts/atomic-operations-in-sql/)

## 2026-06-14 02:46

現在來看一下既有的 ratelimit 模組，避免重複造輪：
以下是完整調研報告，已閱讀既有程式碼後聚焦在三個缺口。

---

## 🔧 研究員報告

### (1) 帳單方案升降級

重點: **業界兩派做法**：Slack/Notion = 立即生效＋剩餘時間補信用額；Zoom/GitHub = 排程至帳期末生效。本專案無真實金流，建議用「立即生效」（最簡單，無需記帳期狀態機）。來源：[Stigg upgrade/downgrade guide](https://www.stigg.io/blog-posts/the-only-guide-youll-ever-need-to-implement-upgrade-downgrade-flows-part-2)

重點: **降級後超量處置**業界主流：**立即封鎖新用量**（count 已超 new limit → 拒絕，HTTP 402/409），歷史用量不刪除，保留記錄。來源：[Indie Hackers SaaS plan limits discussion](https://www.indiehackers.com/post/how-do-you-handle-limits-in-your-saas-plans-3c4f7c692c)

重點: Stigg 建議用**獨立的 `plan_changes` 歷程表**（每列一筆異動），支援未來取消單一變更；不建議直接 overwrite `Tenant.plan` 而不留歷程。來源：[Stigg Part 2](https://www.stigg.io/blog-posts/the-only-guide-youll-ever-need-to-implement-upgrade-downgrade-flows-part-2)

重點: 既有 `PLAN_DAILY_LIMITS` dict 已集中定義，升降級只需改 `Tenant.plan`，`check_and_increment` 會自動讀新 limit，**無需改 quota 邏輯**。來源：現有 `quota.py:87`

重點: **模擬結帳**可用本地端點 `POST /billing/checkout` 接受 `{plan}` 參數，直接寫 DB + 歷程表，回傳 `payment_id: "simulated_xxx"`，完全離線。

建議: 新增 `PlanChangeHistory` model：`(id, tenant_id, from_plan, to_plan, changed_by_user_id, changed_at, reason)`，`Tenant.plan` 仍是主索引，歷程表只記 append-only 異動。

建議: 升降級端點 `POST /billing/upgrade`、`POST /billing/downgrade` 回傳 `{"ok": true, "plan": "pro", "payment_id": "simulated_xxx"}`；降級時先查今日 `ApiUsage.count`，若超過新 limit 回 `409 Conflict` 附 `{"current_usage": N, "new_limit": M}`。

---

### (2) API 速率限制

重點: **既有 `SlidingWindowRateLimiter` 用 `time.monotonic()` hardcode**，無法注入 → 必須重構為接受 `clock: Callable[[], float]` 參數才能讓測試注入假時鐘，避免 `sleep`。來源：現有 `ratelimit.py:27`

重點: 現有速率限制只掛在 `/auth/register`、`/auth/token` 兩個端點，**業務端點（notes/quota 等）完全無速率限制**。來源：現有 `ratelimit.py:56–57`

重點: `Retry-After` header 格式：純整數秒（`Retry-After: 30`），FastAPI 透過 `HTTPException(headers={"Retry-After": str(window)})` 回傳。來源：[MDN Retry-After](https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/Retry-After)、[RFC 7231](https://rfcinfo.com/rfc-7231/7-response-header-fields/)

重點: `fastapi-advanced-rate-limiter` 函式庫支援 in-memory + Redis 兩種後端，含 `get_retry_after()` helper，但引入外部套件。本專案既有邏輯已夠，**不需引入**。來源：[GitHub awais7012/FastAPI-RateLimiter](https://github.com/awais7012/FastAPI-RateLimiter)

重點: Per-key 速率限制（每分鐘 N 次）vs per-tenant：兩者可共存，前者從 `actor.api_key_id` 取 key，後者從 `actor.user.tenant_id` 取 ID，用同一 `SlidingWindowRateLimiter` class 的不同 instance 即可。

建議: 重構 `SlidingWindowRateLimiter.__init__` 加 `clock: Callable[[], float] = time.monotonic`，測試傳入 `FakeClock`（手動遞增），消除所有 sleep 依賴。

建議: 在 `require_quota` dependency 或新的 `require_rate_limit` dependency 內，根據 `actor.api_key_id` 選 key-level limiter、否則用 tenant-level limiter；429 回傳時帶 `Retry-After: {window_seconds}` header。

---

### (3) 管理面板

重點: FastAPI RBAC 最佳實踐：**在 `User` 加 `is_admin: Boolean` 欄位**（或 `role: Enum`），不需引入 Auth0/Permit.io 等外部服務，適合輕量內部 admin 場景。來源：[FastAPI RBAC DEV Community](https://dev.to/jod35/role-based-access-control-using-dependency-injection-add-user-roles-25k2)、[app-generator.dev FastAPI RBAC docs](https://app-generator.dev/docs/technologies/fastapi/rbac.html)

重點: 標準模式：建立 `require_admin` dependency，從 `get_current_actor` 取得 user，`if not user.is_admin: raise 403`；所有 admin router 掛 `dependencies=[Depends(require_admin)]`。來源：[Medium - RBAC with FastAPI](https://medium.com/@bhagyarana80/how-i-built-a-role-based-access-control-system-with-fastapi-and-pydantic-2c49e967efb0)

重點: **`Tenant` 現無 `is_active` 欄位**，但 admin「停用租戶」功能需要它；加 `is_active: Boolean default=True`，並在 `get_current_actor` 後段加檢查（租戶停用 → 403）。來源：現有 `tenant.py`

建議: `User` 加 `is_admin = Column(Boolean, default=False)`；`Tenant` 加 `is_active = Column(Boolean, default=True)`；遷移用 Alembic `alter_table` 或在測試環境重建 schema。

建議: Admin router `GET /admin/tenants`、`GET /admin/tenants/{id}/usage`、`PATCH /admin/tenants/{id}`（啟用/停用/改方案）、`GET /admin/api-keys`；全部掛 `Depends(require_admin)`，非 admin 回 `403 Forbidden`（不是 401，避免洩漏端點存在）。

---

### ⚠️ 跨功能常見坑

重點: 升降級寫 `Tenant.plan` 與寫 `PlanChangeHistory` 必須在**同一 DB transaction** 內，否則歷程表可能漏記。

重點: `SlidingWindowRateLimiter` 的 `_log` 是 in-process dict，**多 worker 時各有獨立計數**；單 worker 開發/測試完全夠用；若未來多 worker 需 Redis，但不要現在過度設計。

重點: `ApiUsage` 現有 `UniqueConstraint("tenant_id", "period")` — 此已在過往調研中確認需調整（per-key 維度），今次速率限制用 in-memory dict，不動 DB schema。

---

**Sources:**
- [Stigg upgrade/downgrade flows Part 2](https://www.stigg.io/blog-posts/the-only-guide-youll-ever-need-to-implement-upgrade-downgrade-flows-part-2)
- [FastAPI rate limiting without external services](https://oneuptime.com/blog/post/2025-01-06-fastapi-rate-limiting/view)
- [FastAPI rate limiting patterns - Anethoth](https://anethoth.com/rate-limiting-fastapi/)
- [MDN Retry-After header](https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/Retry-After)
- [MDN HTTP 429](https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Status/429)
- [FastAPI Rate Limiting with Dependencies](https://www.fastapiinteractive.com/fastapi-advanced-patterns/06-rate-limiting)
- [GitHub awais7012/FastAPI-RateLimiter](https://github.com/awais7012/FastAPI-RateLimiter)
- [FastAPI RBAC - DEV Community](https://dev.to/jod35/role-based-access-control-using-dependency-injection-add-user-roles-25k2)
- [FastAPI RBAC docs - app-generator.dev](https://app-generator.dev/docs/technologies/fastapi/rbac.html)
- [FastAPI RBAC - Medium](https://medium.com/@bhagyarana80/how-i-built-a-role-based-access-control-system-with-fastapi-and-pydantic-2c49e967efb0)
- [Indie Hackers - SaaS plan limits](https://www.indiehackers.com/post/how-do-you-handle-limits-in-your-saas-plans-3c4f7c692c)
- [SQLAlchemy audit log - Medium](https://medium.com/@singh.surbhicse/creating-audit-table-to-log-insert-update-and-delete-changes-in-flask-sqlalchemy-f2ca53f7b02f)

## 2026-06-14 13:17

整理完畢，以下是彙整結果：

---

## 調研：LINE Webhook 接收

### 現有程式碼確認

重點: 專案已有 `Tenant` 模型（含 `is_active`）、`User.is_admin`、`deps.py` 統一依賴入口、Router 架構（`/notes` 為範本）。**無 LINE 相關程式碼**，本任務從零加入。

---

### LINE Messaging API — Webhook 機制

重點: 簽章驗證演算法 = **HMAC-SHA256(channel_secret, raw_body)**，結果 Base64 編碼後放 `x-line-signature` header，與 LINE 傳入值比對。來源：[LINE Developers — Verify webhook signature](https://developers.line.biz/en/docs/messaging-api/verify-webhook-signature/)

重點: 必須用**原始 bytes**（`await request.body()`）驗簽，**絕不能先 JSON parse 再序列化**，任何空白/順序變動都會讓簽章失效。來源：[LINE Developers](https://developers.line.biz/en/docs/messaging-api/verify-webhook-signature/)

重點: Webhook payload 結構：`{"destination": "<bot_user_id>", "events": [...]}`，`destination` 是 **bot 的 user ID**（非 channel ID）；`deliveryContext.isRedelivery` 可判斷是否為重試投遞。來源：[LINE Developers — Receiving messages](https://developers.line.biz/en/docs/messaging-api/receiving-messages/)

重點: LINE Platform 若持續收到 non-2xx 會暫停發送 webhook；預設**不重試**，需在 Console 手動開啟 redelivery。**必須快速回 200**（建議 async 處理，避免逾時）。來源：[LINE Developers — Receiving messages](https://developers.line.biz/en/docs/messaging-api/receiving-messages/)

---

### 套件選擇

重點: **`line-bot-sdk`** (PyPI) 目前最新 **v3.23.0**（2026-04）；`from linebot.v3 import WebhookParser`；v2 與 v3 API 不相容，本次直接用 v3。來源：[GitHub line/line-bot-sdk-python](https://github.com/line/line-bot-sdk-python)

重點: `WebhookParser.parse(body, signature)` — `body` 接受 `bytes` 或 `str`，回傳 `list[Event]`；簽章錯誤拋 `linebot.v3.exceptions.InvalidSignatureError`。需 Python ≥ 3.10（本專案 3.11 ✅）。來源：[GitHub line/line-bot-sdk-python](https://github.com/line/line-bot-sdk-python)

重點: 多租戶場景：每個 tenant 有獨立 channel_secret，**不能用全域單一 WebhookParser**；需在 request 時動態 `WebhookParser(tenant.channel_secret)` 實例化。每次實例化成本低（只是存 secret），沒有效能問題。

---

### 坑

重點: FastAPI 的 `Request.body()` 需 **`await`**（async endpoint 才有），且呼叫後 body stream 已被消費，若後面還需重讀（如 debug logging），需暫存到變數。

重點: 多租戶路由：LINE 的 `destination`（bot user ID）**不等於** tenant ID，需要另一個查詢鍵（如 URL path 含 `tenant_id` 或 channel_id → tenant 映射表）。`/webhook/{tenant_id}` 是最簡單且安全的做法。

重點: 安全：LINE 官方**不建議**以 IP 白名單取代簽章驗證；簽章驗證失敗應回 **400**，不是 401/403。

---

### 建議

建議: 在 `pyproject.toml` 加 `"line-bot-sdk>=3.23"` 依賴；不需引入其他套件。

建議: 新增 `src/saas_mvp/routers/webhook.py`，endpoint 為 `POST /webhook/{tenant_id}`；從 DB 依 `tenant_id` 查出 `channel_secret`，動態建 `WebhookParser`，`await request.body()` 取 raw bytes 驗簽，驗失敗回 400，成功立即回 200，事件丟背景處理（或暫存 list 後同步處理）。

建議: Tenant 模型需新增欄位 `line_channel_secret: str`、`line_channel_access_token: str`、`line_target_language: str`（可先 nullable，等租戶設定）；用 Alembic migration 加欄位。

建議: 簽章驗證邏輯抽成獨立函數（`services/line_webhook.py`），方便單元測試；測試可直接呼叫函數傳 bytes + 正確/錯誤 secret，不需 mock HTTP。

---

**Sources:**
- [LINE Developers — Verify webhook signature](https://developers.line.biz/en/docs/messaging-api/verify-webhook-signature/)
- [LINE Developers — Receiving messages](https://developers.line.biz/en/docs/messaging-api/receiving-messages/)
- [GitHub line/line-bot-sdk-python](https://github.com/line/line-bot-sdk-python)
- [PyPI line-bot-sdk](https://pypi.org/project/line-bot-sdk/)
- [LINE Messaging API reference](https://developers.line.biz/en/reference/messaging-api/)

## 2026-06-14 14:17

先讀現有程式碼，同步搜尋翻譯 API。
再快速看翻譯層其他檔案，補查語言偵測與 DeepL 自動偵測細節。
再確認 LINE client HTTP 實作與 config，看看是否有 async 問題。
已掌握足夠資訊，整理報告。

---

## 調研：訊息翻譯

### 現有程式碼確認（重要：避免重複開發）

重點: 翻譯核心層**已完整實作**——`Translator` ABC、`DeepLTranslator`（stdlib urllib）、`StubTranslator`、`get_translator()` factory 全部存在；webhook router 已呼叫 `translator.translate(text, target_lang)` → `line_client.reply()` 完整流程。本輪任務**不需從零建立**，應找缺口補強。

---

### DeepL API — 同語言偵測（P1 缺口）

重點: DeepL `/v2/translate` 回應結構：`{"translations": [{"detected_source_language": "JA", "text": "..."}]}`，`detected_source_language` **已在回應中**，但現有 `DeepLTranslator.translate()` 丟棄此欄位。來源：[DeepL API Docs](https://developers.deepl.com/docs)

重點: 若 source == target（如 target=`JA` 而用戶本來就用日文），DeepL 仍執行翻譯並消耗 quota；回傳的是原文或接近原文的結果，但**白白扣量**。

重點: 修法：在 `DeepLTranslator.translate()` 讀 `detected_source_language`，若 `.upper() == target_lang.upper()` 則直接返回原文（skip API 或回傳 body 已有結果），不需修改 `Translator` 介面。`StubTranslator` 同理可加簡易 skip 邏輯（`target_lang` 等於某 prefix 就不包 tag）。

---

### DeepL 語言碼 — 常見坑

重點: 繁體中文在 DeepL v2（REST）用 `ZH`（舊）或 `ZH-HANT`（新），**不是** `ZH-TW`。現有程式碼把 BCP-47 `zh-TW` `.upper()` → `ZH-TW` 傳給 DeepL，**會收到 400 Bad Request**。DeepL 官方支援碼：`ZH-HANT`（繁體）、`ZH-HANS`（簡體）。來源：[DeepL API 語言清單](https://developers.deepl.com/docs/api-reference/languages/openapi-spec-for-retrieving-languages)

重點: 修法：在 `DeepLTranslator.translate()` 加語言碼映射表 `{"ZH-TW": "ZH-HANT", "ZH-CN": "ZH-HANS", ...}`，套用後再送 API。其他語言（`JA`、`EN`、`KO` 等）直接 upper 即可。

---

### Sync I/O 在 async FastAPI 的問題

重點: `DeepLTranslator.translate()` 和 `HttpLineReplyClient.reply()` 均用 `urllib`（**阻塞**），但 webhook handler 是 `async def`。低流量 MVP 下影響有限，但**高並發時會阻塞 event loop**（其他請求被卡住）。

重點: 最小成本修法：`asyncio.to_thread(translator.translate, text, lang)` 把 blocking call 移到 thread pool，**無需修改 Translator 介面**，向後兼容。來源：[Python asyncio.to_thread](https://docs.python.org/3/library/asyncio-tasks.html#asyncio.to_thread)

---

### DeepL Free vs Pro API URL

重點: Free tier key 以 `:free` 結尾，**必須**用 `api-free.deepl.com`；Pro key 用 `api.deepl.com`。混用會得 403。現有 config 預設是 free URL，`SAAS_DEEPL_API_URL` 可覆蓋，**這已正確**，但需要在文件中明示。

---

### 建議

建議: **本輪任務核心**是補兩個缺口：① `ZH-TW → ZH-HANT` 語言碼映射（**必修，否則繁中翻譯必 400**）；② `detected_source_language == target` 時 skip 翻譯（節省 quota，P1）。

建議: 語言碼映射放在 `translation/http.py` 的 `DeepLTranslator._normalize_target_lang(lang)` 私有方法，不污染 `Translator` 介面；映射表只需覆蓋 DeepL 不接受的 BCP-47 tag（`ZH-TW`、`ZH-CN`）。

建議: 同語言 skip 邏輯：`body["translations"][0]["detected_source_language"].upper()` 若 == normalized target → 直接 `return text`（原文），不計 quota（因為不呼叫翻譯或翻譯結果就是原文）。

建議: Async 問題 MVP 階段可暫不處理，在 webhook handler 中加 `# NOTE: blocking call — wrap in asyncio.to_thread for high-traffic production` 標記，列入 M2 技術債即可。

建議: `StubTranslator` 加對應 skip 行為：target_lang == `"same"` 時返回原文（方便測試 skip 邏輯）。

---

**Sources:**
- [DeepL API Documentation](https://developers.deepl.com/docs)
- [DeepL Language Detection Best Practices](https://developers.deepl.com/docs/best-practices/language-detection)
- [DeepL Languages OpenAPI Spec](https://developers.deepl.com/docs/api-reference/languages/openapi-spec-for-retrieving-languages)
- [Python asyncio.to_thread](https://docs.python.org/3/library/asyncio-tasks.html#asyncio.to_thread)
- [DeepL Python SDK (GitHub)](https://github.com/DeepLcom/deepl-python)

## 2026-06-14 15:12

現有程式碼已掌握。整理報告如下：

---

## 多租戶管理 — 調研報告

### 現有程式碼狀態（避免重造輪子）

重點: `LineChannelConfig` model、`services/line_config.py`（get/upsert/delete）、Admin 端點 `GET/PUT/DELETE /admin/line-configs/{tenant_id}` **全部已實作完成**。
重點: Webhook 路由 `/line/webhook/{tenant_id}` 已以 `tenant_id` 查 DB 隔離各租戶；service 層可直接 reuse。
重點: **本輪任務真正缺口**：只有 Admin 能管理 LINE config，**租戶自助 API 完全不存在**——`routers/tenants.py` 裡只有 `GET /tenants/me`，沒有 LINE config 相關端點。

---

### LINE Messaging API — 多租戶路由機制確認

重點: LINE 每個 channel 在 Console 只能設定**一個 webhook URL**；現有 `/line/webhook/{tenant_id}` 設計完全正確——每租戶去 LINE Console 填自己的專屬 URL（`https://xxx.com/line/webhook/42`）即可，**無需特殊多路由機制**。來源：[LINE Docs — Receiving messages](https://developers.line.biz/en/docs/messaging-api/receiving-messages/)
重點: webhook payload 的 `destination` 欄位是 LINE Bot userId，不是租戶識別碼；租戶隔離已靠 URL path `tenant_id` 完成，**不需依賴 destination 做路由**。

---

### 自助管理 API 設計參考

重點: 最直接做法：在 `routers/tenants.py` 加三個端點，`tenant_id` 從 `current_user.tenant_id` 取（不從 URL），確保租戶只能看/改自己的設定。
重點: `services/line_config.py` 裡 `get_line_config`、`upsert_line_config`、`delete_line_config` 可**直接 reuse**，介面相容（入參都是 `db, tenant_id, ...`），不需改 service 層。
重點: 應額外回傳 `webhook_url`（如 `"/line/webhook/{tenant_id}"`）欄位，讓租戶知道要在 LINE Console 填什麼，**否則他們無法完成設定**。

---

### 建議

建議: 在 `routers/tenants.py` 加 `GET/PUT/DELETE /tenants/me/line-config`，`tenant_id = current_user.tenant_id`，**直接呼叫既有 service**，不重複邏輯。
建議: `GET /tenants/me/line-config` 回應加 `"webhook_url": f"/line/webhook/{tenant_id}"` 欄位，方便前端引導租戶去 LINE Console 設定。
建議: 不需新增 service 函數——現有三個函數介面已足夠，只需新增 router；`app.py` 不需改（`tenants.router` 已掛載）。
建議: 安全性：路由層只傳自己的 `tenant_id`，無法越權讀取他人設定；admin 路徑維持原狀（`require_admin` dependency 保護），兩套端點並存互不干擾。
建議: 測試策略：仿照 `test_line_task4_admin_line_configs.py` 的 in-memory SQLite 模式，增加「租戶 A 無法讀租戶 B 的 config」隔離驗收測試。

---

**Sources:**
- [LINE Docs — Receiving messages (webhook)](https://developers.line.biz/en/docs/messaging-api/receiving-messages/)
- [LINE Messaging API reference](https://developers.line.biz/en/reference/messaging-api/)
- [LINE Docs — Module channel webhook routing](https://developers.line.biz/en/docs/partner-docs/module-technical-using-messaging-api/)

## 2026-06-14 15:45

先快速看關鍵檔案，同時查 LINE 官方文件。
現有程式碼已讀清楚。整理報告如下：

---

## 租戶路由 — 調研報告

### 現有程式碼狀態（本輪任務真正缺口）

重點: URL-path 路由 `/line/webhook/{tenant_id}` 已完整實作，簽章驗證、quota、去重、多事件全部到位，**不需重做**。
重點: `LineChannelConfig` 只存 `channel_secret_enc`、`access_token_enc`、`default_target_lang`——**沒有 `line_bot_user_id` 欄位**，無法比對 `payload.destination`。
重點: webhook handler 目前**完全不讀 `payload.destination`**，即使 LINE Console 錯誤配置（租戶 A 的 bot 打到租戶 B 的 URL），簽章失敗才攔住，缺少二次語意驗證層。

---

### LINE `destination` 欄位語意確認

重點: `destination` = **bot 的 userId**（格式 `U[0-9a-f]{32}`），不是 channel numeric ID，每個 LINE Official Account 一個，**可作為租戶識別鍵**。來源：[Receive messages (webhook)](https://developers.line.biz/en/docs/messaging-api/receiving-messages/)
重點: LINE 提供 `GET https://api.line.me/v2/bot/info`，回傳 `{"userId": "Uxxxx...", "basicId": "@xxx", "displayName": "..."}` — **憑 channel access_token 即可取得 bot 的 userId**，不需租戶手動填寫。來源：[Messaging API reference — Get bot info](https://developers.line.biz/en/reference/messaging-api/#get-bot-info)

---

### 路由策略比較

重點: **策略一（現行 URL-based）**：每租戶 URL 含 tenant_id，`LineChannelConfig` 直接 lookup → 已完整實作，測試 #13 已驗跨租戶隔離。
重點: **策略二（destination-based 單一 URL）**：`POST /line/webhook`，解析 payload 取 `destination` → DB lookup `line_bot_user_id` → 取 channel_secret → 驗簽章。好處是租戶設定更簡單（LINE Console 只填一個 URL），壞處是必須在驗簽前信任 `destination`（但後續仍驗簽，風險可接受）。

---

### 建議

建議: **本輪最小可行實作**：在 `line_channel_config.py` 加 `line_bot_user_id = Column(String(64), unique=True, nullable=True, index=True)`（nullable 向後相容，`create_all` 自動建欄）。
建議: `upsert_line_config` 完成後，**自動呼叫 `GET https://api.line.me/v2/bot/info`**（傳入 `access_token`），把回傳 `userId` 存入 `line_bot_user_id`；失敗記 warning 但不阻擋 upsert（離線環境不應爆）。
建議: webhook handler 在步驟 4（驗簽後）加二次 check：若 `cfg.line_bot_user_id` 已設定，且 `payload.get("destination") != cfg.line_bot_user_id` → log warning + 回 400（同簽章錯誤的 detail，維持列舉防護）。
建議: **不需改路由策略**——URL-based routing 保留，`line_bot_user_id` 兼作「正確性防護層」與未來 destination-based 路由的資料基礎；兩者不互斥。
建議: 因無 Alembic，舊 DB 的 `line_channel_config` 表需手動 `ALTER TABLE line_channel_configs ADD COLUMN line_bot_user_id VARCHAR(64) UNIQUE`；新環境 `create_all` 自動包含。建議在啟動時加 try-ALTER 或改用 Alembic（M2 技術債）。
建議: 測試面：加「destination 不符」→ 400 案例；「destination 為 None（舊 config 未設）」→ 略過二次 check 仍 200 OK 的向後相容案例。

---

**Sources:**
- [Receive messages (webhook) — LINE Developers](https://developers.line.biz/en/docs/messaging-api/receiving-messages/)
- [Get LINE Official Account (bot) info — Messaging API reference](https://developers.line.biz/en/reference/messaging-api/#get-bot-info)
- [Using the Messaging API from a module channel](https://developers.line.biz/en/docs/partner-docs/module-technical-using-messaging-api/)

## 2026-06-14 16:30

先讀現有 quota 實作與 webhook handler 的關鍵檔案：
現有程式碼已實作 post-deduction，但讓我確認是否還有殘留的舊呼叫點：
已有足夠資訊。整理報告：

---

## 調研報告：quota 後扣改良任務

### 核心發現：**功能已完整實作**

重點: `line_webhook.py` 步驟 6a→6d 已落實後扣模式：`has_quota`（非遞增檢查）→ translate → reply → `increment_usage(+1)`；任一下游失敗不計量，白扣問題已消除。
重點: `quota.py` 的 `increment_usage(plan=...)` 在 `SELECT FOR UPDATE` 鎖內重驗 `count < limit`，消除 `has_quota→increment` 的 TOCTOU 超賣窗口——符合資安最佳實踐（atomic check-then-update with row lock）。
重點: `test_line_task1_quota_billing.py` 已覆蓋全部驗收案例：translate 失敗不扣、reply 失敗不扣、正常路徑 +1、超量回配額訊息不 +1、TOCTOU 防護（increment 鎖內不超賣）；每個「不扣」案例皆附反向對照組防假綠。
重點: 舊版 `check_and_increment`（前扣）仍存在，但**只被 `require_quota` dependency 用於一般 API**（Notes、Billing 等），**不影響 LINE webhook 路徑**，兩套路徑並立、互不干擾。
重點: 業界最佳實踐（TOCTOU 文獻）：對 billing-critical 計量，推薦「SELECT FOR UPDATE + 鎖內最終驗證 + 單一 commit」，現有實作完全對齊。來源：[TOCTOU Wikipedia](https://en.wikipedia.org/wiki/Time-of-check_to_time-of-use)

### 建議

建議: **本輪任務目標已達成，無需新開發**。建議 PM 確認本次改良需求是否來自舊版狀態描述，若是則直接關單，跑一次 `pytest tests/test_line_task1_quota_billing.py` 作驗收憑證即可。
建議: 若團隊仍想補強，唯一可考慮的缺口是：**`require_quota`（一般 API dependency）仍採前扣**。若要統一，可仿 webhook 路徑改為 `has_quota` + 業務邏輯 + `increment_usage`，但需評估其他端點（Notes/Billing）是否有明確的「成功副作用」定義，否則語意模糊不適合強制後扣。
建議: M2 技術債提醒：`line_client.reply` 仍為同步阻塞 I/O（見 webhook 檔案 NOTE），高並發時應 `asyncio.to_thread` 包裝，但與 quota 機制無關，另案追蹤。

---

**Sources:**
- [Time-of-check to time-of-use — Wikipedia](https://en.wikipedia.org/wiki/Time-of-check_to_time-of-use)
- [What Is TOCTOU? — DeepStrike](https://deepstrike.io/blog/what-is-time-of-check-time-of-use-toctou)

## 2026-06-16 06:58

<think>
我已蒐集到足夠的 OWASP 與 RFC 資料。讓我整理調研報告。

關鍵發現：
1. **OWASP Authentication Cheat Sheet 明確建議**：登入失敗的回應要避免區分「帳號不存在」與「密碼錯誤」——同一回應碼、同一訊息字串。原因是攻擊者可用不同回應列舉出有效帳號。
2. **本任務情境**：LINE webhook 是公開端點（無登入），`tenant_id` 暴露在 URL。攻擊者用任意 `tenant_id` 撞 webhook，觀察「未設定 config」vs「簽章錯誤」vs「正常」的差異 → 就能列舉出哪些 `tenant_id` 有綁 LINE channel。
3. **統一回應碼的選擇**：
   - **400 Bad Request**：語意較弱（請求格式不對），但 webhook 場景下，LINE 平台送來的就是這個請求，回 400 不會被 LINE 重試（避免放大攻擊）——事實上 LINE 重試策略對 4xx 都會停止。
   - **401 Unauthorized**：語意最貼「認證失敗」，但 LINE 不會送 WWW-Authenticate，且 webhook 場景不適合 WWW-Authenticate flow。
   - **403 Forbidden**：可選，但語意偏向「你沒權限」，本情境是「請求不存在/格式不對」。
   - **404 Not Found**：會讓攻擊者以為「URL 不存在」而嘗試別的，無助於資安。
4. **LINE 平台對 webhook 失敗回應的行為**：[Webhook retries](https://developers.line.biz/en/docs/messaging-api/retrying-api-calls/) —— 4xx 不重試、5xx 重試。所以用 4xx 既保護用戶隱私又不會被放大流量。
5. **OWASP 的「相同回應」原則**：status code **和** response body 都要一致，否則攻擊者從 body 也能區分。
6. **timing attack 防護**：驗簽章用 `hmac.compare_digest`（常數時間），未設定 config 不應進入 `hmac.compare_digest`（避免從時間差列舉）。可以先 `hmac.compare_digest` 對一個 dummy 值，再去查 config——但 webhook 邏輯裡 config 缺失在簽章檢查**之前**就要決定，因為簽章需要 secret。實務做法：先查 cfg → 若 cfg 缺失 → 用 dummy secret 跑常數時間驗簽 → 失敗 → 一致回 400。這樣 timing 一致。
7. **logging 仍應記錄具體原因**：對外一致回應，server-side log 仍可記 "no config for tenant X" / "signature mismatch"，供監控與除錯。

我已經有足夠資料寫報告。不需要再讀本地源碼（user 已給出過去文檔摘要），重點是業界最佳實踐 + 統一回應的具體策略。

讓我直接寫報告。
</think>
查得差不多了，整理給你看。

---

## 調研報告：webhook 未設定 config 與簽章錯誤回應碼統一化

### 核心發現：**OWASP 對「列舉防護」的明確指引**

重點: **OWASP Authentication Cheat Sheet — Authentication and Error Messages 章節明確要求**：登入或認證失敗的回應，不可區分「帳號/資源不存在」與「密碼/簽章錯誤」——status code、body 字串、Content-Length 都應一致，否則攻擊者可由差異列舉出有效資源。來源：[OWASP Authentication Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Authentication_Cheat_Sheet.html#authentication-and-error-messages)
重點: 同章節具體推薦用同一段 generic error message（如 `"Login failed; Invalid userID or password"`），並建議在伺服器端 log 區分具體原因（供監控），但**對外不洩漏**。來源同上。
重點: OWASP Error Handling Cheat Sheet：4xx/5xx 對外不應夾帶實作細節、stack trace、內部判斷邏輯；對外回應的「區分度」就是攻擊面的資訊洩漏點。來源：[OWASP Error Handling Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Error_Handling_Cheat_Sheet.html)

### 核心發現：**LINE webhook 平台行為**

重點: LINE 平台對 webhook 失敗回應的處理：**收到 4xx 視為客戶端錯誤、不重試；收到 5xx 視為伺服器錯誤、會自動重試**。來源：[Retrying API calls — LINE Developers](https://developers.line.biz/en/docs/messaging-api/retrying-api-calls/)
重點: 結論：4xx 是「友善拒絕」的正確選擇——既不洩漏資訊給攻擊者，也避免 LINE 反覆重試放大攻擊面；5xx 反而會觸發重試把問題擴大。

### 核心發現：**HTTP status code 語意（RFC 9110）**

重點: **400 Bad Request** = 伺服器無法/不處理此請求（請求格式、簽章、payload 任一問題皆適用），是最貼 webhook 拒絕場景的代碼。來源：[RFC 9110 §15.5.1](https://datatracker.ietf.org/doc/html/rfc9110#name-400-bad-request)
重點: **401 Unauthorized** = 缺少/錯誤認證、用戶端可重試補上；但 webhook 場景下 LINE 不會送 `Authorization` 標頭、也不會處理 `WWW-Authenticate` challenge，401 會誤導監控語意。
重點: **403 Forbidden** = 伺服器理解請求但拒絕執行（權限不足）；適合「資源存在但 tenant 被停用」語意，不適合「未設定 config」（資源尚未綁定）。
重點: **404 Not Found** = URL 對應資源不存在——會鼓勵攻擊者換 URL 嘗試（撞 path traversal），且會混淆 web server access log 的解讀。
重點: **結論**：400 是最不洩漏資訊、且符合 RFC 語意（包含「請求的資源無法用此請求處理」涵義）的選擇。原文："The 400 (Bad Request) status code indicates that the server cannot or will not process the request due to something that is perceived to be a client error"。

### 核心發現：**Timing attack 防護（不可忽略的列舉向量）**

重點: 即使 status code 統一，**回應時間差**也會成為列舉通道——例如「未設定 config」分支直接 return，「簽章錯誤」分支跑 `hmac.compare_digest`（常數時間）後再 return，兩者時間差異即可被攻擊者統計區分。
重點: 業界標準防護：對所有「拒絕路徑」執行**常數時間的 dummy 運算**（例如 `hmac.compare_digest(real_sig, b"\x00"*256)`），再統一回應。Python 用 `hmac.compare_digest` 即具常數時間保證。來源：[Python hmac.compare_digest docs](https://docs.python.org/3/library/hmac.html#hmac.compare_digest)
重點: 對應到本專案：`cfg is None` 時仍以 `cfg.channel_secret` 的「替代值」（如固定 dummy bytes）跑一次 `hmac.compare_digest`，再 return 400——這樣兩條拒絕路徑的時間成本對齊。

### 對齊既有架構的觀察

重點: 既有 `line_webhook.py` 步驟（依過去研究文檔）：先 `lookup LineChannelConfig` → 拿 secret → `hmac.verify` → 處理 events。當前設計即已把「cfg 不存在」與「簽章錯誤」視為**不同的失敗原因**（可能不同 status code 或不同 body）——這正是本輪要修的列舉漏洞。
重點: 既有 `test_line_task1_quota_billing.py` 已是完整的測試骨架（每個「不扣」案例附反向對照組防假綠），本輪的「統一 400」測試應沿用相同模式：對「未設定 config」「簽章錯誤」各加案例，斷言 status code + body 內容**逐字節相等**，並附**正向對照組**（有效簽章且 config 存在 → 200/200 OK），防測試本身假綠。

---

## 建議

建議: **回應碼統一為 400 Bad Request**，body 固定為 `{"detail": "Bad Request"}`（或同等的固定字串），**兩條拒絕路徑共用同一 helper**（如 `_reject_webhook(reason_for_log: str)`），對外只回固定 body 與 400，內部 log 區分 `reason_for_log`。LINE 平台對 4xx 不重試，不會放大攻擊面（見 LINE retrying 規範）。
建議: **timing 對齊**：在 `cfg is None` 與「簽章錯誤」兩條拒絕路徑，**都執行一次 `hmac.compare_digest` 常數時間比對**再 return。實作：抽出一個 `_verify_signature_or_dummy(body, sig_header, secret_or_dummy)`，未設定 config 時傳入固定 dummy secret（如 `b"\x00"*32`），保證兩條路徑的 CPU 開銷落在同一量級。這是業界標準做法，OWASP 與 Python 官方文件都支持。
建議: **保留伺服器端 log 區分**：對外一致，log 仍可記 `INFO webhook rejected reason=no_config tenant_id=X` / `reason=bad_signature tenant_id=X`——這是 OWASP 推薦模式，**日誌用於監控，外部回應用於列舉防護**，兩者不衝突。
建議: **測試案例三件套**（沿用既有對照組風格）：
1. `cfg is None` → 400 + 固定 body（不可含 "config" / "not found" / "tenant" 等洩漏字眼）
2. `簽章錯誤` → 400 + **逐字節**相同 body
3. **正向對照**：合法簽章 + 存在 config → 200 / 200 OK（防測試本身假綠）
4. 額外加 `assert response.content_length == ...` 或 `response.text == other_response.text` 的逐字節等價斷言。
建議: **不要同時改其他 status code**（如把 200 改成 4xx、或加 WWW-Authenticate 標頭）——LINE 平台對 webhook 端點的契約是「成功回 200」；改壞契約會讓正常訊息失效。範圍鎖定在「兩條拒絕路徑」的統一。
建議: **不需改路由策略**：URL-based 路由（`/line/webhook/{tenant_id}`）保留；本輪純粹是「對外不可區分」加固，與既有 M1/M2 規劃的 `destination` 防護層互補（本輪是 status code 層級，下輪是 destination 內容層級）。兩者可疊加。
建議: **M2 技術債提醒**：本輪做「timing 對齊」會在 cfg 缺失路徑多做一次 `hmac.compare_digest` 計算；該成本可忽略（sub-microsecond），不影響效能預算。若日後 `compare_digest` 路徑被 hot-path profiler 標紅再回頭優化。

---

**Sources:**
- [OWASP Authentication Cheat Sheet — Authentication and Error Messages](https://cheatsheetseries.owasp.org/cheatsheets/Authentication_Cheat_Sheet.html#authentication-and-error-messages)
- [OWASP Error Handling Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Error_Handling_Cheat_Sheet.html)
- [RFC 9110 §15.5.1 — 400 Bad Request](https://datatracker.ietf.org/doc/html/rfc9110#name-400-bad-request)
- [LINE Developers — Retrying API calls](https://developers.line.biz/en/docs/messaging-api/retrying-api-calls/)
- [Python hmac.compare_digest — 常數時間比對保證](https://docs.python.org/3/library/hmac.html#hmac.compare_digest)

總結一句話：**統一回 400 + 固定 body + 常數時間 dummy 驗簽 + log 內部區分**，這是 OWASP/RFC/Python 文件三邊一致的最乾淨解。

