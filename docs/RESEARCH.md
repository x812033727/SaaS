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

