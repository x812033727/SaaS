"""金流端點（綠界 ECPay）— 公開，無我方 JWT/rate-limit。

* GET  /payments/ecpay/checkout/{order_id} — 渲染自動 submit 的綠界付款表單。
* POST /payments/ecpay/callback            — 綠界 server 回調：先驗 CheckMacValue
  再標記訂單已付，回純文字 "1|OK"。

安全完全靠 CheckMacValue：回調只看 RtnCode 不驗簽會被偽造，故務必先驗簽 + 交叉驗金額。
冪等：綠界會重送直到收到 "1|OK"；mark_order_paid 已付為 no-op，仍回 "1|OK"。
"""

from __future__ import annotations

import html
import logging
import time

from fastapi import APIRouter, Depends, Request, status
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
from saas_mvp.models.order import ORDER_PENDING, Order
from saas_mvp.obs.alerts import capture_alert
from saas_mvp.services import billing as billing_svc
from saas_mvp.services import features as features_svc
from saas_mvp.services import shop as shop_svc
from saas_mvp.services import subscriptions as subs_svc
from saas_mvp.services.payment_ecpay import get_ecpay_client
from saas_mvp.services.payment_newebpay import NewebPayClient
from saas_mvp.services.platform_payment_config import payment_provider

_log = logging.getLogger(__name__)

router = APIRouter(prefix="/payments", tags=["payments"])


def _base36(n: int) -> str:
    chars = "0123456789abcdefghijklmnopqrstuvwxyz"
    if n == 0:
        return "0"
    out = ""
    while n:
        n, r = divmod(n, 36)
        out = chars[r] + out
    return out


def _gen_trade_no(order_id: int) -> str:
    """產生 ≤20 字、英數的唯一 MerchantTradeNo。"""
    return f"OD{order_id}T{_base36(int(time.time()))}"[:20]


@router.get("/ecpay/checkout/{order_id}", response_class=HTMLResponse)
def ecpay_checkout(
    order_id: int,
    db: Session = Depends(get_db),
):
    if payment_provider(db, settings) != "ecpay":
        return HTMLResponse(
            "<h1>綠界付款目前未啟用</h1>",
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    order = db.get(Order, order_id)
    if order is None:
        return HTMLResponse("<h1>找不到訂單</h1>", status_code=status.HTTP_404_NOT_FOUND)
    if order.status != ORDER_PENDING:
        return HTMLResponse(
            f"<h1>訂單 #{order.id} 狀態為 {html.escape(order.status)}，無法付款。</h1>"
        )
    if order.total_cents % 100 != 0:
        return HTMLResponse("<h1>金額單位錯誤（需為整數元）。</h1>", status_code=400)

    # 產生/沿用唯一交易編號（首次寫回 order，重載沿用）
    if not order.merchant_trade_no:
        order.merchant_trade_no = _gen_trade_no(order.id)
        db.commit()
        db.refresh(order)

    base = settings.public_base_url.rstrip("/")
    client = get_ecpay_client(db)
    form = client.build_order_form(
        merchant_trade_no=order.merchant_trade_no,
        amount_twd=order.total_cents // 100,
        item_name=f"訂單{order.id}",
        trade_desc="LINE 商城訂單",
        return_url=f"{base}/payments/ecpay/callback",
        client_back_url=f"{base}/payments/ecpay/done",
    )
    inputs = "\n".join(
        f'<input type="hidden" name="{html.escape(k)}" value="{html.escape(str(v))}">'
        for k, v in form.items()
    )
    page = (
        "<!doctype html><meta charset='utf-8'><title>前往付款</title>"
        f"<body onload='document.forms[0].submit()'>"
        f"<p>正在前往綠界付款頁…</p>"
        f"<form method='post' action='{html.escape(client.aio_url)}'>{inputs}"
        "<noscript><button type='submit'>前往付款</button></noscript></form></body>"
    )
    return HTMLResponse(page)


@router.post("/ecpay/callback", response_class=PlainTextResponse)
async def ecpay_callback(
    request: Request,
    db: Session = Depends(get_db),
):
    # 動態表單欄位須 await request.form()（保持 async）；
    # 驗簽 + sync DB 工作移入 threadpool，避免佔用事件迴圈。
    form = await request.form()
    params = {k: str(v) for k, v in form.items()}
    return await run_in_threadpool(_handle_ecpay_callback, db, params)


def _handle_ecpay_callback(db: Session, params: dict) -> PlainTextResponse:
    client = get_ecpay_client(db)

    # 1) 先驗簽（拒絕偽造）
    if not client.verify(params):
        _log.warning("ecpay callback rejected: bad CheckMacValue")
        capture_alert("payment: ecpay callback bad CheckMacValue")
        return PlainTextResponse("0|CheckMacValue Error")

    trade_no = params.get("MerchantTradeNo", "")
    order = shop_svc.get_order_by_trade_no(db, trade_no) if trade_no else None
    if order is None:
        _log.warning("ecpay callback: order not found for trade_no=%s", trade_no)
        return PlainTextResponse("0|order not found")

    # 2) RtnCode==1 = 付款成功；交叉驗金額後標記已付（冪等）
    if params.get("RtnCode") == "1":
        try:
            paid_amt = int(params.get("TradeAmt", "0"))
        except ValueError:
            paid_amt = -1
        if paid_amt != order.total_cents // 100:
            _log.warning(
                "ecpay callback amount mismatch order=%s expected=%s got=%s",
                order.id, order.total_cents // 100, params.get("TradeAmt"),
            )
            capture_alert("payment: callback amount mismatch")
            return PlainTextResponse("0|amount mismatch")
        shop_svc.mark_order_paid(db, tenant_id=order.tenant_id, order_id=order.id)
        return PlainTextResponse("1|OK")

    # RtnCode != 1：付款未成功，仍回 1|OK 收下通知（不改訂單）
    return PlainTextResponse("1|OK")


def _render_autosubmit(form: dict, action_url: str, title: str) -> HTMLResponse:
    inputs = "\n".join(
        f'<input type="hidden" name="{html.escape(k)}" value="{html.escape(str(v))}">'
        for k, v in form.items()
    )
    page = (
        f"<!doctype html><meta charset='utf-8'><title>{html.escape(title)}</title>"
        "<body onload='document.forms[0].submit()'>"
        f"<p>正在前往綠界…</p>"
        f"<form method='post' action='{html.escape(action_url)}'>{inputs}"
        "<noscript><button type='submit'>前往綠界</button></noscript></form></body>"
    )
    return HTMLResponse(page)


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


@router.get("/ecpay/deposit/{trade_no}", response_class=HTMLResponse)
def ecpay_deposit_checkout(
    trade_no: str,
    db: Session = Depends(get_db),
):
    """定金付款頁（C4）。URL 以不可猜的 deposit_merchant_trade_no 為鍵(非可枚舉的
    reservation_id),未知一律 404,防跨租戶枚舉洩定金金額/狀態。stub 模式渲染本地
    模擬頁(離線/demo 全流程可走);ecpay 模式渲染綠界 AIO 自動送出表單。"""
    from saas_mvp.services import deposit as deposit_svc

    resv = deposit_svc.find_by_trade_no(db, trade_no)
    if resv is None or resv.deposit_status is None:
        return HTMLResponse("<h1>找不到需付定金的預約</h1>", status_code=404)
    if resv.deposit_status == deposit_svc.DEPOSIT_PAID:
        return HTMLResponse("<h1>定金已付款</h1><p>您的預約已確認,可關閉本頁。</p>")
    if resv.deposit_status == deposit_svc.DEPOSIT_EXPIRED:
        return HTMLResponse("<h1>付款期限已過</h1><p>預約已取消,請重新預約。</p>")

    amount_twd = (resv.deposit_cents or 0) // 100
    if payment_provider(db, settings) == "stub":
        # stub:本地模擬付款頁(按鈕打模擬回調)
        return HTMLResponse(
            "<!doctype html><meta charset='utf-8'><h1>模擬定金付款</h1>"
            f"<p>預約 #{resv.id},定金 NT${amount_twd}(stub 模式,不會真扣款)。</p>"
            f"<form method='post' action='/payments/stub/deposit-paid/{resv.deposit_merchant_trade_no}'>"
            "<button type='submit'>模擬付款成功</button></form>"
        )
    if payment_provider(db, settings) != "ecpay":
        # 定金收款後端目前僅實作 ecpay AIO(與 stub 模擬頁);newebpay/linepay 沒有
        # 定金實作。**絕不可**退化成免費模擬頁 —— 那個 POST 端點會未收款就標 paid,
        # 等於在正式金流設定下開放公開的免費定金繞過。
        return HTMLResponse(
            "<h1>定金付款暫不支援目前的金流設定</h1>"
            "<p>定金目前僅支援綠界,請聯繫店家。</p>",
            status_code=503,
        )

    base = settings.public_base_url.rstrip("/")
    client = get_ecpay_client(db)
    form = client.build_order_form(
        merchant_trade_no=resv.deposit_merchant_trade_no,
        amount_twd=amount_twd,
        item_name="預約定金",
        trade_desc="LINE 預約定金",
        return_url=f"{base}/payments/ecpay/deposit-callback",
    )
    return _render_autosubmit(form, client.aio_url, "前往定金付款")


@router.post("/stub/deposit-paid/{trade_no}", response_class=HTMLResponse)
def stub_deposit_paid(
    trade_no: str,
    db: Session = Depends(get_db),
):
    """stub 模擬付款成功（僅 payment_provider == stub 時可用）。URL 以不可猜的
    trade_no 為鍵,未授權者無法枚舉 reservation_id 竊改他人定金(PEA-1)。"""
    from saas_mvp.services import deposit as deposit_svc

    if payment_provider(db, settings) != "stub":
        # 只有 stub 模式允許『模擬付款成功』;任何真實 provider(ecpay/newebpay/
        # linepay)都必須走真實回調驗簽,不得由此公開端點免費標 paid。
        return HTMLResponse("<h1>正式金流模式不提供模擬付款</h1>", status_code=403)
    resv = deposit_svc.find_by_trade_no(db, trade_no)
    if resv is None:
        return HTMLResponse("<h1>找不到預約</h1>", status_code=404)
    if deposit_svc.mark_paid(db, resv, provider="stub", payment_type="stub"):
        return HTMLResponse("<h1>✅ 定金已付款(模擬)</h1><p>您的預約已確認。</p>")
    return HTMLResponse("<h1>付款期限已過</h1><p>預約已取消,請重新預約。</p>")


@router.post("/ecpay/deposit-callback", response_class=PlainTextResponse)
async def ecpay_deposit_callback(
    request: Request,
    db: Session = Depends(get_db),
):
    """定金付款回調:驗簽 → trade_no 查單 → 金額交叉驗 → 冪等標 paid。"""
    form = await request.form()
    params = {k: str(v) for k, v in form.items()}
    return await run_in_threadpool(_handle_ecpay_deposit_callback, db, params)


def _handle_ecpay_deposit_callback(db: Session, params: dict) -> PlainTextResponse:
    from saas_mvp.obs.alerts import capture_alert
    from saas_mvp.services import deposit as deposit_svc

    client = get_ecpay_client(db)
    if not client.verify(params):
        _log.warning("ecpay deposit-callback rejected: bad CheckMacValue")
        capture_alert("payment: ecpay deposit-callback bad CheckMacValue")
        return PlainTextResponse("0|CheckMacValue Error")

    trade_no = params.get("MerchantTradeNo", "")
    resv = deposit_svc.find_by_trade_no(db, trade_no) if trade_no else None
    if resv is None:
        _log.warning("ecpay deposit-callback: reservation not found %s", trade_no)
        return PlainTextResponse("0|reservation not found")

    if params.get("RtnCode") != "1":
        return PlainTextResponse("1|OK")  # 付款失敗:不動狀態,等逾時或重付

    try:
        paid_amount = int(params.get("TradeAmt") or 0)
    except ValueError:
        paid_amount = 0
    if paid_amount != (resv.deposit_cents or 0) // 100:
        _log.warning(
            "ecpay deposit amount mismatch resv=%d expected=%d got=%d",
            resv.id, (resv.deposit_cents or 0) // 100, paid_amount,
        )
        capture_alert("payment: deposit callback amount mismatch")
        return PlainTextResponse("0|amount mismatch")

    if not deposit_svc.mark_paid(
        db,
        resv,
        provider="ecpay",
        provider_merchant_id=client.merchant_id,
        provider_trade_no=params.get("TradeNo") or None,
        payment_type=params.get("PaymentType") or None,
    ):
        # 過期單付款成功:名額可能已釋出 — 告警人工處理退款
        capture_alert(f"payment: deposit paid AFTER expiry resv={resv.id}")
    return PlainTextResponse("1|OK")


@router.get("/ecpay/done", response_class=HTMLResponse)
def ecpay_done():
    """顧客付款完成後的瀏覽器返回頁（ClientBackURL）。"""
    return HTMLResponse(
        "<!doctype html><meta charset='utf-8'><h1>付款流程已完成</h1>"
        "<p>您可以關閉此頁面，返回 LINE 查看訂單狀態。</p>"
    )


# ── LINE Pay（E2,一次性訂單）────────────────────────────────────────────────

@router.get("/linepay/confirm", response_class=HTMLResponse)
def linepay_confirm(
    transactionId: str = "",
    orderId: str = "",
    db: Session = Depends(get_db),
):
    """LINE Pay 付款完成 redirect → Confirm API → 標 paid(冪等)。

    金額以 **DB order.total_cents 為準**傳入 Confirm(不信 query string);
    已 paid 直接回成功頁(重整/重放安全)。
    """
    from saas_mvp.obs.alerts import capture_alert
    from saas_mvp.services.payment_linepay import LinePayClient, LinePayError

    try:
        order_pk = int(orderId)
    except (TypeError, ValueError):
        return HTMLResponse("<h1>訂單參數錯誤</h1>", status_code=400)
    order = db.get(Order, order_pk)
    if order is None:
        return HTMLResponse("<h1>找不到訂單</h1>", status_code=404)
    if order.status == "paid":
        return HTMLResponse("<h1>✅ 付款完成</h1><p>訂單已付款,可關閉本頁。</p>")
    if not transactionId:
        return HTMLResponse("<h1>缺少交易編號</h1>", status_code=400)

    try:
        LinePayClient().confirm_payment(
            transaction_id=transactionId,
            amount_twd=order.total_cents // 100,
            currency=settings.currency or "TWD",
        )
    except LinePayError as exc:
        _log.warning("linepay confirm failed order=%d: %s", order.id, exc)
        capture_alert(f"payment: linepay confirm failed order={order.id}")
        return HTMLResponse(
            "<h1>付款確認失敗</h1><p>請回 LINE Pay 重試或聯繫店家。</p>",
            status_code=502,
        )

    order.status = "paid"
    order.merchant_trade_no = f"LP{transactionId}"[:20]
    db.commit()
    return HTMLResponse("<h1>✅ 付款完成</h1><p>訂單已付款,可關閉本頁。</p>")


@router.get("/linepay/cancel", response_class=HTMLResponse)
def linepay_cancel(orderId: str = ""):
    """顧客在 LINE Pay 取消:不動訂單狀態。"""
    return HTMLResponse(
        "<h1>已取消付款</h1><p>訂單仍保留為未付款,可稍後重新付款。</p>"
    )


# ── 藍新金流 NewebPay（MPG 幕前） ──────────────────────────────────────────────


@router.get("/newebpay/checkout/{order_id}", response_class=HTMLResponse)
def newebpay_checkout(
    order_id: int,
    db: Session = Depends(get_db),
):
    order = db.get(Order, order_id)
    if order is None:
        return HTMLResponse("<h1>找不到訂單</h1>", status_code=status.HTTP_404_NOT_FOUND)
    if order.status != ORDER_PENDING:
        return HTMLResponse(
            f"<h1>訂單 #{order.id} 狀態為 {html.escape(order.status)}，無法付款。</h1>"
        )
    if order.total_cents % 100 != 0:
        return HTMLResponse("<h1>金額單位錯誤（需為整數元）。</h1>", status_code=400)

    # 產生/沿用唯一交易編號（首次寫回 order，重載沿用）
    if not order.merchant_trade_no:
        order.merchant_trade_no = _gen_trade_no(order.id)
        db.commit()
        db.refresh(order)

    base = settings.public_base_url.rstrip("/")
    client = NewebPayClient()
    form = client.build_order_form(
        merchant_trade_no=order.merchant_trade_no,
        amount_twd=order.total_cents // 100,
        item_desc=f"訂單{order.id}",
        return_url=f"{base}/payments/newebpay/done",
        notify_url=f"{base}/payments/newebpay/notify",
        client_back_url=f"{base}/payments/newebpay/done",
    )
    inputs = "\n".join(
        f'<input type="hidden" name="{html.escape(k)}" value="{html.escape(str(v))}">'
        for k, v in form.items()
    )
    page = (
        "<!doctype html><meta charset='utf-8'><title>前往付款</title>"
        f"<body onload='document.forms[0].submit()'>"
        f"<p>正在前往藍新金流付款頁…</p>"
        f"<form method='post' action='{html.escape(client.mpg_url)}'>{inputs}"
        "<noscript><button type='submit'>前往付款</button></noscript></form></body>"
    )
    return HTMLResponse(page)


@router.post("/newebpay/notify", response_class=PlainTextResponse)
async def newebpay_notify(
    request: Request,
    db: Session = Depends(get_db),
):
    form = await request.form()
    params = {k: str(v) for k, v in form.items()}
    return await run_in_threadpool(_handle_newebpay_notify, db, params)


def _handle_newebpay_notify(db: Session, params: dict) -> PlainTextResponse:
    client = NewebPayClient()

    # 1) 先驗 TradeSha（拒絕偽造）
    if not client.verify(params):
        _log.warning("newebpay notify rejected: bad TradeSha")
        capture_alert("payment: newebpay notify bad TradeSha")
        return PlainTextResponse("0|TradeSha Error")

    # 2) 解密 TradeInfo 取交易結果。解析（含 Result 取值）一律包在 try 內：
    #    真實藍新 JSON 回應的欄位巢狀在 Result 物件，若該物件型別異常（非 dict）
    #    或缺欄，視為無效通知回 400，而非讓 .get() 噴 500。
    try:
        info = client.decrypt_trade_info(params.get("TradeInfo", ""))
        # 藍新可能將欄位包在 Result 內（JSON 形式）或攤平（舊 query-string 形式）。
        result = info.get("Result") if isinstance(info.get("Result"), dict) else info
        trade_no = result.get("MerchantOrderNo") or info.get("MerchantOrderNo") or ""
    except Exception:  # noqa: BLE001 — 解密/解析失敗即視為無效通知
        _log.warning("newebpay notify rejected: TradeInfo decrypt/parse failed")
        return PlainTextResponse("0|decrypt failed")

    order = shop_svc.get_order_by_trade_no(db, trade_no) if trade_no else None
    if order is None:
        _log.warning("newebpay notify: order not found for trade_no=%s", trade_no)
        return PlainTextResponse("0|order not found")

    # 3) Status==SUCCESS = 付款成功；交叉驗金額後標記已付（冪等）
    if str(info.get("Status", "")).upper() == "SUCCESS":
        try:
            paid_amt = int(result.get("Amt", info.get("Amt", "0")))
        except (ValueError, TypeError):
            paid_amt = -1
        if paid_amt != order.total_cents // 100:
            _log.warning(
                "newebpay notify amount mismatch order=%s expected=%s got=%s",
                order.id, order.total_cents // 100, paid_amt,
            )
            capture_alert("payment: callback amount mismatch")
            return PlainTextResponse("0|amount mismatch")
        shop_svc.mark_order_paid(db, tenant_id=order.tenant_id, order_id=order.id)
        return PlainTextResponse("1|OK")

    # Status != SUCCESS：付款未成功，仍回收下通知（不改訂單）
    return PlainTextResponse("1|OK")


@router.get("/newebpay/done", response_class=HTMLResponse)
def newebpay_done():
    """顧客付款完成後的瀏覽器返回頁（ReturnURL / ClientBackURL）。"""
    return HTMLResponse(
        "<!doctype html><meta charset='utf-8'><h1>付款流程已完成</h1>"
        "<p>您可以關閉此頁面，返回 LINE 查看訂單狀態。</p>"
    )
