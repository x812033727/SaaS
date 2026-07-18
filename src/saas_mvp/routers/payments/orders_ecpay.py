"""payments 子模組(P5 純搬移自 routers/payments.py):綠界一次性訂單:checkout 表單 + server 回調。"""
from __future__ import annotations

import html

from fastapi import Depends, Request, status
from fastapi.responses import HTMLResponse, PlainTextResponse
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from saas_mvp.config import settings
from saas_mvp.db import get_db
from saas_mvp.models.order import ORDER_PENDING
from saas_mvp.obs.alerts import capture_alert
from saas_mvp.services import shop as shop_svc
from saas_mvp.services.payment_ecpay import get_ecpay_client
from saas_mvp.services.platform_payment_config import payment_provider

from saas_mvp.routers.payments._shared import (
    _log, router,
)

@router.get("/ecpay/checkout/{trade_no}", response_class=HTMLResponse)
def ecpay_checkout(
    trade_no: str,
    db: Session = Depends(get_db),
):
    """訂單結帳頁。URL 以不可猜的 merchant_trade_no 為鍵(非可枚舉的 order_id),
    未知一律 404,防跨租戶枚舉洩訂單金額/狀態(PEA-3,比照定金流)。"""
    if payment_provider(db, settings) != "ecpay":
        return HTMLResponse(
            "<h1>綠界付款目前未啟用</h1>",
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
    client = get_ecpay_client(db)
    form = client.build_order_form(
        merchant_trade_no=order.merchant_trade_no,
        amount_twd=order.total_cents // 100,
        item_name="電子禮物卡" if gc_url else f"訂單{order.id}",
        trade_desc="電子禮物卡" if gc_url else "LINE 商城訂單",
        return_url=f"{base}/payments/ecpay/callback",
        client_back_url=gc_url or f"{base}/payments/ecpay/done",
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
        shop_svc.mark_order_paid(
            db, tenant_id=order.tenant_id, order_id=order.id,
            provider="ecpay",
            provider_merchant_id=client.merchant_id,
            provider_trade_no=(params.get("TradeNo") or None),
        )
        return PlainTextResponse("1|OK")

    # RtnCode != 1：付款未成功，仍回 1|OK 收下通知（不改訂單）
    return PlainTextResponse("1|OK")


