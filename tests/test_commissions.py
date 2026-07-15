"""員工抽成、POS 服務成交、沖銷與薪資結算閉環。"""

from __future__ import annotations

import datetime
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from saas_mvp.db import Base, import_all_models
from saas_mvp.models.booking_slot import BookingSlot
from saas_mvp.models.commission import (
    BASIS_NET,
    ITEM_PRODUCT,
    ITEM_SERVICE,
    ITEM_TIP,
    METHOD_PERCENT,
    PAY_RUN_FINALIZED,
    PAY_RUN_PAID,
    CommissionEarning,
    PayRunItem,
)
from saas_mvp.models.customer import Customer
from saas_mvp.models.order import ORDER_CANCELLED, ORDER_PAID
from saas_mvp.models.order_item import OrderItem
from saas_mvp.models.product import Product
from saas_mvp.models.reservation import RESERVATION_CONFIRMED, Reservation
from saas_mvp.models.service import Service
from saas_mvp.models.staff import Staff
from saas_mvp.models.tenant import Tenant
from saas_mvp.services import commissions as commissions_svc
from saas_mvp.services import pos as pos_svc
from saas_mvp.services import shop as shop_svc

import_all_models()

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


def _seed(db):
    tenant = Tenant(name=f"commission_{uuid.uuid4().hex[:8]}", plan="pro")
    db.add(tenant)
    db.flush()
    staff = Staff(tenant_id=tenant.id, name="Amy", role="設計師")
    customer = Customer(
        tenant_id=tenant.id,
        display_name="王小姐",
        phone="0911222333",
        points_balance=2_000,
    )
    product = Product(
        tenant_id=tenant.id, name="洗髮精", price_cents=10_000, stock=10
    )
    db.add_all([staff, customer, product])
    db.flush()
    return tenant, staff, customer, product


def _rule(db, tenant_id, staff_id, item_type, percent):
    return commissions_svc.save_rule(
        db,
        tenant_id=tenant_id,
        staff_id=staff_id,
        item_type=item_type,
        method=METHOD_PERCENT,
        value=percent * 100,
        calculation_basis=BASIS_NET,
        effective_from=datetime.date(2020, 1, 1),
        actor_user_id=1,
    )


def test_paid_product_uses_net_after_points_and_snapshots_tip(db):
    tenant, staff, customer, product = _seed(db)
    _rule(db, tenant.id, staff.id, ITEM_PRODUCT, 20)
    db.commit()

    order = pos_svc.checkout(
        db,
        tenant_id=tenant.id,
        customer_id=customer.id,
        items=[{"product_id": product.id, "qty": 1}],
        points_to_redeem=1_000,
        staff_id=staff.id,
        payment_method="cash",
        tip_cents=500,
        mark_paid=True,
    )

    assert order.status == ORDER_PAID
    assert order.points_cents == 1_000
    assert order.tip_cents == 500
    assert order.total_cents == 9_500
    earnings = db.query(CommissionEarning).order_by(CommissionEarning.id).all()
    assert [(row.item_type, row.net_cents, row.commission_cents) for row in earnings] == [
        (ITEM_PRODUCT, 9_000, 1_800),
        (ITEM_TIP, 500, 500),
    ]

    # 金流回調或重試不得重複產生抽成。
    commissions_svc.record_paid_order(db, order=order)
    db.commit()
    assert db.query(CommissionEarning).count() == 2


def test_reservation_service_becomes_paid_service_line_and_attendance(db):
    tenant, staff, customer, _product = _seed(db)
    service = Service(
        tenant_id=tenant.id,
        name="剪髮",
        duration_minutes=60,
        price_cents=20_000,
    )
    slot = BookingSlot(
        tenant_id=tenant.id,
        slot_start=datetime.datetime(2030, 1, 2, 3, tzinfo=datetime.timezone.utc),
        max_capacity=1,
    )
    db.add_all([service, slot])
    db.flush()
    reservation = Reservation(
        tenant_id=tenant.id,
        slot_id=slot.id,
        customer_id=customer.id,
        staff_id=staff.id,
        service_id=service.id,
        status=RESERVATION_CONFIRMED,
    )
    db.add(reservation)
    _rule(db, tenant.id, staff.id, ITEM_SERVICE, 30)
    db.commit()

    order = pos_svc.checkout(
        db,
        tenant_id=tenant.id,
        customer_id=None,
        items=[],
        reservation_id=reservation.id,
        payment_method="card",
        mark_paid=True,
    )
    item = db.query(OrderItem).filter_by(order_id=order.id).one()
    earning = db.query(CommissionEarning).one()
    db.refresh(reservation)
    assert order.customer_id == customer.id
    assert order.staff_id == staff.id
    assert item.item_type == ITEM_SERVICE
    assert item.service_id == service.id
    assert earning.commission_cents == 6_000
    assert reservation.attended is True

    with pytest.raises(pos_svc.ReservationAlreadyCheckedOut):
        pos_svc.checkout(
            db,
            tenant_id=tenant.id,
            customer_id=None,
            items=[],
            reservation_id=reservation.id,
            payment_method="cash",
            mark_paid=True,
        )


def test_pay_run_adjust_finalize_and_mark_paid(db):
    tenant, staff, customer, product = _seed(db)
    _rule(db, tenant.id, staff.id, ITEM_PRODUCT, 10)
    db.commit()
    pos_svc.checkout(
        db,
        tenant_id=tenant.id,
        customer_id=customer.id,
        items=[{"product_id": product.id, "qty": 1}],
        staff_id=staff.id,
        payment_method="cash",
        tip_cents=300,
        mark_paid=True,
    )

    today = datetime.datetime.now(datetime.timezone.utc).date()
    run = commissions_svc.create_pay_run(
        db,
        tenant_id=tenant.id,
        period_start=today,
        period_end=today,
        actor_user_id=1,
    )
    db.flush()
    item = db.query(PayRunItem).filter_by(pay_run_id=run.id).one()
    assert (item.commission_cents, item.tip_cents, run.total_cents) == (1_000, 300, 1_300)

    commissions_svc.update_adjustment(
        db,
        tenant_id=tenant.id,
        pay_run_id=run.id,
        staff_id=staff.id,
        adjustment_cents=-100,
        note="預支扣款",
    )
    assert run.total_cents == 1_200
    commissions_svc.finalize_pay_run(
        db, tenant_id=tenant.id, pay_run_id=run.id, actor_user_id=1
    )
    assert run.status == PAY_RUN_FINALIZED
    commissions_svc.mark_pay_run_paid(
        db, tenant_id=tenant.id, pay_run_id=run.id, actor_user_id=1
    )
    assert run.status == PAY_RUN_PAID

    with pytest.raises(commissions_svc.CommissionError):
        commissions_svc.update_adjustment(
            db,
            tenant_id=tenant.id,
            pay_run_id=run.id,
            staff_id=staff.id,
            adjustment_cents=1,
            note=None,
        )


def test_cancel_before_settlement_voids_earning_without_phantom_reversal(db):
    tenant, staff, customer, product = _seed(db)
    _rule(db, tenant.id, staff.id, ITEM_PRODUCT, 10)
    db.commit()
    order = pos_svc.checkout(
        db,
        tenant_id=tenant.id,
        customer_id=customer.id,
        items=[{"product_id": product.id, "qty": 1}],
        staff_id=staff.id,
        payment_method="cash",
        mark_paid=True,
    )
    shop_svc.cancel_order(db, tenant_id=tenant.id, order_id=order.id)
    db.refresh(order)
    rows = db.query(CommissionEarning).all()
    assert order.status == ORDER_CANCELLED
    assert len(rows) == 1
    assert rows[0].reversed_at is not None
    with pytest.raises(commissions_svc.CommissionError, match="沒有尚未結算"):
        today = datetime.datetime.now(datetime.timezone.utc).date()
        commissions_svc.create_pay_run(
            db,
            tenant_id=tenant.id,
            period_start=today,
            period_end=today,
            actor_user_id=1,
        )


def test_cancel_after_finalized_run_creates_next_period_negative_reversal(db):
    tenant, staff, customer, product = _seed(db)
    _rule(db, tenant.id, staff.id, ITEM_PRODUCT, 10)
    db.commit()
    order = pos_svc.checkout(
        db,
        tenant_id=tenant.id,
        customer_id=customer.id,
        items=[{"product_id": product.id, "qty": 1}],
        staff_id=staff.id,
        payment_method="cash",
        mark_paid=True,
    )
    today = datetime.datetime.now(datetime.timezone.utc).date()
    run = commissions_svc.create_pay_run(
        db,
        tenant_id=tenant.id,
        period_start=today,
        period_end=today,
        actor_user_id=1,
    )
    commissions_svc.finalize_pay_run(
        db, tenant_id=tenant.id, pay_run_id=run.id, actor_user_id=1
    )
    db.commit()

    shop_svc.cancel_order(db, tenant_id=tenant.id, order_id=order.id)
    rows = db.query(CommissionEarning).order_by(CommissionEarning.id).all()
    assert len(rows) == 2
    assert rows[0].pay_run_id == run.id
    assert rows[1].pay_run_id is None
    assert rows[1].reversal_of_id == rows[0].id
    assert rows[1].commission_cents == -rows[0].commission_cents

    correction = commissions_svc.create_pay_run(
        db,
        tenant_id=tenant.id,
        period_start=today,
        period_end=today,
        actor_user_id=1,
    )
    assert correction.total_cents == -1_000


def test_cross_tenant_staff_cannot_receive_sale_or_rule(db):
    tenant, _staff, customer, product = _seed(db)
    other = Tenant(name=f"other_{uuid.uuid4().hex[:8]}", plan="pro")
    db.add(other)
    db.flush()
    outsider = Staff(tenant_id=other.id, name="跨店員工")
    db.add(outsider)
    db.commit()

    with pytest.raises(commissions_svc.CommissionError):
        _rule(db, tenant.id, outsider.id, ITEM_PRODUCT, 10)
    with pytest.raises(pos_svc.StaffNotFound):
        pos_svc.checkout(
            db,
            tenant_id=tenant.id,
            customer_id=customer.id,
            items=[{"product_id": product.id, "qty": 1}],
            staff_id=outsider.id,
            payment_method="cash",
            mark_paid=True,
        )


def test_paid_sale_with_rules_or_tip_requires_staff_attribution(db):
    tenant, staff, customer, product = _seed(db)
    _rule(db, tenant.id, staff.id, ITEM_PRODUCT, 10)
    db.commit()
    with pytest.raises(pos_svc.StaffRequired):
        pos_svc.checkout(
            db,
            tenant_id=tenant.id,
            customer_id=customer.id,
            items=[{"product_id": product.id, "qty": 1}],
            payment_method="cash",
            mark_paid=True,
        )
    db.rollback()
    assert db.query(OrderItem).count() == 0
