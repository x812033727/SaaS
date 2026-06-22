"""公開店家頁（public business profile）router — 訪客可見、無需認證。

prefix /p、include_in_schema=False。GET /p/{slug} 伺服器渲染 5 分頁：
服務 / 商品 / 作品集 / 優惠券 / 聯絡。

安全：以 slug 解析（跨租戶查詢但只回單一租戶資料），未發佈或不存在一律 404，
不洩漏租戶 ID 存在性。所有下游資料一律以該 profile.tenant_id 取得，從不混入
其他租戶資料。每個服務附「加入 Google 行事曆」按鈕（calendar_ics.google_calendar_url）。
"""

from __future__ import annotations

import datetime
import json
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from saas_mvp.auth.ratelimit import public_limiter
from saas_mvp.db import get_db
from saas_mvp.services import catalog as catalog_svc
from saas_mvp.services import coupons as coupons_svc
from saas_mvp.services import portfolio as portfolio_svc
from saas_mvp.services import profile as profile_svc
from saas_mvp.services import shop as shop_svc
from saas_mvp.services.calendar_ics import google_calendar_url

_PKG_DIR = Path(__file__).resolve().parent.parent  # src/saas_mvp
templates = Jinja2Templates(directory=str(_PKG_DIR / "templates"))

router = APIRouter(
    prefix="/p",
    tags=["public"],
    include_in_schema=False,
    dependencies=[Depends(public_limiter)],
)


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _safe_http_url(value: str | None) -> str | None:
    """只放行 http/https 連結；其餘 scheme（如 javascript:、data:）一律丟棄回 None。

    防止公開頁把 social_links / banner_url 內的 javascript: 等危險 URI 直接
    渲染成 href / src 造成 XSS。比對前 strip 並小寫化 scheme。
    """
    if not value:
        return None
    candidate = value.strip()
    lowered = candidate.lower()
    if lowered.startswith("http://") or lowered.startswith("https://"):
        return candidate
    return None


def _is_coupon_active(c, now: datetime.datetime) -> bool:
    """券是否在公開可見的有效狀態（啟用 + 在有效期間內）。"""
    if not c.is_active:
        return False
    af = c.active_from.replace(tzinfo=None) if c.active_from else None
    au = c.active_until.replace(tzinfo=None) if c.active_until else None
    n = now.replace(tzinfo=None)
    if af is not None and n < af:
        return False
    if au is not None and n > au:
        return False
    return True


@router.get("/{slug}", response_class=HTMLResponse)
def public_profile(
    slug: str,
    request: Request,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    profile = profile_svc.get_by_slug(db, slug)
    if profile is None:
        # 未發佈或不存在一律 404（不洩漏存在性）。
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

    tenant_id = profile.tenant_id
    now = _utcnow()

    # 服務分頁：附「加入 Google 行事曆」連結。服務本身無固定時段，
    # 以「下一個整點起算 + 服務時長」當示範時間，讓訪客一鍵建草稿事件。
    display_name = profile.display_name or (
        profile.tenant.name if profile.tenant else slug
    )
    services_raw = catalog_svc.list_services(db, tenant_id=tenant_id)
    slot_start = (now + datetime.timedelta(hours=1)).replace(
        minute=0, second=0, microsecond=0
    )
    services = []
    for s in services_raw:
        if not s.is_active:
            continue
        gcal = google_calendar_url(
            title=f"{display_name} — {s.name}",
            start=slot_start,
            end=slot_start + datetime.timedelta(minutes=s.duration_minutes or 60),
            details="預約服務",
            location=display_name,
        )
        services.append({
            "name": s.name,
            "duration_minutes": s.duration_minutes,
            "price_cents": s.price_cents,
            "gcal_url": gcal,
        })

    products = shop_svc.list_products(db, tenant_id=tenant_id, active_only=True)
    portfolio = portfolio_svc.list_public(db, tenant_id)
    coupons = [
        c for c in coupons_svc.list_coupons(db, tenant_id=tenant_id)
        if _is_coupon_active(c, now)
    ]

    # social_links 為 JSON 字串；解析失敗則視為無。
    social: dict = {}
    if profile.social_links:
        try:
            parsed = json.loads(profile.social_links)
            if isinstance(parsed, dict):
                # 僅保留 http/https 連結；javascript: 等危險 scheme 丟棄（防 XSS）。
                for k, v in parsed.items():
                    safe = _safe_http_url(str(v))
                    if safe is not None:
                        social[str(k)] = safe
        except (json.JSONDecodeError, TypeError, ValueError):
            social = {}

    # banner_url 同樣只放行 http/https（模板以此安全值渲染 <img src> / og:image）。
    safe_banner_url = _safe_http_url(profile.banner_url)

    seo_title = profile.seo_title or display_name
    seo_description = profile.seo_description or (profile.intro or display_name)

    return templates.TemplateResponse(
        "public/profile.html",
        {
            "request": request,
            "profile": profile,
            "display_name": display_name,
            "services": services,
            "products": products,
            "portfolio": portfolio,
            "coupons": coupons,
            "social": social,
            "banner_url": safe_banner_url,
            "seo_title": seo_title,
            "seo_description": seo_description,
        },
    )
