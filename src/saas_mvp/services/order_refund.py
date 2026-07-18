"""訂單退款(R6-A3)— 三金流閘道退款 + 手動對帳,比照定金退款架構。

退款金額 = 閘道實付(order.total_cents,禮物卡/點數折抵後的餘額);禮物卡/點數
的回沖屬另一路徑(取消訂單時)。狀態機、部分退款、崩潰安全均比照定金:
* **外呼退款 API 前先 commit PROCESSING**(A2 審查教訓):崩潰只卡 PROCESSING,
  不會回退成可重退 → 防重複退款。
* 逾時/結果不確定 → manual_required 不自動重送;明確拒絕 → failed(可重試)。
* LINE Pay / 藍新原生支援多次部分退款;ECPay 第一次 AUTO 後轉人工(同定金)。
"""

from __future__ import annotations

import datetime
import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from saas_mvp.config import settings
from saas_mvp.models.order import ORDER_PAID, Order

_log = logging.getLogger(__name__)

REFUND_PROCESSING = "processing"
REFUND_REFUNDED = "refunded"
REFUND_PARTIAL = "partially_refunded"
REFUND_FAILED = "failed"
REFUND_MANUAL_REQUIRED = "manual_required"


class OrderRefundError(ValueError):
    """退款不可執行或金流明確拒絕;訊息可安全顯示於 owner 後台。"""


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _remaining(order: Order) -> int:
    return (order.total_cents or 0) - (order.refunded_cents or 0)


def _validate_amount(order: Order, amount_cents: int | None) -> int:
    remaining = _remaining(order)
    amount = amount_cents if amount_cents is not None else remaining
    if amount <= 0 or amount > remaining:
        raise OrderRefundError(
            f"退款金額需大於 0 且不可超過可退餘額({remaining // 100} 元)。"
        )
    if amount % 100 != 0:
        raise OrderRefundError("退款金額需為整數元。")
    return amount


def _manual_required(db: Session, order: Order, message: str) -> None:
    order.refund_status = REFUND_MANUAL_REQUIRED
    order.refund_error = message[:255]
    db.commit()
    raise OrderRefundError(message)


def _apply_success(order: Order, amount: int, *, provider_code: str | None) -> None:
    order.refunded_cents = (order.refunded_cents or 0) + amount
    order.refunded_at = _utcnow()
    order.refund_error = None
    if provider_code is not None:
        order.refund_provider_code = provider_code
    order.refund_status = (
        REFUND_REFUNDED if _remaining(order) <= 0 else REFUND_PARTIAL
    )


def _gift_card_refund_guard(db: Session, order: Order) -> None:
    """R11-A 對抗審查:購卡訂單退款守衛。

    卡片已被折抵(balance < 面額)→ 擋退款(否則商家現金退了、
    已用掉的額度也吞了=雙重損失);未使用 → 放行,退款成功後由
    _void_purchased_card 於同交易作廢,買家不能退了錢還留著可用的卡。
    """
    from saas_mvp.services import gift_card_sales as sales_svc
    from saas_mvp.services import gift_cards as gift_cards_svc

    purchase = sales_svc.purchase_for_order(db, order.id)
    if purchase is None or purchase.gift_card_id is None:
        return
    balance = gift_cards_svc.balance_cents(
        db, tenant_id=order.tenant_id, gift_card_id=purchase.gift_card_id
    )
    if balance < purchase.amount_cents:
        raise OrderRefundError(
            "此禮物卡已被部分或全部使用,無法退款;如需處理請先作廢卡片並與顧客另行結算。"
        )


def _void_purchased_card(db: Session, order: Order, actor_user_id: int | None) -> None:
    """退款成功後同交易作廢已售出的卡(冪等;非購卡訂單 no-op)。"""
    from saas_mvp.services import gift_card_sales as sales_svc
    from saas_mvp.services import gift_cards as gift_cards_svc

    purchase = sales_svc.purchase_for_order(db, order.id)
    if purchase is None or purchase.gift_card_id is None:
        return
    gift_cards_svc.void_card(
        db,
        tenant_id=order.tenant_id,
        gift_card_id=purchase.gift_card_id,
        note="線上購卡退款作廢",
        actor_user_id=actor_user_id,
    )


def _linepay_refund(db, order, amount, *, client=None) -> Order:
    from saas_mvp.services.payment_linepay import LinePayClient

    if not order.payment_txn_id:
        _manual_required(db, order, "缺少 LINE Pay 交易編號，請在 LINE Pay 後台核對並人工退款。")
    client = client or LinePayClient()
    try:
        resp = client.refund(
            transaction_id=order.payment_txn_id, refund_amount_twd=amount // 100
        )
    except Exception as exc:  # noqa: BLE001 — 逾時結果不確定,不自動重送
        _manual_required(
            db, order,
            f"退款結果不確定（{type(exc).__name__}），請先到 LINE Pay 後台核對，勿重複送出。",
        )
    code = str(resp.get("returnCode") or "")[:32]
    order.refund_provider_code = code or None
    if code == "0000":
        _apply_success(order, amount, provider_code=code)
        _void_purchased_card(db, order, order.refund_requested_by_user_id)
        db.commit()
        return order
    msg = " ".join(str(resp.get("returnMessage") or "LINE Pay 拒絕退款").split())[:160]
    order.refund_status = REFUND_FAILED
    order.refund_error = msg
    db.commit()
    raise OrderRefundError(f"LINE Pay 拒絕退款：{msg}")


def _newebpay_refund(db, order, amount, *, client=None) -> Order:
    from saas_mvp.services.payment_newebpay import NewebPayClient

    if not (order.merchant_trade_no and order.provider_trade_no):
        _manual_required(db, order, "缺少藍新交易編號，請在藍新後台核對並人工退款。")
    client = client or NewebPayClient()
    try:
        resp = client.refund(
            merchant_order_no=order.merchant_trade_no,
            trade_no=order.provider_trade_no,
            amount_twd=amount // 100,
        )
    except Exception as exc:  # noqa: BLE001
        _manual_required(
            db, order,
            f"退款結果不確定（{type(exc).__name__}），請先到藍新後台核對，勿重複送出。",
        )
    code = str(resp.get("Status") or "")[:32]
    order.refund_provider_code = code or None
    if code == "SUCCESS":
        _apply_success(order, amount, provider_code=code)
        _void_purchased_card(db, order, order.refund_requested_by_user_id)
        db.commit()
        return order
    msg = " ".join(str(resp.get("Message") or "藍新拒絕退款").split())[:160]
    order.refund_status = REFUND_FAILED
    order.refund_error = msg
    db.commit()
    raise OrderRefundError(f"藍新拒絕退款：{msg}")


def _ecpay_refund(db, order, amount, *, client=None) -> Order:
    # ECPay 多次部分退刷是否被接受無法確證 → 第一次 AUTO 後轉人工(同定金)。
    if (order.refunded_cents or 0) > 0:
        _manual_required(
            db, order,
            "已有一筆綠界退刷紀錄;剩餘金額請至綠界後台退刷後,於系統確認人工退款。",
        )
    if not (order.merchant_trade_no and order.provider_trade_no and order.provider_merchant_id):
        _manual_required(db, order, "缺少綠界交易編號，請在綠界後台核對並人工退款。")

    from saas_mvp.services.platform_payment_config import effective_payment_config

    config = effective_payment_config(db, settings)
    if config.provider != "ecpay" or config.environment != "prod":
        _manual_required(db, order, "綠界自動退刷僅支援正式環境，請人工核對退款。")
    if config.merchant_id != order.provider_merchant_id:
        _manual_required(db, order, "目前綠界商店與原交易不同，請使用原商店人工退款。")

    if client is None:
        from saas_mvp.services.payment_ecpay import get_ecpay_client

        client = get_ecpay_client(db)
    try:
        resp = client.refund_credit(
            merchant_trade_no=order.merchant_trade_no,
            trade_no=order.provider_trade_no,
            amount_twd=amount // 100,
        )
    except Exception as exc:  # noqa: BLE001
        _manual_required(
            db, order,
            f"退款結果不確定（{type(exc).__name__}），請先到綠界後台核對，勿重複送出。",
        )
    code = str(resp.get("RtnCode") or "")[:32]
    order.refund_provider_code = code or None
    if code == "1":
        _apply_success(order, amount, provider_code=code)
        _void_purchased_card(db, order, order.refund_requested_by_user_id)
        db.commit()
        return order
    msg = " ".join(str(resp.get("RtnMsg") or "綠界拒絕退款").split())[:160]
    order.refund_status = REFUND_FAILED
    order.refund_error = msg
    db.commit()
    raise OrderRefundError(f"綠界拒絕退款：{msg}")


def request_order_refund(
    db: Session,
    *,
    tenant_id: int,
    order_id: int,
    actor_user_id: int,
    amount_cents: int | None = None,
    ecpay_client=None,
    linepay_client=None,
    newebpay_client=None,
) -> Order:
    """訂單閘道退款(可部分);鎖列防雙擊/多 worker 重複退。"""
    order = db.execute(
        select(Order).where(Order.id == order_id, Order.tenant_id == tenant_id)
        .with_for_update()
    ).scalar_one_or_none()
    if order is None:
        raise OrderRefundError("訂單不存在。")
    if order.status != ORDER_PAID:
        raise OrderRefundError("僅已付款訂單可退款。")
    if order.refund_status == REFUND_REFUNDED:
        return order  # 已全額退款(終態),冪等
    if order.refund_status == REFUND_PROCESSING:
        raise OrderRefundError("退款正在處理，請勿重複送出。")
    if order.refund_status == REFUND_MANUAL_REQUIRED:
        raise OrderRefundError("此筆退款需要先到金流後台核對，不能自動重送。")
    if (order.total_cents or 0) <= 0:
        raise OrderRefundError("此訂單無閘道實付金額可退(禮物卡/點數請另行處理)。")
    _gift_card_refund_guard(db, order)
    amount = _validate_amount(order, amount_cents)

    order.refund_status = REFUND_PROCESSING
    order.refund_attempts = (order.refund_attempts or 0) + 1
    order.refund_requested_at = _utcnow()
    order.refund_requested_by_user_id = actor_user_id
    order.refund_error = None
    order.refund_provider_code = None
    # 崩潰安全(A2 教訓):外呼前先持久化 PROCESSING → 崩潰卡 PROCESSING 不重退。
    db.commit()

    provider = (order.payment_provider or "").lower()
    if provider == "stub":
        _apply_success(order, amount, provider_code="STUB")
        _void_purchased_card(db, order, order.refund_requested_by_user_id)
        db.commit()
        return order
    if provider == "linepay":
        return _linepay_refund(db, order, amount, client=linepay_client)
    if provider == "newebpay":
        return _newebpay_refund(db, order, amount, client=newebpay_client)
    if provider == "ecpay":
        return _ecpay_refund(db, order, amount, client=ecpay_client)
    _manual_required(db, order, "缺少原付款交易資料，請在金流後台核對並人工退款。")


def confirm_manual_refund(
    db: Session,
    *,
    tenant_id: int,
    order_id: int,
    actor_user_id: int,
    note: str,
    amount_cents: int | None = None,
) -> Order:
    """owner 在外部金流後台完成退款後,於系統對帳為已退款(可部分)。"""
    note = " ".join(note.strip().split())
    if not 2 <= len(note) <= 200:
        raise OrderRefundError("人工退款備註需為 2–200 字。")
    order = db.execute(
        select(Order).where(Order.id == order_id, Order.tenant_id == tenant_id)
        .with_for_update()
    ).scalar_one_or_none()
    if order is None:
        raise OrderRefundError("訂單不存在。")
    if order.status != ORDER_PAID:
        raise OrderRefundError("僅已付款訂單可退款。")
    if order.refund_status == REFUND_REFUNDED:
        return order
    # 崩潰安全對應(A3 審查):PROCESSING 期間(auto 退款已 commit PROCESSING、鎖
    # 已釋放、外呼進行中)不可人工對帳 —— 否則手動 _apply_success 與稍後回來的
    # auto _apply_success 雙重加總 → 超額退/丟失更新。比照 deposit.confirm_manual_refund。
    if order.refund_status == REFUND_PROCESSING:
        raise OrderRefundError("退款仍在處理中，請先確認金流結果再對帳。")
    _gift_card_refund_guard(db, order)
    amount = _validate_amount(order, amount_cents)
    _apply_success(order, amount, provider_code="MANUAL")
    order.refund_error = f"人工退款：{note}"[:255]
    order.refund_requested_by_user_id = actor_user_id
    _void_purchased_card(db, order, actor_user_id)
    db.commit()
    return order
