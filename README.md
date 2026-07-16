# SaaS — 多租戶 LINE 預約 / CRM / 行銷平台

一套**多租戶 LINE 預約 SaaS**，鎖定美業（美髮／美甲／美容）與餐飲等預約型店家。
每個店家以自己的 LINE 官方帳號接入，可一鍵將 bot 切換為**預約模式**（`bot_mode=booking`），
在 LINE 對話內完成時段查詢、自助預約／改期／取消、優惠券兌換、商品購買與會員點數查詢；
店家端則有伺服器渲染的管理後台（`/ui`）統管分店、員工排班、服務目錄、顧客 CRM／分眾、
行銷自動化、AI 客服、公開店家頁與作品集、POS 結帳與金流（綠界 ECPay／藍新 NewebPay）、
進階報表與圖文選單。多數進階模組為 **per-tenant 功能旗標**（freemium 訂閱制，
`services/features.py` 為唯一真相來源）。原始的**多語翻譯機器人**能力仍保留，
店家可選擇 `bot_mode=translation`，兩種模式並存於同一平台。

## 快速啟動

```bash
pip install -e .
python -m saas_mvp        # 或 saas-mvp
# 預設 http://127.0.0.1:8000

# 灌入示範店家（見下方「示範資料」），即可登入 /ui 點過每項功能
python -m saas_mvp.ops.seed_demo
```

### 新版管理端（Next.js foundation）

新版商用管理端位於 `frontend/`，以 Next.js + TypeScript 建置；FastAPI 仍是
業務與整合 API。登入憑證由 Next.js server route 寫入 httpOnly cookie，瀏覽器端
JavaScript 不會取得 bearer token。

```bash
# Terminal 1：FastAPI
python -m saas_mvp

# Terminal 2：Next.js
cd frontend
cp .env.example .env.local   # 本機開發可改為 http://127.0.0.1:8000
npm install
npm run dev
```

目前第一階段提供 `/login`、`/dashboard`，並使用新的 `/api/v1/context` 與
organization scoped RBAC。既有 `/ui` 在全面切換前仍保留作回歸基準。

## 功能總覽

各模組以**功能旗標**（`services/features.py`，per-tenant 訂閱開關）控管，並對應 REST 端點前綴。

| 功能領域 | 旗標 key | 主要端點前綴 | 摘要 |
|----------|----------|--------------|------|
| 預約核心（時段/容量、自助預約/改期/取消） | —（基本免費） | `/booking/slots`、`/booking/reservations` | 原子容量控管、walk-in 保留、LINE 引導式對話 |
| 自動提醒 | `AUTO_REMINDER` | （cron `ops/`，無 REST CRUD） | 建單自動入列前一天/當天提醒，cron push |
| 員工 / 排班 | `STAFF_SCHEDULING` | `/booking/staff` | 員工、週班表、休假、指派；衝突檢查 |
| 服務目錄 | `SERVICE_CATALOG` | `/booking/services` | 服務分類 + 服務（價格/時長）+ 指派員工 |
| 多分店 | `MULTI_LOCATION` | `/booking/locations` | 分店管理（上限 `SAAS_MAX_LOCATIONS_PER_TENANT`） |
| 顧客 / CRM / 分眾 | —（CRM 基本）/ 分眾隨行銷 | `/booking/customers` | LINE 自動建檔、phone/note、標籤分眾 |
| 優惠券 / 會員 / 點數 | `COUPON_SYSTEM` | `/booking/coupons` | percent/amount 券、原子核銷、集點 + 等級 |
| 商品 / POS / 金流 | `PRODUCT_SALES` | `/booking/products`、`/booking/orders`、`/booking/pos`、`/payments` | 商品/訂單、POS 結帳；ECPay + NewebPay 真實金流 |
| 員工抽成／薪資結算 | `STAFF_COMMISSIONS`（pro） | `/ui/commissions`、`/ui/pos` | 固定／階梯抽成、業績目標、小費、成交快照、沖銷、pay run 與 CSV 匯出 |
| 行銷自動化 | `MARKETING_AUTO` | `/booking/campaigns` | 生日/喚回/群發活動，cron 派送 |
| AI 客服 | `AI_ASSISTANT` | `/ai` | FAQ 比對 + LLM（Anthropic Claude）問答 |
| 公開店家頁 / 作品集 | `PUBLIC_PROFILE` | `/booking/profile`、`/booking/portfolio`、`/p/{slug}` | 可發佈的店家頁 + 作品集 |
| 行事曆同步 | —（隨預約） | `/calendar/*.ics` | 店家/員工/顧客 ICS 訂閱連結 |
| Flex / Rich 選單 | `FLEX_MENU` | `/booking/flex-menu`、`/booking/rich-menu` | Flex 卡片選單、Rich Menu 模板套用 |
| 進階報表 | `ADVANCED_REPORTING` | `/booking/analytics` | 摘要、時段使用率、常客、CSV 匯出 |
| 隱私模式 | `PRIVACY_MODE` | `/pii/{token}` | tokenized PII 表單（公開、免登入） |
| 推播額度 / 加量 | `PUSH_BOOST` | `/quota`、`/usage` | 月度推播額度計量，加購提升額度 |
| 網頁預約表單 | `WEB_BOOKING` | `/booking/f/{token}` | bot 發 tokenized 深連結，免登入建單；額滿時可直接候補 |
| 滿意度調查 | `FEEDBACK_SURVEY` | （webhook postback） | 消費後自動邀評，1–5 星 + 追問 |
| AI 預約 agent | `AI_BOOKING_AGENT` | （webhook 對話） | 自由句建單/改期/取消/查詢；AI 只提案、確認按鈕走擁有者驗證 |
| AI 用量加購 | `AI_BOOST` | `/ui/features` | AI 對話月額度加量 |
| 定金收款 | `DEPOSIT_PAYMENT`（pro） | `/payments/ecpay/deposit/{id}` | 防 no-show：線上建單收定金，逾時自動取消回補名額 |
| 服務套票／療程次數 | `SERVICE_PACKAGES`（pro） | `/ui/packages` | 多服務套票、售價／效期快照、顧客錢包、預約扣次與取消自動退次 |
| 電子禮物卡／儲值金 | `GIFT_CARDS`（pro） | `/ui/gift-cards`、`/booking/pos` | 安全卡號、永久效期、分次／混合付款、取消退回與餘額帳本 |
| 顧客諮詢表／療程同意書 | `CLIENT_FORMS`（pro） | `/ui/client-forms`、`/client-forms/{token}` | 依服務自動派發、欄位設計、範本版本快照、姓名確認、不可變簽署紀錄與列印 PDF |
| 房間／設備資源 | `BOOKABLE_RESOURCES`（pro） | `/ui/resources` | 資源類型、分店／容量、週期可用時間、維修停用、服務需求與預約原子自動配置 |
| 重複預約／療程週期 | 基本預約 | `/ui/booking` | 由既有預約建立每週、每雙週或每月系列，衝突逐次追蹤、重試及取消本次與後續 |
| 方案 bundle / 試用 | `BUNDLE_*` | `/ui/plan` | 免費/標準 399/專業 899；14 天試用；降級期末生效 |
| 電子發票 | —（隨訂閱扣款） | （cron 重試） | 綠界 B2C 發票，期扣成功自動開立、失敗每小時重試 |
| LINE Pay | —（隨 POS） | `/payments/linepay/*` | 一次性訂單結帳（sandbox/prod 由 env 切換） |
| Google 日曆同步 | —（隨預約） | `/ui/gcal/*` | OAuth 單向寫入：建單/改期/取消即時同步；失敗自動補送、後台可重試 |
| 稽核日誌 / 代管 | —（平台） | `/ui/admin/audit` | 管理操作全留痕；admin 可代管店家（30 分自動過期） |

## 管理後台 `/ui`

伺服器渲染（Jinja2 + HTMX，與 API 同源）。登入後 JWT 存於 **httpOnly cookie**，
與 API 的 header 認證隔離。瀏覽 `http://127.0.0.1:8000/ui/login`。頁面：

| 路徑 | 頁面 |
|------|------|
| `/ui/login`、`/ui/register`、`/ui/logout` | 登入 / 註冊 / 登出 |
| `/ui/` | 儀表板（bot 狀態 + 今日用量） |
| `/ui/line-config` | LINE 憑證設定、完整教學、一鍵設定 Webhook 與官方連通診斷 |
| `/ui/locations` | 分店 |
| `/ui/staff` | 員工（排班/休假） |
| `/ui/services` | 服務項目 |
| `/ui/booking` | 預約管理（含到場/未到標記、重複預約系列） |
| `/ui/customers` | 顧客 CRM（搜尋/分頁、編輯電話/備註、標籤管理、點數調整、預約歷史、**CSV 批次匯入/匯出**） |
| `/ui/client-forms` | 顧客諮詢表／同意書（owner 建立及啟用；新預約自動派發，完成紀錄可由顧客詳情查看／列印） |
| `/ui/campaigns` | 行銷活動（受眾選擇器：標籤/等級/次數/分店，免手填 JSON） |
| `/ui/notifications` | 通知歷程（預約通知/行銷發送/推播用量三個 tab，唯讀） |
| `/ui/flex-menu` | Flex 圖文選單 |
| `/ui/rich-menu` | Rich Menu |
| `/ui/portfolio` | 作品集 |
| `/ui/profile` | 店家頁 |
| `/ui/pos` | POS 結帳 |
| `/ui/commissions` | 員工抽成規則、成交明細與薪資結算（owner 限定） |
| `/ui/faq` | AI 客服 FAQ |
| `/ui/shop` | 商品 |
| `/ui/coupons` | 優惠券 |
| `/ui/reports` | 進階報表（摘要/使用率/常客/營收/趨勢/產能/回訪） |
| `/ui/features` | 進階功能訂閱（含訂閱狀態、`cancel_failed` 警示、逐期扣款紀錄） |
| `/ui/account` | 帳號設定（變更登入密碼、連結／解除 LINE 帳戶以一鍵登入） |
| `/ui/admin/oauth-settings` | 平台管理員：在後台加密設定 LINE Login 與 Google OAuth，立即供登入及日曆同步使用 |
| `/ui/admin/email-settings` | 平台管理員：加密設定 SMTP、寄送測試信，供驗證信與重設密碼立即使用 |
| `/ui/admin/bots`、`/ui/admin/tenants/{id}` | 平台管理員：跨店家 bot 總覽 / 單一租戶管理 |

## 公開 / 對外端點（免登入，`include_in_schema=False`）

| 路徑 | 說明 |
|------|------|
| `GET /p/{slug}` | 公開店家頁（須 `BusinessProfile.is_published=true` 才解析） |
| `GET /s/{token}` | 員工入口（以 `Staff.access_token` 換頁，看自己的班表/預約） |
| `GET /calendar/shop/{token}.ics`、`/calendar/staff/{token}.ics`、`/calendar/customer/{token}.ics` | 行事曆 ICS 訂閱（店家/員工/顧客） |
| `GET /pii/{token}`、`POST /pii/{token}` | 隱私模式 tokenized PII 表單 |
| `GET /client-forms/{token}`、`POST /client-forms/{token}` | 預約諮詢表／同意書能力連結；免登入、送出後唯讀，回應禁止快取與搜尋引擎索引 |
| `POST /line/webhook/{tenant_id}` | LINE Webhook（須 `X-Line-Signature`） |
| `GET /auth/oauth/{provider}/login`、`/callback` | 社群登入（`provider ∈ {line, google}`）。登入頁可「使用 LINE 登入」；後台帳號頁帶 `?link=1` 可把 LINE 帳戶綁到目前登入者，之後即可一鍵登入管理 |

## 排程作業（`ops/`）

每支腳本均 `--dry-run`（預設）/`--apply`、單實例去重、session_factory 可注入。
以 cron 定時觸發：

| 腳本 | 建議排程 | 作用 |
|------|----------|------|
| `send_due_reminders` | 每 10–15 分鐘 | 派送到期預約提醒（LINE push） |
| `send_due_notifications` | 每 10–15 分鐘 | 派送預約異動通知 |
| `run_birthday_campaigns` | 每日 09:00 | 生日行銷活動 |
| `run_reactivation` | 每日 14:00 | 沉睡顧客喚回活動 |
| `run_scheduled_campaigns` | 每 5–15 分鐘 | 已排程的群發/限時活動 |
| `retry_gcal_syncs` | 每 5 分鐘 | 補送暫時失敗的 Google Calendar 建立／更新／刪除工作 |
| `purge_webhook_events` | 每日 04:00 | 清理過期的 LINE webhook 冪等事件（`--days` 預設 `SAAS_WEBHOOK_EVENT_TTL_DAYS=30`；`--include-failed` 一併清達重試上限者） |
| `retry_stuck_webhook_events` | 每 5 分鐘 | 重放 worker crash/restart 留下的持久化 webhook outbox（CAS 認領、上限後 dead-letter） |
| `reverify_line_credentials` | 每 6 小時 | 重驗超過 24h 的有效 LINE 憑證（逐租戶預算、節流與 circuit breaker） |
| `retry_cancel_failed` | 每小時 | 重試 `cancel_failed` 訂閱的綠界停扣（成功標 `cancelled`） |
| `check_billing_health` | 每 6 小時 | 帳務健康報表（cancel_failed / pending 過期 / 狀態不一致；異常 exit 1） |
| `migrate` | 啟動時（entrypoint） | Alembic schema 遷移（冪等；`--check` 只回報狀態） |

> 容器部署的排程由 **supercronic** 依 `docker/crontab`（UTC）執行（scheduler 服務，
> 單一實例）；每分鐘的提醒+異動通知合成一條並以 `flock -n` 串行防重疊。

```bash
# 例：每 10 分鐘派送提醒（單實例，避免多 worker 重送）
*/10 * * * * python -m saas_mvp.ops.send_due_reminders --apply
```

> 另有一次性維運腳本 `backfill_line_bot_user_id`（回填既有 bot userId，見文末）。

## 可觀測性（observability）

掛上一層 ASGI middleware，每個請求自動取得 **request-id 串接 + 結構化存取日誌 + Prometheus 指標**，無需逐路由埋點。

**探針（probe）：**

| 端點 | 用途 | 回應 |
|------|------|------|
| `GET /healthz` | 存活 + 輕量 DB 檢查（向後相容） | `{status, db, rate_limit_backend}`；DB 異常回 503 |
| `GET /readyz` | 就緒（readiness）：相依逐項檢查 | `{status, checks{db}, rate_limit_backend}`；未就緒回 503 |
| `GET /metrics` | Prometheus 文字格式指標 | `http_requests_total` / `http_request_duration_seconds`（histogram）/ `http_requests_in_progress` |

**request-id：** 用戶端可帶 `X-Request-ID`（合理長度的可列印 token）串接既有 trace；未帶則自動新生。值會回寫到回應 `X-Request-ID` header，並注入同一請求跨模組的所有 log。

**日誌：** `SAAS_LOG_FORMAT=json` 輸出單行 JSON（給 Loki/CloudWatch 等聚合器），`text`（預設）為人類可讀單行；`SAAS_LOG_LEVEL` 控制等級。存取日誌 logger 名為 `saas_mvp.access`，含 `method/path/route/status/duration_ms/request_id`。

**指標：** label 用**路由樣板**（如 `/s/{token}`）而非原始路徑，避免 cardinality 爆炸。`/metrics` 為 **per-worker**（每個 gunicorn worker 一份計數），請於 Prometheus 端 `sum()` 聚合。
- `SAAS_METRICS_ENABLED=false` → `/metrics` 回 404（完全停用）。
- `SAAS_METRICS_TOKEN` 非空 → `/metrics` 需 `Authorization: Bearer <token>`；留空代表不設限，**僅應在內網/受信任網段曝露**。

**未捕捉例外：** 任何未被路由處理的例外會經集中式 handler 轉成**一致的 JSON envelope**，不外洩內部訊息/traceback：

```json
{"error": {"type": "InternalServerError", "message": "伺服器發生未預期錯誤…", "request_id": "abc123def456"}}
```

完整 traceback 連同 `request_id` 記在伺服器端 `saas_mvp.error` ERROR 日誌（用同一 id 對帳），並計入指標 `http_unhandled_exceptions_total{type=...}`。回應 header 亦帶 `X-Request-ID`。

```bash
curl -s localhost:8000/readyz | jq        # 就緒檢查
curl -s localhost:8000/metrics | head     # Prometheus 指標
```

## 正式部署強化（production hardening）

`docker-compose.yml` 已內建多項上線強化：
- **容器埠只綁 loopback**（`127.0.0.1:8099`）：對外一律走主機反代（nginx + TLS + 安全標頭），不開放公網直連容器明文 HTTP。反代須 `proxy_pass http://127.0.0.1:8099` 並送 `X-Forwarded-For`。
- **proxy header 信任**：gunicorn 帶 `--forwarded-allow-ips`（預設 `*`，因埠已綁 loopback 僅本機 nginx 可達），讓 uvicorn 還原真實 client IP——否則 **per-IP 限流會退化成全站共用一桶**（`GUNICORN_FORWARDED_ALLOW_IPS` 可覆寫）。
- **資源上限**：web/scheduler/db/redis 皆設 `mem_limit`/`cpus`/`pids_limit`，避免單租戶吃光整機拖垮同主機其他服務；redis 設 `--maxmemory` + LRU。
- **容器降權**：web/scheduler `cap_drop: ALL` + `no-new-privileges`（非 root uid 10001）。

反代端建議（見部署範例）：`server_tokens off` + `Strict-Transport-Security`/`X-Content-Type-Options`/`X-Frame-Options`/`Referrer-Policy`。**資料庫備份**已由內建 `db-backup` 服務自動處理，詳見下節。

## 資料庫管理與備份

正式資料庫為 PostgreSQL 16（`db` 服務，資料存在 named volume `pgdata`；開發/測試預設用 SQLite）。

### 自動備份（開箱即用）

`docker-compose.yml` 內建 `db-backup` 服務：每日 `BACKUP_TIME`（UTC `HHMM`，預設 `0300`）自動以
`pg_dump` custom format 把整個資料庫 dump 到**主機目錄 `./backups`**，並自動刪除超過
`BACKUP_RETENTION_DAYS`（預設 14）天的舊備份。`docker compose up -d` 後即生效，無需額外設定。

- 設定（`.env`）：`BACKUP_TIME=0300`、`BACKUP_RETENTION_DAYS=14`。
- 與 `db` 同版本 image，`pg_dump`/`pg_restore` 版本精準相符；備份檔命名 `saas-YYYYmmdd-HHMMSS.dump`。
- **離站建議**：單機 `./backups` 目錄仍是單點，請另以 rsync / 物件儲存（S3 等）定期同步異地。

### 手動備份

```bash
docker compose run --rm db-backup /usr/local/bin/backup.sh
# → ./backups/saas-YYYYmmdd-HHMMSS.dump
```

### 還原（破壞性，會覆寫現有資料庫）

```bash
# 列出可用備份
ls -lh ./backups

# 還原指定備份（FORCE=1 跳過互動確認；--clean 會先 DROP 既有物件再重建）
docker compose run --rm -e FORCE=1 db-backup \
  /usr/local/bin/restore.sh /backups/saas-YYYYmmdd-HHMMSS.dump
```

### Schema 管理（Alembic）

版本化遷移：服務啟動時 `entrypoint.sh` 跑一次 `python -m saas_mvp.ops.migrate`（冪等），
三分支——全新 DB → `alembic upgrade head`；**legacy DB**（Alembic 導入前、無
`alembic_version` 表）→ 先以 `legacy_init_db()`（create_all + 手寫 `_migrate_*`）收斂到
baseline 等價 schema，再 `alembic stamp` baseline → upgrade；已納管 → `upgrade head`。

- 遷移腳本在 `src/saas_mvp/migrations/`（隨套件發佈）；開發機 CLI 用根目錄 `alembic.ini`：
  ```bash
  PYTHONPATH=src alembic revision --autogenerate -m "描述"   # model 改完後產 revision
  PYTHONPATH=src alembic upgrade head
  ```
- **新的 schema 變更一律寫 Alembic revision**；`db.py` 的 `_migrate_*` 僅為 legacy DB
  收斂的過渡保留，勿再新增。`Base.metadata.create_all` 僅供測試 fixture 使用。
- 檢查 DB 納管狀態：`python -m saas_mvp.ops.migrate --check`（fresh/legacy/managed）。

### 常用維運指令

```bash
# 互動 SQL（psql 進到 db 容器）
docker compose exec db psql -U saas -d saas

# 灌示範資料（冪等）
docker compose run --rm web seed

# 建立 / 提權平台管理員
docker compose run --rm web python -m saas_mvp.ops.promote_admin --email owner@shop.tw
```

## 示範資料

`ops/seed_demo` 會建立一個開通**全部進階旗標**的示範店家，並灌入分店、員工（含班表/休假）、
服務目錄、時段與預約、商品、POS 已付成交、員工抽成與草稿結算單、優惠券、Flex 選單、作品集、已發佈店家頁（slug `demo`）、FAQ、
生日行銷活動與一位有電話/生日/點數的顧客——讓人能登入 `/ui` 點過每一項功能。**冪等**：重跑不報錯、不重複。

```bash
# 預設帳密：demo@salon.tw / demo1234，租戶「示範美髮沙龍」
python -m saas_mvp.ops.seed_demo

# 自訂帳密 / 租戶名
python -m saas_mvp.ops.seed_demo --email me@shop.tw --password mypass123 --tenant-name 我的沙龍

# 指定 DB（否則用 SAAS_DATABASE_URL）
SAAS_DATABASE_URL=sqlite:////tmp/demo.db python -m saas_mvp.ops.seed_demo
```

執行後會印出登入 URL、帳密、公開店家頁 `/p/demo` 與一條員工入口 `/s/{token}` 連結；
以該帳密登入 `http://127.0.0.1:8000/ui/login` 即可瀏覽所有頁面。

## 管理 UI 設計重點

- 登入後 JWT 存於 **httpOnly cookie**（`SameSite=Lax`，prod 加 `Secure`）。
  UI 路由用獨立的 cookie 認證，**不影響 API 路徑**（API 仍只認 header；
  cookie-only 請求一律 401）。
- HTML 永不輸出明文 `channel_secret` / `access_token`，只揭露 `has_*` 與
  `credential_status`。
- CSRF：**double-submit cookie token**——登入時發放非 httpOnly 的 `csrf_token`
  cookie，所有 `/ui` 非 GET 請求須以 `X-CSRF-Token` header（HTMX 由 `base.html`
  body 級 `hx-headers` 自動帶出）或表單 hidden field 回傳同值，不符回 403。
  `SAAS_UI_CSRF_ENABLED=false` 可關閉（僅測試環境）。

## 認證方式

所有受保護端點支援以下三種認證（互斥選一）：

| 方式 | 標頭範例 |
|------|---------|
| Session JWT | `Authorization: Bearer eyJ...` |
| API Key（X-API-Key） | `X-API-Key: myapp_xxxxxxxx...` |
| API Key（Bearer） | `Authorization: Bearer myapp_xxxxxxxx...` |

## 平台管理員（`is_admin`）

平台管理端點（`/admin/*`、後台 `/ui/admin/bots`、`/ui/admin/tenants/{id}`）以 `User.is_admin`
為閘門。`/auth/register` **刻意不開放**設定此旗標（防自助提權），故管理員須由具 DB 權限者
用 `ops/promote_admin` 設定：

```bash
# 提權既有帳號為管理員
python -m saas_mvp.ops.promote_admin --email owner@shop.tw

# 一次建立全新的專屬管理員帳號（含其租戶）
python -m saas_mvp.ops.promote_admin --email admin@you.tw --password 'S3cret!!' --create

# 取消管理員權限
python -m saas_mvp.ops.promote_admin --email owner@shop.tw --demote
```

容器化部署時於 web 容器內執行（會自動讀 `.env` 連線正式 DB）：

```bash
docker-compose exec web python -m saas_mvp.ops.promote_admin --email admin@you.tw --password 'S3cret!!' --create
```

## 主要端點

### 帳號 `/auth`

| 方法 | 路徑 | 說明 |
|------|------|------|
| POST | `/auth/register` | 註冊（需 tenant_name），回傳 access_token |
| POST | `/auth/token` | 登入，回傳 access_token |
| POST | `/auth/change-password` | 變更密碼（需登入）；驗證目前密碼，新密碼至少 8 字元且不得與目前相同，成功回 204 |

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

## 設定（環境變數，前綴 `SAAS_`）

除既有的 LINE / quota / ECPay 設定（見各章節）外，預約 SaaS 新增以下值（皆有預設，正式環境視需要覆寫）：

| 變數 | 說明 | 預設 |
|------|------|------|
| `SAAS_PUBLIC_BASE_URL` | 對外網址（組金流回呼、公開頁/員工入口/seed 連結絕對網址） | `""` |
| `SAAS_MAX_LOCATIONS_PER_TENANT` | 每租戶可建分店上限 | `5` |
| `SAAS_ANTHROPIC_API_KEY` | AI 客服 LLM 金鑰備援值；後台「平台管理 → AI 設定」的加密資料庫設定優先 | `""` |
| `SAAS_AI_MODEL` | 後台未設定時使用的 Claude 模型備援值 | `claude-sonnet-4-6` |
| `SAAS_PAYMENT_PROVIDER` | 金流 provider 備援值；後台「平台管理 → 金流設定」優先 | `stub` |
| `SAAS_NEWEBPAY_MERCHANT_ID` | 藍新 NewebPay 商店代號 | `""` |
| `SAAS_NEWEBPAY_HASH_KEY` | 藍新 HashKey | `""` |
| `SAAS_NEWEBPAY_HASH_IV` | 藍新 HashIV | `""` |
| `SAAS_NEWEBPAY_ENV` | 藍新環境 `stage` / `prod` | `stage` |
| `SAAS_LINE_LOGIN_CHANNEL_ID` | LINE Login channel id 備援值；後台「平台管理 → 登入設定」的加密資料庫設定優先 | `""` |
| `SAAS_LINE_LOGIN_CHANNEL_SECRET` | LINE Login channel secret 備援值；一般使用者不需接觸 | `""` |
| `SAAS_OAUTH_REDIRECT_BASE` | OAuth callback 對外 base URL；空則使用 `SAAS_PUBLIC_BASE_URL` | `""` |
| `SAAS_GOOGLE_OAUTH_CLIENT_ID` | Google OAuth client id（後台未設定時的備援） | `""` |
| `SAAS_GOOGLE_OAUTH_CLIENT_SECRET` | Google OAuth client secret（後台未設定時的備援） | `""` |
| `SAAS_PUSH_ALLOWANCE_BASE` | 每月基礎推播額度（提醒/通知/行銷共用） | `200` |
| `SAAS_PUSH_ALLOWANCE_BOOST` | 開通 `PUSH_BOOST` 後的額外推播額度 | `500` |
| `SAAS_REACTIVATION_DORMANT_DAYS` | 喚回活動判定沉睡的閒置天數 | `90` |
| `SAAS_REACTIVATION_CAP_PER_SHOP` | 喚回活動每店單次派送上限 | `50` |

### 平台 AI 設定（不需終端機或重啟）

平台管理員可到 `/ui/admin/ai-settings` 設定 Anthropic API Key 與 Claude 模型。
API Key 以 Fernet 加密保存，頁面與稽核紀錄不顯示明文；儲存後 AI 客服、LINE
自由文字回覆與 AI 預約助理立即改用 Claude。頁面提供極小用量的連線測試，能分辨
金鑰、模型、額度或連線問題。移除資料庫設定後會使用 `SAAS_ANTHROPIC_*` 備援；若
備援也未設定，安全退回 FAQ 規則模式，既有 FAQ 不受影響。
| `SAAS_WEBHOOK_MAX_ATTEMPTS` | LINE webhook failed 事件的重試上限（含首次），達上限後重送視為 duplicate | `5` |
| `SAAS_WEBHOOK_EVENT_TTL_DAYS` | `ops/purge_webhook_events` 清理 processed 事件的預設保留天數 | `30` |
| `SAAS_LINE_VERIFY_MAX_ATTEMPTS_PER_HOUR` | 單租戶 bot/info 手動與背景驗證的每小時上限 | `3` |
| `SAAS_LINE_CREDENTIAL_REVERIFY_HOURS` | 自動重驗的 stale 門檻 | `24` |
| `SAAS_LINE_CREDENTIAL_REVERIFY_BATCH_SIZE` | 單次背景重驗最大租戶數 | `100` |
| `SAAS_UI_CSRF_ENABLED` | `/ui` CSRF double-submit token 防護開關（僅測試環境可關） | `true` |

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

商家可在 `/ui/line-config` 儲存憑證後按「一鍵設定並測試 Webhook」。平台會以
伺服器端產生的租戶專屬 HTTPS URL 呼叫 LINE 官方 API，依序設定 endpoint、送出
測試事件並讀取 `Use webhook` 狀態；access token 僅在伺服器端解密使用，不會回傳
到 HTML。若 LINE 回報失敗，頁面會顯示 `reason`、`detail` 與 HTTP status 供診斷。

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
| `我的預約` / `/my` | 列出自己的預約（Flex carousel，每張卡附**改期**/取消按鈕） |
| `改期 <預約編號>` / `/reschedule 7` | **引導改期**：選新日期 → 選新時段 → 原子換時段（舊回補、新扣量，單一交易） |
| `取消 <預約編號>` / `/cancel 7` | 取消（驗證 line_user_id） |
| `候補` / `/waitlist` | 查看/取消自己的額滿候補 |
| `套票` / `/packages` | 查看各服務剩餘次數與到期日；網頁預約可勾選扣次 |
| `禮物卡` / `/giftcards` | 查看錢包餘額；`領取禮物卡 <卡號>` 將受贈卡加入錢包 |
| `留電話` / `填資料` / `/contact` | （`PRIVACY_MODE` 開通）回 tokenized PII 表單連結，顧客自助填姓名/電話/生日 |
| 口語輸入（如「預約明天」「我要改期 7」「取消7」） | **中文容錯解析**：剝意圖前綴 + 最長 alias 前綴比對；高風險指令（取消/改期）黏著參數必須是編號（「取消訂閱」不會誤觸） |
| 額滿回覆的「加入候補」按鈕 | LINE／網頁皆可登記；取消、改期或加開容量後依順位與人數通知第一位，限時未完成會自動遞補下一位 |
| 提醒訊息的「確認出席」按鈕 | 寫入 `customer_confirmed_at`（重複確認冪等）；「取消預約」按鈕直接走取消流程 |

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

候補 offer 由 `python -m saas_mvp.ops.process_waitlists --apply` 每分鐘收斂。
店家可在「預約管理 → 候補名單」查看等候、已通知、已補位、已逾時與已取消狀態；
owner 可設定 5～120 分鐘的回應時間。通知不鎖定名額，顧客完成建單後才會標記為已補位。
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

### 商品銷售（P4）

**商品** `/booking/products`（店家 CRUD）、**訂單** `/booking/orders`：

| 方法 | 路徑 | 說明 |
|------|------|------|
| POST/GET/PUT/DELETE | `/booking/products[/{id}]` | 商品 CRUD（價格 `price_cents` 整數、`stock` NULL=不限） |
| POST | `/booking/orders/` | 下單（`items=[{product_id,qty}]`），回單 + **stub 付款連結** |
| GET | `/booking/orders[/{id}]` | 列出/單一（含明細） |
| POST | `/booking/orders/{id}/pay` | 標記已付 |
| POST | `/booking/orders/{id}/cancel` | 取消並回補庫存 |

- **下單原子性**：依 `product_id` 排序後逐一 `SELECT … FOR UPDATE` 鎖商品（固定順序避免死鎖），
  鎖內驗 `is_active`/`stock`、扣庫存、**快照單價**（`OrderItem.unit_price_cents`，商品改價不影響舊單）。
- 金額一律整數 cents。LINE 指令：`商品`（列商品 + quick-reply 購買鈕）、`購買 <編號> [數量]`
  （postback `action=buy&product_id=N&qty=K`）、`我的訂單`。
- UI：`/ui/shop`（商品 CRUD + 訂單標付/取消）。導覽列加「商品」。

> **金流**：目前為 `StubPaymentProvider`（回傳測試付款連結）。接真實金流（綠界 ECPay /
> Stripe / LINE Pay…）需指定 provider 與帳號，以同一 `PaymentProvider` 介面接上。

### 員工抽成與薪資結算

專業版 owner 可在 `/ui/commissions` 為每位員工分別設定服務／商品抽成。固定規則支援百分比或
每件固定金額、折扣前原價或折扣後淨額，以及生效日版本；新增版本不覆寫舊規則。

1. `/ui/pos` 可選擇既有預約，把服務名稱、成交價、顧客與指派員工帶入訂單，也可同單加入商品、
   指定銷售員、小費及付款方式。
2. 只有勾選「已完成收款」或金流回調成功後才建立抽成快照；待付款單不列入薪資。同一預約只能
   結帳一次，付款回調重送也不會重複計算。
3. 結算單依日期收納尚未結算的抽成與小費，草稿可為個別員工加上獎金、保底或預支扣款；確認後
   鎖定，再由 owner 標記已付款。
4. 取消未結算的已付訂單會作廢原抽成；若已進入確認／付款結算，系統保留原快照並建立負數沖銷，
   留待下一張結算單扣回，完整保留稽核軌跡。
5. 階梯抽成支援每日、每週、每 2 週、每 4 週、每月或每季累計；百分比依各級距邊際分段計算，
   並將跨級明細快照保存在成交紀錄，後續改規則不會回寫歷史。
6. 業績目標可追蹤服務、商品或全部已付成交，進度會自動排除已取消訂單。結算單與成交抽成明細均可下載
   UTF-8 BOM CSV，並對 Excel 公式開頭的外部輸入做安全轉義。

所有金額以 cents 儲存；折扣與點數依明細成交額比例分攤，禮物卡視為付款方式而非折扣，小費 100%
歸屬 POS 指定員工。

| 環境變數 | 說明 | 預設 |
|------|------|------|
| `SAAS_CURRENCY` | 預設幣別 | `TWD` |
| `SAAS_PAYMENT_PROVIDER` | 金流 provider | `stub` |

### 房間／設備資源（Pro）

店主在 `/ui/resources` 建立資源類型（例如包廂、美容床、雷射機），再建立實際資源並設定
分店、同時容量、使用日期、每週可用時間及維修停用區間；最後把服務綁定所需資源類型與數量。

- LINE 與網頁預約會先隱藏資源不足的時段，送出時仍在資料庫鎖內重驗，避免兩位顧客同時搶到同一資源。
- 一筆服務可同時要求多種資源；任一類型不足會回滾整筆預約，不留下名額或部分配置。
- 分店限定資源只能供該分店使用；未指定分店的資源可跨分店共用。
- 改期會原子重新配置；新時段資源不足時保留原預約。取消後立即釋放容量，歷史配置仍可稽核。
- 預約管理頁顯示實際配置資源；資源頁提供近期配置清單。店員可查看，只有店主／平台管理員能修改。

### 重複預約／療程週期

店員可在 `/ui/booking` 的有效預約列展開「建立重複預約」，選擇每週或每月、間隔與總次數
（含原預約，最多 52 次）。例如「每 2 週、共 6 次」適合固定療程、教練課與顧問會談。

- 原預約會成為系列第 1 次；相同顧客、服務、員工、人數與備註會帶到後續預約。
- 可勾選自動補齊缺少的時段；新時段沿用原時段起迄、容量、現場保留名額與分店。
- 每次仍使用正式建單流程，重新檢查時段容量、一對一員工、房間／設備、提醒、顧客表單與 Google Calendar，不會繞過超賣保護。
- 月底採日曆校正，例如 1 月 31 日的下一次會落在 2 月最後一天，之後仍以原始日期計算。
- 某次額滿或資源不足時，該日期會標記為衝突並顯示原因；其他成功預約不受影響。釋出容量後可按「重新檢查並建立」。
- 每個有效日期皆可「取消此筆及後續」；也能用原本單筆取消按鈕只取消一次，系列狀態會同步更新。
- 後台建立系列不會替每一筆自動產生待付定金；如需逐次收取，仍由店家依實際付款流程處理，避免一次建立療程時產生多筆未付款交易。

### 服務套票／療程次數（Pro）

店主可在 `/ui/packages` 建立含多個服務的套票，設定每項服務次數、售價與購買後有效天數；
再從顧客詳情頁確認收款並發行。顧客輸入 LINE「套票」可查看剩餘次數與到期日，透過
網頁預約選定符合服務後可勾選「使用服務套票」。

- 餘額採 append-only 次數帳本：發行為正數、預約扣次為負數、取消退回為正數，保留完整歷史。
- 扣次與預約建單在同一交易完成；以 `SELECT … FOR UPDATE` 鎖顧客套票，並發使用最後一次不會超扣。
- 多張套票同時可用時優先扣最早到期者；已過期、未生效、停售後新發行或次數不足都會拒絕。
- 同一預約以唯一約束保證最多扣一次、退一次；重複取消不會重複增加餘額。
- 誤發或退款時，店主可在顧客詳情輸入對帳原因後作廢；未用次數以帳本沖銷，款項退款仍須依原收款方式處理。
- 套票定義與發行限店主／平台管理員操作，店員只能查看；跨租戶顧客、服務與套票引用一律拒絕。
- 使用套票的預約不再要求定金；後台預約列會標示「套票扣次」，取消後顯示已自動退回。

### 電子禮物卡／儲值金（Pro）

店主可在 `/ui/gift-cards` 收款後發行指定面額的禮物卡，直接綁定顧客或把一次性顯示的
16 碼卡號安全交付收禮人。顧客可在 LINE 輸入 `領取禮物卡 <卡號>` 加入錢包，店內 POS
輸入卡號即可折抵。

- 卡號只保存 SHA-256 雜湊與末四碼，完整卡號僅在發行成功頁顯示一次；資料庫或後台人員無法還原。
- 有償卡不提供到期日欄位，永久有效；餘額可分次使用，卡內不足時只折抵現有餘額，剩餘款以其他方式支付。
- 發行時強制保存履約保障文字快照並要求店主確認；系統不會把 SaaS 功能誤當成店家的法定履約保障。
- 餘額採 append-only 金額帳本；發行、POS 折抵、訂單取消退回、退款後作廢皆可稽核。
- POS 折抵與訂單／庫存異動在同一交易，並鎖定卡片；錯誤卡號或餘額不足不會留下半筆訂單。
- 已綁定的卡不能由其他顧客領取或使用；所有查詢與操作均限制在同一租戶。
- 作廢會沖銷未用餘額，但店家必須先依適用法規完成實際退款並留下原因。

台灣有償商品（服務）禮券的使用期限、未用餘額及履約保障仍應以主管機關最新規定與店家
所屬行業規範為準；上線前請由店家或法律顧問核對發行文字。

### 進階功能旗標 + 訂閱（freemium）

基本預約功能免費；**自動提醒 / 優惠券會員 / 商品銷售**為進階功能，per-tenant 可隨時訂閱／退訂
（付款目前為 stub 模擬月費）。`services/features.is_enabled` 為**唯一真相來源**，REST / webhook /
ops / UI 全部走它。

| 功能 key | 內容 | 月費 |
|------|------|------|
| `AUTO_REMINDER` | 自動提醒（LINE push） | NT$200/月 |
| `COUPON_SYSTEM` | 優惠券 + 會員集點 | NT$200/月 |
| `PRODUCT_SALES` | 商品銷售 | NT$200/月 |
| `SERVICE_PACKAGES` | 服務套票／療程次數 | 專業版內含或單項加購 |
| `GIFT_CARDS` | 電子禮物卡／儲值金 | 專業版內含或單項加購 |
| `BOOKABLE_RESOURCES` | 房間／設備資源與自動配置 | 專業版內含或單項加購 |

| 方法 | 路徑 | 說明 |
|------|------|------|
| GET | `/billing/features` `· /tenants/me/features` | 自家開通狀態 + 月費 |
| POST | `/billing/features/{feature}/subscribe` | 訂閱（stub 付款 → 啟用，回 `payment_id`） |
| POST | `/billing/features/{feature}/unsubscribe` | 退訂（關閉） |
| GET/PUT | `/admin/tenants/{id}/features[/{feature}]` | 平台 admin 查詢/覆寫 |

- **閘門**：`/booking/coupons`、`/booking/products`、`/booking/orders` 未開通 → 403；
  `book_slot` 未開通 AUTO_REMINDER → 不入列提醒；ops 派送前再檢查（已入列後退訂也不送）；
  LINE「優惠券／商品」未開通 → 回「本店尚未開放此功能」。
- **稽核**：每次訂閱/退訂/admin 覆寫寫 `FeatureChangeHistory`（append-only，記 who/when/source）。
- UI：`/ui/features` 訂閱/退訂；受閘門頁（`/ui/coupons`、`/ui/shop`）未開通顯示「前往訂閱」；
  admin 租戶詳情可逐功能開關。導覽列加「進階功能」。

| 環境變數 | 說明 | 預設 |
|------|------|------|
| `SAAS_FEATURES_DEFAULT_ENABLED` | 無設定列時的預設（True=向後相容預設開；False=嚴格 freemium 預設關需訂閱） | `true` |
| `SAAS_FEATURE_MONTHLY_PRICE_CENTS` | 進階功能月費（分） | `20000` |

### 真實金流：綠界 ECPay AIO

商品訂單付款可接真實**綠界 ECPay**（`SAAS_PAYMENT_PROVIDER=ecpay`）。流程：

1. 顧客 LINE「購買」→ 取得付款連結（指向 `/payments/ecpay/checkout/{order_id}`）。
2. 該頁產生唯一 `MerchantTradeNo`（寫回訂單）+ CheckMacValue，**自動 submit** 表單到綠界付款頁。
3. 顧客付款後，綠界 server 回調 `POST /payments/ecpay/callback`：系統**先驗 CheckMacValue 再交叉驗
   金額**，通過才把訂單標記 `paid`，回純文字 `1|OK`（冪等：重送仍 `1|OK`）。

- **CheckMacValue** 逐位元組對齊綠界官方 Python SDK（`quote_plus(safe='-_.!*()').lower()` → SHA256 大寫），
  以官方演算法的 golden 向量鎖定測試；不引入 ECPay SDK 當 runtime 依賴。
- 回調端點公開、無 JWT/rate-limit；**安全完全靠 CheckMacValue 驗簽 + 金額交叉驗證**。

| 環境變數 | 說明 | 預設 |
|------|------|------|
| `SAAS_PAYMENT_PROVIDER` | `stub`（預設）或 `ecpay` | `stub` |
| `SAAS_PUBLIC_BASE_URL` | 對外網址（組綠界 ReturnURL/checkout 絕對網址）；ecpay 模式必填 | `""` |
| `SAAS_ECPAY_MERCHANT_ID` | 商店代號（預設綠界公開測試值） | `2000132` |
| `SAAS_ECPAY_HASH_KEY` | HashKey（預設測試值，正式請覆寫） | `5294y06JbISpM5x9` |
| `SAAS_ECPAY_HASH_IV` | HashIV（預設測試值，正式請覆寫） | `v77hoKGq4kWxNNIS` |
| `SAAS_ECPAY_ENV` | `stage`（測試）/ `prod`（正式） | `stage` |

正式上線請由平台管理員到 `/ui/admin/payment-settings` 填入綠界正式金鑰並切換
Production；HashKey/HashIV 會加密保存且立即套用到商品、定金、方案與進階功能訂閱。
`SAAS_ECPAY_*` 僅為後台未設定時的備援。對外仍需設定可達的 HTTPS
`SAAS_PUBLIC_BASE_URL`，供綠界付款通知回呼。

### 定期定額訂閱月費（綠界信用卡定期定額）

`SAAS_PAYMENT_PROVIDER=ecpay` 時，進階功能月費訂閱改走綠界**信用卡定期定額**真實每月自動扣款
（`stub` 模式維持即時開通）。流程：

1. 店家 `POST /billing/features/{feature}/subscribe`（或 `/ui/features`）→ 建立 pending 訂閱、
   回 `checkout_url`（綠界付款頁）；**功能此時尚未開通**。
2. 該頁自動 submit 定期定額表單（`ChoosePayment=Credit`、`PeriodType=M`、`Frequency=1`、
   `ExecTimes=99`、`PeriodReturnURL`）到綠界 → 店家完成首期授權。
3. 綠界回調 `POST /payments/ecpay/subscribe-callback`（首期 ReturnURL）：驗簽 → `RtnCode==1`
   **才開通功能**。之後每月自動扣款並回調 `POST /payments/ecpay/period-callback`：成功維持開通、
   失敗關閉。
4. 退訂 `POST /billing/features/{feature}/unsubscribe`：**先呼叫綠界 `CreditCardPeriodAction`
   (`Action=Cancel`) 真的停掉後續扣款**，再關閉功能；停扣 API 失敗時仍關功能但把訂閱標
   `cancel_failed`（待 ops 重試），絕不放任繼續扣卡。

| 環境變數 | 說明 | 預設 |
|------|------|------|
| `SAAS_FEATURE_MONTHLY_PRICE_CENTS` | 每期扣款金額（分） | `20000` |
| `SAAS_ECPAY_PERIOD_EXEC_TIMES` | 定期定額執行次數（月扣上限 99≈長期） | `99` |

> **範圍**：商品訂單為一次性付款；進階功能訂閱為定期定額。`ExecTimes=99` 屆滿（約 8 年）自動停、
> 需重訂。`PeriodReturnURL` 須對外可達。CheckMacValue / 驗簽 / 停扣 API 皆沿用同一 `EcpayClient`。

## Production / 多 worker 部署（橫向擴展）

預設單 process 即可跑；要承載更多流量時，用多 worker / 多機橫向擴展。以下三點是
多 worker 安全的必要條件。

### 0. 一鍵容器化（docker-compose，已內建上述三項）

repo 附 `Dockerfile` + `docker-compose.yml`，一鍵起 **PostgreSQL + Redis + 多 worker
API（gunicorn）+ ops 排程器**，且已預設 `SAAS_RATE_LIMIT_BACKEND=redis` 與 PG：

```bash
cp .env.example .env          # 改 SAAS_SECRET_KEY / SAAS_LINE_CHANNEL_ENCRYPT_KEY / 密碼
docker compose up -d --build  # web(:8099) + db + redis + scheduler
curl http://127.0.0.1:8099/healthz   # {"status":"ok","db":"ok","rate_limit_backend":"redis"}
docker compose run --rm web seed     # （選用）灌示範資料 → /ui/login、/p/demo
```

`scheduler` 服務即「排程單實例」（見下方第 4 點），勿 `scale`。手動部署細節見以下各點。

### 1. 跑多 worker

```bash
# uvicorn 內建多 worker
uvicorn saas_mvp.app:app --host 0.0.0.0 --port 8000 --workers 4

# 或 gunicorn + uvicorn worker class（建議 production）
gunicorn saas_mvp.app:app -k uvicorn.workers.UvicornWorker -w 4 -b 0.0.0.0:8000
```

`--workers N` 會 fork N 個獨立 process，**不共享記憶體**。下面兩項就是為了讓
跨 process 的共享狀態仍然正確。

### 2. 用 PostgreSQL（多 worker 不要用 SQLite）

所有並發臨界區（每日用量配額 `quota.py`、月度推播額度 `services/push_quota.py`、
預約容量 `services/booking.book_slot`、優惠券核銷、訂單扣庫存）都靠
**`SELECT … FOR UPDATE` 行鎖**序列化，消除 read-check-write 競態。SQLite 的鎖是
**連線/檔案層級**（`FOR UPDATE` 實質被忽略、寫入互斥且易 `database is locked`），
無法支撐多 worker 的行級並發。多 worker 部署**必須**用 PostgreSQL：

```bash
SAAS_DATABASE_URL=postgresql+psycopg://user:pass@db-host:5432/saas
```

LINE webhook 雖把事件處理丟進 Starlette `BackgroundTasks`（in-process），但跨
worker 仍安全：每個事件以 `line_webhook_events` 的 `webhookEventId` 唯一鍵
**INSERT-claim 去重**（撞鍵即視為已被別的 worker 認領；失敗重試走 `FOR UPDATE`），
因此重送 / 多 worker 重複投遞都只會被處理一次。

### 3. 用 Redis 限流後端（跨 worker 共享）

限流器預設 in-memory（每 process 各一份計數，多 worker 下等於把限額放大 N 倍）。
production 多 worker 請改用 Redis 後端，讓所有 worker 共享同一份滑動視窗：

```bash
SAAS_RATE_LIMIT_BACKEND=redis
SAAS_REDIS_URL=redis://redis-host:6379/0
pip install -e ".[redis]"     # 安裝選用的 redis 套件
```

Redis 後端以 sorted-set + Lua script（`EVAL`）做**原子**滑動視窗，跨 process /
跨機共享、無 TOCTOU。若未裝 `redis` 套件、`SAAS_REDIS_URL` 留空或連不上，會記
warning 並**自動 fallback 回 in-memory**（不會讓服務啟動失敗，但此時限額不跨
worker 共享）。

### 4. ops/ cron 腳本按「排程單實例」跑，不要每個 worker 跑

提醒派送、預約異動通知、行銷活動等在 `ops/` 下是**獨立 cron 腳本**（見上方
各章節），應由排程器（cron / k8s CronJob）**單一實例**觸發，**不要**綁進 web
worker 生命週期——否則 N 個 worker 會各跑一次造成重複推播。各腳本自身也以
`SELECT … FOR UPDATE` + 鎖內重驗 `status=='pending'` 做冪等去重。

### 健康檢查 / 就緒探針

`GET /healthz`（無認證、低成本）供 load balancer / k8s probe 使用：回
`{"status","db","rate_limit_backend"}`，DB 跑一次 `SELECT 1`，不可用時回 `503`
讓 LB 把該 worker 拉出輪替。既有 `GET /` root 端點契約不變。
