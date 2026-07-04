"""pricing.apply_order_discounts 共用折扣 helper 直接單元測試（三路結帳共用契約）。"""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("SAAS_RATE_LIMIT_ENABLED", "false")

from saas_mvp.db import Base, import_all_models  # noqa: E402

import_all_models()

from saas_mvp.models.coupon import Coupon  # noqa: E402
from saas_mvp.models.customer import Customer  # noqa: E402
from saas_mvp.models.order import Order  # noqa: E402
from saas_mvp.models.tenant import Tenant  # noqa: E402
from saas_mvp.services import coupons as coupons_svc  # noqa: E402
from saas_mvp.services import pricing as pricing_svc  # noqa: E402

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


def _order(db, tid) -> Order:
    o = Order(tenant_id=tid, status="pending", total_cents=0, currency="TWD")
    db.add(o)
    db.flush()
    return o


def test_tier_only_sets_discount_and_returns_total(db):
    tid = _tenant(db)
    c = Customer(tenant_id=tid, line_user_id="U1", tier="gold")
    db.add(c)
    db.flush()
    order = _order(db, tid)
    out = pricing_svc.apply_order_discounts(
        db, tenant_id=tid, order=order, customer=c,
        subtotal_cents=10000, line_user_id=None, coupon_code=None,
    )
    assert out == 9000  # gold 10%
    assert order.discount_cents == 1000
    assert order.coupon_code is None


def test_no_customer_no_discount(db):
    tid = _tenant(db)
    order = _order(db, tid)
    out = pricing_svc.apply_order_discounts(
        db, tenant_id=tid, order=order, customer=None,
        subtotal_cents=10000, line_user_id=None, coupon_code=None,
    )
    assert out == 10000 and order.discount_cents == 0


def test_coupon_without_line_user_raises(db):
    tid = _tenant(db)
    db.add(Coupon(tenant_id=tid, code="X", name="x",
                  discount_type="amount", discount_value=100))
    order = _order(db, tid)
    db.flush()
    # 無 customer、無 line_user_id → 不可核銷
    with pytest.raises(coupons_svc.CouponError):
        pricing_svc.apply_order_discounts(
            db, tenant_id=tid, order=order, customer=None,
            subtotal_cents=10000, line_user_id=None, coupon_code="X",
        )


def test_coupon_uses_param_line_user_when_no_customer(db):
    """無建檔顧客但有 line_user_id（LINE 散客）→ 仍可套券，以毛額判 min_spend。"""
    tid = _tenant(db)
    db.add(Coupon(tenant_id=tid, code="Y", name="y",
                  discount_type="amount", discount_value=300))
    order = _order(db, tid)
    db.flush()
    out = pricing_svc.apply_order_discounts(
        db, tenant_id=tid, order=order, customer=None,
        subtotal_cents=5000, line_user_id="Uguest", coupon_code="Y",
    )
    assert out == 4700  # 5000 - 300（無等級折）
    assert order.discount_cents == 300 and order.coupon_code == "Y"
