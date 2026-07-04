"""報表分析 service 測試（DB 直連）。"""

from __future__ import annotations

import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from saas_mvp.models import tenant as _t  # noqa: F401
from saas_mvp.models import customer as _c  # noqa: F401
from saas_mvp.models import booking_slot as _bs  # noqa: F401
from saas_mvp.models import reservation as _r  # noqa: F401
from saas_mvp.models import reservation_reminder as _rr  # noqa: F401
from saas_mvp.models import coupon as _cp, coupon_redemption as _cr, point_transaction as _pt  # noqa: F401

from saas_mvp.db import Base
from saas_mvp.models.booking_slot import BookingSlot
from saas_mvp.models.tenant import Tenant
from saas_mvp.services import analytics as an
from saas_mvp.services import booking as booking_svc

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


def _tenant(db, name="an") -> int:
    t = Tenant(name=name, plan="free")
    db.add(t)
    db.commit()
    return t.id


def _slot(db, tid, hour=18, cap=10) -> int:
    slot = BookingSlot(
        tenant_id=tid,
        slot_start=datetime.datetime(2030, 1, 1, hour, 0, tzinfo=datetime.timezone.utc),
        max_capacity=cap,
    )
    db.add(slot)
    db.commit()
    return slot.id


class TestSummary:
    def test_cancel_rate_and_covers(self, db):
        tid = _tenant(db)
        sid = _slot(db, tid, cap=20)
        r1 = booking_svc.book_slot(db, tenant_id=tid, slot_id=sid, party_size=2, line_user_id="U1")
        booking_svc.book_slot(db, tenant_id=tid, slot_id=sid, party_size=3, line_user_id="U2")
        booking_svc.cancel_reservation(db, tenant_id=tid, reservation_id=r1.id)
        s = an.booking_summary(db, tenant_id=tid)
        assert s["total"] == 2
        assert s["confirmed"] == 1 and s["cancelled"] == 1
        assert s["cancel_rate"] == 0.5
        assert s["total_covers"] == 3  # 僅 confirmed 計入
        assert s["distinct_customers"] == 2
        assert s["no_show_rate"] is None  # 尚未標記到場

    def test_no_show_rate_after_marking(self, db):
        tid = _tenant(db)
        sid = _slot(db, tid, cap=20)
        a = booking_svc.book_slot(db, tenant_id=tid, slot_id=sid, party_size=1, line_user_id="U1")
        b = booking_svc.book_slot(db, tenant_id=tid, slot_id=sid, party_size=1, line_user_id="U2")
        booking_svc.mark_attendance(db, tenant_id=tid, reservation_id=a.id, attended=True)
        booking_svc.mark_attendance(db, tenant_id=tid, reservation_id=b.id, attended=False)
        s = an.booking_summary(db, tenant_id=tid)
        assert s["attended"] == 1 and s["no_show"] == 1
        assert s["no_show_rate"] == 0.5


class TestUtilization:
    def test_buckets_by_hour(self, db):
        tid = _tenant(db)
        s18 = _slot(db, tid, hour=18, cap=10)
        _slot(db, tid, hour=12, cap=4)
        booking_svc.book_slot(db, tenant_id=tid, slot_id=s18, party_size=5, line_user_id="U1")
        util = an.slot_utilization(db, tenant_id=tid)
        by_hour = {u["hour"]: u for u in util}
        assert by_hour[18]["booked"] == 5 and by_hour[18]["capacity"] == 10
        assert by_hour[18]["utilization"] == 0.5
        assert by_hour[12]["utilization"] == 0.0
        # 小時遞增排序（SQL GROUP BY 後 ORDER BY）
        assert [u["hour"] for u in util] == sorted(u["hour"] for u in util)

    def test_empty(self, db):
        tid = _tenant(db)
        assert an.slot_utilization(db, tenant_id=tid) == []


class TestTopCustomers:
    def test_order_by_booking_count(self, db):
        tid = _tenant(db)
        sid = _slot(db, tid, cap=50)
        for _ in range(3):
            booking_svc.book_slot(db, tenant_id=tid, slot_id=sid, party_size=1, line_user_id="Ufreq")
        booking_svc.book_slot(db, tenant_id=tid, slot_id=sid, party_size=1, line_user_id="Urare")
        top = an.top_customers(db, tenant_id=tid, limit=10)
        assert top[0].line_user_id == "Ufreq" and top[0].booking_count == 3


class TestExportRows:
    def test_export_includes_slot_and_attended(self, db):
        tid = _tenant(db)
        sid = _slot(db, tid, cap=10)
        r = booking_svc.book_slot(db, tenant_id=tid, slot_id=sid, party_size=2, line_user_id="U1")
        booking_svc.mark_attendance(db, tenant_id=tid, reservation_id=r.id, attended=True)
        rows = an.export_rows(db, tenant_id=tid)
        assert len(rows) == 1
        assert rows[0]["party_size"] == 2 and rows[0]["attended"] == "yes"
        assert rows[0]["slot_start"]

    def test_cross_tenant_isolation(self, db):
        tid = _tenant(db)
        other = _tenant(db, name="other")
        sid = _slot(db, tid, cap=10)
        booking_svc.book_slot(db, tenant_id=tid, slot_id=sid, party_size=1, line_user_id="U1")
        assert an.export_rows(db, tenant_id=other) == []
        assert an.booking_summary(db, tenant_id=other)["total"] == 0
