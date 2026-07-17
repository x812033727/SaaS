"""UI 子模組(P4 純搬移自 routers/ui.py):方案/定價。"""
from __future__ import annotations

import html

from fastapi import Depends, Form, Query, Request, status
from fastapi.responses import (
    HTMLResponse,
    RedirectResponse,
)
from sqlalchemy.orm import Session

from saas_mvp.deps import (
    Actor,
    get_db,
    require_ui_owner,
)
from saas_mvp.models.tenant import Tenant
from saas_mvp.services import billing as billing_svc
from saas_mvp.services import features as features_svc
from saas_mvp.services import audit as audit_svc
from saas_mvp.services import invoice_profiles as invoice_profiles_svc
from saas_mvp.services import plans as plans_svc
from fastapi import HTTPException

from saas_mvp.routers.ui._shared import (
    router, templates, _ctx,
)

# ── 方案 / 定價（B1） ───────────────────────────────────────────────────────


def _plan_info(tenant: Tenant) -> dict:
    """dashboard/選方案頁的方案摘要：生效方案、試用倒數。"""
    import datetime as _dt

    now = _dt.datetime.now(_dt.timezone.utc)
    effective = plans_svc.effective_plan(tenant, now=now)
    on_trial = plans_svc.trial_active(tenant, now=now)
    days_left = None
    if on_trial:
        ends = tenant.trial_ends_at
        if ends.tzinfo is None:
            ends = ends.replace(tzinfo=_dt.timezone.utc)
        days_left = max(0, (ends - now).days)
    return {
        "effective": effective,
        "effective_label": plans_svc.plan_label(effective),
        "paid": plans_svc.normalize_plan(tenant.plan),
        "paid_label": plans_svc.plan_label(plans_svc.normalize_plan(tenant.plan)),
        "trial_active": on_trial,
        "trial_days_left": days_left,
    }


def _line_insights(db: Session, tenant_id: int) -> dict:
    """dashboard「LINE 經營」卡（A3.4）：本月事件/bot 建單/調查均分。"""
    import datetime as _dt

    from sqlalchemy import func as _func, select as _select

    from saas_mvp.models.line_webhook_event import LineWebhookEvent
    from saas_mvp.models.reservation import Reservation
    from saas_mvp.services import feedback as feedback_svc

    now = _dt.datetime.now(_dt.timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    events = db.execute(
        _select(_func.count(LineWebhookEvent.id)).where(
            LineWebhookEvent.tenant_id == tenant_id,
            LineWebhookEvent.created_at >= month_start,
        )
    ).scalar_one()
    bot_bookings = db.execute(
        _select(_func.count(Reservation.id)).where(
            Reservation.tenant_id == tenant_id,
            Reservation.line_user_id.is_not(None),
            Reservation.created_at >= month_start,
        )
    ).scalar_one()
    return {
        "month_events": int(events),
        "month_bot_bookings": int(bot_bookings),
        "feedback": feedback_svc.summary(db, tenant_id),
    }


@router.get("/pricing", response_class=HTMLResponse)
def pricing_page(request: Request):
    """公開定價頁（免登入）。"""
    return templates.TemplateResponse(
        "pricing.html", _ctx(request, plans=plans_svc.list_plans())
    )


@router.get("/plan", response_class=HTMLResponse)
def plan_page(
    request: Request,
    actor: Actor = Depends(require_ui_owner),
    db: Session = Depends(get_db),
):
    """登入後選方案頁：目前方案 + 各方案內容；訂閱按鈕（B2 接金流）。"""
    tenant = db.get(Tenant, actor.user.tenant_id)
    info = _plan_info(tenant)
    return templates.TemplateResponse(
        "plan.html",
        _ctx(
            request,
            actor,
            plans=plans_svc.list_plans(current=info["effective"]),
            plan_info=info,
        ),
    )


@router.post("/plan/{plan}/subscribe", response_class=HTMLResponse)
def plan_subscribe(
    plan: str,
    request: Request,
    actor: Actor = Depends(require_ui_owner),
    db: Session = Depends(get_db),
):
    """訂閱方案。stub 立即生效導回；ecpay 顯示前往綠界付款卡片。"""
    bundle_key = {v: k for k, v in features_svc.BUNDLE_TO_PLAN.items()}.get(plan)
    tenant = db.get(Tenant, actor.user.tenant_id)
    if bundle_key is None:
        return RedirectResponse("/ui/plan", status_code=status.HTTP_303_SEE_OTHER)
    try:
        result = billing_svc.subscribe_bundle(db, tenant, bundle_key, actor.user.id)
    except HTTPException as exc:
        info = _plan_info(tenant)
        return templates.TemplateResponse(
            "plan.html",
            _ctx(
                request,
                actor,
                plans=plans_svc.list_plans(current=info["effective"]),
                plan_info=info,
                error=str(exc.detail),
            ),
            status_code=exc.status_code,
        )
    audit_svc.record_from_actor(
        db,
        actor,
        action="billing.plan.subscribe",
        target=f"tenant:{tenant.id}",
        detail={"plan": plan, "mode": result.mode},
        request=request,
    )
    db.commit()
    if result.checkout_url:
        url = html.escape(result.checkout_url)
        label = html.escape(plans_svc.plan_label(plan))
        return HTMLResponse(
            '<div class="card success">'
            f"<p>請完成綠界信用卡定期定額授權以啟用「{label}」。</p>"
            f'<a class="btn" href="{url}" target="_blank" rel="noopener">前往綠界付款</a>'
            '<p class="muted">完成首期授權後方案自動生效；可回<a href="/ui/plan">方案頁</a>查看狀態。</p>'
            "</div>"
        )
    return RedirectResponse("/ui/plan", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/plan/unsubscribe", response_class=HTMLResponse)
def plan_unsubscribe(
    request: Request,
    actor: Actor = Depends(require_ui_owner),
    db: Session = Depends(get_db),
):
    """退訂方案 → 降 free（已付費者保留原方案至最後扣款日 + 31 天）。"""
    tenant = db.get(Tenant, actor.user.tenant_id)
    billing_svc.unsubscribe_bundle(db, tenant, actor.user.id)
    audit_svc.record_from_actor(
        db,
        actor,
        action="billing.plan.unsubscribe",
        target=f"tenant:{tenant.id}",
        request=request,
    )
    db.commit()
    return RedirectResponse("/ui/plan", status_code=status.HTTP_303_SEE_OTHER)


def _billing_ctx(request: Request, actor: Actor, db: Session, **extra) -> dict:
    """帳單頁共用資料；儲存發票資料失敗時可原頁顯示錯誤。"""
    from saas_mvp.models.feature_subscription import FeatureSubscription
    from saas_mvp.models.subscription_charge import SubscriptionCharge
    from sqlalchemy import select as _select

    tid = actor.user.tenant_id
    tenant = db.get(Tenant, tid)
    sub = (
        db.execute(
            _select(FeatureSubscription)
            .where(
                FeatureSubscription.tenant_id == tid,
                FeatureSubscription.feature.in_(features_svc.VALID_BUNDLES),
            )
            .order_by(FeatureSubscription.id.desc())
        )
        .scalars()
        .first()
    )
    charges = []
    next_charge_at = None
    if sub is not None:
        charges = (
            db.execute(
                _select(SubscriptionCharge)
                .where(SubscriptionCharge.subscription_id == sub.id)
                .order_by(SubscriptionCharge.id.desc())
                .limit(24)
            )
            .scalars()
            .all()
        )
        if sub.status == "active" and sub.last_charged_at is not None:
            import datetime as _dt

            next_charge_at = sub.last_charged_at + _dt.timedelta(days=30)
    # C2:發票對照(charge_id → Invoice)
    invoice_by_charge: dict = {}
    if charges:
        from saas_mvp.models.invoice import Invoice

        rows = (
            db.execute(
                _select(Invoice).where(
                    Invoice.subscription_charge_id.in_([c.id for c in charges])
                )
            )
            .scalars()
            .all()
        )
        invoice_by_charge = {r.subscription_charge_id: r for r in rows}
    return _ctx(
        request,
        actor,
        plan_info=_plan_info(tenant),
        subscription=sub,
        bundle_label=(
            features_svc.BUNDLE_LABELS.get(sub.feature, sub.feature) if sub else None
        ),
        charges=charges,
        next_charge_at=next_charge_at,
        invoice_by_charge=invoice_by_charge,
        invoice_profile=invoice_profiles_svc.profile_status(db, tid),
        **extra,
    )


@router.get("/billing", response_class=HTMLResponse)
def billing_page(
    request: Request,
    invoice_profile_saved: bool = Query(False),
    actor: Actor = Depends(require_ui_owner),
    db: Session = Depends(get_db),
):
    """帳單頁：方案、扣款明細與發票買受資訊。"""
    return templates.TemplateResponse(
        "billing.html",
        _billing_ctx(
            request,
            actor,
            db,
            invoice_profile_saved=invoice_profile_saved,
        ),
    )


@router.post("/billing/invoice-profile", response_class=HTMLResponse)
def billing_invoice_profile_save(
    request: Request,
    mode: str = Form(..., max_length=16),
    buyer_name: str = Form("", max_length=60),
    buyer_identifier: str = Form("", max_length=8),
    carrier_type: str = Form("ecpay", max_length=16),
    carrier_number: str = Form("", max_length=64),
    donation_code: str = Form("", max_length=7),
    actor: Actor = Depends(require_ui_owner),
    db: Session = Depends(get_db),
):
    """店家 owner 自助保存發票資料；敏感載具號碼加密保存。"""
    try:
        row = invoice_profiles_svc.save_profile(
            db,
            tenant_id=actor.user.tenant_id,
            mode=mode,
            buyer_name=buyer_name,
            buyer_identifier=buyer_identifier,
            carrier_type=carrier_type,
            carrier_number=carrier_number,
            donation_code=donation_code,
            actor_user_id=actor.user.id,
        )
    except invoice_profiles_svc.InvoiceProfileError as exc:
        db.rollback()
        return templates.TemplateResponse(
            "billing.html",
            _billing_ctx(request, actor, db, invoice_profile_error=str(exc)),
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    audit_svc.record_from_actor(
        db,
        actor,
        action="billing.invoice_profile.update",
        target=f"tenant:{actor.user.tenant_id}",
        detail={
            "mode": row.mode,
            "carrier_type": row.carrier_type,
            "has_identifier": bool(row.buyer_identifier),
            "has_donation_code": bool(row.donation_code),
        },
        request=request,
    )
    db.commit()
    return RedirectResponse(
        "/ui/billing?invoice_profile_saved=1",
        status_code=status.HTTP_303_SEE_OTHER,
    )


