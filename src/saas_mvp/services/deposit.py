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
from saas_mvp.models.reservation import RESERVATION_CONFIRMED, Reservation

DEPOSIT_PENDING = "pending"
DEPOSIT_PAID = "paid"
DEPOSIT_EXPIRED = "expired"


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
    """≤20 英數唯一(綠界要求):DP + id36 + 時間36 + 2hex。"""
    return (
        f"DP{_base36(reservation_id)}T{_base36(int(_utcnow().timestamp()))}"
        f"{secrets.token_hex(1).upper()}"
    )[:20]


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
    base = settings.public_base_url.rstrip("/") or ""
    return f"{base}/payments/ecpay/deposit/{resv.id}"


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


def mark_paid(db: Session, resv: Reservation) -> bool:
    """標記已付(冪等;已 paid 回 True 不重寫)。commit。"""
    if resv.deposit_status == DEPOSIT_PAID:
        return True
    if resv.deposit_status != DEPOSIT_PENDING:
        return False  # expired/None:過期單付款成功屬異常,交回調端告警
    resv.deposit_status = DEPOSIT_PAID
    resv.deposit_paid_at = _utcnow()
    db.commit()
    return True


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
