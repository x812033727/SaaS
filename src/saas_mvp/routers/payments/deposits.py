"""payments 子模組(P5 純搬移自 routers/payments.py):預約定金:provider 中立 checkout + 綠界/藍新/LINE Pay 回調。"""
from __future__ import annotations


from fastapi import Depends, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from saas_mvp.config import settings
from saas_mvp.db import get_db
from saas_mvp.obs.alerts import capture_alert
from saas_mvp.services.payment_ecpay import get_ecpay_client
from saas_mvp.services.payment_newebpay import NewebPayClient
from saas_mvp.services.platform_payment_config import payment_provider

from saas_mvp.routers.payments._shared import (
    _log, _render_autosubmit, router,
)

@router.get("/deposit/{trade_no}", response_class=HTMLResponse)
@router.get("/ecpay/deposit/{trade_no}", response_class=HTMLResponse)  # 舊連結 alias
def deposit_checkout(
    trade_no: str,
    db: Session = Depends(get_db),
):
    """定金付款頁（C4）。URL 以不可猜的 deposit_merchant_trade_no 為鍵(非可枚舉的
    reservation_id),未知一律 404,防跨租戶枚舉洩定金金額/狀態。依當下金流設定
    分派:stub 渲染本地模擬頁(離線/demo 全流程可走);ecpay 渲染綠界 AIO 表單;
    newebpay 渲染藍新 MPG 表單;linepay 打 Request API 後 302 至 LINE Pay 付款頁。
    舊路徑 /payments/ecpay/deposit/{trade_no} 保留為 alias(已寄出連結不斷)。"""
    from saas_mvp.services import deposit as deposit_svc

    resv = deposit_svc.find_by_trade_no(db, trade_no)
    if resv is None or resv.deposit_status is None:
        return HTMLResponse("<h1>找不到需付定金的預約</h1>", status_code=404)
    if resv.deposit_status == deposit_svc.DEPOSIT_PAID:
        return HTMLResponse("<h1>定金已付款</h1><p>您的預約已確認,可關閉本頁。</p>")
    if resv.deposit_status == deposit_svc.DEPOSIT_EXPIRED:
        return HTMLResponse("<h1>付款期限已過</h1><p>預約已取消,請重新預約。</p>")

    amount_twd = (resv.deposit_cents or 0) // 100
    provider = payment_provider(db, settings)
    base = settings.public_base_url.rstrip("/")
    if provider == "stub":
        # stub:本地模擬付款頁(按鈕打模擬回調)
        return HTMLResponse(
            "<!doctype html><meta charset='utf-8'><h1>模擬定金付款</h1>"
            f"<p>預約 #{resv.id},定金 NT${amount_twd}(stub 模式,不會真扣款)。</p>"
            f"<form method='post' action='/payments/stub/deposit-paid/{resv.deposit_merchant_trade_no}'>"
            "<button type='submit'>模擬付款成功</button></form>"
        )
    if provider == "ecpay":
        client = get_ecpay_client(db)
        form = client.build_order_form(
            merchant_trade_no=resv.deposit_merchant_trade_no,
            amount_twd=amount_twd,
            item_name="預約定金",
            trade_desc="LINE 預約定金",
            return_url=f"{base}/payments/ecpay/deposit-callback",
        )
        return _render_autosubmit(form, client.aio_url, "前往定金付款")
    if provider == "newebpay":
        client = NewebPayClient()
        if not (client.merchant_id and client.hash_key and client.hash_iv):
            # 藍新憑證未設定:不可渲染壞表單,更不可退化成免費模擬頁。
            return HTMLResponse(
                "<h1>定金付款暫不支援目前的金流設定</h1><p>請聯繫店家。</p>",
                status_code=503,
            )
        form = client.build_order_form(
            merchant_trade_no=resv.deposit_merchant_trade_no,
            amount_twd=amount_twd,
            item_desc="預約定金",
            return_url=f"{base}/payments/newebpay/done",
            notify_url=f"{base}/payments/newebpay/deposit-notify",
            client_back_url=f"{base}/payments/newebpay/done",
        )
        return _render_autosubmit(form, client.mpg_url, "前往定金付款")
    if provider == "linepay":
        from saas_mvp.services.payment_linepay import LinePayClient, LinePayError

        try:
            result = LinePayClient().request_payment(
                order_id=resv.deposit_merchant_trade_no,
                amount_twd=amount_twd,
                currency=settings.currency or "TWD",
                confirm_url=(
                    f"{base}/payments/linepay/deposit-confirm"
                    f"?orderId={resv.deposit_merchant_trade_no}"
                ),
                cancel_url=f"{base}/payments/linepay/deposit-cancel",
                item_name="預約定金",
            )
        except LinePayError as exc:
            # 顧客點連結當下同步外呼:失敗/逾時渲染友善錯誤頁,不可 500。
            _log.warning("linepay deposit request failed resv=%d: %s", resv.id, exc)
            capture_alert(f"payment: linepay deposit request failed resv={resv.id}")
            return HTMLResponse(
                "<h1>付款服務暫時無法使用</h1><p>請稍後重試或聯繫店家。</p>",
                status_code=502,
            )
        # txid↔reservation 綁定;重新進頁會覆寫為最新 txid(舊 confirm 自然失效)。
        resv.deposit_payment_txn_id = result["transaction_id"] or None
        db.commit()
        return RedirectResponse(result["payment_url"], status_code=302)
    # 未知 provider:**絕不可**退化成免費模擬頁 —— stub 的 POST 端點會未收款就
    # 標 paid,等於在正式金流設定下開放公開的免費定金繞過。
    return HTMLResponse(
        "<h1>定金付款暫不支援目前的金流設定</h1>"
        "<p>請聯繫店家。</p>",
        status_code=503,
    )


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


@router.post("/newebpay/deposit-notify", response_class=PlainTextResponse)
async def newebpay_deposit_notify(
    request: Request,
    db: Session = Depends(get_db),
):
    """藍新定金回調:驗 TradeSha → 解密 → trade_no 查單 → 金額交叉驗 → 冪等標 paid。"""
    form = await request.form()
    params = {k: str(v) for k, v in form.items()}
    return await run_in_threadpool(_handle_newebpay_deposit_notify, db, params)


def _handle_newebpay_deposit_notify(db: Session, params: dict) -> PlainTextResponse:
    from saas_mvp.services import deposit as deposit_svc

    client = NewebPayClient()
    if not client.verify(params):
        _log.warning("newebpay deposit-notify rejected: bad TradeSha")
        capture_alert("payment: newebpay deposit-notify bad TradeSha")
        return PlainTextResponse("0|TradeSha Error")

    try:
        info = client.decrypt_trade_info(params.get("TradeInfo", ""))
        result = info.get("Result") if isinstance(info.get("Result"), dict) else info
        trade_no = result.get("MerchantOrderNo") or info.get("MerchantOrderNo") or ""
    except Exception:  # noqa: BLE001 — 解密/解析失敗即視為無效通知
        _log.warning("newebpay deposit-notify rejected: TradeInfo decrypt/parse failed")
        return PlainTextResponse("0|decrypt failed")

    resv = deposit_svc.find_by_trade_no(db, trade_no) if trade_no else None
    if resv is None:
        _log.warning("newebpay deposit-notify: reservation not found %s", trade_no)
        return PlainTextResponse("0|reservation not found")

    if str(info.get("Status", "")).upper() != "SUCCESS":
        return PlainTextResponse("1|OK")  # 付款失敗:不動狀態,等逾時或重付

    try:
        paid_amt = int(result.get("Amt", info.get("Amt", "0")))
    except (ValueError, TypeError):
        paid_amt = -1
    if paid_amt != (resv.deposit_cents or 0) // 100:
        _log.warning(
            "newebpay deposit amount mismatch resv=%d expected=%d got=%s",
            resv.id, (resv.deposit_cents or 0) // 100, paid_amt,
        )
        capture_alert("payment: deposit callback amount mismatch")
        return PlainTextResponse("0|amount mismatch")

    if not deposit_svc.mark_paid(
        db,
        resv,
        provider="newebpay",
        provider_merchant_id=client.merchant_id,
        provider_trade_no=(str(result.get("TradeNo") or "") or None),
        payment_type=(str(result.get("PaymentType") or "") or None),
    ):
        capture_alert(f"payment: deposit paid AFTER expiry resv={resv.id}")
    return PlainTextResponse("1|OK")


@router.get("/linepay/deposit-confirm", response_class=HTMLResponse)
def linepay_deposit_confirm(
    transactionId: str = "",
    orderId: str = "",
    db: Session = Depends(get_db),
):
    """LINE Pay 定金付款完成 redirect → Confirm API → 冪等標 paid。

    orderId 是不可猜的 deposit_merchant_trade_no;transactionId 必須與
    checkout 時落庫的 ``deposit_payment_txn_id`` 一致(txid↔reservation 綁定);
    金額以 **DB deposit_cents 為準**傳入 Confirm。
    """
    from saas_mvp.services import deposit as deposit_svc
    from saas_mvp.services.payment_linepay import LinePayClient, LinePayError

    resv = deposit_svc.find_by_trade_no(db, orderId) if orderId else None
    if resv is None or resv.deposit_status is None:
        return HTMLResponse("<h1>找不到需付定金的預約</h1>", status_code=404)
    if resv.deposit_status == deposit_svc.DEPOSIT_PAID:
        return HTMLResponse("<h1>✅ 定金已付款</h1><p>您的預約已確認,可關閉本頁。</p>")
    if not transactionId:
        return HTMLResponse("<h1>缺少交易編號</h1>", status_code=400)
    if not resv.deposit_payment_txn_id or transactionId != resv.deposit_payment_txn_id:
        _log.warning(
            "linepay deposit-confirm txid mismatch resv=%d got=%s",
            resv.id, transactionId,
        )
        capture_alert(f"payment: linepay deposit txid mismatch resv={resv.id}")
        return HTMLResponse(
            "<h1>交易編號不符</h1><p>請回 LINE 重新取得付款連結。</p>",
            status_code=400,
        )

    try:
        LinePayClient().confirm_payment(
            transaction_id=transactionId,
            amount_twd=(resv.deposit_cents or 0) // 100,
            currency=settings.currency or "TWD",
        )
    except LinePayError as exc:
        _log.warning("linepay deposit confirm failed resv=%d: %s", resv.id, exc)
        capture_alert(f"payment: linepay deposit confirm failed resv={resv.id}")
        return HTMLResponse(
            "<h1>付款確認失敗</h1><p>請回 LINE Pay 重試或聯繫店家。</p>",
            status_code=502,
        )

    if not deposit_svc.mark_paid(
        db,
        resv,
        provider="linepay",
        provider_trade_no=transactionId[:20],
        payment_type="linepay",
    ):
        # 過期單付款成功:名額可能已釋出 — 告警人工處理退款
        capture_alert(f"payment: deposit paid AFTER expiry resv={resv.id}")
        return HTMLResponse(
            "<h1>付款期限已過</h1><p>款項已收到但預約已逾時取消,店家將與您聯繫退款。</p>"
        )
    return HTMLResponse("<h1>✅ 定金已付款</h1><p>您的預約已確認,可關閉本頁。</p>")


@router.get("/linepay/deposit-cancel", response_class=HTMLResponse)
def linepay_deposit_cancel():
    """顧客在 LINE Pay 取消定金付款:不動預約狀態,連結仍可重付。"""
    return HTMLResponse(
        "<h1>已取消付款</h1><p>預約仍保留為待付定金,請於期限內重新付款。</p>"
    )


