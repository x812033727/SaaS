"""伺服器渲染管理 UI（Jinja2 + HTMX，同源伺服）。

兩個使用層級：
  - 店家自助（require_ui_user）：dashboard、LINE 設定、連線測試、店家類型。
  - 平台管理（require_ui_admin）：跨店家 bot 總覽、單一租戶管理。

認證：登入後把 JWT 放進 httpOnly cookie（SameSite=Lax）；UI 路由用獨立的
`require_ui_user` / `require_ui_admin`（cookie 路徑），完全不碰 API 的
`get_current_actor`（仍只認 header）。所有資料一律走既有 service 層，回應永不
輸出明文 channel_secret / access_token，只揭露 has_* 布林與 credential_status。

CSRF（已知限制）：MVP 僅靠 SameSite=Lax + 同源，未實作 per-request CSRF token。
若日後將 UI 暴露給不可信的同源來源，應加上 double-submit cookie token。
"""

from __future__ import annotations

import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Query, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from saas_mvp.config import settings
from saas_mvp.deps import Actor, get_db, require_ui_admin, require_ui_user
from saas_mvp.auth.dependencies import _UI_COOKIE_NAME
from saas_mvp.auth.security import create_access_token, hash_password, verify_password
from saas_mvp.line_client import (
    LineBotInfoClient,
    LineRichMenuClient,
    get_bot_info_client,
    get_rich_menu_client,
)
from saas_mvp.models.tenant import Tenant, normalize_store_type
from saas_mvp.models.user import User
from saas_mvp.quota import get_quota_status
from saas_mvp.routers.line_webhook import webhook_url_for
from saas_mvp.services import admin as admin_svc
from saas_mvp.services import analytics as analytics_svc
from saas_mvp.services import billing as billing_svc
from saas_mvp.services import booking as booking_svc
from saas_mvp.services import coupons as coupons_svc
from saas_mvp.services import features as features_svc
from saas_mvp.services import customers as customers_svc
from saas_mvp.services import line_config as line_config_svc
from saas_mvp.services import rich_menu as rich_menu_svc
from saas_mvp.services import shop as shop_svc
from saas_mvp.services import slots as slots_svc
from fastapi import HTTPException

_PKG_DIR = Path(__file__).resolve().parent.parent  # src/saas_mvp
templates = Jinja2Templates(directory=str(_PKG_DIR / "templates"))

router = APIRouter(prefix="/ui", tags=["ui"], include_in_schema=False)


# ── 共用工具 ────────────────────────────────────────────────────────────────

def _set_auth_cookie(response: Response, token: str) -> None:
    """把 JWT 寫入 httpOnly cookie；prod 加 Secure，dev/test 不加（方便本機/測試）。"""
    response.set_cookie(
        key=_UI_COOKIE_NAME,
        value=token,
        httponly=True,
        samesite="lax",
        secure=settings.env not in ("dev", "test"),
        max_age=settings.access_token_expire_minutes * 60,
        path="/",
    )


def _ctx(request: Request, actor: Actor | None = None, **extra) -> dict:
    """組 template context；Jinja2Templates 需要 request。"""
    base = {
        "request": request,
        "current_user": actor.user if actor else None,
        "is_admin": bool(actor and actor.user.is_admin),
    }
    base.update(extra)
    return base


def _is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request", "").lower() == "true"


def _line_config_or_none(db: Session, tenant_id: int) -> dict | None:
    """取 LINE 設定（遮罩 dict）；未設定回 None（吞 404，與 dashboard 一致）。"""
    try:
        return line_config_svc.get_line_config(db, tenant_id)
    except HTTPException as exc:
        if exc.status_code == status.HTTP_404_NOT_FOUND:
            return None
        raise


# ── 公開：登入 / 註冊 / 登出 ───────────────────────────────────────────────────

@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    return templates.TemplateResponse("login.html", _ctx(request))


@router.post("/login", response_class=HTMLResponse)
def login_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    user = db.query(User).filter(User.email == email).first()
    if not user or not verify_password(password, user.hashed_password):
        # 統一錯誤訊息，避免帳號列舉
        return templates.TemplateResponse(
            "login.html",
            _ctx(request, error="電子郵件或密碼錯誤"),
            status_code=status.HTTP_401_UNAUTHORIZED,
        )
    token = create_access_token(user_id=user.id, tenant_id=user.tenant_id)
    resp = RedirectResponse("/ui/", status_code=status.HTTP_303_SEE_OTHER)
    _set_auth_cookie(resp, token)
    return resp


@router.get("/register", response_class=HTMLResponse)
def register_form(request: Request):
    return templates.TemplateResponse("register.html", _ctx(request))


@router.post("/register", response_class=HTMLResponse)
def register_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    tenant_name: str = Form(...),
    db: Session = Depends(get_db),
):
    # 與 auth.register 同款守衛：重複 email、租戶名唯一、密碼長度
    def _err(msg: str):
        return templates.TemplateResponse(
            "register.html",
            _ctx(request, error=msg, email=email, tenant_name=tenant_name),
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    if len(password) < 8:
        return _err("密碼至少需 8 個字元")
    if db.query(User).filter(User.email == email).first():
        return _err("此電子郵件已註冊")
    if db.query(Tenant).filter(Tenant.name == tenant_name).first():
        return _err("店家名稱已被使用，請換一個唯一名稱")

    tenant = Tenant(name=tenant_name, plan="free")
    db.add(tenant)
    db.flush()
    user = User(email=email, hashed_password=hash_password(password), tenant_id=tenant.id)
    db.add(user)
    db.commit()
    db.refresh(user)

    token = create_access_token(user_id=user.id, tenant_id=user.tenant_id)
    resp = RedirectResponse("/ui/", status_code=status.HTTP_303_SEE_OTHER)
    _set_auth_cookie(resp, token)
    return resp


@router.get("/logout")
def logout():
    resp = RedirectResponse("/ui/login", status_code=status.HTTP_303_SEE_OTHER)
    resp.delete_cookie(_UI_COOKIE_NAME, path="/")
    return resp


# ── 店家自助 ────────────────────────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    tid = actor.user.tenant_id
    tenant = db.get(Tenant, tid)
    line_config = _line_config_or_none(db, tid)
    usage = get_quota_status(db, tid, tenant.plan)
    return templates.TemplateResponse(
        "dashboard.html",
        _ctx(request, actor, tenant=tenant, line_config=line_config, usage=usage),
    )


@router.get("/line-config", response_class=HTMLResponse)
def line_config_page(
    request: Request,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    tid = actor.user.tenant_id
    cfg = _line_config_or_none(db, tid)
    return templates.TemplateResponse(
        "line_config.html",
        _ctx(request, actor, cfg=cfg, webhook_url=webhook_url_for(tid)),
    )


@router.post("/line-config", response_class=HTMLResponse)
def line_config_save(
    request: Request,
    channel_secret: str = Form(..., max_length=64),
    access_token: str = Form(..., max_length=1024),
    default_target_lang: str = Form("zh-TW"),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
    bot_info_client: LineBotInfoClient = Depends(get_bot_info_client),
):
    tid = actor.user.tenant_id
    try:
        cfg = line_config_svc.upsert_line_config(
            db, tid,
            channel_secret=channel_secret,
            access_token=access_token,
            default_target_lang=default_target_lang,
            bot_info_client=bot_info_client,
        )
    except HTTPException as exc:
        return templates.TemplateResponse(
            "_line_config_status.html",
            _ctx(request, actor, cfg=None, webhook_url=webhook_url_for(tid), error=str(exc.detail)),
            status_code=exc.status_code,
        )
    return templates.TemplateResponse(
        "_line_config_status.html",
        _ctx(request, actor, cfg=cfg, webhook_url=webhook_url_for(tid)),
    )


@router.post("/line-config/verify", response_class=HTMLResponse)
def line_config_verify(
    request: Request,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
    bot_info_client: LineBotInfoClient = Depends(get_bot_info_client),
):
    tid = actor.user.tenant_id
    try:
        cfg = line_config_svc.verify_line_config(db, tid, bot_info_client=bot_info_client)
    except HTTPException as exc:
        return templates.TemplateResponse(
            "_line_config_status.html",
            _ctx(request, actor, cfg=None, webhook_url=webhook_url_for(tid), error=str(exc.detail)),
            status_code=exc.status_code,
        )
    return templates.TemplateResponse(
        "_line_config_status.html",
        _ctx(request, actor, cfg=cfg, webhook_url=webhook_url_for(tid)),
    )


@router.post("/line-config/delete", response_class=HTMLResponse)
def line_config_delete(
    request: Request,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    tid = actor.user.tenant_id
    try:
        line_config_svc.delete_line_config(db, tid)
    except HTTPException:
        pass  # 不存在即視為已刪除
    return templates.TemplateResponse(
        "_line_config_status.html",
        _ctx(request, actor, cfg=None, webhook_url=webhook_url_for(tid)),
    )


@router.post("/settings", response_class=HTMLResponse)
def settings_save(
    request: Request,
    store_type: str = Form("", max_length=32),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    tenant = db.get(Tenant, actor.user.tenant_id)
    tenant.store_type = normalize_store_type(store_type)
    db.commit()
    db.refresh(tenant)
    return templates.TemplateResponse(
        "_settings.html",
        _ctx(request, actor, tenant=tenant, saved=True),
    )


# ── 平台管理 ────────────────────────────────────────────────────────────────

@router.get("/admin/bots", response_class=HTMLResponse)
def admin_bots(
    request: Request,
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    store_type: str | None = Query(None),
    is_active: bool | None = Query(None),
    uncategorized: bool = Query(False),
    actor: Actor = Depends(require_ui_admin),
    db: Session = Depends(get_db),
):
    # 空字串 store_type 視為「未指定」
    store_type = store_type or None
    rows = admin_svc.list_line_bots(
        db, skip=skip, limit=limit,
        store_type=store_type, is_active=is_active, uncategorized=uncategorized,
    )
    filters = {
        "store_type": store_type or "",
        "is_active": is_active,
        "uncategorized": uncategorized,
        "skip": skip,
        "limit": limit,
    }
    ctx = _ctx(request, actor, rows=rows, filters=filters)
    # HTMX 篩選請求只回表格 partial
    template = "admin/_bots_table.html" if _is_htmx(request) else "admin/bots.html"
    return templates.TemplateResponse(template, ctx)


@router.get("/admin/tenants/{tenant_id}", response_class=HTMLResponse)
def admin_tenant_detail(
    request: Request,
    tenant_id: int,
    actor: Actor = Depends(require_ui_admin),
    db: Session = Depends(get_db),
):
    tenant = db.get(Tenant, tenant_id)
    if tenant is None:
        return templates.TemplateResponse(
            "admin/tenant_detail.html",
            _ctx(request, actor, tenant=None, not_found=True, tenant_id=tenant_id),
            status_code=status.HTTP_404_NOT_FOUND,
        )
    usage = get_quota_status(db, tenant_id, tenant.plan)
    cfg = _line_config_or_none(db, tenant_id)
    return templates.TemplateResponse(
        "admin/tenant_detail.html",
        _ctx(request, actor, tenant=tenant, usage=usage, cfg=cfg,
             features=features_svc.list_for_tenant(db, tenant_id),
             action_base=f"/ui/admin/tenants/{tenant_id}/line-config"),
    )


@router.post("/admin/tenants/{tenant_id}/features/{feature}", response_class=HTMLResponse)
def admin_set_feature(
    request: Request,
    tenant_id: int,
    feature: str,
    enabled: str = Form(...),
    actor: Actor = Depends(require_ui_admin),
    db: Session = Depends(get_db),
):
    try:
        features_svc.validate_feature(feature)
        features_svc.set_enabled(
            db, tenant_id, feature, enabled == "true",
            actor_user_id=actor.user.id, source="admin",
        )
    except features_svc.UnknownFeatureError:
        pass
    return templates.TemplateResponse(
        "admin/_tenant_features.html",
        _ctx(request, actor, tenant_id=tenant_id,
             features=features_svc.list_for_tenant(db, tenant_id)),
    )


@router.post("/admin/tenants/{tenant_id}/patch", response_class=HTMLResponse)
def admin_tenant_patch(
    request: Request,
    tenant_id: int,
    plan: str = Form(...),
    is_active: str = Form(...),
    store_type: str = Form("", max_length=32),
    actor: Actor = Depends(require_ui_admin),
    db: Session = Depends(get_db),
):
    try:
        result = admin_svc.patch_tenant(
            db, tenant_id,
            is_active=(is_active == "true"),
            plan=plan,
            actor_user_id=actor.user.id,
            store_type=store_type,
            store_type_provided=True,  # 表單一律帶 store_type 欄位
        )
    except HTTPException as exc:
        tenant = db.get(Tenant, tenant_id)
        return templates.TemplateResponse(
            "admin/_tenant_summary.html",
            _ctx(request, actor, tenant=tenant, error=str(exc.detail)),
            status_code=exc.status_code,
        )
    tenant = db.get(Tenant, tenant_id)
    return templates.TemplateResponse(
        "admin/_tenant_summary.html",
        _ctx(request, actor, tenant=tenant, saved=True),
    )


@router.post("/admin/tenants/{tenant_id}/line-config", response_class=HTMLResponse)
def admin_line_config_save(
    request: Request,
    tenant_id: int,
    channel_secret: str = Form(..., max_length=64),
    access_token: str = Form(..., max_length=1024),
    default_target_lang: str = Form("zh-TW"),
    actor: Actor = Depends(require_ui_admin),
    db: Session = Depends(get_db),
    bot_info_client: LineBotInfoClient = Depends(get_bot_info_client),
):
    try:
        cfg = line_config_svc.upsert_line_config(
            db, tenant_id,
            channel_secret=channel_secret,
            access_token=access_token,
            default_target_lang=default_target_lang,
            bot_info_client=bot_info_client,
        )
    except HTTPException as exc:
        return templates.TemplateResponse(
            "_line_config_status.html",
            _ctx(request, actor, cfg=None, webhook_url=webhook_url_for(tenant_id),
                 action_base=f"/ui/admin/tenants/{tenant_id}/line-config", error=str(exc.detail)),
            status_code=exc.status_code,
        )
    return templates.TemplateResponse(
        "_line_config_status.html",
        _ctx(request, actor, cfg=cfg, webhook_url=webhook_url_for(tenant_id),
             action_base=f"/ui/admin/tenants/{tenant_id}/line-config"),
    )


@router.post("/admin/tenants/{tenant_id}/line-config/verify", response_class=HTMLResponse)
def admin_line_config_verify(
    request: Request,
    tenant_id: int,
    actor: Actor = Depends(require_ui_admin),
    db: Session = Depends(get_db),
    bot_info_client: LineBotInfoClient = Depends(get_bot_info_client),
):
    try:
        cfg = line_config_svc.verify_line_config(db, tenant_id, bot_info_client=bot_info_client)
    except HTTPException as exc:
        return templates.TemplateResponse(
            "_line_config_status.html",
            _ctx(request, actor, cfg=None, webhook_url=webhook_url_for(tenant_id),
                 action_base=f"/ui/admin/tenants/{tenant_id}/line-config", error=str(exc.detail)),
            status_code=exc.status_code,
        )
    return templates.TemplateResponse(
        "_line_config_status.html",
        _ctx(request, actor, cfg=cfg, webhook_url=webhook_url_for(tenant_id),
             action_base=f"/ui/admin/tenants/{tenant_id}/line-config"),
    )


@router.post("/admin/tenants/{tenant_id}/line-config/delete", response_class=HTMLResponse)
def admin_line_config_delete(
    request: Request,
    tenant_id: int,
    actor: Actor = Depends(require_ui_admin),
    db: Session = Depends(get_db),
):
    try:
        line_config_svc.delete_line_config(db, tenant_id)
    except HTTPException:
        pass
    return templates.TemplateResponse(
        "_line_config_status.html",
        _ctx(request, actor, cfg=None, webhook_url=webhook_url_for(tenant_id),
             action_base=f"/ui/admin/tenants/{tenant_id}/line-config"),
    )


# ── 店家自助：預約管理 ────────────────────────────────────────────────────────

def _parse_slot_start(value: str) -> datetime.datetime:
    """解析 datetime-local 表單字串（無時區）→ 視為 UTC 的 tz-aware datetime。"""
    dt = datetime.datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt


def _booking_ctx(request: Request, actor: Actor, db: Session, **extra) -> dict:
    """組預約頁 context：bot_mode、時段、預約、顧客。"""
    tid = actor.user.tenant_id
    cfg = _line_config_or_none(db, tid)
    return _ctx(
        request,
        actor,
        cfg=cfg,
        bot_mode=(cfg or {}).get("bot_mode", "translation"),
        has_line_config=cfg is not None,
        slots=slots_svc.list_slots(db, tenant_id=tid),
        reservations=booking_svc.list_reservations(db, tenant_id=tid),
        customers=customers_svc.list_customers(db, tenant_id=tid),
        **extra,
    )


@router.get("/booking", response_class=HTMLResponse)
def booking_page(
    request: Request,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    return templates.TemplateResponse(
        "booking.html", _booking_ctx(request, actor, db)
    )


@router.post("/booking/bot-mode", response_class=HTMLResponse)
def booking_set_bot_mode(
    request: Request,
    bot_mode: str = Form(...),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    tid = actor.user.tenant_id
    error = None
    try:
        line_config_svc.set_bot_mode(db, tid, bot_mode)
    except HTTPException as exc:
        error = str(exc.detail)
    return templates.TemplateResponse(
        "_booking_botmode.html", _booking_ctx(request, actor, db, error=error)
    )


@router.post("/booking/slots", response_class=HTMLResponse)
def booking_create_slot(
    request: Request,
    slot_start: str = Form(...),
    max_capacity: int = Form(...),
    walkin_reserved: int = Form(0),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    tid = actor.user.tenant_id
    error = None
    try:
        slots_svc.create_slot(
            db,
            tenant_id=tid,
            slot_start=_parse_slot_start(slot_start),
            max_capacity=max_capacity,
            walkin_reserved=walkin_reserved,
        )
    except HTTPException as exc:
        error = str(exc.detail)
    except ValueError:
        error = "時段時間格式錯誤"
    return templates.TemplateResponse(
        "_booking_slots.html", _booking_ctx(request, actor, db, error=error)
    )


@router.post("/booking/slots/{slot_id}/deactivate", response_class=HTMLResponse)
def booking_deactivate_slot(
    request: Request,
    slot_id: int,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    tid = actor.user.tenant_id
    try:
        slots_svc.deactivate_slot(db, tenant_id=tid, slot_id=slot_id)
    except HTTPException:
        pass
    return templates.TemplateResponse(
        "_booking_slots.html", _booking_ctx(request, actor, db)
    )


@router.post("/booking/reservations/{reservation_id}/cancel", response_class=HTMLResponse)
def booking_cancel_reservation(
    request: Request,
    reservation_id: int,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    tid = actor.user.tenant_id
    try:
        booking_svc.cancel_reservation(
            db, tenant_id=tid, reservation_id=reservation_id
        )
    except booking_svc.ReservationNotFoundError:
        pass
    return templates.TemplateResponse(
        "_booking_reservations.html", _booking_ctx(request, actor, db)
    )


@router.post("/booking/reservations/{reservation_id}/attendance", response_class=HTMLResponse)
def booking_mark_attendance(
    request: Request,
    reservation_id: int,
    attended: str = Form(...),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    tid = actor.user.tenant_id
    try:
        booking_svc.mark_attendance(
            db, tenant_id=tid, reservation_id=reservation_id,
            attended=(attended == "true"),
        )
    except booking_svc.ReservationNotFoundError:
        pass
    return templates.TemplateResponse(
        "_booking_reservations.html", _booking_ctx(request, actor, db)
    )


# ── 店家自助：商品銷售 ────────────────────────────────────────────────────────

def _shop_ctx(request: Request, actor: Actor, db: Session, **extra) -> dict:
    tid = actor.user.tenant_id
    return _ctx(
        request, actor,
        products=shop_svc.list_products(db, tenant_id=tid),
        orders=shop_svc.list_orders(db, tenant_id=tid),
        **extra,
    )


def _feature_locked(request: Request, actor: Actor, feature: str, label: str):
    return templates.TemplateResponse(
        "feature_locked.html",
        _ctx(request, actor, feature=feature, feature_label=label),
    )


@router.get("/shop", response_class=HTMLResponse)
def shop_page(
    request: Request,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not features_svc.is_enabled(db, actor.user.tenant_id, features_svc.PRODUCT_SALES):
        return _feature_locked(request, actor, features_svc.PRODUCT_SALES, "商品銷售")
    return templates.TemplateResponse("shop.html", _shop_ctx(request, actor, db))


@router.post("/shop/products", response_class=HTMLResponse)
def shop_create_product(
    request: Request,
    name: str = Form(...),
    price_cents: int = Form(...),
    stock: str = Form(""),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    tid = actor.user.tenant_id
    error = None
    try:
        shop_svc.create_product(
            db, tenant_id=tid, name=name, price_cents=price_cents,
            stock=int(stock) if stock.strip() else None,
        )
    except HTTPException as exc:
        error = str(exc.detail)
    except ValueError:
        error = "庫存需為整數"
    return templates.TemplateResponse("_shop.html", _shop_ctx(request, actor, db, error=error))


@router.post("/shop/products/{product_id}/deactivate", response_class=HTMLResponse)
def shop_deactivate_product(
    request: Request,
    product_id: int,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    tid = actor.user.tenant_id
    try:
        shop_svc.deactivate_product(db, tenant_id=tid, product_id=product_id)
    except HTTPException:
        pass
    return templates.TemplateResponse("_shop.html", _shop_ctx(request, actor, db))


@router.post("/shop/orders/{order_id}/pay", response_class=HTMLResponse)
def shop_pay_order(
    request: Request,
    order_id: int,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    tid = actor.user.tenant_id
    try:
        shop_svc.mark_order_paid(db, tenant_id=tid, order_id=order_id)
    except shop_svc.OrderNotFound:
        pass
    return templates.TemplateResponse("_shop.html", _shop_ctx(request, actor, db))


@router.post("/shop/orders/{order_id}/cancel", response_class=HTMLResponse)
def shop_cancel_order(
    request: Request,
    order_id: int,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    tid = actor.user.tenant_id
    try:
        shop_svc.cancel_order(db, tenant_id=tid, order_id=order_id)
    except shop_svc.OrderNotFound:
        pass
    return templates.TemplateResponse("_shop.html", _shop_ctx(request, actor, db))


# ── 店家自助：報表 ────────────────────────────────────────────────────────────

@router.get("/reports", response_class=HTMLResponse)
def reports_page(
    request: Request,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    tid = actor.user.tenant_id
    return templates.TemplateResponse(
        "reports.html",
        _ctx(
            request, actor,
            summary=analytics_svc.booking_summary(db, tenant_id=tid),
            utilization=analytics_svc.slot_utilization(db, tenant_id=tid),
            top=analytics_svc.top_customers(db, tenant_id=tid, limit=10),
        ),
    )


# ── 店家自助：Rich Menu 圖文選單 ──────────────────────────────────────────────

def _rich_menu_ctx(request: Request, actor: Actor, db: Session, **extra) -> dict:
    tid = actor.user.tenant_id
    cfg = _line_config_or_none(db, tid)
    status_dict = None
    if cfg is not None:
        status_dict = rich_menu_svc.get_rich_menu_status(db, tid)
    return _ctx(
        request,
        actor,
        has_line_config=cfg is not None,
        status=status_dict,
        templates_opts=rich_menu_svc.TEMPLATES,
        themes_opts=list(rich_menu_svc.THEMES.keys()),
        **extra,
    )


@router.get("/rich-menu", response_class=HTMLResponse)
def rich_menu_page(
    request: Request,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    return templates.TemplateResponse(
        "rich_menu.html", _rich_menu_ctx(request, actor, db)
    )


@router.post("/rich-menu/apply", response_class=HTMLResponse)
def rich_menu_apply(
    request: Request,
    template: str = Form(...),
    theme: str = Form(...),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
    rich_menu_client: LineRichMenuClient = Depends(get_rich_menu_client),
):
    tid = actor.user.tenant_id
    error = None
    try:
        rich_menu_svc.apply_rich_menu(
            db, tid, template=template, theme=theme, client=rich_menu_client
        )
    except HTTPException as exc:
        error = str(exc.detail)
    return templates.TemplateResponse(
        "_rich_menu_status.html", _rich_menu_ctx(request, actor, db, error=error)
    )


# ── 店家自助：優惠券 ──────────────────────────────────────────────────────────

def _coupons_ctx(request: Request, actor: Actor, db: Session, **extra) -> dict:
    tid = actor.user.tenant_id
    return _ctx(
        request, actor,
        coupons=coupons_svc.list_coupons(db, tenant_id=tid),
        **extra,
    )


@router.get("/coupons", response_class=HTMLResponse)
def coupons_page(
    request: Request,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not features_svc.is_enabled(db, actor.user.tenant_id, features_svc.COUPON_SYSTEM):
        return _feature_locked(request, actor, features_svc.COUPON_SYSTEM, "優惠券／會員")
    return templates.TemplateResponse("coupons.html", _coupons_ctx(request, actor, db))


@router.post("/coupons", response_class=HTMLResponse)
def coupons_create(
    request: Request,
    code: str = Form(...),
    name: str = Form(...),
    discount_type: str = Form(...),
    discount_value: int = Form(...),
    max_redemptions: str = Form(""),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    tid = actor.user.tenant_id
    error = None
    try:
        coupons_svc.create_coupon(
            db, tenant_id=tid, code=code, name=name,
            discount_type=discount_type, discount_value=discount_value,
            max_redemptions=int(max_redemptions) if max_redemptions.strip() else None,
        )
    except HTTPException as exc:
        error = str(exc.detail)
    except ValueError:
        error = "兌換上限需為整數"
    return templates.TemplateResponse(
        "_coupons_list.html", _coupons_ctx(request, actor, db, error=error)
    )


@router.post("/coupons/{coupon_id}/deactivate", response_class=HTMLResponse)
def coupons_deactivate(
    request: Request,
    coupon_id: int,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    tid = actor.user.tenant_id
    try:
        coupons_svc.deactivate_coupon(db, tenant_id=tid, coupon_id=coupon_id)
    except HTTPException:
        pass
    return templates.TemplateResponse(
        "_coupons_list.html", _coupons_ctx(request, actor, db)
    )


# ── 店家自助：進階功能訂閱 ────────────────────────────────────────────────────

def _features_ctx(request: Request, actor: Actor, db: Session, **extra) -> dict:
    return _ctx(
        request, actor,
        features=features_svc.list_for_tenant(db, actor.user.tenant_id),
        **extra,
    )


@router.get("/features", response_class=HTMLResponse)
def features_page(
    request: Request,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    return templates.TemplateResponse("features.html", _features_ctx(request, actor, db))


@router.post("/features/{feature}/subscribe", response_class=HTMLResponse)
def features_subscribe(
    request: Request,
    feature: str,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    tenant = db.get(Tenant, actor.user.tenant_id)
    try:
        features_svc.validate_feature(feature)
        billing_svc.subscribe_feature(db, tenant, feature, actor.user.id)
    except features_svc.UnknownFeatureError:
        pass
    return templates.TemplateResponse("_features_list.html", _features_ctx(request, actor, db))


@router.post("/features/{feature}/unsubscribe", response_class=HTMLResponse)
def features_unsubscribe(
    request: Request,
    feature: str,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    tenant = db.get(Tenant, actor.user.tenant_id)
    try:
        features_svc.validate_feature(feature)
        billing_svc.unsubscribe_feature(db, tenant, feature, actor.user.id)
    except features_svc.UnknownFeatureError:
        pass
    return templates.TemplateResponse("_features_list.html", _features_ctx(request, actor, db))


@router.post("/rich-menu/clear", response_class=HTMLResponse)
def rich_menu_clear(
    request: Request,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
    rich_menu_client: LineRichMenuClient = Depends(get_rich_menu_client),
):
    tid = actor.user.tenant_id
    error = None
    try:
        rich_menu_svc.clear_rich_menu(db, tid, client=rich_menu_client)
    except HTTPException as exc:
        error = str(exc.detail)
    return templates.TemplateResponse(
        "_rich_menu_status.html", _rich_menu_ctx(request, actor, db, error=error)
    )
