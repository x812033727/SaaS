# 架構決策記錄（ADR）

## **Key 格式** — `myapp_` + `secrets.token_urlsafe(32)`，總長約 49 字元。
- 時間：2026-06-14 00:23
- 理由：固定 prefix 方便 regex 掃描洩漏；`token_urlsafe(32)` 提供 256-bit 熵，無需額外防護。

## **key_prefix 取法** — 跳過固定 prefix，取隨機部分前 8 字元，即 `key[6:14]`，存入 `key_prefix` 欄位（加 index）。
- 時間：2026-06-14 00:23
- 理由：原「前 8 字元」含 6 字元固定 `myapp_`，隨機部分只剩 2 字元（4096 種），縮候選集效果近乎無效；取隨機部分前 8 字元（`base64url`，≈ 64^8 ≈ 2^48 種），大量 key 時仍能有效縮候選集。
- 否決方案：取整串前 8 字元（工程師與高級工程師均指出為地雷，強烈否決）。

## **Hash 方式** — `hashlib.sha256(key.encode()).hexdigest()`，無 salt，`key_hash` 加 UNIQUE index。
- 時間：2026-06-14 00:23
- 理由：256-bit 隨機 key 本身高熵，無需 salt 防彩虹表；不加 salt 才能由輸入 key 直接反推 hash 查 DB。

## **ApiKey model 欄位** — `id / user_id(FK users) / tenant_id(FK tenants, index) / name / key_prefix(8 char, index) / key_hash(64 char, unique) / is_active(bool) / created_at`。
- 時間：2026-06-14 00:23

## **ApiKeyUsage 獨立表** — `(id, api_key_id FK api_keys, tenant_id FK tenants, period Date, count)`，`UniqueConstraint("api_key_id", "period")`，`api_key_id` 非 nullable。
- 時間：2026-06-14 00:23
- 理由：SQLite 與 PostgreSQL 的 UNIQUE constraint 中 NULL ≠ NULL，若在 `ApiUsage` 加 nullable `api_key_id` 改 constraint，舊 tenant-level 列無法被唯一鍵攔住重複，`_get_or_create_usage_locked` 邏輯直接炸裂。
- 否決方案：修改 `ApiUsage` 加 nullable `api_key_id`（NULL uniqueness 無解，不可行）。

## **`ApiUsage` 維持原樣** — 欄位、constraint、`check_and_increment` 邏輯全部零改動，現有 tenant-level quota 行為完整保留。
- 時間：2026-06-14 00:23

## **period 型別** — `ApiKeyUsage.period` 與 `ApiUsage.period` 均用 SQLAlchemy `Date` 型別（`datetime.date`），Python 層統一傳 `datetime.date` 物件，避免 `/usage` 聚合兩表時跨格式轉換。
- 時間：2026-06-14 00:23

## **Actor dataclass** — `@dataclass class Actor: user: User; api_key_id: Optional[int] = None`，置於 `auth/dependencies.py`。
- 時間：2026-06-14 00:23

## **dependency 串接** — `get_current_actor` 是新核心 dependency（回傳 `Actor`）；`get_current_user` 包裝它只回傳 `actor.user`（router 零改動）；`require_quota` 改 depends `get_current_actor`，取得 `api_key_id` 後仍回傳 `User`。FastAPI 同一 request 內同一 dependency 只呼叫一次，`get_current_actor` 定義為一般 `def`（非 `async def`）確保快取行為一致。
- 時間：2026-06-14 00:23

## **認證三路 if/elif/else** — `X-API-Key` header 有值 → API key 路徑；`Authorization: Bearer myapp_*` → API key 路徑；`Authorization: Bearer eyJ*` → JWT 路徑；三者皆無或皆無效 → 401。條件必須互斥且窮舉，測試須覆蓋「三者皆無」確認不回 200。
- 時間：2026-06-14 00:23
- 否決方案：巢狀 try/except 隱式判斷（條件不互斥，易漏接）。

## **`check_and_increment_key` 失敗行為** — `ApiKeyUsage` 寫入拋例外一律向上傳播（回 500），不靜默吞掉。
- 時間：2026-06-14 00:23
- 理由：靜默失敗會造成計量偶發性漏記，形成不可見的隱性 bug；MVP 寧可明確 500 也不要無聲計量錯誤。

## **quota 責任切分** — tenant-level 上限判定在 `check_and_increment`（ApiUsage），超量回 429；per-key 只累計計數（ApiKeyUsage），不設額外上限；MVP 階段無 per-key 配額需求。
- 時間：2026-06-14 00:23

## **撤銷** — `is_active=False` 軟刪除；auth lookup 加 `ApiKey.is_active == True` 過濾，撤銷後下一次請求即失效；`ApiKeyUsage` 外鍵保留歷史。
- 時間：2026-06-14 00:23

## **跨租戶隔離** — key lookup 驗 `ApiKey.tenant_id == actor.user.tenant_id`；`/api-keys` 與 `/usage` 均只查自身 tenant 資料；跨租戶回 403。
- 時間：2026-06-14 00:23

## **建立端點回應** — `POST /api-keys` 回傳 `plain_key`（完整明文，唯一一次）+ 其餘欄位；`GET /api-keys` 只回 `key_prefix`，不含 hash 或明文。
- 時間：2026-06-14 00:23

## **/usage 回應結構** — `{ tenant: { plan, daily_limit, used_today, remaining, period }, api_keys: [{ api_key_id, name, key_prefix, used_today, period }] }`；欄位名寫入 README。
- 時間：2026-06-14 00:23

## **新 router 掛載** — `api_keys.router` → `/api-keys`；`usage.router` → `/usage`；`app.py` lifespan 的 `init_db` 補 import 兩個新 model。
- 時間：2026-06-14 00:23

## **`init_db` import 順序** — `db.py` 的 `init_db` 明確列出所有 model import（`api_key`, `api_key_usage`），`create_all()` 前必須 import，順序：被依賴的 model（`ApiKey`）先於依賴它的（`ApiKeyUsage`）。
- 時間：2026-06-14 00:23

## **不引入新外部依賴** — 僅用 `hashlib`、`secrets`（stdlib）與 FastAPI 內建 `fastapi.security.APIKeyHeader`；`OAuth2PasswordBearer` 改 `auto_error=False`。
- 時間：2026-06-14 00:23

## **測試策略** — `conftest.py` 新增 `test_api_key` fixture（含明文 key）；覆蓋：key 建立/撤銷、`X-API-Key` + Bearer 雙標頭、用量累加可手算核對、429 超量、跨租戶 403、「三者皆無→401」；既有測試集不動；全部離線 in-memory SQLite。
- 時間：2026-06-14 00:23

## `PlanChangeHistory` model 欄位：`(id PK, tenant_id FK non-null, from_plan, to_plan, changed_by_user_id FK nullable, changed_at UTC non-null, reason nullable)`。
- 時間：2026-06-14 02:51
- 理由：`changed_by_user_id` 允許 NULL 以應對 API key 認證（見下）。

## actor 為 API key 認證時，`PlanChangeHistory.changed_by_user_id` 填入 `actor.user.id`（API key 所屬 User），不填 NULL。
- 時間：2026-06-14 02:51
- 理由：API key 對應的 `Actor.user` 已在 `get_current_actor` 解析完畢，可直接取 user.id；避免歷程出現無法追溯的空值。
- 否決方案：填 NULL ── 日後 admin 查歷程無法追溯是誰透過 API key 觸發了降級。

## `User.is_admin = Column(Boolean, default=False, nullable=False)`；`Tenant.is_active = Column(Boolean, default=True, nullable=False)`。
- 時間：2026-06-14 02:51

## 停用租戶（`is_active=False`）不建立 audit trail，屬已知技術債，在程式碼加 `# TODO: add TenantAuditLog if compliance needed`。
- 時間：2026-06-14 02:51
- 理由：現階段無 compliance 需求，不過度設計；高級工程師意見已知悉，決策有意識地暫緩。

## `init_db` import 順序：`Tenant` → `User` → `ApiKey` → `ApiKeyUsage` → `ApiUsage` → `PlanChangeHistory`。
- 時間：2026-06-14 02:51

## 降級超量檢查移入 transaction 內，使用 `SELECT ... FOR UPDATE`（`query.with_for_update()`）鎖住 `ApiUsage` row 後再讀 count；若 `count > PLAN_DAILY_LIMITS[new_plan]` 則 rollback 並回 409，否則同一 transaction 寫 `Tenant.plan` 與 `PlanChangeHistory`。
- 時間：2026-06-14 02:51
- 理由：消除 TOCTOU race window；單 worker 鎖無效能負擔，多 worker 亦可正確序列化。
- 否決方案：transaction 外先查再寫 ── 並發降級請求可同時通過檢查，工程師與高級工程師均已指出此缺陷。

## `services/billing.py` 的 `downgrade_plan(db, tenant, new_plan, actor_user_id)` 在同一 db session 內完成：`with_for_update()` 讀 usage → 超量 raise 409 ValueError → 否則更新 plan + insert history → commit。router 捕捉 ValueError 轉成 HTTP 409。
- 時間：2026-06-14 02:51

## 模擬 payment_id 格式：`"simulated_" + secrets.token_hex(6)`（12 hex chars），完全 stdlib，無外部請求。
- 時間：2026-06-14 02:51

## `get_current_actor` 查 User 時改用 `joinedload(User.tenant)`，一次 SQL 取齊 user + tenant，避免 lazy chain `actor.user.tenant.is_active` 觸發 N+1 或 `DetachedInstanceError`。
- 時間：2026-06-14 02:51
- 理由：高級工程師與工程師均指出此風險；eager load 無額外 round-trip，成本低。
- 否決方案：依賴 SQLAlchemy lazy load ── session 若已關閉直接拋例外，且每請求多一次 DB query。

## 租戶停用攔截點：`get_current_actor` 組裝 `Actor` 前，若 `user.tenant.is_active == False` 則 `raise HTTPException(status_code=403, detail="tenant disabled")`；admin 使用者不豁免（停用即全停）。
- 時間：2026-06-14 02:51

## `SlidingWindowRateLimiter.__init__` 新增 `clock: Callable[[], float] = time.monotonic`，存 `self._clock`，內部所有 `time.monotonic()` 改為 `self._clock()`；既有 instance 不傳 clock，預設行為不變。
- 時間：2026-06-14 02:51

## Per-key dict 不加 LRU，但在 `_log` dict 初始化處加註 `# TODO: replace with LRU(maxsize=10_000) before multi-tenant scale` 明確記錄技術債。
- 時間：2026-06-14 02:51
- 理由：當前規模無問題；加 LRU 引入額外依賴不值得現在做。

## Rate limiter 在 README 與部署說明區段明確標注「**單 worker 限定**：in-memory dict 不跨 process 共享，多 worker 需改 Redis 後端」。
- 時間：2026-06-14 02:51

## `require_rate_limit` 依賴判斷優先順序：`actor.api_key_id` 不為 None → per-key limiter；否則 → per-tenant limiter（key 為 `tenant_id`）；兩個 limiter 為 module-level singleton，env var `SAAS_KEY_RATE_LIMIT` / `SAAS_TENANT_RATE_LIMIT` 解析格式 `"{calls}/{window_seconds}"`。
- 時間：2026-06-14 02:51

## 429 response 帶 `Retry-After: {window_seconds}` header（純整數），透過 `HTTPException(headers={"Retry-After": str(window)})` 回傳；不帶 `X-RateLimit-*` 系列 header（非本次需求範圍）。
- 時間：2026-06-14 02:51

## `require_admin` 為獨立 dependency（`deps.py` 匯出），`Depends(get_current_actor)` 後檢查 `actor.user.is_admin`；非 admin 回 `403 Forbidden`，不回 401。
- 時間：2026-06-14 02:51

## `routers/admin.py` 整個 `APIRouter` 掛 `dependencies=[Depends(require_admin)]`，所有子路由自動繼承，不需逐一宣告。
- 時間：2026-06-14 02:51

## `PATCH /admin/tenants/{id}` body `{is_active?: bool, plan?: str}`；改方案時複用 `billing.upgrade_plan` / `billing.downgrade_plan`（不重複邏輯）；停用/啟用僅寫 `Tenant.is_active`，不呼叫 billing service。
- 時間：2026-06-14 02:51

## 新增三個測試檔：`tests/test_billing.py`、`tests/test_ratelimit_enhanced.py`、`tests/test_admin.py`；既有測試檔不改。
- 時間：2026-06-14 02:51

## `FakeClock` 實作為 `conftest.py` 中的 class，`__call__` 回傳 `self.t`，`.advance(n)` 遞增；測試傳入 `SlidingWindowRateLimiter(max_calls=N, window_seconds=W, clock=fake_clock)`，無任何 `time.sleep`。
- 時間：2026-06-14 02:51

## `conftest.py` 新增 `admin_user` fixture（`is_admin=True`）與 `admin_token` fixture；`test_api_key`、`test_tenant` 等既有 fixture 不動。
- 時間：2026-06-14 02:51

