"""payments 子模組(P5 純搬移自 routers/payments.py):進階功能定期定額訂閱(綠界 recurring)。"""
from __future__ import annotations

import html

from fastapi import Depends, Request, status
from fastapi.responses import HTMLResponse, PlainTextResponse
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from saas_mvp.config import settings
from saas_mvp.db import get_db
from saas_mvp.models.feature_subscription import (
    SUB_CANCEL_FAILED,
    SUB_CANCELLED,
    SUB_PENDING,
)
from saas_mvp.obs.alerts import capture_alert
from saas_mvp.services import billing as billing_svc
from saas_mvp.services import features as features_svc
from saas_mvp.services import subscriptions as subs_svc
from saas_mvp.services.payment_ecpay import get_ecpay_client
from saas_mvp.services.platform_payment_config import payment_provider

from saas_mvp.routers.payments._shared import (
    _log, _render_autosubmit, router,
)

# ── 進階功能定期定額訂閱（recurring） ──────────────────────────────────────────

@router.get("/ecpay/subscribe/{subscription_id}", response_class=HTMLResponse)
def ecpay_subscribe(
    subscription_id: int,
    db: Session = Depends(get_db),
):
    if payment_provider(db, settings) != "ecpay":
        return HTMLResponse(
            "<h1>綠界訂閱付款目前未啟用</h1>",
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    sub = subs_svc.get_subscription_by_id(db, subscription_id)
    if sub is None:
        return HTMLResponse("<h1>找不到訂閱</h1>", status_code=status.HTTP_404_NOT_FOUND)
    if sub.status != SUB_PENDING:
        return HTMLResponse(f"<h1>訂閱狀態為 {html.escape(sub.status)}，無法重新付款。</h1>")
    if sub.period_amount_cents % 100 != 0:
        return HTMLResponse("<h1>金額單位錯誤（需為整數元）。</h1>", status_code=400)

    base = settings.public_base_url.rstrip("/")
    client = get_ecpay_client(db)
    form = client.build_period_form(
        merchant_trade_no=sub.merchant_trade_no,
        period_amount_twd=sub.period_amount_cents // 100,
        item_name=(
            f"方案訂閱-{features_svc.BUNDLE_LABELS[sub.feature]}"
            if sub.feature in features_svc.VALID_BUNDLES
            else f"進階功能訂閱-{sub.feature}"
        ),
        trade_desc="LINE SaaS 月費",
        return_url=f"{base}/payments/ecpay/subscribe-callback",
        period_return_url=f"{base}/payments/ecpay/period-callback",
        exec_times=sub.exec_times,
        frequency=sub.frequency,
        period_type=sub.period_type,
        client_back_url=f"{base}/payments/ecpay/done",
    )
    return _render_autosubmit(form, client.aio_url, "前往訂閱付款")


@router.post("/ecpay/subscribe-callback", response_class=PlainTextResponse)
async def ecpay_subscribe_callback(
    request: Request,
    db: Session = Depends(get_db),
):
    """首期授權結果（ReturnURL）：驗簽 → RtnCode==1 才開通功能。"""
    form = await request.form()
    params = {k: str(v) for k, v in form.items()}
    return await run_in_threadpool(_handle_ecpay_subscribe_callback, db, params)


def _handle_ecpay_subscribe_callback(db: Session, params: dict) -> PlainTextResponse:
    client = get_ecpay_client(db)
    if not client.verify(params):
        _log.warning("ecpay subscribe-callback rejected: bad CheckMacValue")
        capture_alert("payment: ecpay subscribe-callback bad CheckMacValue")
        return PlainTextResponse("0|CheckMacValue Error")

    trade_no = params.get("MerchantTradeNo", "")
    sub = subs_svc.get_subscription_by_trade_no(db, trade_no) if trade_no else None
    if sub is None:
        _log.warning("ecpay subscribe-callback: subscription not found %s", trade_no)
        return PlainTextResponse("0|subscription not found")

    if sub.status in (SUB_CANCELLED, SUB_CANCEL_FAILED):
        # 使用者取消 pending 後仍可能在舊付款頁完成交易；不可因此重新開通。
        # 回 1|OK 防止無限重送，同時告警由營運者在綠界後台核對退款。
        if params.get("RtnCode") == "1":
            capture_alert(
                f"payment: subscription paid AFTER cancellation sub={sub.id}"
            )
        return PlainTextResponse("1|OK")

    if params.get("RtnCode") == "1":
        subs_svc.activate(
            db, sub, gwsr=params.get("Gwsr"), auth_code=params.get("AuthCode")
        )
        if sub.feature in features_svc.VALID_BUNDLES:
            # 方案 bundle：改 tenant.plan（含 PlanChangeHistory、清試用）
            billing_svc.apply_bundle_activation(db, sub)
        else:
            features_svc.set_enabled(
                db, sub.tenant_id, sub.feature, True,
                actor_user_id=None, source="subscribe", reason=trade_no,
            )
        _issue_invoice_for_latest_charge(db, sub)  # C2:發票失敗絕不擋回調
        return PlainTextResponse("1|OK")

    subs_svc.mark_failed(db, sub)
    return PlainTextResponse("1|OK")


@router.post("/ecpay/period-callback", response_class=PlainTextResponse)
async def ecpay_period_callback(
    request: Request,
    db: Session = Depends(get_db),
):
    """每期授權結果（PeriodReturnURL）：成功維持開通；失敗關閉。"""
    form = await request.form()
    params = {k: str(v) for k, v in form.items()}
    return await run_in_threadpool(_handle_ecpay_period_callback, db, params)


def _handle_ecpay_period_callback(db: Session, params: dict) -> PlainTextResponse:
    client = get_ecpay_client(db)
    if not client.verify(params):
        _log.warning("ecpay period-callback rejected: bad CheckMacValue")
        capture_alert("payment: ecpay period-callback bad CheckMacValue")
        return PlainTextResponse("0|CheckMacValue Error")

    trade_no = params.get("MerchantTradeNo", "")
    sub = subs_svc.get_subscription_by_trade_no(db, trade_no) if trade_no else None
    if sub is None:
        _log.warning("ecpay period-callback: subscription not found %s", trade_no)
        return PlainTextResponse("0|subscription not found")

    success = params.get("RtnCode") == "1"
    try:
        total = int(params["TotalSuccessTimes"]) if params.get("TotalSuccessTimes") else None
    except ValueError:
        total = None
    subs_svc.record_period(db, sub, success=success, total_success_times=total)
    if sub.feature in features_svc.VALID_BUNDLES:
        # 方案 bundle：成功維持方案；扣款失敗降 free（留 PlanChangeHistory）
        billing_svc.apply_bundle_period(db, sub, success=success)
    else:
        features_svc.set_enabled(
            db, sub.tenant_id, sub.feature, success,
            actor_user_id=None, source="period",
        )
    if success:
        _issue_invoice_for_latest_charge(db, sub)  # C2:發票失敗絕不擋回調
    return PlainTextResponse("1|OK")


def _issue_invoice_for_latest_charge(db, sub) -> None:
    """取該訂閱最新一筆成功扣款開發票(C2)。永不拋錯。"""
    try:
        from sqlalchemy import select as _select

        from saas_mvp.models.subscription_charge import SubscriptionCharge
        from saas_mvp.services import invoices as invoices_svc

        charge = db.execute(
            _select(SubscriptionCharge)
            .where(
                SubscriptionCharge.subscription_id == sub.id,
                SubscriptionCharge.success.is_(True),
            )
            .order_by(SubscriptionCharge.id.desc())
        ).scalars().first()
        if charge is not None:
            invoices_svc.issue_for_charge(db, charge)
    except Exception:  # noqa: BLE001 — 發票絕不影響金流回調
        _log.warning("invoice hook failed sub=%s", getattr(sub, "id", "?"), exc_info=True)


