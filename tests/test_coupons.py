"""優惠券核銷 + 會員集點 service 測試（DB 直連）。"""

from __future__ import annotations

import datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from saas_mvp.models import tenant as _t  # noqa: F401
from saas_mvp.models import customer as _c  # noqa: F401
from saas_mvp.models import booking_slot as _bs  # noqa: F401
from saas_mvp.models import reservation as _r  # noqa: F401
from saas_mvp.models import reservation_reminder as _rr  # noqa: F401
from saas_mvp.models import coupon as _cp  # noqa: F401
from saas_mvp.models import coupon_redemption as _cr  # noqa: F401
from saas_mvp.models import point_transaction as _pt  # noqa: F401

from saas_mvp.db import Base
from saas_mvp.models.booking_slot import BookingSlot
from saas_mvp.models.coupon import Coupon
from saas_mvp.models.customer import Customer
from saas_mvp.models.tenant import Tenant
from saas_mvp.services import booking as booking_svc
from saas_mvp.services import coupons as coupons_svc
from saas_mvp.services import membership as membership_svc

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


def _tenant(db, name="cp") -> int:
    t = Tenant(name=name, plan="free")
    db.add(t)
    db.commit()
    return t.id


def _coupon(db, tid, *, code="SAVE10", max_redemptions=None, is_active=True,
            active_from=None, active_until=None) -> int:
    c = Coupon(
        tenant_id=tid, code=code, name="折扣", discount_type="percent",
        discount_value=10, max_redemptions=max_redemptions, is_active=is_active,
        active_from=active_from, active_until=active_until,
    )
    db.add(c)
    db.commit()
    return c.id


class TestRedeem:
    def test_redeem_success_then_count(self, db):
        tid = _tenant(db)
        _coupon(db, tid, code="SAVE10", max_redemptions=5)
        r = coupons_svc.redeem_coupon(db, tenant_id=tid, code="SAVE10", line_user_id="U1")
        assert r.id is not None
        c = db.execute(select(Coupon).where(Coupon.code == "SAVE10")).scalar_one()
        assert c.redeemed_count == 1

    def test_one_per_user(self, db):
        tid = _tenant(db)
        _coupon(db, tid, code="ONCE")
        coupons_svc.redeem_coupon(db, tenant_id=tid, code="ONCE", line_user_id="U1")
        with pytest.raises(coupons_svc.AlreadyRedeemed):
            coupons_svc.redeem_coupon(db, tenant_id=tid, code="ONCE", line_user_id="U1")
        # 控制組：另一位使用者可兌換
        coupons_svc.redeem_coupon(db, tenant_id=tid, code="ONCE", line_user_id="U2")

    def test_exhausted(self, db):
        tid = _tenant(db)
        _coupon(db, tid, code="LIM", max_redemptions=1)
        coupons_svc.redeem_coupon(db, tenant_id=tid, code="LIM", line_user_id="U1")
        with pytest.raises(coupons_svc.CouponExhausted):
            coupons_svc.redeem_coupon(db, tenant_id=tid, code="LIM", line_user_id="U2")

    def test_inactive(self, db):
        tid = _tenant(db)
        _coupon(db, tid, code="OFF", is_active=False)
        with pytest.raises(coupons_svc.CouponInactive):
            coupons_svc.redeem_coupon(db, tenant_id=tid, code="OFF", line_user_id="U1")

    def test_expired_window(self, db):
        tid = _tenant(db)
        past = datetime.datetime(2020, 1, 1, tzinfo=datetime.timezone.utc)
        _coupon(db, tid, code="OLD", active_until=past)
        with pytest.raises(coupons_svc.CouponExpired):
            coupons_svc.redeem_coupon(db, tenant_id=tid, code="OLD", line_user_id="U1")

    def test_not_found_and_cross_tenant(self, db):
        tid = _tenant(db)
        other = _tenant(db, name="other")
        _coupon(db, tid, code="MINE")
        with pytest.raises(coupons_svc.CouponNotFound):
            coupons_svc.redeem_coupon(db, tenant_id=other, code="MINE", line_user_id="U1")


class TestMembership:
    def test_tier_recompute(self):
        assert membership_svc.recompute_tier(0) == "regular"
        assert membership_svc.recompute_tier(100) == "silver"
        assert membership_svc.recompute_tier(500) == "gold"

    def test_booking_awards_points(self, db):
        tid = _tenant(db)
        slot = BookingSlot(
            tenant_id=tid,
            slot_start=datetime.datetime(2030, 1, 1, 18, tzinfo=datetime.timezone.utc),
            max_capacity=10,
        )
        db.add(slot)
        db.commit()
        booking_svc.book_slot(db, tenant_id=tid, slot_id=slot.id, party_size=1, line_user_id="Up")
        cust = db.execute(select(Customer).where(Customer.line_user_id == "Up")).scalar_one()
        assert cust.points_balance == 10  # SAAS_POINTS_PER_BOOKING 預設 10
        # 第二次建單再加點
        booking_svc.book_slot(db, tenant_id=tid, slot_id=slot.id, party_size=1, line_user_id="Up")
        db.refresh(cust)
        assert cust.points_balance == 20

    def test_redeem_points_insufficient(self, db):
        tid = _tenant(db)
        cust = Customer(tenant_id=tid, line_user_id="Uz", points_balance=5)
        db.add(cust)
        db.commit()
        with pytest.raises(membership_svc.InsufficientPoints):
            membership_svc.redeem_points(db, tenant_id=tid, customer=cust, amount=10, reason="x")
        # 控制組：足夠可扣
        membership_svc.redeem_points(db, tenant_id=tid, customer=cust, amount=5, reason="x")
        db.commit()
        assert cust.points_balance == 0
