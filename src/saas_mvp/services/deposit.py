"""定金服務（C4 防 no-show）。

流程:線上建單(book_slot)時若租戶開 DEPOSIT_PAYMENT 且 deposit_cents>0 →
快照定金欄位(pending + 逾時點 + 唯一 trade_no)→ 回覆附付款連結 →
綠界回調 mark_paid(冪等)→ 逾時未付由 cron 取消回補名額。
"""

from __future__ import annotations

import datetime
import secrets

from sqlalchemy import select
from sqlalchemy.orm import Session

from saas_mvp.config import settings
from saas_mvp.models.reservation import (
    RESERVATION_CANCELLED,
    RESERVATION_CONFIRMED,
    Reservation,
)

DEPOSIT_PENDING = "pending"
DEPOSIT_PAID = "paid"
DEPOSIT_EXPIRED = "expired"
DEPOSIT_REFUNDED = "refunded"

REFUND_PROCESSING = "processing"
REFUND_REFUNDED = "refunded"
REFUND_FAILED = "failed"
REFUND_MANUAL_REQUIRED = "manual_required"


class DepositRefundError(ValueError):
    """退款不可執行或金流明確拒絕；訊息可安全顯示於 owner 後台。"""


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _base36(n: int) -> str:
    chars = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    out = ""
    while n:
        n, r = divmod(n, 36)
        out = chars[r] + out
    return out or "0"


def gen_trade_no(reservation_id: int) -> str:
    """≤20 英數、唯一且**不可猜**(綠界 MerchantTradeNo 上限 20)。

    格式:DP + id36 + 隨機 hex 填滿至 20。隨機段(≥32-bit、一般 48-bit)是定金
    付款頁的 capability key —— URL 以 trade_no 為鍵而非可枚舉的 reservation_id,
    未授權者無法枚舉/竊改他人定金(PEA-1/PEA-2)。id36 前綴確保跨預約唯一。
    """
    prefix = f"DP{_base36(reservation_id)}"
    rand_len = max(6, 20 - len(prefix))
    return (prefix + secrets.token_hex(rand_len).upper())[:20]


def tenant_deposit_required(db: Session, tenant) -> bool:
    """該租戶是否啟用定金(flag 開 + 金額 > 0)。"""
    from saas_mvp.services import features as features_svc

    return bool(
        (tenant.deposit_cents or 0) > 0
        and features_svc.is_enabled(db, tenant.id, features_svc.DEPOSIT_PAYMENT)
    )


def apply_deposit_snapshot(db: Session, tenant, resv: Reservation) -> None:
    """建單交易內快照定金欄位(不 commit,隨 book_slot 一起提交)。"""
    hold = tenant.deposit_hold_minutes or settings.deposit_hold_minutes_default
    resv.deposit_cents = tenant.deposit_cents
    resv.deposit_status = DEPOSIT_PENDING
    resv.deposit_merchant_trade_no = gen_trade_no(resv.id or 0) if resv.id else None
    resv.deposit_expires_at = _utcnow() + datetime.timedelta(minutes=hold)


def ensure_trade_no(db: Session, resv: Reservation) -> str:
    """flush 後補齊 trade_no(建單時 id 可能尚未產生)。"""
    if not resv.deposit_merchant_trade_no:
        resv.deposit_merchant_trade_no = gen_trade_no(resv.id)
    return resv.deposit_merchant_trade_no


def payment_url(resv: Reservation) -> str:
    # URL 以不可猜的 deposit_merchant_trade_no 為鍵(非可枚舉的 resv.id),
    # 防未授權者枚舉/竊改他人定金(PEA-1/PEA-2)。
    # provider 中立路徑:進頁時依當下金流設定分派(stub/ecpay/newebpay/linepay);
    # 舊 /payments/ecpay/deposit/{trade_no} 保留為 alias,已寄出連結不斷。
    base = settings.public_base_url.rstrip("/") or ""
    return f"{base}/payments/deposit/{resv.deposit_merchant_trade_no}"


def deposit_prompt(resv: Reservation, tenant) -> str:
    """建單成功後的付款提示文字(bot 與網頁表單共用)。"""
    hold = tenant.deposit_hold_minutes or settings.deposit_hold_minutes_default
    amount = (resv.deposit_cents or 0) // 100
    return (
        f"請於 {hold} 分鐘內完成定金 NT${amount} 付款以保留預約,"
        "逾時將自動取消。"
    )


def find_by_trade_no(db: Session, trade_no: str) -> Reservation | None:
    return db.execute(
        select(Reservation).where(
            Reservation.deposit_merchant_trade_no == trade_no
        )
    ).scalar_one_or_none()


def mark_paid(
    db: Session,
    resv: Reservation,
    *,
    provider: str | None = None,
    provider_merchant_id: str | None = None,
    provider_trade_no: str | None = None,
    payment_type: str | None = None,
) -> bool:
    """標記已付並保存退款所需交易快照（冪等）。commit。"""
    if resv.deposit_status == DEPOSIT_PAID:
        # 綠界重送 callback 時可補齊舊版第一次回調尚未保存的退款欄位。
        if provider and not resv.deposit_provider:
            resv.deposit_provider = provider
        if provider_merchant_id and not resv.deposit_provider_merchant_id:
            resv.deposit_provider_merchant_id = provider_merchant_id
        if provider_trade_no and not resv.deposit_provider_trade_no:
            resv.deposit_provider_trade_no = provider_trade_no
        if payment_type and not resv.deposit_payment_type:
            resv.deposit_payment_type = payment_type
        db.commit()
        return True
    if resv.deposit_status != DEPOSIT_PENDING:
        return False  # expired/None:過期單付款成功屬異常,交回調端告警
    resv.deposit_status = DEPOSIT_PAID
    resv.deposit_paid_at = _utcnow()
    resv.deposit_provider = provider
    resv.deposit_provider_merchant_id = provider_merchant_id
    resv.deposit_provider_trade_no = provider_trade_no
    resv.deposit_payment_type = payment_type
    db.commit()
    return True


def _manual_required(db: Session, resv: Reservation, message: str) -> None:
    resv.deposit_refund_status = REFUND_MANUAL_REQUIRED
    resv.deposit_refund_error = message[:255]
    db.commit()
    raise DepositRefundError(message)


def _provider_message(response: dict) -> str:
    raw = str(response.get("RtnMsg") or "金流服務拒絕退款")
    return " ".join(raw.split())[:160]


def request_full_refund(
    db: Session,
    *,
    tenant_id: int,
    reservation_id: int,
    actor_user_id: int,
    ecpay_client=None,
) -> Reservation:
    """取消後的已付定金全額退款；鎖列防雙擊／多 worker 重複退刷。

    明確拒絕可修正後重試；網路或回應解析失敗屬結果不確定，轉
    ``manual_required``，禁止自動重試，必須先到金流後台核對。
    """
    resv = db.execute(
        select(Reservation)
        .where(Reservation.id == reservation_id, Reservation.tenant_id == tenant_id)
        .with_for_update()
    ).scalar_one_or_none()
    if resv is None:
        raise DepositRefundError("預約不存在。")
    if resv.status != RESERVATION_CANCELLED:
        raise DepositRefundError("請先取消預約，再執行定金退款。")
    if resv.deposit_status == DEPOSIT_REFUNDED or resv.deposit_refund_status == REFUND_REFUNDED:
        return resv
    if resv.deposit_status != DEPOSIT_PAID:
        raise DepositRefundError("此預約沒有可退款的已付定金。")
    if resv.deposit_refund_status == REFUND_PROCESSING:
        raise DepositRefundError("退款正在處理，請勿重複送出。")
    if resv.deposit_refund_status == REFUND_MANUAL_REQUIRED:
        raise DepositRefundError("此筆退款需要先到金流後台核對，不能自動重送。")

    resv.deposit_refund_status = REFUND_PROCESSING
    resv.deposit_refund_attempts = (resv.deposit_refund_attempts or 0) + 1
    resv.deposit_refund_requested_at = _utcnow()
    resv.deposit_refund_requested_by_user_id = actor_user_id
    resv.deposit_refund_error = None
    resv.deposit_refund_provider_code = None

    provider = (resv.deposit_provider or "").lower()
    if provider == "stub":
        resv.deposit_status = DEPOSIT_REFUNDED
        resv.deposit_refund_status = REFUND_REFUNDED
        resv.deposit_refunded_at = _utcnow()
        resv.deposit_refund_provider_code = "STUB"
        db.commit()
        return resv
    if provider != "ecpay":
        _manual_required(db, resv, "缺少原付款交易資料，請在金流後台核對並人工退款。")
    if not (resv.deposit_payment_type or "").lower().startswith("credit"):
        _manual_required(db, resv, "此筆不是信用卡付款，請依原付款方式人工退款。")
    if (
        not resv.deposit_merchant_trade_no
        or not resv.deposit_provider_trade_no
        or not resv.deposit_provider_merchant_id
    ):
        _manual_required(db, resv, "缺少綠界交易編號，請在綠界後台核對並人工退款。")

    from saas_mvp.services.platform_payment_config import effective_payment_config

    config = effective_payment_config(db, settings)
    if config.provider != "ecpay" or config.environment != "prod":
        _manual_required(db, resv, "綠界自動退刷僅支援正式環境，請人工核對退款。")
    if config.merchant_id != resv.deposit_provider_merchant_id:
        _manual_required(db, resv, "目前綠界商店與原交易不同，請使用原商店人工退款。")

    if ecpay_client is None:
        from saas_mvp.services.payment_ecpay import get_ecpay_client

        ecpay_client = get_ecpay_client(db)
    try:
        response = ecpay_client.refund_credit(
            merchant_trade_no=resv.deposit_merchant_trade_no,
            trade_no=resv.deposit_provider_trade_no,
            amount_twd=(resv.deposit_cents or 0) // 100,
        )
    except Exception as exc:  # noqa: BLE001 — timeout 後結果可能已成功，不得自動重送
        _manual_required(
            db,
            resv,
            f"退款結果不確定（{type(exc).__name__}），請先到綠界後台核對，勿重複送出。",
        )

    code = str(response.get("RtnCode") or "")[:32]
    resv.deposit_refund_provider_code = code or None
    if code == "1":
        resv.deposit_status = DEPOSIT_REFUNDED
        resv.deposit_refund_status = REFUND_REFUNDED
        resv.deposit_refunded_at = _utcnow()
        resv.deposit_refund_error = None
        db.commit()
        return resv

    message = _provider_message(response)
    resv.deposit_refund_status = REFUND_FAILED
    resv.deposit_refund_error = message
    db.commit()
    raise DepositRefundError(f"綠界拒絕退款：{message}")


def confirm_manual_refund(
    db: Session,
    *,
    tenant_id: int,
    reservation_id: int,
    actor_user_id: int,
    note: str,
) -> Reservation:
    """owner 在外部金流後台完成退款後，將本系統安全對帳為已退款。"""
    note = " ".join(note.strip().split())
    if not 2 <= len(note) <= 200:
        raise DepositRefundError("人工退款備註需為 2–200 字。")
    resv = db.execute(
        select(Reservation)
        .where(Reservation.id == reservation_id, Reservation.tenant_id == tenant_id)
        .with_for_update()
    ).scalar_one_or_none()
    if resv is None:
        raise DepositRefundError("預約不存在。")
    if resv.status != RESERVATION_CANCELLED or resv.deposit_status != DEPOSIT_PAID:
        raise DepositRefundError("此預約沒有可確認的人工定金退款。")
    if resv.deposit_refund_status == REFUND_PROCESSING:
        raise DepositRefundError("退款仍在處理中，請先確認金流結果。")
    resv.deposit_status = DEPOSIT_REFUNDED
    resv.deposit_refund_status = REFUND_REFUNDED
    resv.deposit_refunded_at = _utcnow()
    resv.deposit_refund_requested_at = resv.deposit_refund_requested_at or _utcnow()
    resv.deposit_refund_requested_by_user_id = actor_user_id
    resv.deposit_refund_error = f"人工確認：{note}"[:255]
    resv.deposit_refund_provider_code = "MANUAL"
    db.commit()
    return resv


def list_expired_pending(
    db: Session, *, now: datetime.datetime | None = None, limit: int = 200
) -> list[Reservation]:
    """逾時未付且預約仍 confirmed 的清單(供 cron 取消)。"""
    effective_now = now or _utcnow()
    rows = db.execute(
        select(Reservation).where(
            Reservation.deposit_status == DEPOSIT_PENDING,
            Reservation.status == RESERVATION_CONFIRMED,
        ).order_by(Reservation.id).limit(limit)
    ).scalars().all()
    naive_now = effective_now.replace(tzinfo=None)
    out = []
    for r in rows:
        exp = r.deposit_expires_at
        if exp is None:
            continue
        cmp = naive_now if exp.tzinfo is None else effective_now
        if exp < cmp:
            out.append(r)
    return out
