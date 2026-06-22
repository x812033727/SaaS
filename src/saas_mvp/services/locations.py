"""分店（multi-location）服務層 — 店家端 CRUD。

所有查詢走 tenant_query 強制隔離；查無/跨租戶一律 404，不洩漏 ID 存在性。
create() 強制「啟用中分店數 < settings.max_locations_per_tenant」，超限拋
LocationLimitError（router 轉 409）。
"""

from __future__ import annotations

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from saas_mvp.config import settings
from saas_mvp.models.location import Location
from saas_mvp.services.tenants import tenant_query


class LocationLimitError(Exception):
    """啟用中分店數已達上限。"""


def _get_or_404(db: Session, tenant_id: int, location_id: int) -> Location:
    location = (
        tenant_query(db, Location, tenant_id)
        .filter(Location.id == location_id)
        .first()
    )
    if location is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Location not found"
        )
    return location


def list_locations(db: Session, *, tenant_id: int) -> list[Location]:
    return tenant_query(db, Location, tenant_id).order_by(Location.id).all()


def get_location(db: Session, *, tenant_id: int, location_id: int) -> Location:
    return _get_or_404(db, tenant_id, location_id)


def create_location(
    db: Session,
    *,
    tenant_id: int,
    name: str,
    address: str | None = None,
    phone: str | None = None,
    timezone: str | None = None,
) -> Location:
    active_count = (
        tenant_query(db, Location, tenant_id)
        .filter(Location.is_active.is_(True))
        .count()
    )
    if active_count >= settings.max_locations_per_tenant:
        raise LocationLimitError(
            f"active location limit reached ({settings.max_locations_per_tenant})"
        )
    location = Location(
        tenant_id=tenant_id,
        name=name,
        address=address,
        phone=phone,
        timezone=timezone if timezone is not None else "Asia/Taipei",
    )
    db.add(location)
    db.commit()
    db.refresh(location)
    return location


def update_location(
    db: Session,
    *,
    tenant_id: int,
    location_id: int,
    name: str | None = None,
    address: str | None = None,
    phone: str | None = None,
    timezone: str | None = None,
    is_active: bool | None = None,
) -> Location:
    location = _get_or_404(db, tenant_id, location_id)
    if name is not None:
        location.name = name
    if address is not None:
        location.address = address
    if phone is not None:
        location.phone = phone
    if timezone is not None:
        location.timezone = timezone
    if is_active is not None:
        location.is_active = is_active
    db.commit()
    db.refresh(location)
    return location
