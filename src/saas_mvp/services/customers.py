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


def list_customers(db: Session, *, tenant_id: int) -> list[Customer]:
    return (
        tenant_query(db, Customer, tenant_id)
        .order_by(Customer.id.desc())
        .all()
    )


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
