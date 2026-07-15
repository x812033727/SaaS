"""房間／設備資源設定、可用性與預約時的原子自動配置。"""

from __future__ import annotations

import datetime
from collections import defaultdict

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from saas_mvp.models.bookable_resource import (
    BookableResource,
    ReservationResourceAllocation,
    ResourceAvailability,
    ResourceBlock,
    ResourceType,
    ServiceResourceRequirement,
)
from saas_mvp.models.booking_slot import BookingSlot
from saas_mvp.models.location import Location
from saas_mvp.models.reservation import RESERVATION_CONFIRMED, Reservation
from saas_mvp.models.service import Service


class BookableResourceError(ValueError):
    pass


class ResourceNotFound(BookableResourceError):
    pass


class ResourceUnavailable(BookableResourceError):
    pass


def list_types(db: Session, *, tenant_id: int) -> list[ResourceType]:
    return (
        db.execute(
            select(ResourceType)
            .where(ResourceType.tenant_id == tenant_id)
            .order_by(ResourceType.name, ResourceType.id)
        )
        .scalars()
        .all()
    )


def list_resources(db: Session, *, tenant_id: int) -> list[BookableResource]:
    return (
        db.execute(
            select(BookableResource)
            .where(BookableResource.tenant_id == tenant_id)
            .order_by(BookableResource.resource_type_id, BookableResource.name)
        )
        .scalars()
        .all()
    )


def list_requirements(
    db: Session, *, tenant_id: int
) -> list[ServiceResourceRequirement]:
    return (
        db.execute(
            select(ServiceResourceRequirement)
            .where(ServiceResourceRequirement.tenant_id == tenant_id)
            .order_by(
                ServiceResourceRequirement.service_id,
                ServiceResourceRequirement.resource_type_id,
            )
        )
        .scalars()
        .all()
    )


def list_availability(
    db: Session, *, tenant_id: int, resource_id: int | None = None
) -> list[ResourceAvailability]:
    stmt = select(ResourceAvailability).where(
        ResourceAvailability.tenant_id == tenant_id
    )
    if resource_id is not None:
        stmt = stmt.where(ResourceAvailability.resource_id == resource_id)
    return (
        db.execute(
            stmt.order_by(
                ResourceAvailability.resource_id,
                ResourceAvailability.weekday,
                ResourceAvailability.start_time,
            )
        )
        .scalars()
        .all()
    )


def list_blocks(
    db: Session,
    *,
    tenant_id: int,
    resource_id: int | None = None,
    upcoming_only: bool = True,
) -> list[ResourceBlock]:
    stmt = select(ResourceBlock).where(ResourceBlock.tenant_id == tenant_id)
    if resource_id is not None:
        stmt = stmt.where(ResourceBlock.resource_id == resource_id)
    if upcoming_only:
        stmt = stmt.where(
            ResourceBlock.ends_at >= datetime.datetime.now(datetime.timezone.utc)
        )
    return db.execute(stmt.order_by(ResourceBlock.starts_at).limit(200)).scalars().all()


def allocations_for_reservations(
    db: Session, *, tenant_id: int, reservation_ids: list[int]
) -> dict[int, list[ReservationResourceAllocation]]:
    if not reservation_ids:
        return {}
    rows = (
        db.execute(
            select(ReservationResourceAllocation)
            .where(
                ReservationResourceAllocation.tenant_id == tenant_id,
                ReservationResourceAllocation.reservation_id.in_(reservation_ids),
            )
            .order_by(
                ReservationResourceAllocation.reservation_id,
                ReservationResourceAllocation.resource_type_name_snapshot,
                ReservationResourceAllocation.resource_name_snapshot,
            )
        )
        .scalars()
        .all()
    )
    grouped: dict[int, list[ReservationResourceAllocation]] = defaultdict(list)
    for row in rows:
        grouped[row.reservation_id].append(row)
    return dict(grouped)


def list_upcoming_allocations(
    db: Session, *, tenant_id: int, limit: int = 200
) -> list[ReservationResourceAllocation]:
    return (
        db.execute(
            select(ReservationResourceAllocation)
            .join(
                Reservation,
                Reservation.id == ReservationResourceAllocation.reservation_id,
            )
            .where(
                ReservationResourceAllocation.tenant_id == tenant_id,
                ReservationResourceAllocation.ends_at
                >= datetime.datetime.now(datetime.timezone.utc),
                Reservation.status == RESERVATION_CONFIRMED,
            )
            .order_by(ReservationResourceAllocation.starts_at)
            .limit(limit)
        )
        .scalars()
        .all()
    )


def _type(db: Session, tenant_id: int, type_id: int) -> ResourceType:
    row = db.execute(
        select(ResourceType).where(
            ResourceType.id == type_id, ResourceType.tenant_id == tenant_id
        )
    ).scalar_one_or_none()
    if row is None:
        raise ResourceNotFound("資源類型不存在。")
    return row


def _resource(db: Session, tenant_id: int, resource_id: int) -> BookableResource:
    row = db.execute(
        select(BookableResource).where(
            BookableResource.id == resource_id,
            BookableResource.tenant_id == tenant_id,
        )
    ).scalar_one_or_none()
    if row is None:
        raise ResourceNotFound("資源不存在。")
    return row


def create_type(
    db: Session, *, tenant_id: int, name: str, description: str = ""
) -> ResourceType:
    name = (name or "").strip()
    description = (description or "").strip()
    if not name or len(name) > 128:
        raise BookableResourceError("資源類型名稱為必填，最多 128 字。")
    if len(description) > 2000:
        raise BookableResourceError("類型說明最多 2,000 字。")
    duplicate = db.execute(
        select(ResourceType).where(
            ResourceType.tenant_id == tenant_id, ResourceType.name == name
        )
    ).scalar_one_or_none()
    if duplicate:
        raise BookableResourceError("已有同名資源類型。")
    row = ResourceType(
        tenant_id=tenant_id, name=name, description=description or None
    )
    db.add(row)
    db.flush()
    return row


def set_type_active(
    db: Session, *, tenant_id: int, resource_type_id: int, active: bool
) -> ResourceType:
    row = _type(db, tenant_id, resource_type_id)
    row.is_active = active
    db.flush()
    return row


def create_resource(
    db: Session,
    *,
    tenant_id: int,
    resource_type_id: int,
    name: str,
    description: str = "",
    internal_code: str = "",
    capacity: int = 1,
    location_id: int | None = None,
    available_from: datetime.date | None = None,
    available_until: datetime.date | None = None,
) -> BookableResource:
    _type(db, tenant_id, resource_type_id)
    name = (name or "").strip()
    description = (description or "").strip()
    internal_code = (internal_code or "").strip()
    if not name or len(name) > 128:
        raise BookableResourceError("資源名稱為必填，最多 128 字。")
    if len(description) > 2000 or len(internal_code) > 64:
        raise BookableResourceError("資源說明或內部編號過長。")
    if capacity < 1 or capacity > 100:
        raise BookableResourceError("同時可服務數必須介於 1～100。")
    if available_from and available_until and available_until < available_from:
        raise BookableResourceError("可用結束日期不得早於開始日期。")
    if location_id is not None:
        location = db.execute(
            select(Location).where(
                Location.id == location_id, Location.tenant_id == tenant_id
            )
        ).scalar_one_or_none()
        if location is None:
            raise BookableResourceError("分店不存在。")
    duplicate_conditions = [BookableResource.name == name]
    if internal_code:
        duplicate_conditions.append(BookableResource.internal_code == internal_code)
    duplicate = db.execute(
        select(BookableResource).where(
            BookableResource.tenant_id == tenant_id,
            or_(*duplicate_conditions),
        )
    ).scalar_one_or_none()
    if duplicate:
        raise BookableResourceError("已有同名資源或相同內部編號。")
    row = BookableResource(
        tenant_id=tenant_id,
        resource_type_id=resource_type_id,
        location_id=location_id,
        name=name,
        description=description or None,
        internal_code=internal_code or None,
        capacity=capacity,
        available_from=available_from,
        available_until=available_until,
    )
    db.add(row)
    db.flush()
    return row


def set_resource_active(
    db: Session, *, tenant_id: int, resource_id: int, active: bool
) -> BookableResource:
    row = _resource(db, tenant_id, resource_id)
    row.is_active = active
    db.flush()
    return row


def update_resource(
    db: Session,
    *,
    tenant_id: int,
    resource_id: int,
    name: str,
    description: str = "",
    internal_code: str = "",
    capacity: int = 1,
    location_id: int | None = None,
    available_from: datetime.date | None = None,
    available_until: datetime.date | None = None,
) -> BookableResource:
    row = _resource(db, tenant_id, resource_id)
    name = (name or "").strip()
    description = (description or "").strip()
    internal_code = (internal_code or "").strip()
    if not name or len(name) > 128:
        raise BookableResourceError("資源名稱為必填，最多 128 字。")
    if len(description) > 2000 or len(internal_code) > 64:
        raise BookableResourceError("資源說明或內部編號過長。")
    if capacity < 1 or capacity > 100:
        raise BookableResourceError("同時可服務數必須介於 1～100。")
    if available_from and available_until and available_until < available_from:
        raise BookableResourceError("可用結束日期不得早於開始日期。")
    if location_id is not None:
        location = db.execute(
            select(Location).where(
                Location.id == location_id, Location.tenant_id == tenant_id
            )
        ).scalar_one_or_none()
        if location is None:
            raise BookableResourceError("分店不存在。")
    conflicts = [
        BookableResource.tenant_id == tenant_id,
        BookableResource.id != resource_id,
        BookableResource.name == name,
    ]
    duplicate_name = db.execute(
        select(BookableResource).where(*conflicts)
    ).scalar_one_or_none()
    duplicate_code = None
    if internal_code:
        duplicate_code = db.execute(
            select(BookableResource).where(
                BookableResource.tenant_id == tenant_id,
                BookableResource.id != resource_id,
                BookableResource.internal_code == internal_code,
            )
        ).scalar_one_or_none()
    if duplicate_name or duplicate_code:
        raise BookableResourceError("已有同名資源或相同內部編號。")
    row.name = name
    row.description = description or None
    row.internal_code = internal_code or None
    row.capacity = capacity
    row.location_id = location_id
    row.available_from = available_from
    row.available_until = available_until
    db.flush()
    return row


def set_requirement(
    db: Session,
    *,
    tenant_id: int,
    service_id: int,
    resource_type_id: int,
    quantity: int,
) -> ServiceResourceRequirement:
    service = db.execute(
        select(Service).where(Service.id == service_id, Service.tenant_id == tenant_id)
    ).scalar_one_or_none()
    resource_type = _type(db, tenant_id, resource_type_id)
    if service is None:
        raise BookableResourceError("服務不存在。")
    if not resource_type.is_active:
        raise BookableResourceError("資源類型已停用。")
    if quantity < 1 or quantity > 20:
        raise BookableResourceError("每次服務需要數量必須介於 1～20。")
    row = db.execute(
        select(ServiceResourceRequirement).where(
            ServiceResourceRequirement.tenant_id == tenant_id,
            ServiceResourceRequirement.service_id == service_id,
            ServiceResourceRequirement.resource_type_id == resource_type_id,
        )
    ).scalar_one_or_none()
    if row is None:
        row = ServiceResourceRequirement(
            tenant_id=tenant_id,
            service_id=service_id,
            resource_type_id=resource_type_id,
        )
        db.add(row)
    row.quantity = quantity
    db.flush()
    return row


def remove_requirement(
    db: Session, *, tenant_id: int, requirement_id: int
) -> None:
    row = db.execute(
        select(ServiceResourceRequirement).where(
            ServiceResourceRequirement.id == requirement_id,
            ServiceResourceRequirement.tenant_id == tenant_id,
        )
    ).scalar_one_or_none()
    if row is None:
        raise ResourceNotFound("服務資源需求不存在。")
    db.delete(row)
    db.flush()


def add_availability(
    db: Session,
    *,
    tenant_id: int,
    resource_id: int,
    weekday: int,
    start_time: datetime.time,
    end_time: datetime.time,
) -> ResourceAvailability:
    _resource(db, tenant_id, resource_id)
    if weekday < 0 or weekday > 6:
        raise BookableResourceError("星期格式不正確。")
    if end_time <= start_time:
        raise BookableResourceError("可用結束時間必須晚於開始時間。")
    overlap = db.execute(
        select(ResourceAvailability).where(
            ResourceAvailability.tenant_id == tenant_id,
            ResourceAvailability.resource_id == resource_id,
            ResourceAvailability.weekday == weekday,
            ResourceAvailability.start_time < end_time,
            ResourceAvailability.end_time > start_time,
        )
    ).scalar_one_or_none()
    if overlap:
        raise BookableResourceError("此資源已有重疊的可用時段。")
    row = ResourceAvailability(
        tenant_id=tenant_id,
        resource_id=resource_id,
        weekday=weekday,
        start_time=start_time,
        end_time=end_time,
    )
    db.add(row)
    db.flush()
    return row


def remove_availability(
    db: Session, *, tenant_id: int, availability_id: int
) -> None:
    row = db.execute(
        select(ResourceAvailability).where(
            ResourceAvailability.id == availability_id,
            ResourceAvailability.tenant_id == tenant_id,
        )
    ).scalar_one_or_none()
    if row is None:
        raise ResourceNotFound("資源可用時段不存在。")
    db.delete(row)
    db.flush()


def add_block(
    db: Session,
    *,
    tenant_id: int,
    resource_id: int,
    starts_at: datetime.datetime,
    ends_at: datetime.datetime,
    reason: str = "",
) -> ResourceBlock:
    _resource(db, tenant_id, resource_id)
    if ends_at <= starts_at:
        raise BookableResourceError("停用結束時間必須晚於開始時間。")
    if ends_at - starts_at > datetime.timedelta(days=366):
        raise BookableResourceError("單次停用區間不得超過 366 天。")
    reason = (reason or "").strip()
    if len(reason) > 255:
        raise BookableResourceError("停用原因最多 255 字。")
    row = ResourceBlock(
        tenant_id=tenant_id,
        resource_id=resource_id,
        starts_at=starts_at,
        ends_at=ends_at,
        reason=reason or None,
    )
    db.add(row)
    db.flush()
    return row


def remove_block(db: Session, *, tenant_id: int, block_id: int) -> None:
    row = db.execute(
        select(ResourceBlock).where(
            ResourceBlock.id == block_id, ResourceBlock.tenant_id == tenant_id
        )
    ).scalar_one_or_none()
    if row is None:
        raise ResourceNotFound("資源停用區間不存在。")
    db.delete(row)
    db.flush()


def _interval(
    db: Session, *, reservation: Reservation, slot: BookingSlot
) -> tuple[datetime.datetime, datetime.datetime, Service | None]:
    service = None
    if reservation.service_id is not None:
        service = db.execute(
            select(Service).where(
                Service.id == reservation.service_id,
                Service.tenant_id == reservation.tenant_id,
            )
        ).scalar_one_or_none()
    starts_at = slot.slot_start
    ends_at = slot.slot_end
    if ends_at is None or ends_at <= starts_at:
        minutes = service.duration_minutes if service is not None else 60
        ends_at = starts_at + datetime.timedelta(minutes=max(1, minutes or 60))
    return starts_at, ends_at, service


def _matches_weekly_window(
    windows: list[ResourceAvailability],
    starts_at: datetime.datetime,
    ends_at: datetime.datetime,
) -> bool:
    if not windows:
        return True
    if ends_at.date() != starts_at.date():
        return False
    start_time = starts_at.timetz().replace(tzinfo=None)
    end_time = ends_at.timetz().replace(tzinfo=None)
    return any(
        window.weekday == starts_at.weekday()
        and window.start_time <= start_time
        and window.end_time >= end_time
        for window in windows
    )


def _capacity_for_type(
    db: Session,
    *,
    tenant_id: int,
    resource_type_id: int,
    starts_at: datetime.datetime,
    ends_at: datetime.datetime,
    location_id: int | None,
    lock: bool,
    exclude_reservation_id: int | None = None,
) -> list[tuple[BookableResource, int]]:
    stmt = (
        select(BookableResource)
        .where(
            BookableResource.tenant_id == tenant_id,
            BookableResource.resource_type_id == resource_type_id,
            BookableResource.is_active.is_(True),
        )
        .order_by(BookableResource.id)
    )
    if location_id is not None:
        stmt = stmt.where(
            or_(
                BookableResource.location_id.is_(None),
                BookableResource.location_id == location_id,
            )
        )
    else:
        # 沒有分店脈絡的服務只能使用全店共用資源，不能誤配某一分店的房間。
        stmt = stmt.where(BookableResource.location_id.is_(None))
    if lock:
        stmt = stmt.with_for_update()
    resources = db.execute(stmt).scalars().all()
    if not resources:
        return []
    resource_ids = [row.id for row in resources]
    windows = list_availability(db, tenant_id=tenant_id)
    windows_by_resource: dict[int, list[ResourceAvailability]] = defaultdict(list)
    for window in windows:
        if window.resource_id in resource_ids:
            windows_by_resource[window.resource_id].append(window)
    blocked_ids = set(
        db.execute(
            select(ResourceBlock.resource_id).where(
                ResourceBlock.tenant_id == tenant_id,
                ResourceBlock.resource_id.in_(resource_ids),
                ResourceBlock.starts_at < ends_at,
                ResourceBlock.ends_at > starts_at,
            )
        ).scalars()
    )
    usage_stmt = (
        select(
            ReservationResourceAllocation.resource_id,
            func.coalesce(func.sum(ReservationResourceAllocation.quantity), 0),
        )
        .join(
            Reservation,
            Reservation.id == ReservationResourceAllocation.reservation_id,
        )
        .where(
            ReservationResourceAllocation.tenant_id == tenant_id,
            ReservationResourceAllocation.resource_id.in_(resource_ids),
            ReservationResourceAllocation.starts_at < ends_at,
            ReservationResourceAllocation.ends_at > starts_at,
            Reservation.status == RESERVATION_CONFIRMED,
        )
        .group_by(ReservationResourceAllocation.resource_id)
    )
    if exclude_reservation_id is not None:
        usage_stmt = usage_stmt.where(Reservation.id != exclude_reservation_id)
    used = {resource_id: int(quantity) for resource_id, quantity in db.execute(usage_stmt)}
    available: list[tuple[BookableResource, int]] = []
    target_date = starts_at.date()
    for resource in resources:
        if resource.id in blocked_ids:
            continue
        if resource.available_from and target_date < resource.available_from:
            continue
        if resource.available_until and target_date > resource.available_until:
            continue
        if not _matches_weekly_window(
            windows_by_resource.get(resource.id, []), starts_at, ends_at
        ):
            continue
        free = max(0, (resource.capacity or 1) - used.get(resource.id, 0))
        if free:
            available.append((resource, free))
    return sorted(
        available,
        key=lambda item: (
            ((item[0].capacity or 1) - item[1]) / (item[0].capacity or 1),
            item[0].id,
        ),
    )


def slot_has_required_resources(
    db: Session,
    *,
    tenant_id: int,
    service_id: int | None,
    slot: BookingSlot,
) -> bool:
    """列表顯示用的即時可用性預檢；建單仍會鎖資源重驗。"""
    if service_id is None:
        return True
    requirements = db.execute(
        select(ServiceResourceRequirement).where(
            ServiceResourceRequirement.tenant_id == tenant_id,
            ServiceResourceRequirement.service_id == service_id,
        )
    ).scalars().all()
    if not requirements:
        return True
    probe = Reservation(
        tenant_id=tenant_id,
        slot_id=slot.id,
        service_id=service_id,
        location_id=slot.location_id,
    )
    starts_at, ends_at, service = _interval(db, reservation=probe, slot=slot)
    location_id = slot.location_id or (service.location_id if service else None)
    for requirement in requirements:
        capacity = sum(
            free
            for _, free in _capacity_for_type(
                db,
                tenant_id=tenant_id,
                resource_type_id=requirement.resource_type_id,
                starts_at=starts_at,
                ends_at=ends_at,
                location_id=location_id,
                lock=False,
            )
        )
        if capacity < requirement.quantity:
            return False
    return True


def allocate_for_reservation(
    db: Session, *, reservation: Reservation, slot: BookingSlot
) -> list[ReservationResourceAllocation]:
    """依服務需求鎖定候選資源並自動配置；不 commit，與建單同一交易。"""
    existing = (
        db.execute(
            select(ReservationResourceAllocation).where(
                ReservationResourceAllocation.tenant_id == reservation.tenant_id,
                ReservationResourceAllocation.reservation_id == reservation.id,
            )
        )
        .scalars()
        .all()
    )
    if existing:
        return existing
    if reservation.service_id is None:
        return []
    requirements = (
        db.execute(
            select(ServiceResourceRequirement)
            .where(
                ServiceResourceRequirement.tenant_id == reservation.tenant_id,
                ServiceResourceRequirement.service_id == reservation.service_id,
            )
            .order_by(ServiceResourceRequirement.resource_type_id)
        )
        .scalars()
        .all()
    )
    if not requirements:
        return []
    starts_at, ends_at, service = _interval(db, reservation=reservation, slot=slot)
    location_id = (
        reservation.location_id
        or slot.location_id
        or (service.location_id if service else None)
    )
    allocated: list[ReservationResourceAllocation] = []
    for requirement in requirements:
        resource_type = _type(
            db, reservation.tenant_id, requirement.resource_type_id
        )
        candidates = _capacity_for_type(
            db,
            tenant_id=reservation.tenant_id,
            resource_type_id=requirement.resource_type_id,
            starts_at=starts_at,
            ends_at=ends_at,
            location_id=location_id,
            lock=True,
            exclude_reservation_id=reservation.id,
        )
        remaining = requirement.quantity
        for resource, free in candidates:
            quantity = min(remaining, free)
            if quantity <= 0:
                continue
            row = ReservationResourceAllocation(
                tenant_id=reservation.tenant_id,
                reservation_id=reservation.id,
                resource_id=resource.id,
                resource_type_id=resource_type.id,
                quantity=quantity,
                starts_at=starts_at,
                ends_at=ends_at,
                resource_name_snapshot=resource.name,
                resource_type_name_snapshot=resource_type.name,
            )
            db.add(row)
            allocated.append(row)
            remaining -= quantity
            if remaining == 0:
                break
        if remaining:
            raise ResourceUnavailable(
                f"「{resource_type.name}」在此時段需要 {requirement.quantity} 個，"
                "目前可用數量不足。"
            )
    db.flush()
    return allocated


def reallocate_for_reservation(
    db: Session, *, reservation: Reservation, slot: BookingSlot
) -> list[ReservationResourceAllocation]:
    """改期時以同一交易重配資源；失敗由呼叫端交易回滾，舊配置仍會保留。"""
    existing = (
        db.execute(
            select(ReservationResourceAllocation).where(
                ReservationResourceAllocation.tenant_id == reservation.tenant_id,
                ReservationResourceAllocation.reservation_id == reservation.id,
            )
        )
        .scalars()
        .all()
    )
    for row in existing:
        db.delete(row)
    db.flush()
    return allocate_for_reservation(db, reservation=reservation, slot=slot)
