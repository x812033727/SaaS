"""UI 子模組(P4 純搬移自 routers/ui.py):店家自助:商品銷售 + 報表 + Rich Menu + 優惠券 + 進階功能訂閱。"""
from __future__ import annotations

import html

from fastapi import Depends, Form, Query, Request
from fastapi.responses import (
    HTMLResponse,
    Response,
)
from sqlalchemy.orm import Session

from saas_mvp.config import settings
from saas_mvp.deps import (
    Actor,
    get_db,
    require_ui_owner,
    require_ui_user,
)
from saas_mvp.line_client import (
    LineRichMenuClient,
    get_rich_menu_client,
)
from saas_mvp.models.tenant import Tenant
from saas_mvp.services import analytics as analytics_svc
from saas_mvp.services import reporting as reporting_svc
from saas_mvp.services import billing as billing_svc
from saas_mvp.services import coupons as coupons_svc
from saas_mvp.services import features as features_svc
from saas_mvp.services import audit as audit_svc
from saas_mvp.services import order_refund as order_refund_svc
from saas_mvp.services import platform_payment_config as platform_payment_svc
from saas_mvp.services import rich_menu as rich_menu_svc
from saas_mvp.services import shop as shop_svc
from fastapi import HTTPException

from saas_mvp.routers.ui._shared import (
    router, templates, _ctx, _line_config_or_none, _require_ui_feature,
)

# ── 店家自助：商品銷售 ────────────────────────────────────────────────────────


def _shop_ctx(request: Request, actor: Actor, db: Session, **extra) -> dict:
    tid = actor.user.tenant_id
    return _ctx(
        request,
        actor,
        products=shop_svc.list_products(db, tenant_id=tid),
        orders=shop_svc.list_orders(db, tenant_id=tid),
        shop_payment_provider=platform_payment_svc.payment_provider(db, settings),
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
    if not _require_ui_feature(db, actor, features_svc.PRODUCT_SALES):
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
    if not _require_ui_feature(db, actor, features_svc.PRODUCT_SALES):
        return _feature_locked(request, actor, features_svc.PRODUCT_SALES, "商品銷售")
    tid = actor.user.tenant_id
    error = None
    try:
        shop_svc.create_product(
            db,
            tenant_id=tid,
            name=name,
            price_cents=price_cents,
            stock=int(stock) if stock.strip() else None,
        )
    except HTTPException as exc:
        error = str(exc.detail)
    except ValueError:
        error = "庫存需為整數"
    return templates.TemplateResponse(
        "_shop.html", _shop_ctx(request, actor, db, error=error)
    )


@router.post("/shop/products/{product_id}/deactivate", response_class=HTMLResponse)
def shop_deactivate_product(
    request: Request,
    product_id: int,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.PRODUCT_SALES):
        return _feature_locked(request, actor, features_svc.PRODUCT_SALES, "商品銷售")
    tid = actor.user.tenant_id
    error = None
    try:
        shop_svc.deactivate_product(db, tenant_id=tid, product_id=product_id)
    except HTTPException as exc:
        error = str(exc.detail)
    return templates.TemplateResponse(
        "_shop.html", _shop_ctx(request, actor, db, error=error)
    )


@router.get("/shop/products/{product_id}/edit", response_class=HTMLResponse)
def shop_edit_product_form(
    request: Request,
    product_id: int,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.PRODUCT_SALES):
        return _feature_locked(request, actor, features_svc.PRODUCT_SALES, "商品銷售")
    return templates.TemplateResponse(
        "_shop.html", _shop_ctx(request, actor, db, editing_id=product_id)
    )


@router.post("/shop/products/{product_id}/update", response_class=HTMLResponse)
def shop_update_product(
    request: Request,
    product_id: int,
    name: str = Form(...),
    price_cents: int = Form(...),
    stock: str = Form(""),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.PRODUCT_SALES):
        return _feature_locked(request, actor, features_svc.PRODUCT_SALES, "商品銷售")
    tid = actor.user.tenant_id
    error = None
    editing_id = None
    try:
        shop_svc.update_product(
            db,
            tenant_id=tid,
            product_id=product_id,
            name=name,
            price_cents=price_cents,
            stock=int(stock) if stock.strip() else None,
        )
    except HTTPException as exc:
        error = str(exc.detail)
        editing_id = product_id
    except ValueError:
        error = "庫存需為整數"
        editing_id = product_id
    return templates.TemplateResponse(
        "_shop.html", _shop_ctx(request, actor, db, error=error, editing_id=editing_id)
    )


@router.post("/shop/products/{product_id}/delete", response_class=HTMLResponse)
def shop_delete_product(
    request: Request,
    product_id: int,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.PRODUCT_SALES):
        return _feature_locked(request, actor, features_svc.PRODUCT_SALES, "商品銷售")
    tid = actor.user.tenant_id
    error = None
    try:
        shop_svc.delete_product(db, tenant_id=tid, product_id=product_id)
    except HTTPException as exc:
        error = str(exc.detail)
    return templates.TemplateResponse(
        "_shop.html", _shop_ctx(request, actor, db, error=error)
    )


@router.post("/shop/orders/{order_id}/pay", response_class=HTMLResponse)
def shop_pay_order(
    request: Request,
    order_id: int,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.PRODUCT_SALES):
        return _feature_locked(request, actor, features_svc.PRODUCT_SALES, "商品銷售")
    tid = actor.user.tenant_id
    error = None
    try:
        shop_svc.mark_order_paid(db, tenant_id=tid, order_id=order_id)
    except shop_svc.OrderNotFound:
        error = "訂單不存在"
    return templates.TemplateResponse(
        "_shop.html", _shop_ctx(request, actor, db, error=error)
    )


@router.post("/shop/orders/{order_id}/cancel", response_class=HTMLResponse)
def shop_cancel_order(
    request: Request,
    order_id: int,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.PRODUCT_SALES):
        return _feature_locked(request, actor, features_svc.PRODUCT_SALES, "商品銷售")
    tid = actor.user.tenant_id
    error = None
    try:
        shop_svc.cancel_order(db, tenant_id=tid, order_id=order_id)
    except shop_svc.OrderNotFound:
        error = "訂單不存在"
    return templates.TemplateResponse(
        "_shop.html", _shop_ctx(request, actor, db, error=error)
    )


@router.post("/shop/orders/{order_id}/refund", response_class=HTMLResponse)
def shop_refund_order(
    request: Request,
    order_id: int,
    amount_twd: int | None = Form(None, ge=1),
    actor: Actor = Depends(require_ui_owner),
    db: Session = Depends(get_db),
):
    """已付訂單閘道退款(可部分,預設全額餘額);owner 限定、服務層鎖列防重(R6-A3)。"""
    if not _require_ui_feature(db, actor, features_svc.PRODUCT_SALES):
        return _feature_locked(request, actor, features_svc.PRODUCT_SALES, "商品銷售")
    error = None
    saved = None
    try:
        order = order_refund_svc.request_order_refund(
            db,
            tenant_id=actor.user.tenant_id,
            order_id=order_id,
            actor_user_id=actor.user.id,
            amount_cents=amount_twd * 100 if amount_twd is not None else None,
        )
        refunded = (order.refunded_cents or 0) // 100
        audit_svc.record_from_actor(
            db, actor, action="shop.order.refund", target=f"order:{order_id}",
            detail={"refunded_twd": refunded}, request=request,
        )
        db.commit()
        saved = f"訂單已退款 NT${refunded}。"
    except order_refund_svc.OrderRefundError as exc:
        error = str(exc)
    return templates.TemplateResponse(
        "_shop.html", _shop_ctx(request, actor, db, error=error, saved=saved)
    )


@router.post("/shop/orders/{order_id}/manual-refund", response_class=HTMLResponse)
def shop_manual_refund_order(
    request: Request,
    order_id: int,
    note: str = Form(..., min_length=2, max_length=200),
    amount_twd: int | None = Form(None, ge=1),
    actor: Actor = Depends(require_ui_owner),
    db: Session = Depends(get_db),
):
    """在外部金流後台退款後,於系統對帳為已退(R6-A3);owner 限定。"""
    if not _require_ui_feature(db, actor, features_svc.PRODUCT_SALES):
        return _feature_locked(request, actor, features_svc.PRODUCT_SALES, "商品銷售")
    error = None
    saved = None
    try:
        order = order_refund_svc.confirm_manual_refund(
            db,
            tenant_id=actor.user.tenant_id,
            order_id=order_id,
            actor_user_id=actor.user.id,
            note=note,
            amount_cents=amount_twd * 100 if amount_twd is not None else None,
        )
        audit_svc.record_from_actor(
            db, actor, action="shop.order.refund.manual", target=f"order:{order_id}",
            detail={"refunded_twd": (order.refunded_cents or 0) // 100, "note": note[:200]},
            request=request,
        )
        db.commit()
        saved = "已對帳為人工退款。"
    except order_refund_svc.OrderRefundError as exc:
        error = str(exc)
    return templates.TemplateResponse(
        "_shop.html", _shop_ctx(request, actor, db, error=error, saved=saved)
    )


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
            request,
            actor,
            summary=analytics_svc.booking_summary(db, tenant_id=tid),
            utilization=analytics_svc.slot_utilization(db, tenant_id=tid),
            top=analytics_svc.top_customers(db, tenant_id=tid, limit=10),
            revenue=analytics_svc.revenue_summary(db, tenant_id=tid),
            trend=analytics_svc.trend_series(
                db, tenant_id=tid, period="week", periods=12
            ),
            staff_prod=reporting_svc.staff_performance(db, tenant_id=tid),
            service_prod=reporting_svc.popular_services(db, tenant_id=tid),
            retention=reporting_svc.return_rate(db, tenant_id=tid),
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


@router.get("/rich-menu/preview.png")
def rich_menu_preview(
    template: str = Query(...),
    theme: str = Query(...),
    actor: Actor = Depends(require_ui_user),
):
    """套用前預覽：即席產生選單圖(不動 DB、不打 LINE)。"""
    rich_menu_svc._validate(template, theme)
    _, image = rich_menu_svc.build_rich_menu_payload(template, theme)
    return Response(
        content=image,
        media_type="image/png",
        headers={"Cache-Control": "no-store"},
    )


# ── 店家自助：優惠券 ──────────────────────────────────────────────────────────


def _coupons_ctx(request: Request, actor: Actor, db: Session, **extra) -> dict:
    tid = actor.user.tenant_id
    return _ctx(
        request,
        actor,
        coupons=coupons_svc.list_coupons(db, tenant_id=tid),
        **extra,
    )


@router.get("/coupons", response_class=HTMLResponse)
def coupons_page(
    request: Request,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.COUPON_SYSTEM):
        return _feature_locked(
            request, actor, features_svc.COUPON_SYSTEM, "優惠券／會員"
        )
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
    if not _require_ui_feature(db, actor, features_svc.COUPON_SYSTEM):
        return _feature_locked(
            request, actor, features_svc.COUPON_SYSTEM, "優惠券／會員"
        )
    tid = actor.user.tenant_id
    error = None
    try:
        coupons_svc.create_coupon(
            db,
            tenant_id=tid,
            code=code,
            name=name,
            discount_type=discount_type,
            discount_value=discount_value,
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
    error = None
    try:
        coupons_svc.deactivate_coupon(db, tenant_id=tid, coupon_id=coupon_id)
    except HTTPException as exc:
        error = str(exc.detail)
    return templates.TemplateResponse(
        "_coupons_list.html", _coupons_ctx(request, actor, db, error=error)
    )


@router.get("/coupons/{coupon_id}/edit", response_class=HTMLResponse)
def coupons_edit_form(
    request: Request,
    coupon_id: int,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.COUPON_SYSTEM):
        return _feature_locked(
            request, actor, features_svc.COUPON_SYSTEM, "優惠券／會員"
        )
    return templates.TemplateResponse(
        "_coupons_list.html", _coupons_ctx(request, actor, db, editing_id=coupon_id)
    )


@router.post("/coupons/{coupon_id}/update", response_class=HTMLResponse)
def coupons_update(
    request: Request,
    coupon_id: int,
    name: str = Form(...),
    max_redemptions: str = Form(""),
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.COUPON_SYSTEM):
        return _feature_locked(
            request, actor, features_svc.COUPON_SYSTEM, "優惠券／會員"
        )
    tid = actor.user.tenant_id
    error = None
    editing_id = None
    try:
        coupons_svc.update_coupon(
            db,
            tenant_id=tid,
            coupon_id=coupon_id,
            name=name,
            max_redemptions=int(max_redemptions) if max_redemptions.strip() else None,
        )
    except HTTPException as exc:
        error = str(exc.detail)
        editing_id = coupon_id
    except ValueError:
        error = "兌換上限需為整數"
        editing_id = coupon_id
    return templates.TemplateResponse(
        "_coupons_list.html",
        _coupons_ctx(request, actor, db, error=error, editing_id=editing_id),
    )


@router.post("/coupons/{coupon_id}/delete", response_class=HTMLResponse)
def coupons_delete(
    request: Request,
    coupon_id: int,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    if not _require_ui_feature(db, actor, features_svc.COUPON_SYSTEM):
        return _feature_locked(
            request, actor, features_svc.COUPON_SYSTEM, "優惠券／會員"
        )
    tid = actor.user.tenant_id
    error = None
    try:
        coupons_svc.delete_coupon(db, tenant_id=tid, coupon_id=coupon_id)
    except HTTPException as exc:
        error = str(exc.detail)
    return templates.TemplateResponse(
        "_coupons_list.html", _coupons_ctx(request, actor, db, error=error)
    )


# ── 店家自助：進階功能訂閱 ────────────────────────────────────────────────────


def _features_ctx(request: Request, actor: Actor, db: Session, **extra) -> dict:
    from saas_mvp.models.feature_subscription import FeatureSubscription
    from saas_mvp.models.subscription_charge import SubscriptionCharge

    tid = actor.user.tenant_id
    # 扣款紀錄（最新 20 筆,附 feature 名）
    charges = (
        db.query(SubscriptionCharge, FeatureSubscription.feature)
        .join(
            FeatureSubscription,
            SubscriptionCharge.subscription_id == FeatureSubscription.id,
        )
        .filter(SubscriptionCharge.tenant_id == tid)
        .order_by(SubscriptionCharge.id.desc())
        .limit(20)
        .all()
    )
    return _ctx(
        request,
        actor,
        features=features_svc.list_for_tenant(db, tid),
        charges=charges,
        feature_labels=features_svc._FEATURE_LABELS,
        **extra,
    )


@router.get("/features", response_class=HTMLResponse)
def features_page(
    request: Request,
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    return templates.TemplateResponse(
        "features.html", _features_ctx(request, actor, db)
    )


@router.post("/features/{feature}/subscribe", response_class=HTMLResponse)
def features_subscribe(
    request: Request,
    feature: str,
    actor: Actor = Depends(require_ui_owner),
    db: Session = Depends(get_db),
):
    tenant = db.get(Tenant, actor.user.tenant_id)
    try:
        features_svc.validate_feature(feature)
        result = billing_svc.subscribe_feature(db, tenant, feature, actor.user.id)
        audit_svc.record_from_actor(
            db,
            actor,
            action="billing.feature.subscribe",
            target=f"tenant:{tenant.id}",
            detail={"feature": feature},
            request=request,
        )
        db.commit()
    except features_svc.UnknownFeatureError:
        result = None
    # ecpay 模式：尚未開通，導向綠界定期定額付款頁（首期授權成功後自動開通）。
    if result is not None and result.checkout_url:
        url = html.escape(result.checkout_url)
        return HTMLResponse(
            '<div class="card success">'
            f"<p>請完成綠界信用卡定期定額授權以開通「{html.escape(feature)}」。</p>"
            f'<a class="btn" href="{url}" target="_blank" rel="noopener">前往綠界付款</a>'
            '<p class="muted">完成首期授權後，功能將自動開通；可重新整理本頁查看狀態。</p>'
            "</div>"
        )
    return templates.TemplateResponse(
        "_features_list.html", _features_ctx(request, actor, db)
    )


@router.post("/features/{feature}/unsubscribe", response_class=HTMLResponse)
def features_unsubscribe(
    request: Request,
    feature: str,
    actor: Actor = Depends(require_ui_owner),
    db: Session = Depends(get_db),
):
    tenant = db.get(Tenant, actor.user.tenant_id)
    try:
        features_svc.validate_feature(feature)
        billing_svc.unsubscribe_feature(db, tenant, feature, actor.user.id)
        audit_svc.record_from_actor(
            db,
            actor,
            action="billing.feature.unsubscribe",
            target=f"tenant:{tenant.id}",
            detail={"feature": feature},
            request=request,
        )
        db.commit()
    except features_svc.UnknownFeatureError:
        pass
    return templates.TemplateResponse(
        "_features_list.html", _features_ctx(request, actor, db)
    )


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


