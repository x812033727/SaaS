"""伺服器渲染管理 UI（Jinja2 + HTMX，同源伺服）。

兩個使用層級：
  - 店家自助（require_ui_user）：dashboard、LINE 設定、連線測試、店家類型。
  - 平台管理（require_ui_admin）：跨店家 bot 總覽、單一租戶管理。

認證：登入後把 JWT 放進 httpOnly cookie（SameSite=Lax）；UI 路由用獨立的
`require_ui_user` / `require_ui_admin`（cookie 路徑），完全不碰 API 的
`get_current_actor`（仍只認 header）。所有資料一律走既有 service 層，回應永不
輸出明文 channel_secret / access_token，只揭露 has_* 布林與 credential_status。

CSRF：double-submit cookie token——登入時發非 httpOnly 的 csrf_token cookie，
所有 /ui 非 GET 請求須以 X-CSRF-Token header（HTMX 由 base.html body 級
hx-headers 自動帶）或表單欄位 csrf_token 回傳同值，router 級依賴
_enforce_csrf 常數時間比對，不符回 403。SAAS_UI_CSRF_ENABLED=false 可關閉
（僅測試環境）。
"""

from __future__ import annotations

from saas_mvp.auth.ratelimit import (  # noqa: F401 — 測試 patch 點(test_onboarding_email)
    email_identity_limiter,
    email_ip_limiter,
    email_user_limiter,
)
from saas_mvp.routers.ui._shared import (  # noqa: F401 — app.py/oauth.py 引用面
    UICSRFInvalid,
    _enforce_csrf,
    _set_auth_cookie,
    _set_mfa_pending_cookie,
    maybe_renew_ui_cookie,
    router,
    templates,
)

# ⚠️ 按原檔區段順序 import — 路由註冊順序即路徑匹配順序,不可重排(P4 純搬移)。
# R12-C3a:已遷 console 的頁面模組實體刪除(booking/customers/automation/
# commerce/marketing/resources/pos/calendar_ui);GET 由 ui_retirement 302。
from saas_mvp.routers.ui import session, billing, account, admin, org, crm, assistant  # noqa: E402, F401
