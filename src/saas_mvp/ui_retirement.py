"""/ui 已遷移頁退役(R11-D,使用者核准)。

設計:**可逆重導,不刪碼**。settings.ui_retired=True(prod 預設)時,
GET /ui/<已遷頁> 302 → console 對應頁;豁免 admin/認證/公開/SSO/精靈等
無 console 對應或必須留 server 端的路徑;POST/HTMX 不動(舊分頁過渡期
送出的表單不受影響)。回滾=關旗標,Jinja 頁全數原樣復活。

302(非 301):避免瀏覽器永久快取,旗標關閉即刻生效。
"""

from __future__ import annotations

from fastapi import Request
from fastapi.responses import RedirectResponse

# 前綴比對,最長優先;值=console 路徑(不含網域)
_REDIRECT_MAP: dict[str, str] = {
    "/ui/dashboard": "/console/dashboard",
    "/ui/booking": "/console/reservations",
    "/ui/slots": "/console/slots",
    "/ui/calendar": "/console/calendar",
    "/ui/customers": "/console/customers",
    "/ui/client-forms": "/console/client-forms",
    "/ui/resources": "/console/resources",
    "/ui/notes": "/console/notes",
    "/ui/line-chat": "/console/line-chat",
    "/ui/line-config": "/console/line-settings",
    "/ui/auto-reply": "/console/auto-reply",
    "/ui/rich-menu": "/console/rich-menu",
    "/ui/flex-menu": "/console/flex-menu",
    "/ui/assistant": "/console/faq",
    "/ui/services": "/console/services",
    "/ui/packages": "/console/packages",
    "/ui/gift-cards": "/console/gift-cards",
    "/ui/pos": "/console/pos",
    "/ui/shop": "/console/shop",
    "/ui/coupons": "/console/coupons",
    "/ui/locations": "/console/locations",
    "/ui/staff": "/console/staff",
    "/ui/commissions": "/console/commissions",
    "/ui/campaigns": "/console/campaigns",
    "/ui/notifications": "/console/notifications",
    "/ui/portfolio": "/console/portfolio",
    "/ui/profile": "/console/profile",
    "/ui/reports": "/console/reports",
    "/ui/plan": "/console/plan",
    "/ui/billing": "/console/billing",
    "/ui/features": "/console/features",
    "/ui/api-keys": "/console/api-keys",
    "/ui/members": "/console/members",
    "/ui/account": "/console/account",
}

# 明確豁免(即使落在上表前綴之外也絕不重導):
# - admin*:平台管理刻意留 legacy
# - login/register/logout/join/verify-email/resend-verification:認證+公開流
# - onboarding/settings/gcal:精靈與 OAuth 連接流(無 console 對應)
_EXEMPT_PREFIXES = (
    "/ui/admin",
    "/ui/login",
    "/ui/register",
    "/ui/logout",
    "/ui/join",
    "/ui/verify-email",
    "/ui/resend-verification",
    "/ui/onboarding",
    "/ui/settings",
    "/ui/gcal",
)

_SORTED_PREFIXES = sorted(_REDIRECT_MAP, key=len, reverse=True)


def retirement_redirect(request: Request) -> RedirectResponse | None:
    """已退役 /ui GET → console 302;不適用回 None(照常處理)。"""
    from saas_mvp.config import settings

    if not settings.ui_retired or request.method != "GET":
        return None
    path = request.url.path
    if not path.startswith("/ui"):
        return None
    if any(path.startswith(p) for p in _EXEMPT_PREFIXES):
        return None
    if path in ("/ui", "/ui/"):
        return RedirectResponse("/console/dashboard", status_code=302)
    for prefix in _SORTED_PREFIXES:
        if path == prefix or path.startswith(prefix + "/"):
            return RedirectResponse(_REDIRECT_MAP[prefix], status_code=302)
    return None
