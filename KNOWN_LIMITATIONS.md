# 已知限制（Known Limitations）

本次以「核心可用、帶已知限制」版本交付；以下項目尚未滿足,已留待後續改良:

- [ ] TOTP 2FA(R5-D2)兩個刻意取捨:①**API key 認證不受 2FA 影響**（`X-API-Key` / Bearer `myapp_*` 為程式整合正道,啟用 2FA 的帳號請以 API key 供程式使用,`/auth/token` 對 2FA 帳號一律要求 `otp` 欄位）;②驗證視窗 ±30 秒內**同一組 TOTP 碼可重複使用**（未追蹤 last-used timecode;有 per-user 10 次/5 分鐘節流兜底,風險=肩窺者 30 秒內重放,接受）。

- [x] `base.py` 含 `TranslationResult`，為 `@dataclass(frozen=True, slots=True)`，欄位含 `text: str`、`detected_lang: str | None`、`skipped: bool`；`Translator.translate` 型別標註回傳 `TranslationResult`（見 `translation/base.py`，早已實作，此清單先前未同步更新）。
- [x] 管理 UI（`/ui/*`）的 CSRF 防護：已實作 double-submit cookie token——登入時發放非 httpOnly 的 `csrf_token` cookie，所有 `/ui` 非 GET 請求須以 `X-CSRF-Token` header（HTMX 由 `base.html` body 級 `hx-headers` 自動帶出）或表單 hidden field 回傳同值，router 級依賴常數時間比對，不符回 403（見 `routers/ui/_shared.py:_enforce_csrf`；`SAAS_UI_CSRF_ENABLED=false` 僅供測試環境關閉）。
- [x] 速率限制後端：限流器（`auth/ratelimit.py`）採**可插拔後端**——預設 in-memory（dev/test/單 worker），多 worker 部署可設定 `SAAS_RATE_LIMIT_BACKEND=redis` + `SAAS_REDIS_URL`，改用 Redis sorted-set + Lua 原子滑動視窗，跨 worker / 跨機共享同一份計數（見 `auth/ratelimit_backend.py`）。Redis 不可用時自動 fallback in-memory，不阻擋啟動。部署細節見 README「Production / 多 worker 部署」。
