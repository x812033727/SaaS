"""顧客標籤 / 分眾服務層 — 標籤 CRUD、掛/卸標籤、分眾查詢。

所有查詢一律走 tenant_query 強制租戶隔離。segment_customers 是 Phase 4 行銷
自動化的 targeting 後端：以純 tenant_query filter 組合（標籤、等級、預約次數、
最後預約時間、分店），回傳符合的 Customer 清單。

掛標籤冪等：UniqueConstraint(customer_id, tag_id) 擋重複，catch IntegrityError。
"""

from __future__ import annotations

import datetime

from fastapi import HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from saas_mvp.models.customer import Customer
from saas_mvp.models.customer_tag import CustomerTag
from saas_mvp.models.customer_tag_link import CustomerTagLink
from saas_mvp.services.tenants import tenant_query


# ── 標籤 CRUD ────────────────────────────────────────────────────────────────

def _get_tag_or_404(db: Session, tenant_id: int, tag_id: int) -> CustomerTag:
    tag = (
        tenant_query(db, CustomerTag, tenant_id)
        .filter(CustomerTag.id == tag_id)
        .first()
    )
    if tag is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Tag not found"
        )
    return tag


def _get_customer_or_404(db: Session, tenant_id: int, customer_id: int) -> Customer:
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


def create_tag(
    db: Session, *, tenant_id: int, name: str, color: str | None = None
) -> CustomerTag:
    """建立標籤；同租戶同名重複回 409。"""
    tag = CustomerTag(tenant_id=tenant_id, name=name, color=color)
    db.add(tag)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Tag name already exists"
        )
    db.refresh(tag)
    return tag


def list_tags(db: Session, *, tenant_id: int) -> list[CustomerTag]:
    return (
        tenant_query(db, CustomerTag, tenant_id)
        .order_by(CustomerTag.name)
        .all()
    )


def delete_tag(db: Session, *, tenant_id: int, tag_id: int) -> None:
    """刪除標籤（連帶 CASCADE 刪掉其所有掛載）；查無/跨租戶回 404。"""
    tag = _get_tag_or_404(db, tenant_id, tag_id)
    db.delete(tag)
    db.commit()


# ── 掛 / 卸標籤 ──────────────────────────────────────────────────────────────

def attach_tag(
    db: Session, *, tenant_id: int, customer_id: int, tag_id: int
) -> CustomerTagLink:
    """把標籤掛到顧客上；重複掛載冪等（回既有 link）。查無/跨租戶回 404。"""
    _get_customer_or_404(db, tenant_id, customer_id)
    _get_tag_or_404(db, tenant_id, tag_id)

    link = CustomerTagLink(
        tenant_id=tenant_id, customer_id=customer_id, tag_id=tag_id
    )
    db.add(link)
    try:
        db.commit()
    except IntegrityError:
        # 已掛載：冪等回既有列。
        db.rollback()
        existing = (
            tenant_query(db, CustomerTagLink, tenant_id)
            .filter(
                CustomerTagLink.customer_id == customer_id,
                CustomerTagLink.tag_id == tag_id,
            )
            .first()
        )
        return existing
    db.refresh(link)
    return link


def detach_tag(
    db: Session, *, tenant_id: int, customer_id: int, tag_id: int
) -> None:
    """卸下顧客的標籤；未掛載為 no-op（冪等）。"""
    link = (
        tenant_query(db, CustomerTagLink, tenant_id)
        .filter(
            CustomerTagLink.customer_id == customer_id,
            CustomerTagLink.tag_id == tag_id,
        )
        .first()
    )
    if link is not None:
        db.delete(link)
        db.commit()


def list_tags_for_customer(
    db: Session, *, tenant_id: int, customer_id: int
) -> list[CustomerTag]:
    """列出某顧客身上的所有標籤。查無/跨租戶回 404。"""
    _get_customer_or_404(db, tenant_id, customer_id)
    return (
        tenant_query(db, CustomerTag, tenant_id)
        .join(CustomerTagLink, CustomerTagLink.tag_id == CustomerTag.id)
        .filter(CustomerTagLink.customer_id == customer_id)
        .order_by(CustomerTag.name)
        .all()
    )


# ── 分眾查詢（Phase 4 行銷 targeting 後端） ────────────────────────────────────

def segment_customers(
    db: Session,
    *,
    tenant_id: int,
    tag_ids: list[int] | None = None,
    tier: str | None = None,
    min_bookings: int | None = None,
    last_booked_before: datetime.datetime | None = None,
    location_id: int | None = None,
) -> list[Customer]:
    """以多條件組合篩出顧客（純 tenant_query filter 組合）。

    - tag_ids：須同時擁有**所有**指定標籤（AND 語意）。
    - tier：會員等級精確比對。
    - min_bookings：booking_count >= min_bookings。
    - last_booked_before：last_booked_at < 此時間（找久未回訪者）。
    - location_id：顧客綁定分店精確比對。
    """
    q = tenant_query(db, Customer, tenant_id)

    if tier is not None:
        q = q.filter(Customer.tier == tier)
    if min_bookings is not None:
        q = q.filter(Customer.booking_count >= min_bookings)
    if last_booked_before is not None:
        q = q.filter(Customer.last_booked_at < last_booked_before)
    if location_id is not None:
        q = q.filter(Customer.location_id == location_id)

    if tag_ids:
        # 須擁有所有指定標籤：對每個 tag_id 做一次 EXISTS-style join filter。
        for tag_id in tag_ids:
            link_alias = (
                tenant_query(db, CustomerTagLink, tenant_id)
                .filter(CustomerTagLink.tag_id == tag_id)
                .subquery()
            )
            q = q.join(
                link_alias, link_alias.c.customer_id == Customer.id
            )

    return q.order_by(Customer.id).all()
