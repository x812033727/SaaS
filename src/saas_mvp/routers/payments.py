"""金流端點（綠界 ECPay）— 公開，無我方 JWT/rate-limit。

* GET  /payments/ecpay/checkout/{order_id} — 渲染自動 submit 的綠界付款表單。
* POST /payments/ecpay/callback            — 綠界 server 回調：先驗 CheckMacValue
  再標記訂單已付，回純文字 "1|OK"。

安全完全靠 CheckMacValue：回調只看 RtnCode 不驗簽會被偽造，故務必先驗簽 + 交叉驗金額。
冪等：綠界會重送直到收到 "1|OK"；mark_order_paid 已付為 no-op，仍回 "1|OK"。
"""

from __future__ import annotations

import datetime
import html
import logging
import time

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import HTMLResponse, PlainTextResponse
from sqlalchemy.orm import Session

from saas_mvp.config import settings
from saas_mvp.db import get_db
from saas_mvp.models.feature_subscription import SUB_PENDING
from saas_mvp.models.order import ORDER_PENDING, Order
from saas_mvp.services import features as features_svc
from saas_mvp.services import shop as shop_svc
from saas_mvp.services import subscriptions as subs_svc
from saas_mvp.services.payment_ecpay import EcpayClient
from saas_mvp.services.payment_newebpay import NewebPayClient

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
    client = EcpayClient()
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
    form = await request.form()
    params = {k: str(v) for k, v in form.items()}
    client = EcpayClient()

    # 1) 先驗簽（拒絕偽造）
    if not client.verify(params):
        _log.warning("ecpay callback rejected: bad CheckMacValue")
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
    sub = subs_svc.get_subscription_by_id(db, subscription_id)
    if sub is None:
        return HTMLResponse("<h1>找不到訂閱</h1>", status_code=status.HTTP_404_NOT_FOUND)
    if sub.status != SUB_PENDING:
        return HTMLResponse(f"<h1>訂閱狀態為 {html.escape(sub.status)}，無法重新付款。</h1>")
    if sub.period_amount_cents % 100 != 0:
        return HTMLResponse("<h1>金額單位錯誤（需為整數元）。</h1>", status_code=400)

    base = settings.public_base_url.rstrip("/")
    client = EcpayClient()
    form = client.build_period_form(
        merchant_trade_no=sub.merchant_trade_no,
        period_amount_twd=sub.period_amount_cents // 100,
        item_name=f"進階功能訂閱-{sub.feature}",
        trade_desc="LINE SaaS 進階功能月費",
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
    client = EcpayClient()
    if not client.verify(params):
        _log.warning("ecpay subscribe-callback rejected: bad CheckMacValue")
        return PlainTextResponse("0|CheckMacValue Error")

    trade_no = params.get("MerchantTradeNo", "")
    sub = subs_svc.get_subscription_by_trade_no(db, trade_no) if trade_no else None
    if sub is None:
        _log.warning("ecpay subscribe-callback: subscription not found %s", trade_no)
        return PlainTextResponse("0|subscription not found")

    if params.get("RtnCode") == "1":
        subs_svc.activate(
            db, sub, gwsr=params.get("Gwsr"), auth_code=params.get("AuthCode")
        )
        features_svc.set_enabled(
            db, sub.tenant_id, sub.feature, True,
            actor_user_id=None, source="subscribe", reason=trade_no,
        )
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
    client = EcpayClient()
    if not client.verify(params):
        _log.warning("ecpay period-callback rejected: bad CheckMacValue")
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
    features_svc.set_enabled(
        db, sub.tenant_id, sub.feature, success,
        actor_user_id=None, source="period",
    )
    return PlainTextResponse("1|OK")


@router.get("/ecpay/done", response_class=HTMLResponse)
def ecpay_done():
    """顧客付款完成後的瀏覽器返回頁（ClientBackURL）。"""
    return HTMLResponse(
        "<!doctype html><meta charset='utf-8'><h1>付款流程已完成</h1>"
        "<p>您可以關閉此頁面，返回 LINE 查看訂單狀態。</p>"
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
    client = NewebPayClient()

    # 1) 先驗 TradeSha（拒絕偽造）
    if not client.verify(params):
        _log.warning("newebpay notify rejected: bad TradeSha")
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
