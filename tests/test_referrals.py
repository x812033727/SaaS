"""R11-B — 評論連結佔位符 + 推薦迴路。"""

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

from saas_mvp.models.booking_slot import BookingSlot  # noqa: E402
from saas_mvp.models.business_profile import BusinessProfile  # noqa: E402
from saas_mvp.models.customer import Customer  # noqa: E402
from saas_mvp.models.reservation import Reservation  # noqa: E402
from saas_mvp.models.tenant import Tenant  # noqa: E402
from saas_mvp.services import booking as booking_svc  # noqa: E402
from saas_mvp.services import marketing as marketing_svc  # noqa: E402
from saas_mvp.services import referrals as referrals_svc  # noqa: E402

_engine = create_engine(
    "sqlite:///:memory:", connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
_Session = sessionmaker(autocommit=False, autoflush=False, bind=_engine)


@pytest.fixture(autouse=True)
def _fresh():
    Base.metadata.drop_all(bind=_engine)
    Base.metadata.create_all(bind=_engine)
    yield


def _tenant(db) -> int:
    t = Tenant(name=f"t_{uuid.uuid4().hex[:6]}", plan="free")
    db.add(t)
    db.flush()
    return t.id


def _customer(db, tid, line=None) -> Customer:
    c = Customer(
        tenant_id=tid, line_user_id=line or f"U{uuid.uuid4().hex[:8]}",
        display_name="客", points_balance=0,
    )
    db.add(c)
    db.flush()
    return c


class TestReferralCode:
    def test_code_stable_and_unique(self):
        db = _Session()
        try:
            tid = _tenant(db)
            a = _customer(db, tid)
            b = _customer(db, tid)
            code_a = referrals_svc.get_or_create_code(db, a)
            assert referrals_svc.get_or_create_code(db, a) == code_a
            assert referrals_svc.get_or_create_code(db, b) != code_a
            assert len(code_a) == 6
        finally:
            db.close()

    def test_bind_rules(self):
        db = _Session()
        try:
            tid = _tenant(db)
            referrer = _customer(db, tid)
            newbie = _customer(db, tid)
            code = referrals_svc.get_or_create_code(db, referrer)
            # 自薦擋
            referrals_svc.get_or_create_code(db, newbie)
            with pytest.raises(referrals_svc.ReferralError):
                referrals_svc.bind_by_code(
                    db, customer=newbie, code=newbie.referral_code
                )
            # 正常綁定(小寫也收)
            referrals_svc.bind_by_code(db, customer=newbie, code=code.lower())
            assert newbie.referred_by_customer_id == referrer.id
            # 換綁擋
            with pytest.raises(referrals_svc.ReferralError):
                referrals_svc.bind_by_code(db, customer=newbie, code=code)
            # 跨租戶碼無效
            tid2 = _tenant(db)
            other = _customer(db, tid2)
            with pytest.raises(referrals_svc.ReferralError):
                referrals_svc.bind_by_code(db, customer=other, code=code)
        finally:
            db.close()


class TestReferralReward:
    _slot_seq = iter(range(1, 10_000))

    def _reservation(self, db, tid, customer) -> Reservation:
        import datetime

        slot = BookingSlot(
            tenant_id=tid,
            slot_start=datetime.datetime(2031, 1, 1, 10)
            + datetime.timedelta(seconds=next(self._slot_seq)),
            max_capacity=5,
        )
        db.add(slot)
        db.flush()
        r = Reservation(
            tenant_id=tid, slot_id=slot.id, party_size=1,
            status="confirmed", customer_id=customer.id,
            line_user_id=customer.line_user_id,
        )
        db.add(r)
        db.flush()
        return r

    def test_first_attendance_rewards_once(self):
        db = _Session()
        try:
            tid = _tenant(db)
            referrer = _customer(db, tid)
            newbie = _customer(db, tid)
            code = referrals_svc.get_or_create_code(db, referrer)
            referrals_svc.bind_by_code(db, customer=newbie, code=code)
            r = self._reservation(db, tid, newbie)
            db.commit()
            booking_svc.mark_attendance(
                db, tenant_id=tid, reservation_id=r.id, attended=True
            )
            db.refresh(referrer)
            assert referrer.points_balance == 50  # 預設 referral_points
            assert newbie.referral_rewarded_at is not None
            # 再標到場(或第二筆預約)不重複發
            r2 = self._reservation(db, tid, newbie)
            db.commit()
            booking_svc.mark_attendance(
                db, tenant_id=tid, reservation_id=r2.id, attended=True
            )
            db.refresh(referrer)
            assert referrer.points_balance == 50
        finally:
            db.close()

    def test_unreferred_attendance_noop(self):
        db = _Session()
        try:
            tid = _tenant(db)
            c = _customer(db, tid)
            r = self._reservation(db, tid, c)
            db.commit()
            booking_svc.mark_attendance(
                db, tenant_id=tid, reservation_id=r.id, attended=True
            )
            assert c.referral_rewarded_at is None
        finally:
            db.close()


class TestPlaceholders:
    def test_render_review_and_referral(self):
        db = _Session()
        try:
            tid = _tenant(db)
            db.add(BusinessProfile(
                tenant_id=tid, slug=f"s{uuid.uuid4().hex[:6]}",
                is_published=True, review_url="https://g.page/r/xyz",
            ))
            c = _customer(db, tid)
            db.flush()
            text = marketing_svc._render(
                "感謝 {name}!評論:{review_url} 推薦碼:{referral_code}",
                c, db=db, review_url="https://g.page/r/xyz",
            )
            assert "https://g.page/r/xyz" in text
            assert c.referral_code in text
            assert "{" not in text
        finally:
            db.close()

    def test_render_without_placeholders_unchanged(self):
        db = _Session()
        try:
            tid = _tenant(db)
            c = _customer(db, tid)
            text = marketing_svc._render("哈囉 {name}", c, db=db)
            assert text == "哈囉 客"
            assert c.referral_code is None  # 不需要就不產碼
        finally:
            db.close()
