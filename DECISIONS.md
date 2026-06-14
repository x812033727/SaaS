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

## `DeepLTranslator.translate()` 開頭一行 `norm = self._normalize_target_lang(target_lang)`，後續 API payload 與 skip 比較**均使用 `norm`**，消除兩處各自 `.upper()` 造成的不一致風險
- 時間：2026-06-14 14:24
- 理由：工程師指出若漏改一處，繁中 400 或 skip 比較結果會靜默錯誤；單一變數是最低成本的防呆

## `_normalize_target_lang()` 為 `DeepLTranslator` 的 `@staticmethod`，白名單映射 `{"ZH-TW": "ZH-HANT", "ZH-CN": "ZH-HANS"}`，其餘 `.upper()` 不變；不污染 `Translator` ABC 介面
- 時間：2026-06-14 14:24

## 同語言 skip 判斷點維持「API 回應後讀 `detected_source_language`」；設計依據調整為「避免把同語言翻譯結果回覆給用戶（UX 正確性）」，**移除「省 DeepL 字符 quota」的主張**
- 時間：2026-06-14 14:24
- 理由：高工疑慮 1 成立——HTTP 已發出、DeepL 是否計費未確認；功能本身仍合理，只是依據需更正，避免日後誤導
- 否決方案：「skip 前不送 API 以省 quota」——需要額外語言偵測 API call，複雜度增加且不在本輪範圍

## 在 skip 比較邏輯旁加程式碼註記：「DeepL 對中文偵測回傳 `ZH`，正規化後的 target 為 `ZH-HANT`，兩者不等，繁中→繁中不觸發 skip；此為 DeepL API 行為限制，非 bug」
- 時間：2026-06-14 14:24
- 理由：工程師指出此限制若不記錄，日後排查會誤判 skip 無效

## `StubTranslator(source_lang: str | None = None)` 建構子；skip 比較用單純 `target_lang.upper() == source_lang.upper()`，**不引入 `_normalize_target_lang`**；測試碼須使用能 `.upper()` 相等的語言碼（如 `"JA"/"JA"` 或 `"ZH-HANT"/"ZH-HANT"`），不用 `ZH-TW` 對 `ZH-HANT`
- 時間：2026-06-14 14:24
- 理由：Stub 只驗 webhook 下游流程，不需複製 DeepL 特定正規化邏輯；在 PR 描述備注此侷限即可
- 否決方案：Stub 內引入正規化映射表——過度複製 DeepL 實作細節至測試替身

## Webhook handler（`async def line_webhook`）內翻譯呼叫改為 `translated = await asyncio.to_thread(translator.translate, text, lang)`；`line_client.reply()` 同為阻塞但本輪不動，加 `# NOTE: blocking — wrap in asyncio.to_thread for high-traffic (M2)` 技術債標記
- 時間：2026-06-14 14:24
- 理由：工程師確認 handler 為 `async def`，一行修改低風險；TestClient 下 thread pool 可正常運作

## 新增 `tests/test_translation_enhanced.py`，覆蓋：`_normalize_target_lang` 直接靜態呼叫（`ZH-TW→ZH-HANT`、`ZH-CN→ZH-HANS`、`JA→JA`）、`DeepLTranslator.translate()` 以 `unittest.mock.patch` 替換 urllib 模擬回應（正常翻譯與 skip 兩條路徑）、`StubTranslator` skip 行為；不改任何既有測試檔
- 時間：2026-06-14 14:24

## 所有新端點加入 `routers/tenants.py`，`app.py` 不動。
- 時間：2026-06-14 15:15

## `services/line_config.py` 三函數原封不動，router 只做組裝呼叫。
- 時間：2026-06-14 15:15

## 三端點路徑均為 `/tenants/me/line-config`，無 `{tenant_id}` path param；`tenant_id` 唯一來源是 `current_user.tenant_id`。
- 時間：2026-06-14 15:15

## 使用現有 `get_current_user` dependency，不需 `require_admin`；admin 端點原狀不動。
- 時間：2026-06-14 15:15

## 在 `routers/tenants.py` 定義 Pydantic response model `TenantLineConfigResponse`，欄位：`tenant_id / has_channel_secret / has_access_token / default_target_lang / created_at / updated_at / webhook_url`；`created_at / updated_at` 宣告為 `str | None`（service 層已 `.isoformat()` 序列化，非 datetime 物件）。
- 時間：2026-06-14 15:15
- 理由：避免 Pydantic 對 str 重複序列化造成語意混淆。

## `GET` 與 `PUT`（upsert 成功）均回傳完整 `TenantLineConfigResponse`（HTTP 200）；`DELETE` 回 **HTTP 204 No Content**。
- 時間：2026-06-14 15:15
- 理由：204 對齊 self-service 同層級慣例（`notes.py`、`api_keys.py` 皆 204），非對齊 admin 端點（admin DELETE 實際回 200 + body，兩套契約本來就允許不同）。
- 否決方案：「DELETE 回 200 + body 以對齊 admin」——admin 是後台契約；self-service 面向租戶，應與同層 REST 慣例一致。

## `webhook_url` 值為相對路徑 `f"/line/webhook/{tenant_id}"`，router 以 `{**svc_dict, "webhook_url": f"/line/webhook/{tenant_id}"}` 組裝後傳入 response model。
- 時間：2026-06-14 15:15
- 理由：host 因部署環境不同無法硬編碼，README 說明拼接方式。

## 錯誤處理全部由既有 HTTPException 直傳——`InvalidTargetLangError` 已在 service 層 L70-72 內部轉成 HTTPException(400)，router **不另加 try/except**，寫了是死代碼。
- 時間：2026-06-14 15:15
- 否決方案：「router 捕獲 InvalidTargetLangError 回 400」——service 層已處理，此寫法不可達。

## 越權測試語意落地為：租戶 A 的 token 打 `GET /tenants/me/line-config`，若 A 無設定則回 A 的 **404**（而非洩漏 B 的資料）；無法透過此端點表達「A 直接操作 B」，隔離保障由「路徑無 param、token 強綁 tenant_id」結構性保證。
- 時間：2026-06-14 15:15

## 新增 `tests/test_self_service_line_config.py`，覆蓋：CRUD happy path、`webhook_url` 欄位值、A token → GET/PUT/DELETE 回 A 的 404（非 B 的資料）、未設定時 GET 回 404。
- 時間：2026-06-14 15:15

## 本輪不改 admin 端點回應格式、不新增 secret/token 明文欄位、不實作 absolute URL 組裝（M2 backlog）。
- 時間：2026-06-14 15:15

## **`line_bot_user_id` 欄位**：`models/line_channel_config.py` 加 `line_bot_user_id = Column(String(64), nullable=True, unique=True, index=True)`；`unique=True` 由 SQLAlchemy 產生獨立 index，非 inline constraint。
- 時間：2026-06-14 15:50
- 理由：nullable 保向後相容；分離 index 利於 ALTER 拆步執行。

## **Schema 遷移（無 Alembic）**：`db.py` 的 `init_db()` 尾端加 `_migrate_add_line_bot_user_id()`；邏輯為「`sqlalchemy.inspect` 先查欄位存在性 → 缺則 `ALTER TABLE line_channel_configs ADD COLUMN line_bot_user_id VARCHAR(64)` → 再 `CREATE UNIQUE INDEX IF NOT EXISTS ix_lcfg_lbuid ON line_channel_configs(line_bot_user_id)`」；兩條 SQL 分開執行；整段 `try/except` 包住，失敗僅 `logger.warning`，不阻啟動。
- 時間：2026-06-14 15:50
- 理由：拆成 ADD COLUMN + 獨立 CREATE INDEX，規避 SQLite 老版本 ALTER 不支援 inline UNIQUE 的問題；新欄位初始全為 NULL，SQLite unique index 允許多 NULL，migration 前無重複實值疑慮，工程師已確認安全。
- 否決方案：直接 ALTER TABLE ADD COLUMN line_bot_user_id VARCHAR(64) UNIQUE —— SQLite 老版本 inline UNIQUE 支援不穩定，拆步更可移植。

## **`LineBotInfoClient` 介面用 `ABC` + `@abstractmethod`**，不用 `Protocol`；與既有 `LineReplyClient`（`ABC`）統一。
- 時間：2026-06-14 15:50
- 理由：Protocol subclass 不實作方法在 runtime 才炸；ABC 在實例化時即報錯，保護更早。
- 否決方案：Protocol —— 與既有慣例不一致，且少了 abstractmethod 的靜態保護。

## **`LineBotInfoClient` 實作放在既有 `line_client.py`**：新增 `HttpLineBotInfoClient`（stdlib `urllib`，`get_user_id(access_token) -> str | None`）與 `StubLineBotInfoClient(user_id: str | None)`（測試用）。
- 時間：2026-06-14 15:50
- 理由：不引入新模組，與 `HttpLineReplyClient` / `FakeLineReplyClient` 同檔，維持現有「client 模組集中」慣例。

## **`get_bot_info_client` FastAPI dependency**：定義於 `line_client.py`，回傳 `HttpLineBotInfoClient()`；測試透過 `app.dependency_overrides[get_bot_info_client] = lambda: StubLineBotInfoClient(...)` 注入，不用 monkeypatch。
- 時間：2026-06-14 15:50

## **upsert router 確認為 `def`（sync）**，服務內直接呼叫 `client.get_user_id()` 無需 `asyncio.to_thread()`；設計文件移除此 TODO。
- 時間：2026-06-14 15:50

## **`upsert_line_config()` 新增尾參數 `bot_info_client: LineBotInfoClient | None = None`**；在第一次 `db.commit()` + `db.refresh()` 成功後，執行下列固定模式：
- 時間：2026-06-14 15:50
- 理由：IntegrityError 後若不 rollback，SQLAlchemy session 進入不可用狀態，會污染 connection pool 中的後續請求。
- 否決方案：分開 try/except（先包 get_user_id，再單獨包 commit）—— 多一層結構複雜度，語意等價，統一包更清晰。

## **Webhook destination 二次驗證**：位置在「HMAC 驗簽通過後、quota check 前」；條件 `if cfg.line_bot_user_id and payload.get("destination") != cfg.line_bot_user_id`；失敗回 `JSONResponse(400, {"detail": _INVALID_SIGNATURE_DETAIL})`（共用同一常數）；log 訊息**不含 destination 值與 tenant_id**，僅中性描述如 `"destination mismatch, rejecting webhook"`。
- 時間：2026-06-14 15:50
- 理由：共用常數防租戶列舉；log 不帶值防 log 側資訊洩漏，為高級工程師補充審查項。
- 否決方案：自訂 detail 字串 —— 會形成可區分的 oracle，讓攻擊者能列舉租戶存在性。

## **測試案例 ①（upsert 後驗 `line_bot_user_id` 被寫入）**：不靠 API response（`_to_response()` 未暴露此欄位），改為 `PUT` 後直接用 `_Session()` 查 ORM 物件，斷言 `cfg.line_bot_user_id == expected_user_id`；寫法對齊 `TestQuotaExceeded` 的 ORM 直查模式。
- 時間：2026-06-14 15:50

## **測試覆蓋四邊界**（跨兩個測試檔擴充）：① upsert + bot/info 成功 → ORM 直查 `line_bot_user_id` 有值；② upsert + bot/info 拋例外 → upsert 仍回 200、`line_bot_user_id` 為 None、session 無髒狀態；③ webhook destination 不符（`line_bot_user_id` 已設）→ 400 且 detail 與簽章失敗字串完全相同；④ `line_bot_user_id` 為 None（舊 config）→ destination 任意值仍 200 OK。
- 時間：2026-06-14 15:50

## **本輪不改 `_to_response()` 暴露 `line_bot_user_id`**；該欄位為內部識別鍵，不需出現在租戶 API response，M2 視需求評估。
- 時間：2026-06-14 15:50

## 模組結構 — LineBotInfoClient 系列放在 line_client/ 套件，與 HttpLineReplyClient / FakeLineReplyClient 同層，維持「client 集中」慣例。
- 時間：2026-06-14 16:36

## Webhook 驗證順序固定為 raw body → DB query → 解密 → HMAC 驗簽 → JSON decode → destination 二次驗證 → quota 檢查 → 翻譯 → 回覆 → 計量。
- 時間：2026-06-14 16:36

## Quota 計費點在「翻譯+回覆皆成功後」（6d increment_usage）；失敗路徑不計量，消除白扣。
- 時間：2026-06-14 16:36

## increment_usage 在 SELECT FOR UPDATE 鎖內重驗 count < limit，消除 has_quota → increment 的 TOCTOU 超賣窗口。
- 時間：2026-06-14 16:36

## destination 二次驗證失敗與 HMAC 失敗共用同一 _INVALID_SIGNATURE_DETAIL 常數，防租戶列舉。
- 時間：2026-06-14 16:36

## HttpLineBotInfoClient.get_user_id() 讓例外向上傳播；由 upsert_line_config 的 try/except 統一吞掉並 rollback+refresh，不在 get_user_id 層 catch。
- 時間：2026-06-14 16:36
- 理由：責任分離更清晰——client 只負責呼叫，例外語意由呼叫端決定；原設計文件描述「catch→return None」有誤，以此行更正。
- 否決方案：在 get_user_id 內 catch 所有例外回傳 None——會讓 client 靜默吞掉不同類型例外（網路逾時 vs HTTP 4xx vs JSON 格式錯），呼叫端失去判斷能力。

## 技術債注釋寫在 quota.py，緊貼 require_quota 函式定義處（非 ARCHITECTURE.md，因該檔不存在）。
- 時間：2026-06-14 16:36
- 理由：讀 code 的人需要在現場看到語意差異；散落文件檔的注釋實際上不會被讀到。
- 否決方案：寫入 ARCHITECTURE.md——該檔不存在且須另建，溝通成本高；寫入 DECISIONS.md——只給決策閱讀者看，不在程式碼現場。

## 注釋內容須同時點明兩件事：① require_quota 採前扣語意（與 webhook 後扣並存）；② webhook 後扣存在「單次溢出」語意——has_quota 通過後若 increment_usage 鎖內重驗發現已達上限，該次翻譯/回覆已送出但不計量，屬設計可接受的一次性溢出，M2 統一後扣時須重新評估此邊界。
- 時間：2026-06-14 16:36
- 理由：工程師指出若不寫明，M2 接手時容易誤判為 bug 或超賣。

## reply() 同步阻塞、to_thread 包裝、line_bot_user_id 是否出 API，均標記 M2，本輪不處理。
- 時間：2026-06-14 16:36

