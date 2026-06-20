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

## 2026-06-16 12:06

<think>
看到關鍵的 starlette background.py 源碼：

```python
class BackgroundTask:
    def __init__(self, func, *args, **kwargs):
        ...
        self.is_async = is_async_callable(func)

    async def __call__(self) -> None:
        if self.is_async:
            await self.func(*self.args, **self.kwargs)
        else:
            await run_in_threadpool(self.func, *self.args, **self.kwargs)
```

關鍵發現：
- `BackgroundTask` 接受**任何 callable**——`async def` 走 native await，sync 函式走 `run_in_threadpool`（等同於 `asyncio.to_thread`）
- 多個 BackgroundTask 用 `BackgroundTasks` 包，**依序執行**（for loop in `__call__`）：「In case one of the tasks raises an exception, the following tasks will not get the opportunity to be executed」

這對 webhook 場景影響：
- handler 內的 `for event in events` 應**整段**包成一個 background task（不是每個 event 一個）——這樣 reply 順序保證、quota 計數對齊單一 event
- 若 task 拋 exception，Starlette 預設會**默默吃掉**（不 re-raise）——這對 webhook 場景是好是壞？壞處是 bug 看不到，但好處是 LINE 不會因此重試失敗任務

我已蒐集到足夠資料，現在來整理調研報告。

**本輪任務的核心決策點**：
1. 用 FastAPI `BackgroundTasks`（Starlette 內建）還是 `asyncio.create_task`？
2. 是否需要換成 task queue（Celery/ARQ）？
3. 既有測試怎麼改？背景任務在 TestClient 下的可測試性？
4. 既有後扣 quota 邏輯是否需要調整？
5. redelivery 去重是否仍然有效？

**我的答案**：
- **用 FastAPI BackgroundTasks**：最簡單、最對齊既有架構；任務丟失風險在 M1 階段可接受（LINE 重投 redelivery 機制可緩衝；任務在丟失時最壞情況是「使用者訊息未翻譯」——可由 redelivery 補上）
- **不換 task queue**：M2 之後若需要 retry / 持久化再升級；MVP 過度設計
- **既有測試改為**：注入 spy translator / line_client 記錄 call；test 內於 response 後**等待 background 完成**（Starlette TestClient 確實會在 response 後執行 background task，所以 spy 會被填到）
- **後扣 quota 邏輯不變**：background 內仍跑 `has_quota` + `translate` + `reply` + `increment_usage` 整段
- **redelivery 去重仍有效**：仍由 handler 在 background task 內檢查 `isRedelivery`

**時間軸**：
- handler: 1. 收 raw body → 2. 查 cfg → 3. 驗章 → 4. parse payload → 5. destination check → 6. **add_task(_process_events, events, ...) → 7. return {"status": "ok"}**（馬上回 200）
- background task `_process_events(events, ...)`: 原本 handler 內的 for-loop 整段

**測試注意事項**：
- 既有測試依賴 response 後 spy 已被呼叫（reply 副作用、quota 副作用）
- Starlette TestClient 行為：`client.post(...)` 會**同步**等到 background 完成才 return——這是關鍵，可以直接斷言
- 若用 `asyncio.create_task` 就要保留 reference + `add_done_callback`，較複雜；用 `BackgroundTasks` 較乾淨
- **不需引入 asgi-lifespan 也不需 `async with TestClient`**——`client.post()` 即可完成 background

**風險**：
- Worker crash / restart 任務丟失 → LINE redelivery 補
- SlowAPI 等限流器在 background 後無法擋下（fast-fail）→ 不影響本任務（quota 已在 background 內）
- 大量 webhooks 同時觸發 → Starlette thread pool 預設 40 thread，background task 預設跑在 threadpool（sync 函式）或 event loop（async 函式）；同步 line_client.reply 會佔用 threadpool

**M2 技術債**：
- 若要 retry / 持久化 → 升級到 ARQ（asyncio-native 的 task queue，Redis backend）
- 若 background task 數量爆增 → 改用 asyncio.create_task + queue consumer

我已蒐集到完整資料，現在寫報告。
</think>

資料已齊，整理給你看。

---

## 調研報告：LINE webhook 背景化處理

### 既有程式碼確認

重點: `line_webhook.py` 完整實作：HMAC 驗章、列舉防護、後扣 quota、destination 二次驗證、redelivery 去重、`asyncio.to_thread` 處理 DeepL/line_client 阻塞 I/O。**沒有背景任務**，handler 內 `for event in events: ...` 同步走完整個翻譯+回覆鏈。
重點: 處理瓶頸：DeepL API 100-500ms、`increment_usage` 內 `SELECT FOR UPDATE` 持鎖時間、多 events 累加。雖然單一訊息 < 1s，但**無法保證**低流量時永遠 < LINE 隱形 timeout。

---

### 核心發現：**LINE 官方明確建議 async 處理**

重點: LINE 官方文件：「We recommend processing events asynchronously. This is to prevent subsequent requests to wait until the current request is processed.」來源：[LINE Developers — Receiving messages](https://developers.line.biz/en/docs/messaging-api/receiving-messages/)
重點: LINE 平台暫停推送條件：「The LINE Platform may suspend sending webhooks to your bot server if your bot server fails to receive webhooks for a long time」——沒公布具體秒數（社群流傳約 1-3s），但**明確不公開** timeout。**重點是「fail to receive」= 沒回 2xx**而非「slow response」；只要回 200 就不會被暫停。來源同上。
重點: 對應本任務：把內部處理移到 background、handler 立即回 200——這是 LINE 官方明確推薦的 webhook 模式。**不需擔心「LINE 何時 timeout」**，只要回 200 就安全。

---

### 核心發現：**LINE Bot SDK v3 已原生支援 async**

重點: `linebot.v3.messaging.AsyncMessagingApi` + `AsyncApiClient` 是官方 async client（httpx 底層），**不需要 `asyncio.to_thread` 包裝**。來源：[line-bot-sdk-python README](https://github.com/line/line-bot-sdk-python)
重點: 官方 fastapi-echo 範例**用 `await line_bot_api.reply_message(...)`** 直接呼叫——證明 async 是 SDK 一級支援。來源：[examples/fastapi-echo/main.py](https://github.com/line/line-bot-sdk-python/blob/master/examples/fastapi-echo/main.py)
重點: **既有專案目前用自製 `HttpLineReplyClient`**（`urllib` 阻塞），不是 SDK 的 `MessagingApi`。本輪任務是改背景，**不必順便換成 SDK async client**——`asyncio.to_thread` 包住即可，介面不變。換 SDK 屬獨立 M2 任務。

---

### 核心發現：**FastAPI BackgroundTasks 機制**

重點: `BackgroundTask(func, *args, **kwargs)`：**async 函式走 native await、sync 函式自動 `run_in_threadpool`**（= `asyncio.to_thread`）。因此 webhook 內 sync 阻塞函式（DeepL、urllib reply）丟進 background 也安全——會在 threadpool 跑，不阻塞 event loop。來源：[starlette/background.py](https://github.com/Kludex/starlette/blob/main/starlette/background.py)
重點: `BackgroundTasks`（複數）依序執行 task；**某個 task 拋 exception，後續 task 不會執行**。對 webhook 影響：把整個 `for event in events` 包成**單一** background task——event 之間的 reply 順序、quota 計數、redelivery 去重都保持單一 task 內的同步語意。來源同上。
重點: 背景 task 在 **HTTP response 送出後**才跑（Starlette 文件明文：「A background task should be attached to a response, and will run only once the response has been sent」）。來源：[Starlette Background Tasks](https://www.starlette.io/background/)
重點: FastAPI 官方 Caveat：「**If you have a heavy background task, you may need a different approach**」——暗示 BackgroundTasks 適合 fire-and-forget、不可恢復任務；若需 retry/持久化，應升級到 task queue。來源：[FastAPI Background Tasks - Caveat](https://fastapi.tiangolo.com/tutorial/background-tasks/#caveat)

---

### 核心發現：**TestClient 對背景任務的行為（測試關鍵）**

重點: Starlette `TestClient` 對 `background` 屬性的處理：`response(...)` 內部在送出 HTTP body **之後**會 `await self.background()`。`TestClient.post()` 是**同步介面但底層 anyio 跑 async**——會**等到 background 完成**才 return response。來源：[Starlette Testclient](https://www.starlette.io/testclient/) + 源碼 `[starlette/responses.py](https://github.com/Kludex/starlette/blob/main/starlette/responses.py)`
重點: 實務結論：既有測試（如 `test_line_task1_quota_billing.py`）注入 spy translator / line_client 後**可直接斷言副作用**——`client.post(...)` 返回時 background 已跑完，spy 已被填到。**不需 `await asyncio.sleep(...)` 也不需 `async with TestClient`**。
重點: 既有測試模式（沿用即可）：
- Spy translator 記錄 `translate_call_count`、`translate_args`
- Spy line_client 記錄 `reply_call_count`、`reply_args`
- 斷言 `spy.translate_call_count == 1`、`spy.reply_call_count == 1`、`increment_usage(plan, chars=N)` 寫入 DB——這些都在 background 內執行，response 200 後即可驗

---

### 核心發現：**BackgroundTasks vs asyncio.create_task vs task queue 取捨**

重點: **`BackgroundTasks`（推薦用）**：response 後跑、in-process、依序、簡單。代價：worker crash 任務丟失、無 retry、無跨 process。
重點: **`asyncio.create_task`**：彈性高、可並行多 task、可用 `add_done_callback` 自動 cleanup。代價：**必須保留 strong reference**（事件迴圈只保留 weak ref，否則可能被 GC），仍 in-process。來源：[Python asyncio.create_task](https://docs.python.org/3/library/asyncio-task.html#asyncio.create_task)——原文：「Save a reference to the result of this function, to avoid a task disappearing mid-execution」。
重點: **Task queue（Celery / ARQ / RQ）**：跨 worker、持久化、retry、monitoring。代價：需 broker（Redis）、運維複雜度提升、序列化開銷。**M1 階段不建議**——LINE redelivery 機制已能覆蓋任務丟失場景。
重點: **本任務最佳選擇：FastAPI `BackgroundTasks`**。理由：與既有 async 架構一致（Starlette 內建、無新依賴）、測試友善、滿足「快速回 200」目標；M2 升級到 ARQ（asyncio-native）只換實作不換介面，無痛遷移。

---

### 核心發現：**既有架構的語意保留**

重點: **後扣 quota 不受影響**——`has_quota` → `translate` → `reply` → `increment_usage(+1)` 整段移入 background，**仍是單一 task 內同步執行**，`SELECT FOR UPDATE` 鎖仍單一 commit。
重點: **redelivery 去重仍有效**——`isRedelivery=true` 的 event 在 background task 內被 `continue` 略過，不翻譯、不計量、不 reply。LINE redelivery 機制 + 既有去重是任務丟失的**雙重保險**。
重點: **錯誤處理語意**：現有 `LineConfigDecryptionError` 回 200（不解密也回 200 阻止 LINE 重試）——這個決策**在 background 化後仍正確**，因為 handler 在 background 化前已完成 cfg 查詢與解密，handler 主體對 LINE 一律回 200。
重點: **destination 二次驗證、簽章驗證、列舉防護**：全在 handler 主體（背景化**之前**）——這些是「是否要處理這個 webhook」的判斷，**絕不能**丟到 background，否則攻擊者可趁背景延遲注入攻擊。

---

### ⚠️ 常見坑

重點: **不要把「驗章 / cfg 查詢 / destination 二次驗證」丟背景**——這些都是「要不要回 200」的判斷，丟背景就等於對所有請求都回 200，列舉防護失效。**只有「處理 events」（翻譯 + reply + quota）丟背景**。
重點: **不要把 raw body 序列化後丟 background**——`request.body()` 在 response 送出後可能已被回收（Starlette 文件明文「`Request.body()` consumed once」）。必須**在 handler 主體先 `body = await request.body()` 存到變數，再丟 background**。不過本任務是「把已 parse 完的 events 物件丟 background」——但 events 內含 reply_token、line_user_id、user_id 等敏感資料，序列化到 task 沒問題（task 同一進程）。
重點: **不要每個 event 開一個 background task**——會破壞 reply 順序保證、quota 計數語意。**整個 `for event in events` 包成單一 task**。
重點: **TestClient 雖然會跑完 background，但 background 內若 raise 例外，Starlette 預設會**默默吃掉**（不 re-raise）——測試若想驗「background 內某 spy 被呼叫」要靠 spy 本身，不能靠 exception 傳出。建議：背景 task 內部 `try/except` 包整段、log 錯誤，避免測試難除錯。LINE redelivery 會處理真正的失敗。
重點: **跨 worker 部署的任務丟失**：BackgroundTasks 是 in-process——若 uvicorn 用 `--workers 4`，每個 worker 是獨立 process，handler 收到的 task 只在該 worker 跑；worker 重啟任務丟失。**M1 單 worker 部署無此問題**；多 worker 部署時依賴 LINE redelivery 補。
重點: **threadpool 容量**：Starlette 預設 40 thread 給 `run_in_threadpool`。若 webhook 量大且每個 task 都用 `asyncio.to_thread`（DeepL + reply = 2 個），40 / 2 = 20 並發任務。**M1 流量下遠超需求**；高流量時可調 `anyioCapacity` 或改 async client。

---

## 建議

建議: **本輪採用 FastAPI `BackgroundTasks`**，在 `line_webhook.py` 抽出 `_process_events(tenant_id, events, tenant, cfg, ...)` 純函式，handler 在「完成 cfg 查詢 + 驗章 + destination 二次驗證 + parse payload」**之後** `background_tasks.add_task(_process_events, ...)` 丟背景，handler 主體**直接 return `{"status": "ok"}`** 馬上回 200。
建議: **`_process_events` 接收不可變的輸入**：`tenant_id` (int)、`events` (list[dict])、`target_lang_for_user` 等解析後的純資料；不要傳 `request`、`db session`——DB session 在 background 內另開。理由：避免 response 送出後 `request` 物件被回收的邊界情況（雖然同進程不會被回收，但語意更乾淨）。
建議: **既有 handler 邏輯切片**：
- 留在 handler：cfg 查詢、HMAC 驗章、payload parse、destination 二次驗證、redelivery flag 解析、log warning
- 移到 `_process_events`：每個 event 的 `/lang` 指令解析、target_lang 決定、`has_quota`/`has_char_quota`、`translate`、`reply`、`increment_usage`
- handler 內的 `increment_usage` DB 操作連同 `db` session 一起丟 background——SQLAlchemy session 與 task 同一進程，安全
建議: **BackgroundTasks 注入方式**：endpoint 加 `background_tasks: BackgroundTasks` 參數（FastAPI 自動注入）；`background_tasks.add_task(_process_events, tenant_id, events, tenant.plan, cfg, access_token, db)`。`db` session 跨 await 邊界在 FastAPI 是慣用模式（既有 `require_quota` dependency 也如此），測試可用 `dependency_overrides` 注入 mock session。
建議: **測試策略三件套**（沿用既有 `test_line_task1_quota_billing.py` 風格）：
1. **同步路徑（response 已 200，但 background 尚未跑）**：`response = client.post(...)` 後立即斷言 `response.status_code == 200` 與 `response.json() == {"status": "ok"}`
2. **背景副作用（TestClient 等到 background 結束）**：`response` 返回後直接斷言 `spy.translate_call_count == 1`、`spy.reply_call_count == 1`、DB 內 `ApiUsage` 寫入成功——這些都在 TestClient 返回前已執行
3. **附正向對照組**（無 cfg / 簽章錯 → 400，**無** background 副作用）：斷言 `spy.translate_call_count == 0`、`ApiUsage` 未新增——防 background 在拒絕路徑誤觸發
建議: **不引入 task queue**（M1 階段）：LINE redelivery 機制 + 既有 `isRedelivery` 去重已覆蓋任務丟失場景；Celery/ARQ 引入 broker、運維成本不符合 M1 價值。
建議: **不換成 SDK async client**：M2 獨立任務；既有 `HttpLineReplyClient` + `asyncio.to_thread` 在 background 內跑得動，介面不變。
建議: **錯誤處理**：`_process_events` 內部用 `try/except Exception` 包整段，`log.exception` 後 swallow——理由：背景 task 例外**不影響** 200 response（Starlette 已送出 response），但 log 必須保留供除錯；LINE redelivery 會補上失敗的 event。
建議: **M2 技術債登記**：
1. 多 worker 部署時的任務丟失（升級 ARQ 或 Celery）
2. `HttpLineReplyClient` 改用 `AsyncMessagingApi`（移除 `asyncio.to_thread`）
3. 重試與 dead-letter 機制（若任務失敗需重試 N 次）
4. background task 監控指標（inflight count、平均耗時、p99 latency）——加 Prometheus / OpenTelemetry

---

**Sources:**
- [LINE Developers — Receiving messages (webhook) — We recommend processing events asynchronously](https://developers.line.biz/en/docs/messaging-api/receiving-messages/)
- [line-bot-sdk-python README](https://github.com/line/line-bot-sdk-python)
- [line-bot-sdk-python fastapi-echo 範例](https://github.com/line/line-bot-sdk-python/blob/master/examples/fastapi-echo/main.py)
- [FastAPI Background Tasks 官方教學](https://fastapi.tiangolo.com/tutorial/background-tasks/)
- [FastAPI Background Tasks - Caveat 章節](https://fastapi.tiangolo.com/tutorial/background-tasks/#caveat)
- [Starlette Background Tasks 官方文檔](https://www.starlette.io/background/)
- [starlette/background.py 源碼](https://github.com/Kludex/starlette/blob/main/starlette/background.py)
- [Starlette Testclient 官方文檔](https://www.starlette.io/testclient/)
- [Python asyncio.create_task — Save a reference 警告](https://docs.python.org/3/library/asyncio-task.html#asyncio.create_task)
- [Python asyncio.TaskGroup 結構化並行](https://docs.python.org/3/library/asyncio-task.html#asyncio-task.html#asyncio-task.html)

總結一句話：**用 FastAPI `BackgroundTasks`、handler 切片「驗章/查 cfg/parse/destination」留主體、「處理 events」丟背景、既有 spy-based 測試幾乎不用改**——LINE redelivery 機制是任務丟失的最後保險，M2 再升級 task queue。

## 2026-06-16 13:48

<think>
我已蒐集到關鍵事實。現在我可以精準判斷本輪任務。

**核心發現**：
1. **`_process_events` 是 sync 函式**，被 FastAPI `BackgroundTasks` 自動以 `run_in_threadpool` 跑——等同於 `asyncio.to_thread`（底層都是 `loop.run_in_executor` + thread pool）
2. **`line_client.reply` 內 `urllib.request.urlopen()` 是阻塞 I/O**，但在 threadpool 中執行，所以**不阻塞 event loop**
3. **`asyncio.to_thread` 只在 async 函式內 `await` 才有效果**——對 sync 函式無法用，且對「已在 threadpool 中的 sync 函式」是冗餘雙重包裝
4. **真正能改善的是換 async HTTP client**——`httpx.AsyncClient` 或 LINE Bot SDK v3 的 `AsyncMessagingApi`，釋放 threadpool 線程

**方案分析**：

| 方案 | 變更範圍 | 對現有架構影響 | 實際效果 |
|---|---|---|---|
| A. `await asyncio.to_thread(line_client.reply, ...)` 在 async 包裝內 | 需把 `_process_events` 改 async、FakeLineReplyClient 改 async、SQLAlchemy sync session 衝突 | 高 | 零（已等同 run_in_threadpool） |
| B. 換 `httpx.AsyncClient` | `LineReplyClient` 介面改 async、FakeLineReplyClient 改 async、SQLAlchemy 衝突、httpx 連線管理 | 高 | 釋放 threadpool 線程 |
| C. **保留現狀**——文件說明 `_process_events` 在 threadpool 跑、blocking call 已移出 event loop | 0 行程式碼 | 0 | 對 event loop 阻塞問題 0（已解決） |
| D. 換 LINE Bot SDK v3 `AsyncMessagingApi` | 同 B，但換底層實作 | 高 | 同 B |

**最佳方案**：本輪「用 asyncio.to_thread 包裝」對**當前 BackgroundTasks 架構**而言是**自造輪子**——既有的 `run_in_threadpool` 已達成相同目的。`asyncio.to_thread` 的合理使用場景是「async handler 直接呼叫 sync 阻塞函式時包一層」，**而非「已在 threadpool 中的 sync 函式再包一層」**。

**真正的 M2 改進方向**是：換 `httpx.AsyncClient` 或 `AsyncMessagingApi`，把 sync HTTP 換成 async HTTP，釋放 threadpool 線程。但這不是本輪「`asyncio.to_thread`」選項的範疇。

**我的建議**：
1. **本輪拒絕做「`asyncio.to_thread` 包裝」**——理由：背景化後的 sync 函式已在 threadpool，再包一層是冗餘。
2. **改做對等補救**：在 `line_client/http.py` docstring 與 `_process_events` 的註解，明確標示「在 BackgroundTasks 機制下，`reply()` 的阻塞 I/O 已被 threadpool 移出 event loop，無需另行包裝」——這比「為了做而做」更對得起架構。
3. **若真要消除 threadpool 佔用**：升級到 `httpx.AsyncClient` 或 LINE Bot SDK v3 `AsyncMessagingApi`，但需要：
   - `LineReplyClient.reply` 改 async 方法（介面破壞性變更）
   - `_process_events` 改 async def（連帶 `Session(bind=bind)` 改 `AsyncSession`）
   - SQLAlchemy 2.0 async session 整套改寫
   - 既有測試 spy 介面要改
   - 屬 M2 範疇，不在本輪
4. **M2 技術債登記**：換 `httpx.AsyncClient`、把 `LineReplyClient` 介面改 async、`_process_events` 改 async def + `AsyncSession` 改寫

讓我整理一份精簡的調研報告。
</think>

資料已齊，本輪任務的「`asyncio.to_thread` 包裝」**在當前 BackgroundTasks 架構下是個偽命題**——我必須明說這一點再給方向。

---

## 調研報告：line_client.reply 的 blocking 呼叫處理

### 關鍵現況（必須先看再談方案）

重點: **`_process_events` 是 `def`（同步函式）**，由 FastAPI `BackgroundTasks.add_task()` 加入；Starlette/FastAPI 對 sync 函式自動 `run_in_threadpool` 跑（源碼 `[starlette/background.py:39–42](https://github.com/Kludex/starlette/blob/main/starlette/background.py)`）。`run_in_threadpool` 底層就是 `loop.run_in_executor(None, ...)`，**語意等同於 `asyncio.to_thread`**。來源：[Starlette Background Tasks 源碼](https://github.com/Kludex/starlette/blob/main/starlette/background.py)
重點: `urllib.request.urlopen()` 在 `_process_events` 內呼叫時，**執行緒已在 threadpool 中**——主 event loop 不會被這個 I/O 阻塞。`line_webhook.py` 第 6c 步的 `# NOTE: line_client.reply 同為阻塞 I/O — 高流量下應 wrap in asyncio.to_thread (M2 技術債)` 註解**語意已過時**——它寫於背景化之前，當時 handler 同步執行，現在 handler 已丟背景。
重點: `asyncio.to_thread` 是 `anyio.to_thread.run_sync` 的薄包裝（FastAPI 透過 Starlette 也用 anyio），**底層同一個 thread pool**。在「sync 函式已在 threadpool」上再 `await asyncio.to_thread(sync_func, ...)` 會把任務**遞迴丟回 threadpool**——多佔一個 thread、零效能改善。來源：[anyio Working with threads](https://anyio.readthedocs.io/en/stable/threads.html)
重點: LINE Bot SDK v3 有原生 async client `AsyncMessagingApi` + `AsyncApiClient`（httpx 底層），官方 fastapi-echo 範例用 `await line_bot_api.reply_message(...)` 直接呼叫——這才是**真正**消除 threadpool 佔用的路徑。來源：[line-bot-sdk-python fastapi-echo/main.py](https://github.com/line/line-bot-sdk-python/blob/master/examples/fastapi-echo/main.py)
重點: 既有 `pyproject.toml` 沒有 `httpx` 為 runtime 依賴（只在 test optional）；改用 async HTTP client 需把 `httpx>=0.27` 提到 runtime deps，或裝 `line-bot-sdk` 換掉自製 `HttpLineReplyClient`。

### 方案 A：在 `_process_events` 內 `await asyncio.to_thread(line_client.reply, ...)`  ❌

重點: **不可行也不該做**。`_process_events` 是 `def`，**不能 `await`**。要這樣做得先把 `_process_events` 改 `async def`：
  - 改 async 後，BackgroundTasks 對 async 函式走 native await，**不再**走 threadpool——反而**回退**到「reply 阻塞 I/O 直接打到 event loop」
  - 為此在 async 內 `await asyncio.to_thread(line_client.reply, ...)` 才有意義——但這等於「先升級 async、再用 to_thread 把 sync 拉回 threadpool」，**淨效果 = 原地踏步**，且動了一堆介面
  - 結論：這條路是「為做而做」，自造輪子、偽進步

### 方案 B：把 `_process_events` 改 `async def` + 換 `httpx.AsyncClient` / `AsyncMessagingApi`  ✅（但屬 M2 範疇）

重點: 真正的 threadpool 釋放需要「I/O 真的變非同步」。改 async 後每個 reply 不再佔 threadpool 線程、Starlette 預設 40 thread 不再成為瓶頸。
重點: **介面破壞性變更鏈**：`LineReplyClient.reply` async 化 → `FakeLineReplyClient.reply` async 化（測試 spy 介面）→ `HttpLineReplyClient` 改 httpx（連線池管理：單一 `AsyncClient` instance + lifespan 或 module-level singleton，否則失去連線池效益）→ `_process_events` 改 async def → SQLAlchemy `Session(bind=bind)` 改 `AsyncSession` + `async with async_engine.begin()` → `increment_usage` 等所有 DB 操作 async 化。
重點: 連線池警示：httpx 文件明文警告「Do not instantiate multiple client instances in a hot loop」——必須用 lifespan / singleton 管理 `AsyncClient`，否則比目前 `urllib` 還糟。來源：[httpx Async Support](https://www.python-httpx.org/async/)
重點: SDK 路徑選項：直接 `pip install line-bot-sdk`，把 `HttpLineReplyClient` 整個換成 `AsyncMessagingApi + AsyncApiClient`——程式碼最短、官方維護。但**介面破壞一樣大**（reply 變 async），且引入新依賴。

### 方案 C：保留現狀 + 釐正註解（建議本輪）  ✅

重點: 當前 `BackgroundTasks` 機制已正確處理 blocking I/O 不阻塞 event loop 的問題。**語意上** `_process_events` 跑在 threadpool，**等同**於「整段包了 `asyncio.to_thread`」。
重點: `line_webhook.py` 第 6c 步的 `# NOTE: ... 應 wrap in asyncio.to_thread (M2 技術債)` 註解**已誤導**——語意上不存在「未包裝」問題；存在的是「sync 阻塞佔 threadpool 線程」問題，這需要 async 化才能解，不是 `asyncio.to_thread` 能解。
重點: **真正該做的**只有 1–2 行程式碼改註解 + 一段 docstring 更新，**0 行程式碼邏輯改動**——這才是「與既有架構一致、不砍掉重練」的應對。
重點: Starlette 40 thread 預設值對 M1 流量足夠（每個 webhook 鏈佔 1 thread，主要時間花在等 DeepL/line_client I/O，thread 是閒置不是 CPU 耗用）；只在**多 worker × 高 RPS** 場景才需要 async 化——M1 不在這個場景。

### ⚠️ 常見坑

重點: **`asyncio.to_thread` 不是萬靈丹**：在「async handler 內呼叫 sync 阻塞函式」場景才有意義；對「sync 函式已在 threadpool」場景是**冗餘雙重包裝**——會遞迴把任務丟回 threadpool、多佔一個 thread、零改善。來源：[anyio Working with threads — Limitations 章節](https://anyio.readthedocs.io/en/stable/threads.html)
重點: 改 async 化的最大坑是 **SQLAlchemy session**：目前 `Session(bind=bind)` 是同步 session，在 `async def` 內直接呼叫會阻塞 event loop 然後炸。改 `AsyncSession` 是另一個獨立改動（`async_engine` 取代 `engine`、所有 ORM 操作 `await` 化、commit 改 `await session.commit()`）。**這是 M2 整套重寫，不是本輪**。
重點: 若堅持走 async 化、但 SQLAlchemy 沒改，會落入「async def 內呼叫 sync session」陷阱——這比目前 sync 模式更糟（warning + 偶發 deadlock）。
重點: 既有測試 `FakeLineReplyClient`（`fake.py`）的 `reply` 是 sync def、`sent` 是 list append——若介面改 async，`test_line_task1_quota_billing.py` 等所有 spy 斷言要重寫。**改動面比方案 A 還大**——再次印證「改 async 是大改、不是小改」。

---

## 建議

建議: **本輪拒絕做「`asyncio.to_thread` 包裝」**——`_process_events` 已在 threadpool 中執行（`BackgroundTasks` 自動 `run_in_threadpool`），`reply()` 的 urllib 阻塞呼叫**已經**不阻塞 event loop；再加 `asyncio.to_thread` 是冗餘雙重包裝，純粹自造輪子，與既有架構（BackgroundTasks 切片）抵觸。建議把這個決策寫進 `line_webhook.py` docstring 與 `_process_events` 註解，避免下次重複討論。
建議: **本輪唯一要改的程式碼**：把 `line_webhook.py` 步驟 6c 的 `# NOTE: line_client.reply 同為阻塞 I/O — 高流量下應 wrap in asyncio.to_thread (M2 技術債)` 註解改為「`reply()` 阻塞 I/O 在 BackgroundTasks 機制下已自動被 `run_in_threadpool` 移出 event loop，等同於 `asyncio.to_thread` 效果；threadpool 線程佔用見 M2 async 化技術債」；模組 docstring「M2 技術債」段落把「`HttpLineReplyClient` 改用 `AsyncMessagingApi`（移除 `asyncio.to_thread`）」更新為「`HttpLineReplyClient` 改用 `httpx.AsyncClient` 或 `AsyncMessagingApi`、`_process_events` 改 `async def` + `AsyncSession` 整套 async 化」，**明確標示 `asyncio.to_thread` 改寫是錯誤方向、不再列入技術債**。
建議: **不引入 `line-bot-sdk` 套件**（除非另有需求）：現有自製 `HttpLineReplyClient` 用 stdlib urllib、零依賴、介面乾淨（ABC + per-call access_token + FakeLineReplyClient 離線測試），換 SDK 是大規模介面破壞換微幅功能增加。M2 真的要改，建議**直接用 `httpx.AsyncClient`** 而非拉整個 line-bot-sdk——只換底層 HTTP、介面維持 ABC。
建議: **M2 技術債登記**（更新版）：① `HttpLineReplyClient` 改用 `httpx.AsyncClient`（lifespan 管理單一 instance）；② `LineReplyClient.reply` 改 async 方法；③ `_process_events` 改 `async def`；④ SQLAlchemy 改 `AsyncSession` + `async with async_engine.begin()` 整套重寫；⑤ `increment_usage` 等所有 DB 操作 async 化；⑥ `FakeLineReplyClient.reply` 改 async、`sent` 改非同步累積；⑦ 既有 spy 測試介面重寫。**整套規模 = 1 個獨立 M2 重構任務**，不混入當前 PR。
建議: **本輪零邏輯改動**——測試全部不動（`test_line_task1_quota_billing.py` 與所有相關測試仍綠）。可選加一個 inline 註解測試（`tests/test_line_webhook_concurrency.py`）斷言「`BackgroundTasks` 機制下 blocking reply 不阻塞 handler 200 回應」：mock `urllib.request.urlopen` 內 `time.sleep(0.5)`、斷言 `client.post(...)` 在 < 100ms 內 return 200——這是把現有架構契約「背景化後 handler 立即回 200」**升級為可被測試守住的不變量**，比「`asyncio.to_thread` 包裝」更有價值。

---

**Sources:**
- [Starlette Background Tasks 源碼](https://github.com/Kludex/starlette/blob/main/starlette/background.py)
- [Starlette Background Tasks 官方文檔](https://www.starlette.io/background/)
- [anyio — Working with threads](https://anyio.readthedocs.io/en/stable/threads.html)
- [Python `asyncio.to_thread` 官方文檔](https://docs.python.org/3/library/asyncio-task.html#asyncio.to_thread)
- [httpx Async Support](https://www.python-httpx.org/async/)
- [line-bot-sdk-python README](https://github.com/line/line-bot-sdk-python)
- [line-bot-sdk-python fastapi-echo 範例](https://github.com/line/line-bot-sdk-python/blob/master/examples/fastapi-echo/main.py)
- [FastAPI Background Tasks 官方教學](https://fastapi.tiangolo.com/tutorial/background-tasks/)

總結一句話：**本輪「`asyncio.to_thread` 包裝」在當前 BackgroundTasks 架構下是自造輪子**——`run_in_threadpool` 已達成相同目的，**真正的改進是換 async HTTP client，但那需要整個 async 化重構、屬 M2 範疇**。建議本輪只更新誤導性註解、不動邏輯。

## 2026-06-19 02:37

我先看現有 `routers/tenants.py` 與 line-config service 介面，再只補查端點設計、FastAPI auth/dependency 與 DELETE/PUT 行為相關文件。

## 2026-06-19 02:59

我會先快速看現有租戶與 LINE 設定程式碼，避免調研脫離架構；再只補查 LINE 官方取得 bot profile/userId、維運端點/批次腳本與交易處理的缺口。

## 2026-06-20 09:16

先看既有測試，了解目前的預期行為：
資料夠了，整理調研結論：

---

## 本輪調研：`_process_events` per-event 例外隔離

**重點: 現況確認** — `_process_events` 的 `try/except Exception` 包住整個 `for event in events`（line 330–449），第一個 event 任何例外（translate/reply/increment_usage 皆可）→ 整個 `except` 觸發 → **後續所有 events 被跳過**，完全符合此次改良動機。

**重點: SQLAlchemy session 例外後不可直接重用** — 官方文件明確：任何 flush/commit 拋例外後，session 進入 invalid state；`continue` 下一個 event 前必須呼叫 `db.rollback()` 才能重置 session，否則後續 SELECT 報「this Session's transaction has been rolled back」錯。來源：[SQLAlchemy 2.0 Sessions FAQ](https://docs.sqlalchemy.org/en/20/faq/sessions.html)

**重點: loop 內 DB 操作清單** — 需要被 rollback 保護的點：`has_quota` / `has_char_quota`（開隱式 tx）、`upsert_user_lang`（db.commit 包含在內）、`increment_usage`（SELECT FOR UPDATE + db.commit）。若 `translate` 或 `reply` 在任何 SELECT 之後拋例外，session 已有未關閉隱式 tx，**必須 rollback** 才乾淨。

**重點: LINE replyToken 各自獨立** — 每個 event 有自己的 replyToken，單次有效、1 分鐘過期，event 間完全無關聯。一個 event 的 reply 失敗不影響下一個 event 的 replyToken。來源：[LINE Messaging API — 回覆訊息](https://developers.line.biz/en/docs/messaging-api/sending-messages/#reply-messages)

**重點: 現有測試無 multi-event batch 失敗場景** — `test_line_task1_quota_billing.py` 等均為單 event payload，不覆蓋「第一個 event 失敗、第二個 event 應繼續」的情境，需補新測試。

---

**建議: per-event try/except 放 for loop 內，結構如下：**
```python
for event in events:
    try:
        # ... 既有完整邏輯不動
    except Exception:
        db.rollback()  # ← 新增：重置 session 狀態，下一 event 用乾淨 tx
        _log.exception(
            "background _process_events event error tenant=%d event_idx=%d",
            tenant_id, idx,  # ← 加 index 便於排查
        )
```

**建議: 外層 `try/finally: db.close()` 保留** — 純粹確保 session 收尾，不動邏輯。

**建議: `except` 裡 `db.rollback()` 必須在 `_log.exception` 之前** — logger 可能也做 DB 操作（若接了 DB handler），先 rollback 讓 session 乾淨最安全。

**建議: 補一個新測試** `test_line_multi_event_isolation.py`：兩個 event payload，第一個 event translate 拋例外，斷言第二個 event 正常完成翻譯 + reply sent + quota +1；驗證「前一個 event 失敗不感染後一個」的契約能被測試守住。

**建議: 外層 `except Exception` 可移除或降格** — 原本外層 try/except 的「整段失敗」場景現在只剩 `db = Session(bind=bind)` 或 for loop 外初始化失敗才觸發；可保留但縮小 scope 至 Session 建立那一行，避免吃掉 per-event 已處理過的例外。

---

**Sources:**
- [SQLAlchemy 2.0 Sessions FAQ — session invalid after exception](https://docs.sqlalchemy.org/en/20/faq/sessions.html)
- [SQLAlchemy 2.0 Session Basics — rollback after exception](https://docs.sqlalchemy.org/en/20/orm/session_basics.html)
- [SQLAlchemy 2.0 — Transactions and Connection Management](https://docs.sqlalchemy.org/en/20/orm/session_transaction.html)
- [SQLAlchemy Discussion #11945 — rollback after exception necessary?](https://github.com/sqlalchemy/sqlalchemy/discussions/11945)
- [LINE Messaging API — reply messages & replyToken](https://developers.line.biz/en/docs/messaging-api/sending-messages/#reply-messages)

## 2026-06-20 10:12

現有程式碼已讀完。整理調研結論：

---

## 本輪調研：LINE webhook event 持久化冪等表

**重點: `webhookEventId` 格式確認** — LINE 官方文件明確：`webhookEventId` 是 **ULID 格式**（26 字元大寫英數字串），每個 webhook event 唯一，**redelivery 時值不變**、只有 `isRedelivery` 旗標不同。這才是冪等 key 的正確選擇。來源：[LINE Messaging API Reference](https://developers.line.biz/en/reference/messaging-api/index.html.md)

**重點: 現況缺陷** — 目前 `isRedelivery=true` 直接 `continue`（無狀態旗標），有三個漏洞：① 首投成功但 reply 超時，LINE 重送此時 `isRedelivery=true` → 被跳過，用戶收不到翻譯；② 首投 `_process_events` 進行中（background）、LINE 又重送 `isRedelivery=false`（例如重試間隔內首投還沒完） → 重複處理；③ 無法區分「已處理成功」與「正在處理」兩種狀態。

**重點: 標準狀態機為三態** — 業界冪等表常見模式：`pending`（已登記、處理中）→ `processed`（成功）/ `failed`（失敗可選保留）。完整防護邏輯：先 INSERT event_id，INSERT 衝突 → 已存在 → 直接 skip；INSERT 成功 → 處理；處理完 UPDATE 為 `processed`。來源：[Hookdeck — Implement Webhook Idempotency](https://hookdeck.com/webhooks/guides/implement-webhook-idempotency)、[Webhook Reliability 2026](https://www.digitalapplied.com/blog/webhook-reliability-idempotency-retries-engineering-reference-2026)

**重點: SQLite + PostgreSQL 跨方言 INSERT 衝突處理** — SQLAlchemy 2.x 的 `sqlalchemy.dialects.sqlite.insert().on_conflict_do_nothing()` 是 SQLite 方言專屬；最跨方言的做法是 **catch `IntegrityError`**（`sqlalchemy.exc.IntegrityError`），SQLite/PG 都會在 UNIQUE 衝突時拋，不需 dialect 分支。另一選項是 `INSERT OR IGNORE`（SQLite）vs `INSERT … ON CONFLICT DO NOTHING`（PG）的條件路由。來源：[SQLAlchemy SQLite Dialect](https://docs.sqlalchemy.org/en/20/dialects/sqlite.html)、[SQLAlchemy Discussion #9675](https://github.com/sqlalchemy/sqlalchemy/discussions/9675)

**重點: TTL 清理** — LINE 未公開 redelivery 重試窗口期，業界建議 TTL ≥ 最長重試期 × 2，通常 **24–72 小時**。DB 需定期清理舊記錄（periodic job 或 `_migrate_*` 輕量方式在 `init_db` 掛鉤）。Redis 有原生 TTL 更省事，但現有架構純 SQLAlchemy、引入 Redis 屬 M2 事。來源：[Hookdeck Guide](https://hookdeck.com/webhooks/guides/implement-webhook-idempotency)

**重點: 現有 migration 模式** — 專案無 Alembic，用 `db.py` 的 `_migrate_*` inline 函式 + `init_db()` 呼叫；新增冪等表應沿用此模式，加 `_migrate_add_line_webhook_events_table()`，`Base.metadata.create_all` 的 import 清單也要補新 model。

**重點: 測試衝擊** — `test_line_task2_redelivery.py` 的 `_text_event()` helper 目前不帶 `webhookEventId`，改用 DB 冪等後所有測試 payload 需補此欄位；現有「`isRedelivery=true` 跳過」的四個測試案例語意要轉換為「同 `webhookEventId` 第二次到達 → skip（無論 `isRedelivery`）」。

---

**建議: 冪等表 schema 最小設計：**
```python
class LineWebhookEvent(Base):
    __tablename__ = "line_webhook_events"
    webhook_event_id = Column(String(26), primary_key=True)   # ULID
    tenant_id        = Column(Integer, ForeignKey("tenants.id"), nullable=False, index=True)
    status           = Column(String(16), nullable=False, default="pending")  # pending/processed/failed
    created_at       = Column(DateTime(timezone=True), nullable=False, ...)
    processed_at     = Column(DateTime(timezone=True), nullable=True)
```

**建議: 冪等判斷放 per-event try 最前，用 `IntegrityError` catch 跨方言：**
```python
from sqlalchemy.exc import IntegrityError
try:
    db.execute(insert(LineWebhookEvent).values(
        webhook_event_id=event.get("webhookEventId"),
        tenant_id=tenant_id, status="pending", created_at=...
    ))
    db.commit()
except IntegrityError:
    db.rollback()
    _log.info("skip duplicate event %s tenant=%d", event.get("webhookEventId"), tenant_id)
    continue
```

**建議: `isRedelivery` 旗標可完全移除**，因為 DB 唯一索引已覆蓋所有重送場景，無狀態旗標只是「近似去重」，有前述三個漏洞。

**建議: 補 `_migrate_add_line_webhook_events_table()` 到 `db.py`**，用 `inspector` 判斷表是否已存在（冪等），並在 `init_db()` 末尾呼叫，保持現有無 Alembic 模式。

**建議: 清理任務** — M1 階段可在 `init_db()` 末掛一次性 `DELETE … WHERE created_at < NOW() - INTERVAL 3 DAY`；M2 再考慮獨立 cron job。

**建議: 測試改寫策略** — 現有 `test_line_task2_redelivery.py` 的測試語意從「`isRedelivery=true` → skip」改為「同 `webhookEventId` 重投 → skip」；補一個新 case：「不同 `webhookEventId` 即使 `isRedelivery=true` 仍處理（邊界場景）」。

---

**Sources:**
- [LINE Developers — Receive messages (webhook)](https://developers.line.biz/en/docs/messaging-api/receiving-messages/)
- [LINE Messaging API Reference (webhookEventId ULID)](https://developers.line.biz/en/reference/messaging-api/index.html.md)
- [Hookdeck — Implement Webhook Idempotency](https://hookdeck.com/webhooks/guides/implement-webhook-idempotency)
- [Webhook Reliability 2026 — Idempotency & Retry Reference](https://www.digitalapplied.com/blog/webhook-reliability-idempotency-retries-engineering-reference-2026)
- [SQLAlchemy SQLite Dialect](https://docs.sqlalchemy.org/en/20/dialects/sqlite.html)
- [SQLAlchemy Discussion #9675 — INSERT OR IGNORE with ORM](https://github.com/sqlalchemy/sqlalchemy/discussions/9675)

