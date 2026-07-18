"""payments 子模組(P5 純搬移自 routers/payments.py):一次性訂單其餘閘道:綠界 done + LINE Pay confirm/cancel + 藍新 MPG。"""
from __future__ import annotations

import html

from fastapi import Depends, Request, status
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from saas_mvp.config import settings
from saas_mvp.db import get_db
from saas_mvp.models.order import ORDER_PENDING
from saas_mvp.obs.alerts import capture_alert
from saas_mvp.services import shop as shop_svc
from saas_mvp.services.payment_newebpay import NewebPayClient
from saas_mvp.services.platform_payment_config import payment_provider

from saas_mvp.routers.payments._shared import (
    _log, router,
)

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

    orderId 是我方不可猜的 merchant_trade_no(非可枚舉 order_id,PEA-3);
    transactionId 必須與 create_checkout 時落庫的 ``payment_txn_id`` 一致
    (txid↔order 綁定,不信任 query string);金額以 **DB order.total_cents
    為準**傳入 Confirm;已 paid 直接回成功頁(重整/重放安全)。
    """
    from saas_mvp.obs.alerts import capture_alert
    from saas_mvp.services.payment_linepay import LinePayClient, LinePayError

    from saas_mvp.services import gift_card_sales as gift_card_sales_svc

    order = shop_svc.get_order_by_trade_no(db, orderId) if orderId else None
    if order is None:
        return HTMLResponse("<h1>找不到訂單</h1>", status_code=404)
    if order.status == "paid":
        # R11-A:購卡訂單導回狀態頁(顯示卡號),一般訂單維持完成頁
        gc_url = gift_card_sales_svc.status_url_for_order(db, order)
        if gc_url:
            return RedirectResponse(gc_url, status_code=303)
        return HTMLResponse("<h1>✅ 付款完成</h1><p>訂單已付款,可關閉本頁。</p>")
    if not transactionId:
        return HTMLResponse("<h1>缺少交易編號</h1>", status_code=400)
    if not order.payment_txn_id or transactionId != order.payment_txn_id:
        _log.warning(
            "linepay confirm txid mismatch order=%d got=%s", order.id, transactionId
        )
        capture_alert(f"payment: linepay confirm txid mismatch order={order.id}")
        return HTMLResponse(
            "<h1>交易編號不符</h1><p>請回 LINE 重新取得付款連結。</p>",
            status_code=400,
        )

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

    # merchant_trade_no 即結帳鍵,不再以 LP{txid} 覆寫(txid 已落 payment_txn_id)。
    # 統一走訂單付款服務，補 paid_at 並在有 POS 員工歸屬時冪等建立抽成快照。
    # R6-A3:記 provider=linepay(退款以既有 payment_txn_id 為 transactionId)。
    shop_svc.mark_order_paid(
        db, tenant_id=order.tenant_id, order_id=order.id, provider="linepay",
    )
    gc_url = gift_card_sales_svc.status_url_for_order(db, order)
    if gc_url:
        return RedirectResponse(gc_url, status_code=303)
    return HTMLResponse("<h1>✅ 付款完成</h1><p>訂單已付款,可關閉本頁。</p>")


@router.get("/linepay/cancel", response_class=HTMLResponse)
def linepay_cancel(orderId: str = ""):
    """顧客在 LINE Pay 取消:不動訂單狀態。"""
    return HTMLResponse(
        "<h1>已取消付款</h1><p>訂單仍保留為未付款,可稍後重新付款。</p>"
    )


# ── 藍新金流 NewebPay（MPG 幕前） ──────────────────────────────────────────────


@router.get("/newebpay/checkout/{trade_no}", response_class=HTMLResponse)
def newebpay_checkout(
    trade_no: str,
    db: Session = Depends(get_db),
):
    """訂單結帳頁(藍新)。URL 鍵與 404 行為同 ecpay_checkout(PEA-3)。"""
    if payment_provider(db, settings) != "newebpay":
        return HTMLResponse(
            "<h1>藍新付款目前未啟用</h1>",
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    order = shop_svc.get_order_by_trade_no(db, trade_no)
    if order is None:
        return HTMLResponse("<h1>找不到訂單</h1>", status_code=status.HTTP_404_NOT_FOUND)
    if order.status != ORDER_PENDING:
        return HTMLResponse(
            f"<h1>訂單 #{order.id} 狀態為 {html.escape(order.status)}，無法付款。</h1>"
        )
    if order.total_cents % 100 != 0:
        return HTMLResponse("<h1>金額單位錯誤（需為整數元）。</h1>", status_code=400)

    from saas_mvp.services import gift_card_sales as gift_card_sales_svc

    base = settings.public_base_url.rstrip("/")
    # R11-A:購卡訂單付款後導回狀態頁(顯示卡號)
    gc_url = gift_card_sales_svc.status_url_for_order(db, order)
    back_url = gc_url or f"{base}/payments/newebpay/done"
    item_desc = "電子禮物卡" if gc_url else f"訂單{order.id}"
    client = NewebPayClient()
    form = client.build_order_form(
        merchant_trade_no=order.merchant_trade_no,
        amount_twd=order.total_cents // 100,
        item_desc=item_desc,
        return_url=back_url,
        notify_url=f"{base}/payments/newebpay/notify",
        client_back_url=back_url,
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
        shop_svc.mark_order_paid(
            db, tenant_id=order.tenant_id, order_id=order.id,
            provider="newebpay",
            provider_merchant_id=client.merchant_id,
            provider_trade_no=(result.get("TradeNo") or None),
        )
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
