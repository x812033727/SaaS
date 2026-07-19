"""Web 客(無 LINE 身分)通知管道(R12-B)測試。

覆蓋:
- enqueue_reminders:walk-in 客有 email → 入列 line_user_id=NULL;無 email → 0
- send_due_reminders:email-only 列直送 email 佇列(無 LINE cfg 也送)、
  標 SENT、不佔推播額度;顧客 email 事後被清 → skipped(no_email)
- booking_notify:walk-in 客有 email → cancel/change 入列;派送走 email
  (主旨依 kind);無 email → 不入列
- 網路預約確認信:POST 成功 → booking_confirmation 佇列(含 portal 連結)
- 回歸:LINE 客行為不變(推播照走、無 email 列)
"""

from __future__ import annotations

import datetime
import os
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("SAAS_RATE_LIMIT_ENABLED", "false")

from saas_mvp.models import tenant as _t, user as _u  # noqa: F401,E402
from saas_mvp.models import customer as _c, booking_slot as _bs  # noqa: F401,E402
from saas_mvp.models import reservation as _r  # noqa: F401,E402
from saas_mvp.models import business_profile as _bp  # noqa: F401,E402

from saas_mvp.app import create_app  # noqa: E402
from saas_mvp.db import Base, get_db  # noqa: E402
from saas_mvp.line_client import FakeLinePushClient  # noqa: E402
from saas_mvp.models.booking_notification import BookingNotification  # noqa: E402
from saas_mvp.models.booking_slot import BookingSlot  # noqa: E402
from saas_mvp.models.business_profile import BusinessProfile  # noqa: E402
from saas_mvp.models.customer import Customer  # noqa: E402
from saas_mvp.models.email_delivery import EmailDelivery  # noqa: E402
from saas_mvp.models.reservation_reminder import ReservationReminder  # noqa: E402
from saas_mvp.models.service import Service  # noqa: E402
from saas_mvp.models.tenant import Tenant  # noqa: E402
from saas_mvp.ops.send_due_notifications import send_due_notifications  # noqa: E402
from saas_mvp.ops.send_due_reminders import send_due_reminders  # noqa: E402
from saas_mvp.services import booking as booking_svc  # noqa: E402
from saas_mvp.services import booking_notify as notify_svc  # noqa: E402
from saas_mvp.services import features as features_svc  # noqa: E402

_engine = create_engine(
    "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
)
_Session = sessionmaker(autocommit=False, autoflush=False, bind=_engine)

_SLOT_START = datetime.datetime(2030, 6, 1, 18, 0, tzinfo=datetime.timezone.utc)


@pytest.fixture(autouse=True)
def _fresh_db():
    Base.metadata.drop_all(bind=_engine)
    Base.metadata.create_all(bind=_engine)
    yield


@pytest.fixture()
def client():
    app = create_app()

    def override_db():
        s = _Session()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = override_db
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


def _seed(*, features=("AUTO_REMINDER", "BOOKING_NOTIFY", "WEB_BOOKING")):
    """建租戶+時段;回傳 (tenant_id, slot_id)。刻意不建 LineChannelConfig。"""
    db = _Session()
    try:
        t = Tenant(name=f"wn_{uuid.uuid4().hex[:8]}", plan="free")
        db.add(t)
        db.flush()
        slot = BookingSlot(
            tenant_id=t.id,
            slot_start=_SLOT_START,
            slot_end=_SLOT_START + datetime.timedelta(hours=1),
            max_capacity=10,
        )
        db.add(slot)
        db.flush()
        for feat in features:
            features_svc.set_enabled(
                db, t.id, getattr(features_svc, feat), True,
                actor_user_id=None, source="test",
            )
        db.commit()
        return t.id, slot.id
    finally:
        db.close()


def _walkin_customer(tid, *, email="web@mail.tw"):
    db = _Session()
    try:
        c = Customer(
            tenant_id=tid, line_user_id=None,
            display_name="網路客", phone="0912000111", email=email,
        )
        db.add(c)
        db.commit()
        return c.id
    finally:
        db.close()


def _book(tid, slot_id, customer_id):
    db = _Session()
    try:
        resv = booking_svc.book_slot(
            db, tenant_id=tid, slot_id=slot_id, customer_id=customer_id
        )
        return resv.id
    finally:
        db.close()


def _reminders(tid):
    db = _Session()
    try:
        return list(
            db.execute(
                select(ReservationReminder).where(
                    ReservationReminder.tenant_id == tid
                )
            ).scalars()
        )
    finally:
        db.close()


def _emails():
    db = _Session()
    try:
        return list(db.execute(select(EmailDelivery)).scalars())
    finally:
        db.close()


class TestReminderEnqueue:
    def test_walkin_with_email_enqueued_null_line(self):
        tid, slot_id = _seed()
        cid = _walkin_customer(tid)
        _book(tid, slot_id, cid)
        rows = _reminders(tid)
        assert len(rows) == 2  # day_before + day_of
        assert all(r.line_user_id is None for r in rows)

    def test_walkin_without_email_not_enqueued(self):
        tid, slot_id = _seed()
        cid = _walkin_customer(tid, email=None)
        _book(tid, slot_id, cid)
        assert _reminders(tid) == []


class TestReminderEmailDelivery:
    def _run(self):
        return send_due_reminders(
            session_factory=_Session,
            push_client=FakeLinePushClient(),
            apply=True,
            now=_SLOT_START + datetime.timedelta(minutes=1),
        )

    def test_email_only_row_sends_email_without_line_cfg(self):
        tid, slot_id = _seed()
        cid = _walkin_customer(tid)
        _book(tid, slot_id, cid)
        results = self._run()
        assert sorted(r.reason for r in results) == ["emailed", "emailed"]
        rows = _emails()
        assert len(rows) == 2
        assert all(r.category == "booking_reminder" for r in rows)
        assert all(r.recipient == "web@mail.tw" for r in rows)
        # portal 連結進提醒內文(book_slot 已補發 token;base url 見 conftest)
        db = _Session()
        try:
            rems = _reminders(tid)
            assert all(r.status == "sent" for r in rems)
        finally:
            db.close()

    def test_email_removed_after_enqueue_skipped(self):
        tid, slot_id = _seed()
        cid = _walkin_customer(tid)
        _book(tid, slot_id, cid)
        db = _Session()
        try:
            db.get(Customer, cid).email = None
            db.commit()
        finally:
            db.close()
        results = self._run()
        assert all(r.reason == "no_email" for r in results)
        assert _emails() == []


class TestBookingNotifyEmail:
    def test_cancel_notification_emailed(self):
        tid, slot_id = _seed()
        cid = _walkin_customer(tid)
        rid = _book(tid, slot_id, cid)
        db = _Session()
        try:
            resv = db.get(_r.Reservation, rid)
            slot = db.get(BookingSlot, slot_id)
            added = notify_svc.enqueue_cancel(
                db, reservation=resv, slot=slot,
                enabled=features_svc.is_enabled(
                    db, tid, features_svc.BOOKING_NOTIFY
                ),
            )
            db.commit()
            assert added == 1
            notif = db.execute(select(BookingNotification)).scalar_one()
            assert notif.line_user_id is None
        finally:
            db.close()
        results = send_due_notifications(
            session_factory=_Session,
            push_client=FakeLinePushClient(),
            apply=True,
            now=datetime.datetime.now(datetime.timezone.utc)
            + datetime.timedelta(minutes=5),
        )
        assert [r.reason for r in results] == ["emailed"]
        rows = _emails()
        assert len(rows) == 1
        assert rows[0].category == "booking_notify"
        assert "取消" in rows[0].subject

    def test_walkin_without_email_not_enqueued(self):
        tid, slot_id = _seed()
        cid = _walkin_customer(tid, email=None)
        rid = _book(tid, slot_id, cid)
        db = _Session()
        try:
            resv = db.get(_r.Reservation, rid)
            slot = db.get(BookingSlot, slot_id)
            added = notify_svc.enqueue_cancel(
                db, reservation=resv, slot=slot, enabled=True
            )
            assert added == 0
        finally:
            db.close()


class TestWebBookingConfirmation:
    def _seed_public(self):
        tid, slot_id = _seed()
        db = _Session()
        try:
            slug = f"shop-{uuid.uuid4().hex[:8]}"
            db.add(
                BusinessProfile(
                    tenant_id=tid, slug=slug, display_name="測試小店",
                    is_published=True, online_booking_enabled=True,
                )
            )
            svc = Service(
                tenant_id=tid, name="剪髮", duration_minutes=60, price_cents=80000
            )
            db.add(svc)
            db.commit()
            return tid, slug, svc.id, slot_id
        finally:
            db.close()

    def test_confirmation_email_queued_with_portal_link(self, client, monkeypatch):
        from saas_mvp.config import settings

        monkeypatch.setattr(settings, "public_base_url", "https://t.example")
        tid, slug, sid, slot_id = self._seed_public()
        r = client.post(
            f"/p/{slug}/book",
            data={
                "slot_id": slot_id, "party_size": 2, "service_id": sid,
                "name": "王小明", "phone": "0912345678",
                "email": "ming@example.com",
            },
        )
        assert r.status_code == 200 and "預約完成" in r.text
        rows = [e for e in _emails() if e.category == "booking_confirmation"]
        assert len(rows) == 1
        assert rows[0].recipient == "ming@example.com"
        assert "預約成立" in rows[0].subject
        assert "https://t.example/booking/my/" in rows[0].body

    def test_no_email_no_confirmation(self, client):
        tid, slug, sid, slot_id = self._seed_public()
        r = client.post(
            f"/p/{slug}/book",
            data={
                "slot_id": slot_id, "party_size": 1, "service_id": sid,
                "name": "無信箱客", "phone": "0955000222", "email": "",
            },
        )
        assert r.status_code == 200 and "預約完成" in r.text
        assert [e for e in _emails() if e.category == "booking_confirmation"] == []


class TestLineCustomerRegression:
    def test_line_reservation_reminders_keep_line_uid(self):
        tid, slot_id = _seed()
        db = _Session()
        try:
            resv = booking_svc.book_slot(
                db, tenant_id=tid, slot_id=slot_id,
                line_user_id="Uline9", display_name="LINE客",
            )
            assert resv.line_user_id == "Uline9"
        finally:
            db.close()
        rows = _reminders(tid)
        assert len(rows) == 2
        assert all(r.line_user_id == "Uline9" for r in rows)
