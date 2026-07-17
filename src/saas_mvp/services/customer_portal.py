"""顧客自助入口網(R5-B1「我的預約」)— token 簽發/解析 + 預約檢視資料。

token 即能力:`Customer.portal_token`(migration 0048)為**長效**憑證——
這是顧客的常駐入口(提醒簡訊/LINE 訊息附連結),與 booking_form 的 30 分
一次性 token 不同。可經 rotate_portal_token 輪替(舊連結即失效)。

安全邊界:
  * token 不可猜(secrets.token_urlsafe(32)),解析失敗一律 404 不洩漏存在性。
  * 所有寫入動作走 services/booking 的 customer_id 擁有者驗證路徑,
    portal 層絕不繞過 service 層直接改 DB。
"""

from __future__ import annotations

import datetime
import secrets

from sqlalchemy import select
from sqlalchemy.orm import Session

from saas_mvp.config import settings
from saas_mvp.models.booking_slot import BookingSlot
from saas_mvp.models.customer import Customer
from saas_mvp.models.reservation import (
    RESERVATION_CANCELLED,
    RESERVATION_CONFIRMED,
    Reservation,
)
from saas_mvp.models.service import Service
from saas_mvp.models.staff import Staff


class PortalTokenNotFound(Exception):
    """token 查無(不存在或已輪替);對外一律 404。"""


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def ensure_portal_token(db: Session, customer: Customer) -> str:
    """惰性產生並 commit 顧客的 portal_token(已有則直接回傳,比照 ics_token)。"""
    if customer.portal_token:
        return customer.portal_token
    customer.portal_token = secrets.token_urlsafe(32)
    db.commit()
    db.refresh(customer)
    return customer.portal_token


def rotate_portal_token(db: Session, customer: Customer) -> str:
    """輪替 token(舊連結立即失效);顧客自助或店家代操作皆經此。"""
    customer.portal_token = secrets.token_urlsafe(32)
    db.commit()
    db.refresh(customer)
    return customer.portal_token


def portal_url(customer: Customer) -> str | None:
    """組顧客入口網完整 URL;未設 public_base_url 或尚無 token 回 None。

    唯讀:不簽發 token(簽發需明確走 ensure_portal_token,避免讀路徑寫 DB)。
    """
    base = settings.public_base_url.rstrip("/")
    if not base or not customer.portal_token:
        return None
    return f"{base}/booking/my/{customer.portal_token}"


def resolve_portal_token(db: Session, token: str) -> Customer:
    """token → Customer;查無拋 PortalTokenNotFound(對外 404)。"""
    if not token or len(token) > 64:
        raise PortalTokenNotFound()
    customer = db.execute(
        select(Customer).where(Customer.portal_token == token)
    ).scalar_one_or_none()
    if customer is None:
        raise PortalTokenNotFound()
    return customer


def portal_reservations(
    db: Session,
    customer: Customer,
    *,
    now: datetime.datetime | None = None,
    history_limit: int = 5,
) -> dict:
    """入口網預約檢視資料:未來 confirmed + 近期歷史(含已取消)。

    回傳 {"upcoming": [row...], "history": [row...]};row 為 dict:
    reservation / slot_start / slot_end / service_name / staff_name。
    join 一次取齊顯示欄位,不觸發 N+1。
    """
    now = now or _utcnow()
    rows = db.execute(
        select(Reservation, BookingSlot, Service.name, Staff.name)
        .join(BookingSlot, Reservation.slot_id == BookingSlot.id)
        .outerjoin(Service, Reservation.service_id == Service.id)
        .outerjoin(Staff, Reservation.staff_id == Staff.id)
        .where(
            Reservation.tenant_id == customer.tenant_id,
            Reservation.customer_id == customer.id,
        )
        .order_by(BookingSlot.slot_start.asc())
    ).all()

    def _row(resv, slot, service_name, staff_name):
        return {
            "reservation": resv,
            "slot_start": slot.slot_start,
            "slot_end": slot.slot_end,
            "service_name": service_name,
            "staff_name": staff_name,
        }

    naive_now = now.replace(tzinfo=None)

    def _is_future(slot) -> bool:
        start = slot.slot_start
        if start.tzinfo is not None:
            return start >= now
        return start >= naive_now

    upcoming = [
        _row(*r)
        for r in rows
        if r[0].status == RESERVATION_CONFIRMED and _is_future(r[1])
    ]
    history = [
        _row(*r)
        for r in rows
        if r[0].status == RESERVATION_CANCELLED or not _is_future(r[1])
    ]
    history.sort(key=lambda x: x["slot_start"], reverse=True)
    return {"upcoming": upcoming, "history": history[:history_limit]}
