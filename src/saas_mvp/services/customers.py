"""顧客 CRM 服務層 — 店家端唯讀查詢 + 補欄位（phone/note）。

顧客檔由 LINE 預約流程自動建立（models/customer.upsert_customer_from_line）；
此處只提供店家端 list/get/PATCH。所有查詢走 tenant_query 強制隔離。
"""

from __future__ import annotations

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from saas_mvp.models.customer import Customer
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
