"""顧客 CRM 服務層 — 店家端查詢 + 補欄位（phone/note）+ 刪除（PII）。

顧客檔由 LINE 預約流程自動建立（models/customer.upsert_customer_from_line）；
所有查詢走 tenant_query 強制隔離。
"""

from __future__ import annotations

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from saas_mvp.models.campaign_send import CampaignSend
from saas_mvp.models.coupon_redemption import CouponRedemption
from saas_mvp.models.customer import Customer
from saas_mvp.models.customer_tag_link import CustomerTagLink
from saas_mvp.models.line_message import LineMessage
from saas_mvp.models.order import Order
from saas_mvp.models.point_transaction import PointTransaction
from saas_mvp.models.reservation import Reservation
from saas_mvp.services.tenants import tenant_query


def _get_or_404(db: Session, tenant_id: int, customer_id: int) -> Customer:
    customer = (
        tenant_query(db, Customer, tenant_id)
        .filter(Customer.id == customer_id)
        .first()
    )
    if customer is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Customer not found"
        )
    return customer


def _customers_query(db: Session, tenant_id: int, q: str | None = None):
    query = tenant_query(db, Customer, tenant_id)
    if q:
        like = f"%{q}%"
        query = query.filter(
            (Customer.display_name.ilike(like)) | (Customer.phone.ilike(like))
        )
    return query


def list_customers(
    db: Session,
    *,
    tenant_id: int,
    q: str | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> list[Customer]:
    """列出租戶顧客（新→舊）。limit=None 回傳全部，內部呼叫端行為不變。

    q：以顯示名稱 / 電話模糊搜尋（後台顧客頁用）。
    """
    query = _customers_query(db, tenant_id, q).order_by(Customer.id.desc())
    if offset:
        query = query.offset(offset)
    if limit is not None:
        query = query.limit(limit)
    return query.all()


def count_customers(db: Session, *, tenant_id: int, q: str | None = None) -> int:
    """租戶顧客總數（供分頁 X-Total-Count / 後台頁碼）。"""
    return _customers_query(db, tenant_id, q).count()


def get_customer(db: Session, *, tenant_id: int, customer_id: int) -> Customer:
    return _get_or_404(db, tenant_id, customer_id)


def update_customer(
    db: Session,
    *,
    tenant_id: int,
    customer_id: int,
    phone: str | None = None,
    note: str | None = None,
) -> Customer:
    customer = _get_or_404(db, tenant_id, customer_id)
    if phone is not None:
        customer.phone = phone
    if note is not None:
        customer.note = note
    db.commit()
    db.refresh(customer)
    return customer


def set_blacklist(
    db: Session,
    *,
    tenant_id: int,
    customer_id: int,
    blacklisted: bool,
    reason: str | None = None,
) -> Customer:
    """設定顧客黑名單狀態（硬性阻擋線上預約）。

    blacklisted=True 時記錄 reason（選填）；解除時一併清空 reason。
    """
    customer = _get_or_404(db, tenant_id, customer_id)
    customer.blacklisted = blacklisted
    customer.blacklist_reason = reason if blacklisted else None
    db.commit()
    db.refresh(customer)
    return customer


def is_blacklisted(db: Session, *, tenant_id: int, line_user_id: str) -> bool:
    """此 LINE 顧客是否被列入黑名單（查無顧客檔 = 非黑名單）。"""
    customer = (
        tenant_query(db, Customer, tenant_id)
        .filter(Customer.line_user_id == line_user_id)
        .first()
    )
    return bool(customer and customer.blacklisted)


# 刪除顧客時的關聯處理（= schema 宣告的 FK ondelete 語意）。
# SQLite（預設 DB）未開 PRAGMA foreign_keys，DB 層 ondelete 不會發生，
# 必須在應用層明做，否則留下孤兒/懸空參照。
_DETACH_MODELS = (Reservation, LineMessage, CouponRedemption, Order)  # SET NULL
_PURGE_MODELS = (PointTransaction, CustomerTagLink, CampaignSend)  # CASCADE


def delete_customer(db: Session, *, tenant_id: int, customer_id: int) -> None:
    """刪除顧客（PII 清除）。

    - 預約/訊息/兌換/訂單：保留歷史列，customer_id 設 NULL（去識別化）。
    - 點數異動/標籤掛載/行銷發送：連同個人資料一併刪除。
    """
    customer = _get_or_404(db, tenant_id, customer_id)
    for model in _DETACH_MODELS:
        (
            tenant_query(db, model, tenant_id)
            .filter(model.customer_id == customer_id)
            .update({"customer_id": None}, synchronize_session=False)
        )
    for model in _PURGE_MODELS:
        (
            tenant_query(db, model, tenant_id)
            .filter(model.customer_id == customer_id)
            .delete(synchronize_session=False)
        )
    db.delete(customer)
    db.commit()
