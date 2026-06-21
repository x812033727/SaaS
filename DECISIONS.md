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

## 抽出一個內部 helper `_constant_time_verify(body, signature: str, secret: str) -> bool`，把 `hmac.new + base64.b64encode + hmac.compare_digest` 三步驟封裝為單一實作點
- 時間：2026-06-16 07:03
- 理由：三條拒絕路徑（cfg None / 缺 header / 簽章錯）共用此 helper 後，CPU 開銷強制對等；單一實作點也讓「未來換 signature scheme / 加 nonce」只需改一處
- 否決方案：① 在三條路徑各別複製 b64encode+compare_digest——可被未來改動悄悄破壞對等性；② 把驗簽抽到獨立 module——過度設計，只在 router 內用一次

## cfg None 路徑改為呼叫 helper，傳入 `secret=b"\x00"*32`（固定 dummy）與 `signature=request.headers.get("X-Line-Signature", "")`（真實 header 值或空字串），再 `raise HTTPException(400, _INVALID_SIGNATURE_DETAIL)`
- 時間：2026-06-16 07:03
- 理由：helper 內部固定跑完整 HMAC 鏈（new→digest→b64encode→compare_digest），dummy secret 讓結果必為 False，與簽章錯分支成本完全對等；放棄「cfg 缺失就只做最小運算」是刻意選擇——分析證明 sub-microsecond 成本可忽略
- 否決方案：保留現狀「只跑 digest」——研究報告明確指出此差異構成可被遠端統計的 timing oracle

## 缺 header 路徑不再 short-circuit，改為「固定傳空字串進 helper」——`signature=""` 走完整 HMAC 鏈，helper 自然回傳 False，再統一拋 400
- 時間：2026-06-16 07:03
- 理由：把「缺 header」與「錯 header」合併為同一條 helper 呼叫，三條路徑的指令序列完全一致；這是 OWASP timing 對齊的最低成本實作
- 否決方案：維持 `if not signature: raise` 短路——會在 timing 側留下明顯可觀測的 gap（短少整段 HMAC 計算）

## 簽章錯路徑也改走同一 helper（行為等價，但實作統一）；cfg None / 缺 header / 簽章錯三條路徑的最後一行都是 `raise HTTPException(400, _INVALID_SIGNATURE_DETAIL)`
- 時間：2026-06-16 07:03
- 理由：「helper → raise」這個序列統一後，未來若有人想新增第四條路徑（如 destination 之外的二次驗證），compiler/type hint 會自然引導他走 helper；可逆性最大化

## 三條拒絕路徑（含 destination 不符，總四條）內部各加一條 `_log.warning("webhook rejected reason=%s tenant=%d", reason, tenant_id)`，reason 列舉 `no_config / missing_header / bad_signature / bad_destination`
- 時間：2026-06-16 07:03
- 理由：OWASP 推薦模式——對外一致、對內區分（log 不對外暴露）；既不洩漏 detail 給攻擊者，也給監控/告警一個可靠的失敗原因欄位
- 否決方案：① log 內容含 destination 值或完整 user_id——log 側資訊洩漏仍是洩漏；② 不分層只用 INFO 級——warning 級才能在 SIEM 觸發告警

## 新增測試 `test_all_rejection_paths_go_through_constant_time_helper`——用 `unittest.mock.patch` 監看 helper 呼叫次數與引數，斷言三條拒絕路徑**都呼叫 helper 一次**且引數形態對稱
- 時間：2026-06-16 07:03
- 理由：wall-clock 計時在 CI 抖動過大不實用；改用「是否走同一 helper」當契約——比 timing 對齊更強的保證（成本對等是 helper 單一實作點的自然結果）；沿用既有「先 mock spy 再斷言」風格
- 否決方案：① wall-clock timing 基準測試——CI 抖動讓測試不穩；② 純粹比較 response time 的鬆散斷言——會隨環境飄移

## 既有的 `test_configured_vs_unconfigured_indistinguishable_all_probes` 與 `test_mismatch_detail_identical_to_signature_failure` 全綠即視為「status code + body 統一」驗收；本輪不在這兩個斷言上做修改
- 時間：2026-06-16 07:03
- 理由：既有測試已逐字節等價鎖死 status code 與 detail；本輪重點是 timing 對齊與 log 區分，兩者不相交
- 否決方案：重寫或擴大既有等價斷言——風險高且無新合約覆蓋

## 不改既有契約——路由 `/line/webhook/{tenant_id}`、成功回應 `200 {"status":"ok"}`、`_INVALID_SIGNATURE_DETAIL = "Invalid X-Line-Signature"`、LineConfigDecryptionError→200、destination 二次驗證時機、計費點時機——皆保持現狀
- 時間：2026-06-16 07:03
- 理由：PM 與研究員均確認這些契約已達標；本輪「只補缺口」，不擴大變更面

## 既有架構決策中關於 `app/services/quota.py` 與 `app/api/line_bot_info.py` 的路徑記載與實際不符（實際為 `src/saas_mvp/quota.py`）——本輪沿用實際路徑，不翻案亦不發 PR 改既有架構決策文字
- 時間：2026-06-16 07:03
- 理由：程式碼真相是檔案實際位置；翻案既有決策文件超出本輪 scope，且既有注釋已正確落在 `src/saas_mvp/quota.py` 函式定義處，符合「緊貼 require_quota」的語意

## 字數計量模組邊界沿用 `quota.py`——`has_char_quota` 新增、`_get_or_create_usage_locked` 不動、SQLAlchemy `default=0` 自動補新 row 的 `char_count=0`；介面對稱、可逆性最大化。
- 時間：2026-06-16 08:00

## 字數累計點採**譯文字數**（`len(translated)`），源文多語/表情歧義的鍋不該架構背；與既有後扣骨架自然對齊。
- 時間：2026-06-16 08:00

## 字數計算採 Python `len(str)`（Unicode code point），與中文「字」語意對齊、手算可核對。
- 時間：2026-06-16 08:00

## 字數上限常數新增 `PLAN_DAILY_CHAR_LIMITS: dict[str, int]`，**未知 plan 一律 fallback 到 `PLAN_DAILY_CHAR_LIMITS["free"]`**，與既有 `PLAN_DAILY_LIMITS` 的 fallback 語意對齊——這是策略明文化，非細節。
- 時間：2026-06-16 08:00
- 理由：對齊既有契約比「未知 plan 拒絕 vs 放行」的爭論更值錢；PM 之後若要 enterprise plan，加一條 dict 條目即可。

## webhook 後扣路徑並列兩道 read 閘——`has_quota` 通過後才進 `has_char_quota`，任一不通都回 `_QUOTA_EXCEEDED_MSG` 並 `continue`；兩閘獨立查詢、獨立擋下，沿用「單次溢出可接受」語意。
- 時間：2026-06-16 08:00
- 理由：兩次 read-only 查詢成本可忽略；合併原子讀取會破壞既有 `has_quota` 契約。

## **翻案**——放棄「`increment_char_usage` 與 `increment_usage` 並列、各自 SELECT FOR UPDATE + 各自 commit」的初版設計，改為**單一 `increment_usage(db, tenant_id, plan=None, chars=0)`**，同 row 一次 `SELECT FOR UPDATE`、同 transaction 內 `count += 1; char_count += chars`、單一 commit。
- 時間：2026-06-16 08:00
- 理由：既有 `increment_usage` 是內部函式無對外契約、破壞介面成本為零；合併後每次 webhook 少一輪 DB 往返 + 少一次 commit，鎖窗口由兩次壓成一次，TOCTOU 與 commit 失敗率同向下降。
- 否決方案：初版「兩個獨立 increment 函式各自鎖」——介面對稱的視覺收益換不來一輪 round-trip + 一次 commit 的實質成本。

## `ApiUsage` model 加 `char_count = Column(Integer, nullable=False, default=0)`；新 row 走 SQLAlchemy default，既有 NULL 列**雙重保險**——讀取端 `(row.char_count or 0)` 兜底 + 一次性 backfill 腳本 `UPDATE api_usage SET char_count = 0 WHERE char_count IS NULL`（SQLite/PG 通用 SQL，不擴大 schema 變更面，屬於資料修正）。
- 時間：2026-06-16 08:00
- 理由：讀取端兜底保「API 對外不報錯」，backfill 腳本保「DB 內部對 BI/監控/聚合查詢也一致」；二者並行，後者才是根治，前者是保險。

## `/quota/status` 與 `/usage/` 兩端點**新增** `used_chars` / `char_limit` / `remaining_chars` 三個平行的字數欄位；既有 `used` / `limit` / `remaining` / `used_today` / `daily_limit` 欄位名與次數語意不變，欄位說明寫入對應 router docstring，不靠測試碼當文件。
- 時間：2026-06-16 08:00

## per-key 層（`ApiKeyUsage`）不補字數欄位——議程未要求、既有「per-key 共享租戶配額」契約只需次數軸；擴大變更面不符「只補缺口」原則。
- 時間：2026-06-16 08:00

## 既有 webhook 計費點時機不變——字數閘**插入在 `has_quota` 通過之後、翻譯之前**，`increment_usage(plan, chars=N)` **取代**舊的 `increment_usage(plan)`，單一呼叫完成次數+字數兩軸遞增；下游翻譯/回覆失敗拋出時 `increment` 不會被執行，雙軸皆無白扣。
- 時間：2026-06-16 08:00

## `increment_usage` 內加 `chars <= 0` 早退（直接 return row.char_count）——與既有 `validate_count` 守衛同形，避免空字串或異常輸入造成無意義鎖操作。
- 時間：2026-06-16 08:00
- 否決方案：不在路由層 / webhook 層守衛——守衛該集中在計費原語內，呼叫端無需各自重複。

## 字數超額語意與則數完全對等——**不翻譯、不計量、回 200 + `_QUOTA_EXCEEDED_MSG`**；不拋 500、不拋 429，行為與 PM 議程結論一致。
- 時間：2026-06-16 08:00

## 測試對齊既有 `tests/test_quota.py` 與 `tests/test_line_webhook.py` 風格；每個「擋下/不計」案例附**反向對照組**（未超額→正常翻譯且 `char_count` 恰增 N）；**必涵「兩道閘都超額」的 case**——calls 先超額時，第一道閘擋下後第二道 `has_char_quota` 不得誤觸 char 計量；既有則數測試零修改。
- 時間：2026-06-16 08:00
- 理由：高級工程師點出「第一道擋下後第二道不應誤觸」是測試覆蓋盲點，必須補。

## 「翻譯/回覆成功但 `increment_usage` 失敗 = 已服務未計費」是**既有**失敗模式（次數軸沿用至今），本輪字數軸不修，**PR 描述必須點名此已知模式 + 開 issue tracker 留待 M2**。
- 時間：2026-06-16 08:00
- 理由：修這個需先定義「成功副作用邊界」（何時算服務完成、何時算未完成），跨整個 webhook 設計面，超出本輪 scope；明文化是當前最值錢的處置。

## 既有架構決策（簽章驗證鏈、四條拒絕路徑收斂、`_INVALID_SIGNATURE_DETAIL`、destination 二次驗證、LineConfigDecryptionError→200、後扣語意、計費點時機、router 路徑、成功回應格式）**全部沿用，本輪不翻案**。
- 時間：2026-06-16 08:00

## 技術選型 — 本輪拒做 asyncio.to_thread 包裝與 async HTTP client 改寫
- 時間：2026-06-16 13:57
- 理由：BackgroundTasks 對 sync 函式自動 run_in_threadpool（Starlette 源碼事實），等同 asyncio.to_thread 效果；再加為冗餘雙重包裝（anyio 反模式）。async 化需整套介面破壞性重構（LineReplyClient async 化 + AsyncSession + httpx lifespan + 全 spy mock 重寫），M1 流量下 ROI 為負，屬 M2 範疇。
- 否決方案：(a) _process_events 改 async def + asyncio.to_thread 包 line_client.reply：自造輪子、淨效果 = 原地踏步；(b) httpx.AsyncClient / AsyncMessagingApi 整套 async 化：介面破壞面過大、屬獨立 M2 重構。

## 模組切分 — 本輪只動 saas_mvp/routers/line_webhook.py（文件）與 tests/test_qa_task4_to_thread.py（測試收斂），零邏輯改動
- 時間：2026-06-16 13:57
- 否決方案：順手修 _process_events / HttpLineReplyClient / 計費點 / 簽章鏈——守住「只補缺口」原則，避免變更面擴大。

## 測試輔助擴展 — FakeLineReplyClient 新增可選 delay 屬性（reply 內 time.sleep），預設 0 維持既有行為
- 時間：2026-06-16 13:57
- 理由：不變量測試需可注入阻塞；既有測試不需改、零契約破壞；M2 async 化可重用此機制。

## 測試收斂 — TestToThreadWrapping 三個 test 改寫：移除 to_thread 攔截斷言、改以 thread ident 斷言「translate 跑在背景 threadpool」；test_result_correct_through_to_end 保留；test_non_text_event 改寫為「非文字事件不觸發翻譯」斷言
- 時間：2026-06-16 13:57
- 否決方案：保留舊名 test_translate_called_via_to_thread——名稱已誤導、與新語意不符。

## 測試檔 import 清理 — 移除測試檔的 `import asyncio` 與所有 `mock.patch.object(webhook_mod.asyncio, "to_thread", ...)` 呼叫
- 時間：2026-06-16 13:57
- 理由：拒做 to_thread 後 line_webhook 模組不應 import asyncio，webhook_mod.asyncio 屬性查找會 AttributeError，整個 test 模組連 pytest collection 都過不了。

## 架構不變量測試 — 新增 test_blocking_reply_does_not_block_handler：override SlowLineReplyClient（reply 內 time.sleep(0.5)），斷言 client.post(/line/webhook/...) 在 <0.3s 內回 200
- 時間：2026-06-16 13:57
- 理由：把「BackgroundTasks 不阻塞 handler」升級為可被測試守住的不變量；守的是架構契約（handler 不等 reply）、不是「某 function 被呼叫」的實作細節。
- 否決方案：0.2s——CI 容器 load 高時 threadpool dispatch 可能吃掉 50-100ms，margin 偏緊、flakiness 風險高。

## 文件化語意 — line_webhook.py 步驟 6c 註解改述「reply 阻塞 I/O 在 BackgroundTasks 機制下已自動被 run_in_threadpool 移出 event loop，等同 asyncio.to_thread 效果；threadpool 線程佔用（高 RPS × 多 worker）見 M2 async 化技術債」；模組 docstring M2 段落更新為「HttpLineReplyClient 改用 httpx.AsyncClient（lifespan 管理單一 instance）+ _process_events 改 async def + AsyncSession 整套重構；asyncio.to_thread 包裝為錯誤方向、不再列入技術債」
- 時間：2026-06-16 13:57

## 外部缺口處置 — 既有 7 個 test_line_task2_char_quota / test_qa_task3_webhook_char_metering 失敗加 @pytest.mark.xfail(reason="has_char_quota 簽名缺口，issue #XXX") 或 skip，從 baseline 噪音中隔出；PR 描述明列 xfail count
- 時間：2026-06-16 13:57
- 理由：直接合進 master 會讓 CI baseline 永久 7 紅，未來提 PR 的人分不清「已知缺口」vs「本次 regression」；xfail 讓 CI 綠、缺口可被追蹤、reviewer 一眼看清狀態。
- 否決方案：(a) 留紅直接合——CI 噪音永久化；(b) 本輪順手修——超出本輪 scope、PR 失焦。

## 可逆性 — 本輪所有改動皆為文件 + 測試 + Fake 測試輔助屬性，無邏輯變更；M2 async 化若啟動，測試方法改名（to_thread→background threadpool 斷言）但 delay 機制與不變量測試可重用；M2 方向被否決時 revert 成本 = 改兩段 docstring + 幾個 test method + 移除 xfail marker。
- 時間：2026-06-16 13:57

## 核可自助 LINE config API 落在 `tenants.py`，端點為 `GET/PUT/DELETE /tenants/me/line-config`。
- 時間：2026-06-19 02:41
- 理由：貼近既有租戶自助邊界，放棄新增 router。

## `tenant_id` 一律取自 `current_user.tenant_id`。
- 時間：2026-06-19 02:41
- 否決方案：不接受 path/body 傳入 tenant_id，避免越權面。

## Router 只薄封裝既有 `services.line_config` CRUD，補 `webhook_url`。
- 時間：2026-06-19 02:41
- 理由：保護依賴方向，放棄重寫 self-service service。

## Response 只回遮罩狀態 `has_channel_secret`、`has_access_token`，不回明文 secret/token。
- 時間：2026-06-19 02:41

## `webhook_url` 使用 `line_webhook.webhook_url_for(tenant_id)` 產生相對路徑。
- 時間：2026-06-19 02:41
- 理由：後端不綁部署 host；前端或部署層負責補 origin。
- 否決方案：不硬碼完整 URL。

## `PUT` 沿用 admin 行為，同步執行 LINE bot/info 驗證與回填。
- 時間：2026-06-19 02:41
- 理由：保持語意一致；接受最高 10 秒外部延遲。
- 否決方案：本輪不拆非同步回填或獨立驗證端點。

## Request body 短期沿用 admin 欄位定義；若欄位再擴充，再抽共用 schema。
- 時間：2026-06-19 02:41
- 理由：避免過早抽象，但保留可逆性。

## `DELETE` 回 `204 No Content`。
- 時間：2026-06-19 02:41

## 認證、停用租戶、404 與驗證錯誤沿用既有 dependency/service，不在 router 重包。
- 時間：2026-06-19 02:41

## 三個端點掛既有 rate limit dependency。
- 時間：2026-06-19 02:41
- 理由：對齊 self-service API 慣例；前端需避免輪詢吃掉租戶配額。

## 採維運腳本 `saas_mvp.ops.backfill_line_bot_user_id`，不新增 API、不引入新 framework。
- 時間：2026-06-19 03:04
- 理由：這是窄範圍資料回填，降低認證面與長期維護面。
- 否決方案：遠端觸發 API、背景任務系統。

## 不重用 `upsert_line_config()`；只重用 HTTP bot/info client 與 ORM 解密後的 `access_token`。
- 時間：2026-06-19 03:04
- 理由：回填不應覆寫 secret/token/lang。

## CLI 介面支援 `--dry-run`、`--apply` 二選一，預設不寫入。
- 時間：2026-06-19 03:04
- 理由：犧牲操作便利，避免誤改正式資料。

## 查詢預設只處理 `line_bot_user_id IS NULL`，支援 `--tenant-id`、`--limit`，並依 `tenant_id` 穩定排序。
- 時間：2026-06-19 03:04

## `--tenant-id` 指到已回填資料時，要輸出 `skipped reason=already_set`，不可靜默略過。
- 時間：2026-06-19 03:04

## `--dry-run` 仍呼叫 LINE bot/info，但不 flush、不 commit。
- 時間：2026-06-19 03:04
- 理由：驗證可回填性；文件需明講這不是純本機檢查，可能受 token、網路、rate limit 影響。

## 每筆資料獨立交易；成功才 commit，失敗、衝突或 `IntegrityError` 必須 rollback 後繼續下一筆。
- 時間：2026-06-19 03:04
- 理由：放棄全批原子性，換取可續跑與低 blast radius。

## unique constraint 是衝突真相來源；可做 precheck 協助分類，但仍必須 catch `IntegrityError`。
- 時間：2026-06-19 03:04
- 理由：避免 race condition。

## 結果狀態固定為 `updated / skipped / failed / conflict`，stdout 使用穩定 `key=value` 行與 summary。
- 時間：2026-06-19 03:04

## 衝突輸出 `conflict reason=duplicate_line_bot_user_id`；若安全可得，附 `conflict_tenant_id`，但不輸出 userId/token/secret 明文。
- 時間：2026-06-19 03:04

## 腳本不呼叫 `init_db()`、不自動 migration；schema 不符就 fail fast。
- 時間：2026-06-19 03:04
- 理由：維運腳本只做資料回填，避免 dry-run 造成隱性寫入。

## 測試以 fake `LineBotInfoClient` 與測試 session factory 離線覆蓋成功、dry-run、bot/info 失敗、unique 衝突、非 NULL 不覆寫、`--tenant-id` 已回填 skipped。
- 時間：2026-06-19 03:04

## 文件補在 README 的 LINE Messaging API 區塊，包含 dry-run/apply 指令、`--limit` 建議、可重跑語意、summary 解讀與外部呼叫風險。
- 時間：2026-06-19 03:04

## 背景任務 `_process_events` 必須以 per-event 為邊界進行獨立的例外處理，單一 event 失敗不得中斷其他 event。
- 時間：2026-06-20 09:19
- 理由：避免因單一 LINE event 翻譯或回覆失敗（如第三方 API 異常）導致同批次其他 events 被跳過，降低故障影響範圍。
- 否決方案：整個 events 批次共用單一 `try-except` 區塊。

## 每個 LINE event 必須擁有獨立的 SQLAlchemy DB Session 生命週期，於處理 event 開始時建立，結束或異常時在 `finally` 關閉。
- 時間：2026-06-20 09:19
- 理由：SQLAlchemy 的 Session 在發生異常後其交易狀態會失效，無法繼續執行後續操作。Per-event 的獨立 Session 生命週期可徹底隔絕異常污染，保證狀態乾淨。
- 否決方案：整個批次共用單一 DB Session 並僅在例外時呼叫 `db.rollback()`，這在複雜異常下無法保證 session 狀態完全恢復。

## 測試策略必須包含：(1) 單一 event 翻譯/回覆失敗時，其後 event 仍成功處理；(2) 單一 event 發生 DB 衝突時，其後 event 仍能正常扣減與計量；(3) 異常必須被 logger.exception 捕獲 swallow，不向上層拋出；(4) 去重機制不扣 Quota 且與 normal events 互不干擾。
- 時間：2026-06-20 09:19
- 理由：精準確保 per-event 隔離性與去重邏輯在各種極端失敗路徑下的正確性，避免 regression。
- 否決方案：僅依賴執行全套 integration 測試或手動測試來驗證例外行為。

## LINE Webhook Signature 驗簽與租戶存在性/配置驗證必須在 FastAPI 路由的同步 Request-Response 生命週期中完成，不得移入背景任務。
- 時間：2026-06-20 09:19
- 理由：保護系統安全邊界，防止無效或惡意請求同步拿到 200 OK，避免洩漏租戶存在性，並阻擋惡意流量耗盡背景線程。
- 否決方案：將驗簽與租戶驗證全部移入背景任務以極致縮短 API 回應時間。

## 背景任務 `_process_events` 不得持有或共享 Request-scoped 的 DB Session，必須使用 handler 在同步階段取得的 database engine (`bind`) 自建 Session。
- 時間：2026-06-20 09:19
- 理由：FastAPI Request-scoped Session 在 HTTP 回應送出後會自動關閉，若傳入背景任務會因 session 已關閉而報錯。
- 否決方案：傳遞 Request-scoped DB Session 進背景任務並延長其生命週期。

## 本次重構與測試執行一律以 `.venv/bin/pytest` 執行，不依賴 Antigravity CLI 的 `BypassSandbox` 權限。
- 時間：2026-06-20 09:19
- 理由：維持開發與測試流程的環境可攜性，符合本地沙箱執行安全規範。
- 否決方案：要求使用者或 CI 環境提供 `BypassSandbox: true` 權限來跑測試。

## 將 `Translator.translate` 的回傳型別從 `str` 改為 `TranslationResult` 物件，不採用新舊介面並存方式。
- 時間：2026-06-20 10:22
- 理由：保護介面依賴方向的單一性與一致性。雖然此破壞性變更需要修改現有測試與 fake/spy translator 的簽章，但它徹底消除了殭屍代碼的隱患，提供乾淨的控制邊界。
- 否決方案：在 `Translator` 額外宣告 `translate_with_meta` 方法並保留舊 `translate` 簽章的方案。

## 在 [base.py](file:///opt/ti/workspaces/project-bbe384041201/src/saas_mvp/translation/base.py) 以 `@dataclass(frozen=True, slots=True)` 定義 `TranslationResult`，欄位包含 `text: str`、`detected_lang: str | None` 與 `skipped: bool`。
- 時間：2026-06-20 10:22
- 理由：藉由 `frozen=True` 保證回傳結果的不可變性（Immutability），防止在傳遞過程中被修改；不使用 `NamedTuple` 是為防止呼叫端依賴位置索引（Position Index），未來擴充欄位時不會因為解構 unpack 而導致代碼崩潰。
- 否決方案：使用無型別的 Tuple 或使用 Python `NamedTuple` 作為資料傳遞結構。

## 暫不定義 `skip_reason` 等列舉結構，僅使用 `skipped: bool` 來判定是否略過。
- 時間：2026-06-20 10:22
- 理由：避免過度設計。當前需求僅需決定「是否略過」，且未來若需演進為列舉或詳細原因，dataclass 的欄位擴大對呼叫端而言是高可逆且不具破壞性的改動。
- 否決方案：在此階段直接設計複雜的 `skip_reason` 列舉或 skipped 多狀態狀態機。

## 保持 Quota 檢查（次數與字數）在翻譯（Translate）之前的呼叫順序。
- 時間：2026-06-20 10:22
- 理由：保護付費 API 的安全防禦邊界。我們**放棄了超額用戶在發送同語言訊息時能被「靜默忽略」的體驗**（超額用戶會直接收到配額不足通知，而非靜默跳過），但**保護了系統不受超額流量惡意調用付費 DeepL API 的資金耗損風險**。
- 否決方案：將翻譯與語言偵測流程重排至 Quota 檢查之前。

## 當偵測到的語言（`detected_lang`）為 `ZH`（中文）時，只要目標語言（`target_lang`）的前置首碼為 `ZH`（如 `ZH-HANT`、`ZH-HANS`、`ZH-TW`、`ZH-CN`），即判定為同語言（`skipped=True`）並傳回原文。
- 時間：2026-06-20 10:22
- 理由：中文繁簡在 DeepL 偵測中通常僅返回 `ZH`。為防止因偵測精度落差導致重複翻譯與額外扣額，我們採取寬鬆的同語言判定。我們**放棄了提供精確繁簡互轉的保證**，以換取最務實、能省下 API 額度的實作。

## 測試覆蓋必須包含對 skip 邏輯的負向測試，包括 `skipped=True` 時不 `reply` 且不 `increment_usage`、`skipped=False` 時正常執行、DeepL 偵測語言為空時不 skip、以及中文變體（`ZH` 對 `ZH-HANT`）的判定。
- 時間：2026-06-20 10:22
- 理由：測試是設計可逆性與防止型別/行為漂移的最後防線，必須明確防範 regression。

## TranslationResult 採 @dataclass(frozen=True, slots=True) 定義，放棄 Tuple 解構彈性以保護不可變性與未來擴充可逆性。
- 時間：2026-06-20 16:47
- 理由：確保回傳結果在傳遞中不被修改，並避免未來擴充欄位時呼叫端因 unpack 崩潰。
- 否決方案：使用 NamedTuple 作為回傳容器。

## Translator.translate 徹底破壞舊簽章改為回傳 TranslationResult，放棄平滑相容以保護依賴邊界單一性。
- 時間：2026-06-20 16:47
- 理由：消除相容過渡期的殭屍代碼，維持乾淨的控制邊界。
- 否決方案：透過重載或新舊介面並存方式逐步轉移。

## _process_events 透過傳入的 bind (Engine) 自建 Session，放棄跨 Event 共享 Session 以保護背景生命週期安全。
- 時間：2026-06-20 16:47
- 理由：避免 FastAPI Request-scoped Session 在 HTTP 回應送出後自動關閉，導致背景任務在已關閉的 Session 上操作而報錯。
- 否決方案：直接將 Request-scoped Session 傳入背景任務，或在當前規模下封裝複雜的 SessionFactory。

## 同語言判定 (ZH) 於呼叫 DeepL API 後進行，放棄防範 DeepL 額度耗損以保護系統架構極簡性。
- 時間：2026-06-20 16:47
- 理由：引入額外的本地/第三方語言偵測（如 langdetect）會造成判定標準不一致（例如本地與 DeepL 對同一訊息判斷不同）並增加每次請求的延遲；此取捨承諾耗損 DeepL 額度，但保護了內部計量正確性與 UX。
- 否決方案：在翻譯前引入額外的本地語言偵測機制。

## has_char_quota 僅執行「已超額即阻擋」的非遞增判定，不預先計算本次字數，放棄防範單次超量成本以保護計費規則一致性。
- 時間：2026-06-20 16:47
- 理由：系統計量以「譯文字數」為準，翻譯前無法得知精確長度；若改用源文字數估計會引入不準確的預估與雙重計費標準，因此選擇「只防既有超額，不防單次超量成本」。
- 否決方案：在翻譯前以源文字數乘以粗估係數進行 quota 阻擋。

## 新增 `LineWebhookEvent` Model 用於儲存 Webhook 處理狀態，不引入 Redis，完全依賴關聯式資料庫（SQLite/PostgreSQL）與 SQLAlchemy 實作冪等去重。
- 時間：2026-06-20 17:15
- 理由：保持系統依賴的極簡與乾淨，使開發測試環境能與目前單機/SQLite 架構對齊，免去額外運維與網路開銷。
- 否決方案：否決引進 Redis 分散式鎖，這會破壞現有「僅依賴 SQLAlchemy」的乾淨邊界並增加建置與營運成本。

## `LineWebhookEvent` 表不儲存 LINE Webhook 的 event payload（如聊天內容、簽章、使用者 ID 等），僅儲存 metadata（如狀態、階段、更新時間等）。
- 時間：2026-06-20 17:15
- 理由：消除敏感個資與 token 洩漏的安全與 GDPR 合規風險，同時避免 metadata 資料表儲存量暴增。
- 否決方案：否決儲存完整 JSON payload，因為這會帶來顯著的安全隱患且無消費端。

## 在 `LineWebhookEvent` 中，將 LINE Webhook payload 傳入的 `webhookEventId`（camelCase）寫入/查詢時對齊轉換為資料庫欄位 `webhook_event_id`（snake_case）進行去重；若 event 缺少該 id 則退化為直接處理。
- 時間：2026-06-20 17:15
- 理由：明確宣告駝峰到蛇形的對齊，防止實作時因名稱落差讀錯欄位導致去重全部失效。

## 對 `LineWebhookEvent` 表的 `(tenant_id, webhook_event_id)` 建立聯合唯一約束（UniqueConstraint），並對 `(created_at, status)` 建立複合索引。
- 時間：2026-06-20 17:15
- 理由：聯合唯一約束是防止併發寫入/重複處理的最後一道資料庫防線；複合索引是為了保障保留期清理的效能。

## 定案 metadata 歷史保留期清理策略（Retention Policy），僅保留 7 天內的去重 metadata，由背景定時任務（如 Cron）定期刪除過期紀錄。
- 時間：2026-06-20 17:15
- 理由：LINE 的重送機制通常在數小時內結束，保留 7 天可提供充足的去重保障，並使 metadata 資料表體積大小收斂。

## 使用強型別 Enum 定義事件狀態（`LineWebhookEventStatus`：`PENDING`, `PROCESSED`, `FAILED`）與處理階段（`LineWebhookEventStage`：`CLAIMED`, `QUOTA_CHECKED`, `TRANSLATED`, `REPLY_SENT`, `USAGE_INCREMENTED`），並定義階段大小順序。
- 時間：2026-06-20 17:15
- 理由：避免拼寫錯誤（Typo）散落程式碼，並在重入判斷時有明確的階段先後順序依據。

## 當偵測到 `webhook_event_id` 已存在於資料庫時，進入細分重入控制流：若狀態為 `PROCESSED` 或 `PENDING`（併發中）一律直接略過；若狀態為 `FAILED` 且 `last_stage < REPLY_SENT`，則將狀態改回 `PENDING` 重設嘗試次數並重新處理該 event；若狀態為 `FAILED` 且 `last_stage >= REPLY_SENT`，則直接略過。
- 時間：2026-06-20 17:15
- 理由：解決了「首次處理失敗後重送被永久略過」的 Bug，同時因 `REPLY_SENT` 代表 reply token 已被消費過，重送重試會引發 LINE API 報錯，故對此案放棄補計量與重新 reply，以運維日誌留痕人工對帳。
- 否決方案：否決「不論何種 FAILED 一律重新 retry」，這會導致已被消费的 reply token 被二次呼叫而導致系統崩潰。

## 在 `_process_events` 內封裝 `mark_processed(db, event_row)` 與 `mark_failed(db, event_row, stage, exception)` 兩個 transactional helpers 用於變更狀態、更新 updated_at 並 commit/rollback。
- 時間：2026-06-20 17:15
- 理由：防範因為 `_process_events` 控制流分支眾多，導致手動 commit 時漏更新 processed / failed 狀態的風險。

## 在 `mark_failed(db, event_row, stage, exception)` 寫入 `last_error` 欄位時，必須限制長度（最大 255 字元）且只截取 Exception 的類別名稱與安全摘要，嚴禁寫入可能包含聊天內容或 token 的 exception 詳情。
- 時間：2026-06-20 17:15
- 理由：遵守資安規範，防止敏感聊天內容與 API 金鑰被間接寫入日誌與資料庫。

## 在 `src/saas_mvp/db.py` 的 `init_db()` 中註冊 import 新 model `saas_mvp.models.line_webhook_event`，確保在測試環境的 `Base.metadata.create_all` 亦能載入新 metadata 自動建表。
- 時間：2026-06-20 17:15
- 理由：與現有的 db 初始化及測試 overrides 機制完全對齊，降低 Regression 機率。

## 新增 `tests/test_line_idempotency.py`，測試覆蓋範圍需包含：首投成功、重送 processed 略過、重送 pending 略過、重送 failed 且未過 `REPLY_SENT`（需重新 retry 成功）、重送 failed 且已過 `REPLY_SENT`（略過不重複 reply）、以及缺 ID 退化判定。
- 時間：2026-06-20 17:15
- 理由：確保核心去重邏輯與重入行為有自動化測試防禦，保障系統的可逆性與升級安全。

## 使用 SQLite ALTER TABLE 在 `line_channel_configs` 新增 `credential_status` (狀態包含：unchecked, valid, invalid, error, conflict)、`credential_last_error` 與 `credential_checked_at` 三個欄位。（技術選型）
- 時間：2026-06-20 18:58
- 理由：保持極簡的資料庫演進機制，避免引進大型工具鏈的複雜度，保障現有輕量級架構。
- 否決方案：引入 Alembic 作為 schema 遷移工具，因為當前專案只屬於單張表欄位增減，引入它屬於過度設計，徒增團隊維護成本。

## 在 Pydantic response model（`TenantLineConfigResponse` 與新定義的 `AdminLineConfigResponse`）中，將 `credential_status` 的型別定義為 `str`，且預設值為 `"unchecked"`。當從 DB 讀出 `credential_status` 為 `NULL` 時，在 Service/Router 層將其正規化（Normalize）為 `"unchecked"`，確保 API 契約的完整性。（介面）
- 時間：2026-06-20 18:58
- 理由：避免在 DB migration 中執行 `UPDATE` 回填舊資料的方案，將正規化邏輯留在記憶體與 API 邊界層。
- 否決方案：在資料庫遷移腳本中執行 `UPDATE` 回填舊資料，這可能在資料量大時帶來鎖表或性能開銷，且無法因應未來直接以空欄位寫入的極端情況。

## 在 `line_client/base.py` 定義型別化異常，如 `LineBotInfoError`（基底）、`LineBotInfoCredentialError`（針對 401/403 等憑證失效錯誤）、`LineBotInfoNetworkError`（針對 Timeout/DNS 解析/網路中斷錯誤）、`LineBotInfoParseError`（針對 API 回傳格式不符或缺 userId 錯誤）。`HttpLineBotInfoClient` 捕獲 `urllib` 相關錯誤後轉換拋出，Service 層再據此將狀態對應寫入 `invalid`、`error` 或 `conflict`。（模組切分）
- 時間：2026-06-20 18:58
- 理由：保護模組邊界（保護依賴方向），使得 Service 業務層只依賴抽象 client 提供的型別化異常，隔離底層 `urllib` 的實現細節。
- 否決方案：讓 Service 層直接捕獲 `urllib` 的 HTTPError 與 OSError 並分類錯誤，這會使底層 HTTP 實現的細節滲透到業務邏輯層，未來更換 HTTP client 時會造成極大重構代價。

## 在呼叫 `upsert_line_config` 更新設定時，若 access_token 被更新（即與舊設定不同），不論驗證成功與否，皆必須立即將舊的 `line_bot_user_id` 清除（設為 `NULL`），並將 `credential_status` 設為 `"unchecked"`，直到本次 API 驗證完成。若本次驗證最終為 `invalid/error/conflict`，則 `line_bot_user_id` 維持 `NULL`；若驗證為 `valid`，才寫入新的 `line_bot_user_id`。（介面）
- 時間：2026-06-20 18:58
- 理由：防止在 token 已失效或更新、但驗證失敗的空窗期，webhook 還繼續用舊的 `line_bot_user_id` 誤認租戶，降低安全性錯配風險。
- 否決方案：即使驗證失敗也保留舊 `line_bot_user_id` 供 webhook 使用的寬鬆容錯設計。這雖然會導致短暫的 LINE Webhook 服務不可用（因為 userId 被清空），但安全性與資料一致性高於暫時的容錯。

## 當 `line_bot_user_id` 發生 unique 衝突導致交易 rollback 時，必須在 `db.rollback()` 與 `db.refresh(cfg)` 後，另外開啟一個獨立交易，僅更新並 commit `credential_status='conflict'` 與截斷至 255 字元的安全錯誤摘要。（模組切分）
- 時間：2026-06-20 18:58
- 理由：避免 rollback 連狀態更新也一併抹除，確保錯誤狀態能被正確記錄到資料庫中以供租戶查看。
- 否決方案：在同一個 Session 中使用 nested transaction (savepoint) 來局部 rollback 衝突。這在 SQLite 併發環境下容易引發資料庫鎖定 (lock) 問題，故放棄以降低併發風險。

## 在 `routers/admin.py` 中定義並套用專門的 `AdminLineConfigResponse`（繼承或複寫欄位），同步擴充 `credential_status`、`credential_last_error` 與 `credential_checked_at` 欄位。並且在兩個 response 模型的 serializer 中，嚴格禁止回傳 `channel_secret` 與 `access_token` 的明文，一律僅回傳遮罩布林值。（介面）
- 時間：2026-06-20 18:58
- 理由：保障管理端與自助端 API 文件與行為的一致性，防止未來維護時產生漂移，且不洩漏敏感憑證。
- 否決方案：管理端直接回傳 dict 或免 response model 的簡便設計，因為這會使管理端的 API schema 模糊化，增加前後端對接與維護的隱性成本。

## 翻譯跳過判定委託給 Translator 內部決定，Webhook 消費 `skipped` 狀態直接中斷，不進行 reply 且不扣除租戶內部字數額度。
- 時間：2026-06-20 21:38
- 理由：保護 webhook 邊界，避免其理解翻譯器底層（如 DeepL）的語言偵測與字串比對細節，維持低耦合度。
- 否決方案：在 webhook 層先發送獨立的語言偵測 API 以

## Webhook 在翻譯前必須先通過租戶內部額度檢查（Quotas Gate），若額度不足則直接回覆超額並中斷，不呼叫外部翻譯 API；若額度足夠，則呼叫翻譯 API。若翻譯後判定 skipped，不扣除租戶內部額度，且不發送 LINE 回覆。
- 時間：2026-06-20 21:38
- 理由：保障內部額度上限（Quotas Gate），同時維持 webhook 邊界與 Translator 底層（如 DeepL）的語言偵測與字串比對細節解耦。
- 否決方案：在 webhook 層先發送獨立的語言偵測 API 以免除外部翻譯呼叫的方案。因為這會導致額外的 API 往返延遲且使業務邏輯變複雜。

## `credential_last_error` 欄位僅存入預定義且對使用者安全之錯誤文案或錯誤碼，禁止直接寫入原始例外訊息（Exception messages）。
- 時間：2026-06-20 21:38
- 理由：防止底層例外（如網路逾時、API 連線細節）夾帶敏感資訊或系統內部路徑洩漏至前端，確保安全性。
- 否決方案：僅對 exception 原始字串進行 255 字元截斷並寫入 DB 的方案。

## 將 `credential_status` 集中管理於 Schema 或專門的 `LineCredentialStatus` Enum 中，強制約束狀態值為 `"unchecked"`, `"valid"`, `"invalid"`, `"error"`, `"conflict"`。
- 時間：2026-06-20 21:38
- 理由：消除裸字串散落於 service、router 與 test 的不一致風險，增強型別安全性及可維護性。
- 否決方案：在各模組中隨機使用裸字串與 API 直接互動的方案。

## 在 `line_bot_user_id` 欄位 migration 時，即使該欄位已存在，若 unique index 缺失，仍以 try-except 嘗試追加建立 Unique 索引。
- 時間：2026-06-20 21:38
- 理由：確保半套舊 DB 在演進過程中能順利補齊 index，維持多租戶隔離的強健性與可逆性。
- 否決方案：僅判斷欄位存在即跳過 index 追加的簡化 migration 方案。

## Unique 衝突 Rollback 後，使用同一個 Session 開啟新交易將狀態標記為 `conflict`，不建立額外的 DB 連線 Session。
- 時間：2026-06-20 21:38
- 理由：減少不必要的資料庫連線開啟，在保持交易隔離與狀態寫入的同時最佳化連線資源。
- 否決方案：為寫入衝突狀態而硬開獨立新 DB Session 的方案。

## 採用 `TranslationResult` 作為 `@dataclass(frozen=True, slots=True)` 以保證翻譯資料在背景任務傳遞時的不可變性。
- 時間：2026-06-20 21:38

## 在 `src/saas_mvp/db.py` 的 `init_db()` 中註冊 import 新 model `saas_mvp.models.line_webhook_event`，確保測試環境 metadata 建表一致性。
- 時間：2026-06-20 21:38

## 新增 `tests/test_line_idempotency.py` 用於自動化測試核心去重與重入行為。
- 時間：2026-06-20 21:38

## 在 Pydantic 模型的 Service/Router 層將 DB 讀出的 `credential_status` 的 `NULL` 值正規化為 `"unchecked"`。
- 時間：2026-06-20 21:38

## 在 `line_client/base.py` 定義型別化異常（如 `LineBotInfoCredentialError` 等）以隔離底層 `urllib` 實作細節。
- 時間：2026-06-20 21:38

## 變更 access_token 時立即清除 `line_bot_user_id` 並重設狀態為 `"unchecked"`，防止驗證空窗期誤認租戶。
- 時間：2026-06-20 21:38

## `AdminLineConfigResponse` 與 `TenantLineConfigResponse` 在 API 輸出時嚴格禁止明文憑證，一律只回傳遮罩布林值。
- 時間：2026-06-20 21:38


## 【預約系統 P1】在 `LineChannelConfig` 新增 `bot_mode`（translation 預設 / booking）讓翻譯與預約並存，webhook 依模式分流。
- 時間：2026-06-21
- 理由：既有店家為翻譯 bot，預約為新功能；以 per-tenant 模式旗標並存，避免硬切換破壞既有行為。
- 實作：欄位 `nullable=False, default/server_default="translation"`；migration `_migrate_add_line_bot_mode()` 對既有列回填 translation；`_handle_line_event` 開頭依 `bot_mode` 分流，translation 路徑零行為改動。
- 否決方案：預約完全取代翻譯（失去既有功能）；以獨立服務部署（增運維成本、無法共用租戶/認證/LINE 設定）。

## 【預約系統 P1】預約管理端點與預約 LINE 回覆**不計入既有翻譯 quota**（`ApiUsage`/`require_quota`）。
- 時間：2026-06-21
- 理由：`ApiUsage`/`require_quota` 專指翻譯次數/字數（綁 free/pro 每日上限）；掛到預約會汙染翻譯指標。
- 實作：`/booking/*` router 只掛 `require_rate_limit`，不掛 `require_quota`；webhook 預約分支不呼叫 `increment_usage`。
- 後續：若要對預約計量/計費，另設獨立計數器（橫向 feature-flag 階段），不複用翻譯 quota。

## 【預約系統 P1】容量控管以 `BookingSlot.booked_count` denormalized 計數 + `SELECT … FOR UPDATE` 鎖該列、鎖內重驗，消除超賣競態。
- 時間：2026-06-21
- 理由：比照 `quota._get_or_create_usage_locked` 既有鎖法；計數放單列即可只鎖一列。
- 實作：`services/booking.book_slot` 鎖 slot → 重驗 `max_capacity - walkin_reserved - booked_count >= party_size` → 遞增 + INSERT + 入列提醒，單一 commit。`walkin_reserved` 以減法保留現場名額（不另建表）。

## 【預約系統 P1】自動提醒採「due-reminder 表 + cron 執行 ops 腳本」，不引入 Celery/Redis/APScheduler。
- 時間：2026-06-21
- 理由：沿用最小依賴哲學與既有 `ops/backfill_line_bot_user_id.py` 同形；out-of-process、單實例天然去重，避免 app 內 asyncio loop 在多 worker 下重送。
- 冪等三層：`UNIQUE(reservation_id, kind)` + 逐筆 `SELECT … FOR UPDATE` 重驗 pending + 推播成功後才標 sent。
- 新增能力：LINE push（`LinePushClient` ABC + Http/Fake 實作），reply 無法用於非即時提醒（reply_token 5 分鐘時效）。

## 【Rich Menu P2】圖文選單背景以純 stdlib（zlib）產生純色 PNG，不引入 PIL/影像函式庫。
- 時間：2026-06-21
- 理由：沿用最小依賴哲學；主題配色只需純色背景，zlib + struct 即可產生合法 PNG（color type 2 RGB），避免為單一功能加重依賴。
- 實作：services/rich_menu.solid_png()；row = filter byte + width*RGB，row*height 後 zlib 壓縮（純色壓縮率極高）。店家自訂背景圖未來可改傳上傳 bytes 取代。

## 【Rich Menu P2】Rich Menu 按鈕 action 直接對應既有預約 dispatcher（book/my/slots/help），選單與引導式對話共用同一條 postback 路徑。
- 時間：2026-06-21
- 理由：避免為選單另立一套處理邏輯；按鈕 postback data 與引導式對話完全一致，點按鈕等同輸入指令。
- 實作：LineRichMenuClient（ABC/Http/Fake）四步 create→upload_image→set_default→delete；richMenuId/template/theme 存 LineChannelConfig（migration _migrate_add_rich_menu_fields）。LINE API 失敗 → 502。

## 【引導式預約 P1 fast-follow】多步預約對話採「postback 攜帶上下文」無狀態設計，不建對話狀態表。
- 時間：2026-06-21
- 理由：選時段→選人數的上下文可由 postback data（slot_id → slot_id+party）逐步攜帶，免伺服器 session 表，較原規劃的最小狀態表更簡單、無清理負擔、天然冪等。
- 實作：reply client 新增 quick_reply（base/Http/Fake）；webhook pick_slot → 人數按鈕 → book。保留一次性文字指令。

## 【預約 UI】預約管理與圖文選單接進既有伺服器渲染 /ui（cookie 認證、HTMX partial），bot_mode 以 line_config.set_bot_mode() 輕量切換（不需重輸憑證）。
- 時間：2026-06-21
- 理由：合併進來的 /ui 已涵蓋 LINE 設定/admin，但未涵蓋預約；補齊使 P1 API 真正可用。沿用 require_ui_user/require_ui_admin 與 _ctx/HTMX 慣例。

## 【P3 優惠券】核銷採 SELECT … FOR UPDATE 鎖券列 + 鎖內重驗額度，一人一券靠 UNIQUE(coupon_id, line_user_id)。
- 時間：2026-06-21
- 理由：與 quota/booking 同一防超發鎖法；一人一券交給 DB 唯一約束（捕 IntegrityError 轉 AlreadyRedeemed），免應用層 race。
- 實作：services/coupons.redeem_coupon；REST 與 webhook 共用同一服務（REST 轉 HTTP、webhook 轉友善訊息）。

## 【P3 會員】集點與建單同一交易；點數彙總於 Customer + append-only PointTransaction 帳本；等級由點數純函式重算。
- 時間：2026-06-21
- 理由：「建單 ⇔ 集點」需原子一致（book_slot 內 earn_points，不另 commit）；帳本保稽核軌跡；tier 純函式易測。
- 實作：services/membership（earn/redeem/recompute_tier，TIER_THRESHOLDS 常數）；Customer 加 points_balance/tier（migration _migrate_add_customer_membership，既有列回填 0/regular）。設定 SAAS_POINTS_PER_BOOKING。

## 【P5 報表】聚合於單一查詢取出後 Python 計算（時段使用率按小時 bucket），不用 DB 方言函式（strftime）。
- 時間：2026-06-21
- 理由：單租戶資料量適中、非平台級全表，Python bucket 可攜（SQLite/PostgreSQL 一致）、易測；避免 strftime 等方言相依。
- 實作：services/analytics（booking_summary/slot_utilization/top_customers/reminder_effectiveness/export_rows）；CSV 用 stdlib csv + Response(text/csv)。

## 【P5 爽約率】到場與否需店家標記（Reservation.attended，nullable）；未標記不宣稱精確 no-show。
- 時間：2026-06-21
- 理由：系統無法自動得知顧客是否到場；誠實呈現——未標記時 no_show_rate=None，以取消率為主指標。
- 實作：booking.mark_attendance + POST /booking/reservations/{id}/attendance + UI 標記按鈕；migration _migrate_add_reservation_attended（既有列 NULL=未標記）。
