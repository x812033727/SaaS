"""R2-6 測試 — F4 報表深度 + E3 SMS fallback + D4 FAQ 自學。"""

from __future__ import annotations

import datetime
import os
import uuid

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("SAAS_RATE_LIMIT_ENABLED", "false")

from saas_mvp.config import settings  # noqa: E402
from saas_mvp.db import Base  # noqa: E402
from saas_mvp.models.ai_unanswered_question import (  # noqa: E402
    UNANSWERED_CONVERTED,
    UNANSWERED_OPEN,
    AiUnansweredQuestion,
)
from saas_mvp.models.booking_slot import BookingSlot  # noqa: E402
from saas_mvp.models.customer import Customer  # noqa: E402
from saas_mvp.models.faq_entry import FAQEntry  # noqa: E402
from saas_mvp.models.order import ORDER_PAID, Order  # noqa: E402
from saas_mvp.models.reservation import Reservation  # noqa: E402
from saas_mvp.models.tenant import Tenant  # noqa: E402
from saas_mvp.services import analytics as analytics_svc  # noqa: E402
from saas_mvp.services import booking as booking_svc  # noqa: E402
from saas_mvp.services import faq as faq_svc  # noqa: E402
from saas_mvp.services import reporting as reporting_svc  # noqa: E402
from saas_mvp.services.sms import StubSmsProvider  # noqa: E402

_engine = create_engine(
    "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
)
_Session = sessionmaker(autocommit=False, autoflush=False, bind=_engine)

_NOW = datetime.datetime(2030, 6, 15, 12, 0, tzinfo=datetime.timezone.utc)


@pytest.fixture()
def db():
    Base.metadata.drop_all(bind=_engine)
    Base.metadata.create_all(bind=_engine)
    s = _Session()
    try:
        yield s
    finally:
        s.close()


def _tenant(db) -> Tenant:
    t = Tenant(name=f"r26_{uuid.uuid4().hex[:8]}", plan="pro")
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


def _paid_order(db, tid, cents, paid_at):
    o = Order(tenant_id=tid, line_user_id="U", status=ORDER_PAID,
              total_cents=cents, paid_at=paid_at)
    db.add(o)
    db.commit()
    return o


# ── F4 ───────────────────────────────────────────────────────────────────────

class TestRevenueSummary:
    def test_sums_paid_only(self, db):
        t = _tenant(db)
        _paid_order(db, t.id, 50000, _NOW)
        _paid_order(db, t.id, 30000, _NOW)
        db.add(Order(tenant_id=t.id, line_user_id="U", status="pending",
                     total_cents=99900))
        db.commit()
        out = analytics_svc.revenue_summary(db, tenant_id=t.id)
        assert out == {"paid_orders": 2, "revenue_cents": 80000,
                       "avg_order_cents": 40000}

    def test_empty(self, db):
        t = _tenant(db)
        out = analytics_svc.revenue_summary(db, tenant_id=t.id)
        assert out["paid_orders"] == 0 and out["avg_order_cents"] == 0


class TestTrendSeries:
    def test_buckets_bookings_and_revenue(self, db):
        t = _tenant(db)
        slot = BookingSlot(tenant_id=t.id, slot_start=_NOW, max_capacity=8)
        db.add(slot)
        db.commit()
        booking_svc.book_slot(db, tenant_id=t.id, slot_id=slot.id,
                              party_size=1, line_user_id="U1")
        booking_svc.book_slot(db, tenant_id=t.id, slot_id=slot.id,
                              party_size=1, line_user_id="U2")
        _paid_order(db, t.id, 12300, _NOW)
        out = analytics_svc.trend_series(
            db, tenant_id=t.id, period="week", periods=4, now=_NOW
        )
        assert len(out) == 4
        cur = out[-1]  # 最新一期在尾端
        assert cur["bookings"] == 2 and cur["revenue_cents"] == 12300
        assert all(b["bookings"] == 0 for b in out[:-1])

    def test_month_period_keys(self, db):
        t = _tenant(db)
        out = analytics_svc.trend_series(
            db, tenant_id=t.id, period="month", periods=3, now=_NOW
        )
        assert [b["period"] for b in out] == ["2030-04", "2030-05", "2030-06"]


class TestStaffAttendance:
    def test_staff_performance_includes_attendance(self, db):
        from saas_mvp.models.staff import Staff

        t = _tenant(db)
        st = Staff(tenant_id=t.id, name="Amy")
        slot = BookingSlot(tenant_id=t.id, slot_start=_NOW, max_capacity=8)
        db.add_all([st, slot])
        db.commit()
        r1 = booking_svc.book_slot(db, tenant_id=t.id, slot_id=slot.id,
                                   party_size=1, line_user_id="U1", staff_id=st.id)
        r2 = booking_svc.book_slot(db, tenant_id=t.id, slot_id=slot.id,
                                   party_size=1, line_user_id="U2", staff_id=st.id)
        r1.attended = True
        r2.attended = False
        db.commit()
        out = reporting_svc.staff_performance(db, tenant_id=t.id)
        assert out[0]["attended"] == 1 and out[0]["no_show"] == 1
        assert out[0]["attendance_rate"] == 0.5

    def test_unmarked_rate_none(self, db):
        from saas_mvp.models.staff import Staff

        t = _tenant(db)
        st = Staff(tenant_id=t.id, name="Bob")
        slot = BookingSlot(tenant_id=t.id, slot_start=_NOW, max_capacity=8)
        db.add_all([st, slot])
        db.commit()
        booking_svc.book_slot(db, tenant_id=t.id, slot_id=slot.id,
                              party_size=1, line_user_id="U1", staff_id=st.id)
        out = reporting_svc.staff_performance(db, tenant_id=t.id)
        assert out[0]["attendance_rate"] is None


# ── E3 SMS ───────────────────────────────────────────────────────────────────

class TestSmsFallback:
    def _setup(self, db, phone="0912345678"):
        t = _tenant(db)
        db.add(Customer(tenant_id=t.id, line_user_id="Usms", phone=phone))
        db.commit()
        return t

    def test_fallback_sends_when_enabled_and_phone(self, db, monkeypatch):
        from saas_mvp.ops import send_due_reminders as mod
        from saas_mvp.services import sms as sms_mod

        t = self._setup(db)
        stub = StubSmsProvider()
        monkeypatch.setattr(settings, "sms_fallback_enabled", True)
        monkeypatch.setattr(sms_mod, "_stub_singleton", stub)
        mod._sms_fallback(db, t.id, "Usms", "提醒:明天 10:00 預約")
        assert len(stub.sent) == 1
        assert stub.sent[0].to == "0912345678"

    def test_flag_off_noop(self, db, monkeypatch):
        from saas_mvp.ops import send_due_reminders as mod
        from saas_mvp.services import sms as sms_mod

        t = self._setup(db)
        stub = StubSmsProvider()
        monkeypatch.setattr(settings, "sms_fallback_enabled", False)
        monkeypatch.setattr(sms_mod, "_stub_singleton", stub)
        mod._sms_fallback(db, t.id, "Usms", "x")
        assert stub.sent == []

    def test_no_phone_noop(self, db, monkeypatch):
        from saas_mvp.ops import send_due_reminders as mod
        from saas_mvp.services import sms as sms_mod

        t = self._setup(db, phone=None)
        stub = StubSmsProvider()
        monkeypatch.setattr(settings, "sms_fallback_enabled", True)
        monkeypatch.setattr(sms_mod, "_stub_singleton", stub)
        mod._sms_fallback(db, t.id, "Usms", "x")
        assert stub.sent == []


# ── D4 FAQ 自學 ──────────────────────────────────────────────────────────────

class TestUnanswered:
    def test_record_upsert_dedup(self, db):
        t = _tenant(db)
        faq_svc.record_unanswered(db, tenant_id=t.id, question="請問有停車位嗎?")
        faq_svc.record_unanswered(db, tenant_id=t.id, question="請問有停車位嗎?")
        rows = db.execute(select(AiUnansweredQuestion)).scalars().all()
        assert len(rows) == 1 and rows[0].hit_count == 2

    def test_convert_creates_faq_and_marks(self, db):
        t = _tenant(db)
        faq_svc.record_unanswered(db, tenant_id=t.id, question="可以帶寵物嗎?")
        row = db.execute(select(AiUnansweredQuestion)).scalar_one()
        faq = faq_svc.convert_unanswered(
            db, tenant_id=t.id, unanswered_id=row.id, answer="可以,需牽繩。"
        )
        assert isinstance(faq, FAQEntry) and faq.question == "可以帶寵物嗎?"
        db.refresh(row)
        assert row.status == UNANSWERED_CONVERTED
        assert faq_svc.list_unanswered(db, tenant_id=t.id) == []

    def test_convert_rejects_empty_answer(self, db):
        from fastapi import HTTPException

        t = _tenant(db)
        faq_svc.record_unanswered(db, tenant_id=t.id, question="有停車位嗎")
        row = db.execute(select(AiUnansweredQuestion)).scalar_one()
        with pytest.raises(HTTPException) as ei:
            faq_svc.convert_unanswered(
                db, tenant_id=t.id, unanswered_id=row.id, answer="   "
            )
        assert ei.value.status_code == 400
        # 未建空白 FAQ、未標 converted、問題仍在待答清單
        db.refresh(row)
        assert row.status != UNANSWERED_CONVERTED
        assert len(faq_svc.list_unanswered(db, tenant_id=t.id)) == 1

    def test_reasked_after_convert_reopens(self, db):
        t = _tenant(db)
        faq_svc.record_unanswered(db, tenant_id=t.id, question="有素食嗎")
        row = db.execute(select(AiUnansweredQuestion)).scalar_one()
        faq_svc.convert_unanswered(
            db, tenant_id=t.id, unanswered_id=row.id, answer="有"
        )
        faq_svc.record_unanswered(db, tenant_id=t.id, question="有素食嗎")
        db.refresh(row)
        assert row.status == UNANSWERED_OPEN and row.hit_count == 2

    def test_tenant_isolation(self, db):
        t1, t2 = _tenant(db), _tenant(db)
        faq_svc.record_unanswered(db, tenant_id=t1.id, question="Q1")
        row = db.execute(select(AiUnansweredQuestion)).scalar_one()
        from fastapi import HTTPException

        with pytest.raises(HTTPException):
            faq_svc.convert_unanswered(
                db, tenant_id=t2.id, unanswered_id=row.id, answer="A"
            )

    def test_dismiss(self, db):
        t = _tenant(db)
        faq_svc.record_unanswered(db, tenant_id=t.id, question="Q")
        row = db.execute(select(AiUnansweredQuestion)).scalar_one()
        faq_svc.dismiss_unanswered(db, tenant_id=t.id, unanswered_id=row.id)
        assert faq_svc.list_unanswered(db, tenant_id=t.id) == []
