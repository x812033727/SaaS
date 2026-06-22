"""服務目錄（service catalog）服務層 — 分類 + 服務項目 + 員工指派 CRUD。

所有查詢走 tenant_query 強制隔離；查無/跨租戶一律 404，不洩漏 ID 存在性。
"""

from __future__ import annotations

from fastapi import HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from saas_mvp.models.service import Service
from saas_mvp.models.service_category import ServiceCategory
from saas_mvp.models.service_staff import ServiceStaff
from saas_mvp.models.staff import Staff
from saas_mvp.services.tenants import tenant_query


# ── 分類 ──────────────────────────────────────────────────────────────────────

def _get_category_or_404(
    db: Session, tenant_id: int, category_id: int
) -> ServiceCategory:
    cat = (
        tenant_query(db, ServiceCategory, tenant_id)
        .filter(ServiceCategory.id == category_id)
        .first()
    )
    if cat is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Category not found"
        )
    return cat


def list_categories(db: Session, *, tenant_id: int) -> list[ServiceCategory]:
    return (
        tenant_query(db, ServiceCategory, tenant_id)
        .order_by(ServiceCategory.sort_order, ServiceCategory.id)
        .all()
    )


def get_category(
    db: Session, *, tenant_id: int, category_id: int
) -> ServiceCategory:
    return _get_category_or_404(db, tenant_id, category_id)


def create_category(
    db: Session, *, tenant_id: int, name: str, sort_order: int = 0
) -> ServiceCategory:
    cat = ServiceCategory(tenant_id=tenant_id, name=name, sort_order=sort_order)
    db.add(cat)
    db.commit()
    db.refresh(cat)
    return cat


def update_category(
    db: Session,
    *,
    tenant_id: int,
    category_id: int,
    name: str | None = None,
    sort_order: int | None = None,
    is_active: bool | None = None,
) -> ServiceCategory:
    cat = _get_category_or_404(db, tenant_id, category_id)
    if name is not None:
        cat.name = name
    if sort_order is not None:
        cat.sort_order = sort_order
    if is_active is not None:
        cat.is_active = is_active
    db.commit()
    db.refresh(cat)
    return cat


# ── 服務項目 ──────────────────────────────────────────────────────────────────

def _get_service_or_404(db: Session, tenant_id: int, service_id: int) -> Service:
    svc = (
        tenant_query(db, Service, tenant_id)
        .filter(Service.id == service_id)
        .first()
    )
    if svc is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Service not found"
        )
    return svc


def list_services(
    db: Session,
    *,
    tenant_id: int,
    location_id: int | None = None,
    category_id: int | None = None,
) -> list[Service]:
    """列出服務項目，可依 location_id / category_id 篩選。

    location_id 篩選含 NULL（不限分店）的服務，使「指定分店」也看得到通用項目。
    """
    q = tenant_query(db, Service, tenant_id)
    if location_id is not None:
        q = q.filter(
            (Service.location_id == location_id) | (Service.location_id.is_(None))
        )
    if category_id is not None:
        q = q.filter(Service.category_id == category_id)
    return q.order_by(Service.id).all()


def get_service(db: Session, *, tenant_id: int, service_id: int) -> Service:
    return _get_service_or_404(db, tenant_id, service_id)


def create_service(
    db: Session,
    *,
    tenant_id: int,
    name: str,
    category_id: int | None = None,
    duration_minutes: int = 60,
    price_cents: int = 0,
    location_id: int | None = None,
) -> Service:
    svc = Service(
        tenant_id=tenant_id,
        name=name,
        category_id=category_id,
        duration_minutes=duration_minutes,
        price_cents=price_cents,
        location_id=location_id,
    )
    db.add(svc)
    db.commit()
    db.refresh(svc)
    return svc


def update_service(
    db: Session,
    *,
    tenant_id: int,
    service_id: int,
    name: str | None = None,
    category_id: int | None = None,
    duration_minutes: int | None = None,
    price_cents: int | None = None,
    location_id: int | None = None,
    is_active: bool | None = None,
) -> Service:
    svc = _get_service_or_404(db, tenant_id, service_id)
    if name is not None:
        svc.name = name
    if category_id is not None:
        svc.category_id = category_id
    if duration_minutes is not None:
        svc.duration_minutes = duration_minutes
    if price_cents is not None:
        svc.price_cents = price_cents
    if location_id is not None:
        svc.location_id = location_id
    if is_active is not None:
        svc.is_active = is_active
    db.commit()
    db.refresh(svc)
    return svc


# ── 服務 ↔ 員工 指派 ──────────────────────────────────────────────────────────

def assign_staff(
    db: Session, *, tenant_id: int, service_id: int, staff_id: int
) -> ServiceStaff:
    _get_service_or_404(db, tenant_id, service_id)
    staff = (
        tenant_query(db, Staff, tenant_id).filter(Staff.id == staff_id).first()
    )
    if staff is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Staff not found"
        )
    link = ServiceStaff(
        tenant_id=tenant_id, service_id=service_id, staff_id=staff_id
    )
    db.add(link)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Staff already assigned to this service",
        )
    db.refresh(link)
    return link


def unassign_staff(
    db: Session, *, tenant_id: int, service_id: int, staff_id: int
) -> None:
    _get_service_or_404(db, tenant_id, service_id)
    link = (
        tenant_query(db, ServiceStaff, tenant_id)
        .filter(
            ServiceStaff.service_id == service_id,
            ServiceStaff.staff_id == staff_id,
        )
        .first()
    )
    if link is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Assignment not found"
        )
    db.delete(link)
    db.commit()


def list_service_staff(
    db: Session, *, tenant_id: int, service_id: int
) -> list[ServiceStaff]:
    _get_service_or_404(db, tenant_id, service_id)
    return (
        tenant_query(db, ServiceStaff, tenant_id)
        .filter(ServiceStaff.service_id == service_id)
        .order_by(ServiceStaff.id)
        .all()
    )
