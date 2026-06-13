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

