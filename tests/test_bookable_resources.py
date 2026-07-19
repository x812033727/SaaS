"""房間／設備資源：配置、容量、改期、分店、公開時段與後台權限。"""

from __future__ import annotations

import datetime
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from saas_mvp.app import create_app
from saas_mvp.db import Base, get_db
from saas_mvp.models.bookable_resource import ReservationResourceAllocation
from saas_mvp.models.booking_slot import BookingSlot
from saas_mvp.models.location import Location
from saas_mvp.models.reservation import Reservation
from saas_mvp.models.service import Service
from saas_mvp.models.tenant import Tenant
from saas_mvp.models.user import User
from saas_mvp.routers.line_webhook import _slots_fitting_service
from saas_mvp.services import bookable_resources as resources_svc
from saas_mvp.services import booking as booking_svc
from saas_mvp.services import booking_form as booking_form_svc

_engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
_Session = sessionmaker(autocommit=False, autoflush=False, bind=_engine)


@pytest.fixture()
def db():
    Base.metadata.drop_all(bind=_engine)
    Base.metadata.create_all(bind=_engine)
    with _Session() as session:
        yield session


@pytest.fixture()
def client(db):
    app = create_app()

    def override_db():
        with _Session() as session:
            yield session

    app.dependency_overrides[get_db] = override_db
    with TestClient(app, follow_redirects=False) as test_client:
        yield test_client


def _seed(db, *, capacity: int = 1, with_resource: bool = True):
    tenant = Tenant(name=f"resources-{uuid.uuid4().hex[:8]}", plan="pro")
    db.add(tenant)
    db.flush()
    service = Service(
        tenant_id=tenant.id,
        name="深層護理",
        duration_minutes=60,
        price_cents=180000,
    )
    start = datetime.datetime(2032, 5, 3, 10, tzinfo=datetime.timezone.utc)
    slot = BookingSlot(
        tenant_id=tenant.id,
        slot_start=start,
        slot_end=start + datetime.timedelta(hours=1),
        max_capacity=10,
    )
    db.add_all([service, slot])
    db.flush()
    resource_type = resources_svc.create_type(
        db, tenant_id=tenant.id, name="美容床"
    )
    resource = None
    if with_resource:
        resource = resources_svc.create_resource(
            db,
            tenant_id=tenant.id,
            resource_type_id=resource_type.id,
            name="美容床 A",
            capacity=capacity,
        )
    resources_svc.set_requirement(
        db,
        tenant_id=tenant.id,
        service_id=service.id,
        resource_type_id=resource_type.id,
        quantity=1,
    )
    db.commit()
    return tenant, service, slot, resource_type, resource


def _book(db, tenant, service, slot, suffix: str):
    return booking_svc.book_slot(
        db,
        tenant_id=tenant.id,
        slot_id=slot.id,
        line_user_id=f"U-resource-{suffix}",
        display_name=f"顧客 {suffix}",
        service_id=service.id,
    )


def test_booking_auto_allocates_and_cancel_releases_capacity(db):
    tenant, service, slot, resource_type, resource = _seed(db)
    first = _book(db, tenant, service, slot, "one")
    allocation = db.query(ReservationResourceAllocation).filter_by(
        reservation_id=first.id
    ).one()
    assert allocation.resource_id == resource.id
    assert allocation.resource_type_id == resource_type.id
    assert allocation.resource_name_snapshot == "美容床 A"

    with pytest.raises(booking_svc.ResourceUnavailableError):
        _book(db, tenant, service, slot, "two")
    assert db.query(Reservation).count() == 1
    db.refresh(slot)
    assert slot.booked_count == 1

    booking_svc.cancel_reservation(
        db, tenant_id=tenant.id, reservation_id=first.id
    )
    second = _book(db, tenant, service, slot, "two")
    assert second.id != first.id
    # 取消保留歷史配置，但只有 confirmed 預約會占用容量。
    assert db.query(ReservationResourceAllocation).count() == 2


def test_resource_capacity_and_overlapping_slots(db):
    tenant, service, slot, _, _ = _seed(db, capacity=2)
    _book(db, tenant, service, slot, "one")
    _book(db, tenant, service, slot, "two")
    with pytest.raises(booking_svc.ResourceUnavailableError):
        _book(db, tenant, service, slot, "three")

    overlap = BookingSlot(
        tenant_id=tenant.id,
        slot_start=slot.slot_start + datetime.timedelta(minutes=30),
        slot_end=slot.slot_start + datetime.timedelta(minutes=90),
        max_capacity=10,
    )
    db.add(overlap)
    db.commit()
    with pytest.raises(booking_svc.ResourceUnavailableError):
        _book(db, tenant, service, overlap, "overlap")


def test_weekly_availability_and_block_hide_public_and_line_slots(db):
    tenant, service, slot, _, resource = _seed(db)
    resources_svc.add_availability(
        db,
        tenant_id=tenant.id,
        resource_id=resource.id,
        weekday=slot.slot_start.weekday(),
        start_time=datetime.time(8),
        end_time=datetime.time(9),
    )
    db.commit()
    date = slot.slot_start.date().isoformat()
    assert booking_form_svc.slots_for(
        db, tenant.id, date=date, service_id=service.id
    ) == []
    assert _slots_fitting_service(db, tenant.id, [slot], service.id) == []
    with pytest.raises(booking_svc.ResourceUnavailableError):
        _book(db, tenant, service, slot, "outside-window")

    resources_svc.remove_availability(
        db,
        tenant_id=tenant.id,
        availability_id=resources_svc.list_availability(
            db, tenant_id=tenant.id
        )[0].id,
    )
    resources_svc.add_block(
        db,
        tenant_id=tenant.id,
        resource_id=resource.id,
        starts_at=slot.slot_start - datetime.timedelta(minutes=5),
        ends_at=slot.slot_end + datetime.timedelta(minutes=5),
        reason="保養",
    )
    db.commit()
    assert booking_form_svc.slots_for(
        db, tenant.id, date=date, service_id=service.id
    ) == []


def test_multi_requirement_failure_is_atomic(db):
    tenant, service, slot, _, _ = _seed(db)
    missing_type = resources_svc.create_type(
        db, tenant_id=tenant.id, name="雷射機"
    )
    resources_svc.set_requirement(
        db,
        tenant_id=tenant.id,
        service_id=service.id,
        resource_type_id=missing_type.id,
        quantity=1,
    )
    db.commit()

    with pytest.raises(booking_svc.ResourceUnavailableError):
        _book(db, tenant, service, slot, "atomic")
    assert db.query(Reservation).count() == 0
    assert db.query(ReservationResourceAllocation).count() == 0
    db.refresh(slot)
    assert slot.booked_count == 0


def test_reschedule_reallocates_and_failure_keeps_original_booking(db):
    tenant, service, old_slot, _, resource = _seed(db)
    original = _book(db, tenant, service, old_slot, "original")
    new_start = old_slot.slot_start + datetime.timedelta(hours=2)
    new_slot = BookingSlot(
        tenant_id=tenant.id,
        slot_start=new_start,
        slot_end=new_start + datetime.timedelta(hours=1),
        max_capacity=10,
    )
    db.add(new_slot)
    db.commit()
    occupying = _book(db, tenant, service, new_slot, "occupying")

    with pytest.raises(booking_svc.ResourceUnavailableError):
        booking_svc.reschedule_reservation(
            db,
            tenant_id=tenant.id,
            reservation_id=original.id,
            new_slot_id=new_slot.id,
        )
    db.refresh(original)
    db.refresh(old_slot)
    db.refresh(new_slot)
    assert original.slot_id == old_slot.id
    assert old_slot.booked_count == 1
    assert new_slot.booked_count == 1
    original_allocation = db.query(ReservationResourceAllocation).filter_by(
        reservation_id=original.id
    ).one()
    assert original_allocation.starts_at == old_slot.slot_start.replace(tzinfo=None)

    booking_svc.cancel_reservation(
        db, tenant_id=tenant.id, reservation_id=occupying.id
    )
    moved = booking_svc.reschedule_reservation(
        db,
        tenant_id=tenant.id,
        reservation_id=original.id,
        new_slot_id=new_slot.id,
    )
    moved_allocation = db.query(ReservationResourceAllocation).filter_by(
        reservation_id=moved.id
    ).one()
    assert moved.slot_id == new_slot.id
    assert moved_allocation.resource_id == resource.id
    assert moved_allocation.starts_at == new_slot.slot_start.replace(tzinfo=None)


def test_location_scoping_and_tenant_isolation(db):
    tenant, service, slot, resource_type, resource = _seed(db, with_resource=False)
    branch_a = Location(tenant_id=tenant.id, name="A 店")
    branch_b = Location(tenant_id=tenant.id, name="B 店")
    db.add_all([branch_a, branch_b])
    db.flush()
    branch_resource = resources_svc.create_resource(
        db,
        tenant_id=tenant.id,
        resource_type_id=resource_type.id,
        name="A 店包廂",
        location_id=branch_a.id,
    )
    slot.location_id = branch_b.id
    db.commit()
    with pytest.raises(booking_svc.ResourceUnavailableError):
        _book(db, tenant, service, slot, "wrong-branch")

    global_resource = resources_svc.create_resource(
        db,
        tenant_id=tenant.id,
        resource_type_id=resource_type.id,
        name="共用設備",
    )
    db.commit()
    reservation = _book(db, tenant, service, slot, "global")
    allocation = db.query(ReservationResourceAllocation).filter_by(
        reservation_id=reservation.id
    ).one()
    assert allocation.resource_id == global_resource.id
    assert allocation.resource_id != branch_resource.id

    other = Tenant(name=f"other-{uuid.uuid4().hex[:6]}", plan="pro")
    db.add(other)
    db.commit()
    with pytest.raises(resources_svc.ResourceNotFound):
        resources_svc.set_resource_active(
            db,
            tenant_id=other.id,
            resource_id=global_resource.id,
            active=False,
        )


def _login_owner(client: TestClient) -> tuple[int, str]:
    email = f"resource-owner-{uuid.uuid4().hex[:8]}@example.com"
    password = "Test1234!"
    response = client.post(
        "/auth/register",
        json={
            "email": email,
            "password": password,
            "tenant_name": f"resource-ui-{uuid.uuid4().hex[:8]}",
        },
    )
    assert response.status_code == 201
    with _Session() as session:
        user = session.query(User).filter_by(email=email).one()
        tenant_id = user.tenant_id
        session.get(Tenant, tenant_id).plan = "pro"
        session.add(
            Service(
                tenant_id=tenant_id,
                name="頭皮護理",
                duration_minutes=60,
                price_cents=100000,
            )
        )
        session.commit()
    assert client.post(
        "/ui/login", data={"email": email, "password": password}
    ).status_code == 303
    return tenant_id, email

