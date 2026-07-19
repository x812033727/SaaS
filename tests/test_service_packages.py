"""服務套票：發行、扣次、退次、到期、跨租戶與後台權限。"""

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
from saas_mvp.models.booking_slot import BookingSlot
from saas_mvp.models.customer import Customer
from saas_mvp.models.service import Service
from saas_mvp.models.service_package import PackageCreditLedger
from saas_mvp.models.tenant import Tenant
from saas_mvp.models.user import User
from saas_mvp.services import booking as booking_svc
from saas_mvp.services import booking_form as booking_form_svc
from saas_mvp.services import service_packages as packages_svc
from saas_mvp.booking.commands import parse_booking_command
from saas_mvp.routers.line_webhook import _packages_reply

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


def _seed(db, suffix: str = "a"):
    tenant = Tenant(name=f"pkg-{suffix}-{uuid.uuid4().hex[:6]}", plan="pro")
    db.add(tenant)
    db.flush()
    customer = Customer(
        tenant_id=tenant.id,
        line_user_id=f"U-{suffix}",
        display_name=f"顧客 {suffix}",
    )
    service = Service(
        tenant_id=tenant.id,
        name=f"護理 {suffix}",
        duration_minutes=60,
        price_cents=100000,
    )
    slot = BookingSlot(
        tenant_id=tenant.id,
        slot_start=datetime.datetime(2030, 1, 1, 10, tzinfo=datetime.timezone.utc),
        max_capacity=2,
    )
    db.add_all([customer, service, slot])
    db.commit()
    return tenant, customer, service, slot


def _package(db, tenant, service, *, quantity=3, validity_days=365):
    package = packages_svc.create_package(
        db,
        tenant_id=tenant.id,
        name=f"{service.name} {quantity} 次卡",
        description="療程套票",
        price_cents=240000,
        validity_days=validity_days,
    )
    packages_svc.add_or_update_item(
        db,
        tenant_id=tenant.id,
        package_id=package.id,
        service_id=service.id,
        included_quantity=quantity,
    )
    db.commit()
    return package


def test_issue_redeem_and_cancel_refund_are_auditable_and_idempotent(db):
    tenant, customer, service, slot = _seed(db)
    package = _package(db, tenant, service)
    owned = packages_svc.issue_package(
        db,
        tenant_id=tenant.id,
        customer_id=customer.id,
        package_id=package.id,
        actor_user_id=123,
        issuance_key="same-checkout-request",
    )
    db.commit()
    duplicate = packages_svc.issue_package(
        db,
        tenant_id=tenant.id,
        customer_id=customer.id,
        package_id=package.id,
        actor_user_id=123,
        issuance_key="same-checkout-request",
    )
    db.commit()
    assert duplicate.id == owned.id
    assert db.query(PackageCreditLedger).filter_by(
        customer_package_id=owned.id, kind="issue"
    ).count() == 1

    wallet = packages_svc.customer_wallet(
        db, tenant_id=tenant.id, customer_id=customer.id
    )
    assert [(row.service.id, row.remaining) for row in wallet] == [(service.id, 3)]
    assert owned.package_name_snapshot == package.name
    assert owned.price_cents_snapshot == 240000

    reservation = booking_svc.book_slot(
        db,
        tenant_id=tenant.id,
        slot_id=slot.id,
        line_user_id=customer.line_user_id,
        service_id=service.id,
        use_package=True,
    )
    assert packages_svc.eligible_credit_count(
        db,
        tenant_id=tenant.id,
        customer_id=customer.id,
        service_id=service.id,
    ) == 2
    redeem = db.query(PackageCreditLedger).filter_by(
        reservation_id=reservation.id, kind="redeem"
    ).one()
    assert redeem.delta == -1 and redeem.customer_package_id == owned.id

    booking_svc.cancel_reservation(
        db, tenant_id=tenant.id, reservation_id=reservation.id
    )
    booking_svc.cancel_reservation(
        db, tenant_id=tenant.id, reservation_id=reservation.id
    )
    assert packages_svc.eligible_credit_count(
        db,
        tenant_id=tenant.id,
        customer_id=customer.id,
        service_id=service.id,
    ) == 3
    refunds = db.query(PackageCreditLedger).filter_by(
        reservation_id=reservation.id, kind="refund"
    ).all()
    assert len(refunds) == 1 and refunds[0].delta == 1

    cancelled = packages_svc.cancel_customer_package(
        db,
        tenant_id=tenant.id,
        customer_id=customer.id,
        customer_package_id=owned.id,
        actor_user_id=123,
        note="顧客退款",
    )
    db.commit()
    assert cancelled.status == "cancelled"
    assert packages_svc.customer_wallet(
        db, tenant_id=tenant.id, customer_id=customer.id
    ) == []
    adjustments = db.query(PackageCreditLedger).filter_by(
        customer_package_id=owned.id, kind="adjust"
    ).all()
    assert len(adjustments) == 1 and adjustments[0].delta == -3


def test_uses_earliest_expiring_package_first(db):
    tenant, customer, service, slot = _seed(db)
    long = _package(db, tenant, service, quantity=1, validity_days=365)
    short = packages_svc.create_package(
        db,
        tenant_id=tenant.id,
        name="短效護理卡",
        description=None,
        price_cents=100,
        validity_days=30,
    )
    packages_svc.add_or_update_item(
        db,
        tenant_id=tenant.id,
        package_id=short.id,
        service_id=service.id,
        included_quantity=1,
    )
    db.commit()
    long_owned = packages_svc.issue_package(
        db,
        tenant_id=tenant.id,
        customer_id=customer.id,
        package_id=long.id,
        actor_user_id=None,
    )
    short_owned = packages_svc.issue_package(
        db,
        tenant_id=tenant.id,
        customer_id=customer.id,
        package_id=short.id,
        actor_user_id=None,
    )
    db.commit()
    reservation = booking_svc.book_slot(
        db,
        tenant_id=tenant.id,
        slot_id=slot.id,
        line_user_id=customer.line_user_id,
        service_id=service.id,
        use_package=True,
    )
    redeem = db.query(PackageCreditLedger).filter_by(
        reservation_id=reservation.id, kind="redeem"
    ).one()
    assert redeem.customer_package_id == short_owned.id
    assert redeem.customer_package_id != long_owned.id


def test_expired_or_empty_package_cannot_reserve_and_does_not_consume_capacity(db):
    tenant, customer, service, slot = _seed(db)
    package = _package(db, tenant, service, quantity=1)
    owned = packages_svc.issue_package(
        db,
        tenant_id=tenant.id,
        customer_id=customer.id,
        package_id=package.id,
        actor_user_id=None,
    )
    owned.expires_at = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1)
    db.commit()
    with pytest.raises(packages_svc.PackageCreditUnavailable):
        booking_svc.book_slot(
            db,
            tenant_id=tenant.id,
            slot_id=slot.id,
            line_user_id=customer.line_user_id,
            service_id=service.id,
            use_package=True,
        )
    db.rollback()
    db.refresh(slot)
    assert slot.booked_count == 0


def test_cross_tenant_references_are_rejected(db):
    tenant_a, customer_a, service_a, _ = _seed(db, "a")
    tenant_b, customer_b, service_b, _ = _seed(db, "b")
    package_a = _package(db, tenant_a, service_a)
    with pytest.raises(packages_svc.PackageNotFound):
        packages_svc.add_or_update_item(
            db,
            tenant_id=tenant_a.id,
            package_id=package_a.id,
            service_id=service_b.id,
            included_quantity=1,
        )
    with pytest.raises(packages_svc.PackageNotFound):
        packages_svc.issue_package(
            db,
            tenant_id=tenant_a.id,
            customer_id=customer_b.id,
            package_id=package_a.id,
            actor_user_id=None,
        )


def test_line_package_command_lists_balance_and_expiry(db):
    tenant, customer, service, _ = _seed(db)
    package = _package(db, tenant, service, quantity=2)
    packages_svc.issue_package(
        db,
        tenant_id=tenant.id,
        customer_id=customer.id,
        package_id=package.id,
        actor_user_id=None,
    )
    db.commit()
    assert parse_booking_command("我的套票") == ("packages", {})
    reply = _packages_reply(db, tenant.id, customer.line_user_id)
    assert package.name in reply
    assert service.name in reply
    assert "剩 2 次" in reply and "到期" in reply


@pytest.fixture()
def client():
    Base.metadata.drop_all(bind=_engine)
    Base.metadata.create_all(bind=_engine)
    app = create_app()

    def override_db():
        with _Session() as session:
            yield session

    app.dependency_overrides[get_db] = override_db
    with TestClient(app, follow_redirects=False) as test_client:
        yield test_client


def _login(client: TestClient) -> tuple[int, str]:
    email = f"pkg-{uuid.uuid4().hex[:8]}@example.com"
    password = "Test1234!"
    response = client.post(
        "/auth/register",
        json={"email": email, "password": password, "tenant_name": f"pkg-{uuid.uuid4().hex[:8]}"},
    )
    assert response.status_code == 201
    with _Session() as db:
        user = db.query(User).filter_by(email=email).one()
        tenant_id = user.tenant_id
        db.get(Tenant, tenant_id).plan = "pro"
        db.commit()
    assert client.post("/ui/login", data={"email": email, "password": password}).status_code == 303
    return tenant_id, email


def test_web_booking_can_opt_in_to_package_credit(client):
    tenant_id, _ = _login(client)
    with _Session() as db:
        customer = Customer(
            tenant_id=tenant_id,
            line_user_id="U-web-package",
            display_name="網頁套票顧客",
        )
        service = Service(
            tenant_id=tenant_id,
            name="網頁護理",
            duration_minutes=60,
            price_cents=1000,
        )
        slot = BookingSlot(
            tenant_id=tenant_id,
            slot_start=datetime.datetime(2030, 2, 3, 10, tzinfo=datetime.timezone.utc),
            max_capacity=1,
        )
        db.add_all([customer, service, slot])
        db.flush()
        package = packages_svc.create_package(
            db,
            tenant_id=tenant_id,
            name="網頁 2 次卡",
            description=None,
            price_cents=1800,
            validity_days=365,
        )
        packages_svc.add_or_update_item(
            db,
            tenant_id=tenant_id,
            package_id=package.id,
            service_id=service.id,
            included_quantity=2,
        )
        db.commit()
        packages_svc.issue_package(
            db,
            tenant_id=tenant_id,
            customer_id=customer.id,
            package_id=package.id,
            actor_user_id=None,
        )
        db.commit()
        token = booking_form_svc.issue_token(
            db,
            tenant_id=tenant_id,
            line_user_id=customer.line_user_id,
            display_name=customer.display_name,
        ).token
        service_id, slot_id, customer_id = service.id, slot.id, customer.id

    page = client.get(
        f"/booking/f/{token}?service_id={service_id}&date=2030-02-03"
    )
    assert page.status_code == 200
    assert "使用服務套票扣 1 次" in page.text
    response = client.post(
        f"/booking/f/{token}",
        data={
            "slot_id": str(slot_id),
            "party_size": "1",
            "service_id": str(service_id),
            "use_package": "true",
        },
    )
    assert response.status_code == 200
    assert "服務套票已扣 1 次" in response.text
    with _Session() as db:
        assert packages_svc.eligible_credit_count(
            db,
            tenant_id=tenant_id,
            customer_id=customer_id,
            service_id=service_id,
        ) == 1
