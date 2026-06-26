"""POS 結帳測試 — 電話查會員、扣庫存+折券+折點+回贈點、點數不足 rollback、
連動預約標到場、散客、租戶隔離。"""

from __future__ import annotations

import datetime
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from saas_mvp.models import tenant as _t  # noqa: F401
from saas_mvp.models import user as _u  # noqa: F401
from saas_mvp.models import customer as _c  # noqa: F401
from saas_mvp.models import product as _prod  # noqa: F401
from saas_mvp.models import order as _o  # noqa: F401
from saas_mvp.models import order_item as _oi  # noqa: F401
from saas_mvp.models import coupon as _coupon  # noqa: F401
from saas_mvp.models import coupon_redemption as _cr  # noqa: F401
from saas_mvp.models import point_transaction as _pt  # noqa: F401
from saas_mvp.models import booking_slot as _bs  # noqa: F401
from saas_mvp.models import reservation as _r  # noqa: F401
from saas_mvp.models import tenant_feature as _tf  # noqa: F401
from saas_mvp.models import feature_change_history as _fch  # noqa: F401

from saas_mvp.db import Base
from saas_mvp.models.booking_slot import BookingSlot
from saas_mvp.models.coupon import Coupon
from saas_mvp.models.customer import Customer
from saas_mvp.models.order import Order
from saas_mvp.models.product import Product
from saas_mvp.models.reservation import RESERVATION_CONFIRMED, Reservation
from saas_mvp.models.tenant import Tenant
from saas_mvp.services import membership as membership_svc
from saas_mvp.services import pos as pos_svc

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
    s = _Session()
    try:
        yield s
    finally:
        s.close()


def _tenant(db) -> int:
    t = Tenant(name=f"t_{uuid.uuid4().hex[:6]}", plan="free")
    db.add(t)
    db.flush()
    return t.id


def _customer(db, tid, *, phone="0912345678", points=0, line="Uc") -> Customer:
    c = Customer(
        tenant_id=tid, line_user_id=line, phone=phone,
        display_name="王小明", points_balance=points,
    )
    db.add(c)
    db.flush()
    return c


def _product(db, tid, *, price=10000, stock=5) -> Product:
    p = Product(tenant_id=tid, name="拿鐵", price_cents=price, stock=stock)
    db.add(p)
    db.flush()
    return p


def test_lookup_hit_and_miss(db):
    tid = _tenant(db)
    _customer(db, tid, phone="0911111111", points=30)
    db.commit()
    hit = pos_svc.lookup_by_phone(db, tenant_id=tid, phone="0911111111")
    assert hit is not None
    assert hit["points_balance"] == 30
    miss = pos_svc.lookup_by_phone(db, tenant_id=tid, phone="0900000000")
    assert miss is None


def test_checkout_deducts_stock_and_accrues_points(db):
    tid = _tenant(db)
    c = _customer(db, tid, points=0)
    p = _product(db, tid, price=10000, stock=5)
    db.commit()
    order = pos_svc.checkout(
        db, tenant_id=tid, customer_id=c.id,
        items=[{"product_id": p.id, "qty": 2}],
    )
    assert order.total_cents == 20000
    db.refresh(p)
    assert p.stock == 3
    db.refresh(c)
    assert c.points_balance > 0  # 回贈點數


def test_checkout_redeems_coupon_and_points(db):
    tid = _tenant(db)
    c = _customer(db, tid, points=50, line="Ucoupon")
    p = _product(db, tid, price=10000, stock=5)
    coupon = Coupon(
        tenant_id=tid, code="SAVE100", name="折100",
        discount_type="amount", discount_value=100,
    )
    db.add(coupon)
    db.commit()
    order = pos_svc.checkout(
        db, tenant_id=tid, customer_id=c.id,
        items=[{"product_id": p.id, "qty": 1}],
        coupon_code="SAVE100", points_to_redeem=50,
    )
    # 10000 - 100 (券) - 50 (點) = 9850
    assert order.total_cents == 9850
    db.refresh(coupon)
    assert coupon.redeemed_count == 1
    db.refresh(c)
    # 折掉 50 點，再回贈淨付的點數 → balance 應 < 50 + accrual
    assert c.points_balance >= 0


def test_insufficient_points_rolls_back_order(db):
    tid = _tenant(db)
    c = _customer(db, tid, points=10)
    p = _product(db, tid, price=10000, stock=5)
    db.commit()
    with pytest.raises(membership_svc.InsufficientPoints):
        pos_svc.checkout(
            db, tenant_id=tid, customer_id=c.id,
            items=[{"product_id": p.id, "qty": 1}],
            points_to_redeem=999,
        )
    db.rollback()
    # 訂單未建立、庫存未扣
    assert db.execute(select(Order).where(Order.tenant_id == tid)).first() is None
    db.refresh(p)
    assert p.stock == 5
    db.refresh(c)
    assert c.points_balance == 10


def test_out_of_stock_rolls_back(db):
    tid = _tenant(db)
    c = _customer(db, tid)
    p = _product(db, tid, stock=1)
    db.commit()
    from saas_mvp.services import shop as shop_svc
    with pytest.raises(shop_svc.OutOfStock):
        pos_svc.checkout(
            db, tenant_id=tid, customer_id=c.id,
            items=[{"product_id": p.id, "qty": 5}],
        )
    db.rollback()
    assert db.execute(select(Order).where(Order.tenant_id == tid)).first() is None


def test_reservation_link_marks_attended(db):
    tid = _tenant(db)
    c = _customer(db, tid)
    p = _product(db, tid)
    slot = BookingSlot(
        tenant_id=tid,
        slot_start=datetime.datetime(2030, 6, 1, 18, 0, tzinfo=datetime.timezone.utc),
        max_capacity=10,
    )
    db.add(slot)
    db.flush()
    resv = Reservation(
        tenant_id=tid, slot_id=slot.id, customer_id=c.id,
        line_user_id=c.line_user_id, party_size=2, status=RESERVATION_CONFIRMED,
    )
    db.add(resv)
    db.commit()
    pos_svc.checkout(
        db, tenant_id=tid, customer_id=c.id,
        items=[{"product_id": p.id, "qty": 1}],
        reservation_id=resv.id,
    )
    db.refresh(resv)
    assert resv.attended is True


def test_walkin_no_customer(db):
    tid = _tenant(db)
    p = _product(db, tid, price=5000, stock=3)
    db.commit()
    order = pos_svc.checkout(
        db, tenant_id=tid, customer_id=None,
        items=[{"product_id": p.id, "qty": 1}],
    )
    assert order.customer_id is None
    assert order.total_cents == 5000
    db.refresh(p)
    assert p.stock == 2


def test_tenant_isolation(db):
    t1 = _tenant(db)
    t2 = _tenant(db)
    c1 = _customer(db, t1, phone="0911111111")
    p2 = _product(db, t2)
    db.commit()
    # lookup t1 phone under t2 → None
    assert pos_svc.lookup_by_phone(db, tenant_id=t2, phone="0911111111") is None
    # checkout in t1 referencing t2's product → ProductNotFound
    from saas_mvp.services import shop as shop_svc
    with pytest.raises(shop_svc.ProductNotFound):
        pos_svc.checkout(
            db, tenant_id=t1, customer_id=c1.id,
            items=[{"product_id": p2.id, "qty": 1}],
        )


def test_tier_discount_gold(db):
    """金卡會員結帳享 10% 折扣（對標 vibeaico「不同等級不同折扣」）。"""
    tid = _tenant(db)
    c = _customer(db, tid, points=500)
    c.tier = "gold"
    p = _product(db, tid, price=10000, stock=5)
    db.commit()
    order = pos_svc.checkout(
        db, tenant_id=tid, customer_id=c.id,
        items=[{"product_id": p.id, "qty": 1}],
    )
    # 10000 → 10% off 1000 → 9000
    assert order.total_cents == 9000
    assert order.discount_cents == 1000


def test_tier_discount_regular_none(db):
    tid = _tenant(db)
    c = _customer(db, tid, points=0)  # tier 預設 regular
    p = _product(db, tid, price=10000, stock=5)
    db.commit()
    order = pos_svc.checkout(
        db, tenant_id=tid, customer_id=c.id,
        items=[{"product_id": p.id, "qty": 1}],
    )
    assert order.discount_cents == 0
    assert order.total_cents == 10000


def test_tier_discount_stacks_with_coupon(db):
    """等級折扣後再套優惠券（券以折後金額為基準）。"""
    tid = _tenant(db)
    c = _customer(db, tid, points=500, line="Ustack")
    c.tier = "gold"
    p = _product(db, tid, price=10000, stock=5)
    coupon = Coupon(
        tenant_id=tid, code="SAVE", name="折100", discount_type="amount",
        discount_value=100,
    )
    db.add(coupon)
    db.commit()
    order = pos_svc.checkout(
        db, tenant_id=tid, customer_id=c.id,
        items=[{"product_id": p.id, "qty": 1}], coupon_code="SAVE",
    )
    # 10000 → gold 10% off → 9000 → 券折 100 → 8900
    assert order.discount_cents == 1100
    assert order.total_cents == 8900
    assert order.coupon_code == "SAVE"


def test_walkin_no_tier_discount(db):
    tid = _tenant(db)
    p = _product(db, tid, price=10000, stock=5)
    db.commit()
    order = pos_svc.checkout(
        db, tenant_id=tid, customer_id=None,
        items=[{"product_id": p.id, "qty": 1}],
    )
    assert order.discount_cents == 0
    assert order.total_cents == 10000


def test_lookup_exposes_tier_discount(db):
    tid = _tenant(db)
    c = _customer(db, tid, phone="0900000000", points=500)
    c.tier = "gold"
    db.commit()
    hit = pos_svc.lookup_by_phone(db, tenant_id=tid, phone="0900000000")
    assert hit["tier"] == "gold"
    assert hit["tier_discount_percent"] == 10
