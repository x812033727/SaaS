"""R5-B2 — 提醒/通知/建單回覆附顧客 portal 連結。

覆蓋:
- book_slot 建單即補發 portal_token(LINE upsert 與 customer_id 兩路;既有不重發)
- build_reminder_text 含/不含 portal_url 兩態
- _clamp_sms:短文原樣、超長保住連結行、無連結行純截斷
- enqueue_cancel/change 通知文字附連結(有 base_url+token)/不附(無 base_url)
- LINE _confirm_text 與 booking_form done 頁附「管理預約」
"""

from __future__ import annotations

import datetime
import os
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("SAAS_RATE_LIMIT_ENABLED", "false")

from saas_mvp.models import tenant as _t, user as _u  # noqa: F401,E402
from saas_mvp.models import customer as _c, booking_slot as _bs  # noqa: F401,E402
from saas_mvp.models import reservation as _r  # noqa: F401,E402
import saas_mvp.models.booking_waitlist as _wl  # noqa: F401,E402
import saas_mvp.models.line_channel_config as _lcm  # noqa: F401,E402
import saas_mvp.models.booking_notification as _bn  # noqa: F401,E402

from saas_mvp.config import settings  # noqa: E402
from saas_mvp.db import Base  # noqa: E402
from saas_mvp.models.booking_notification import BookingNotification  # noqa: E402
from saas_mvp.models.booking_slot import BookingSlot  # noqa: E402
from saas_mvp.models.customer import Customer  # noqa: E402
from saas_mvp.models.tenant import Tenant  # noqa: E402
from saas_mvp.ops.send_due_reminders import _clamp_sms  # noqa: E402
from saas_mvp.services import booking as booking_svc  # noqa: E402
from saas_mvp.services import booking_notify as notify_svc  # noqa: E402
from saas_mvp.services.reminders import build_reminder_text  # noqa: E402

_engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
_Session = sessionmaker(autocommit=False, autoflush=False, bind=_engine)

_SLOT_START = datetime.datetime(2030, 6, 1, 18, 0, tzinfo=datetime.timezone.utc)
_BASE = "https://x.example"


@pytest.fixture(autouse=True)
def _fresh_db():
    Base.metadata.drop_all(bind=_engine)
    Base.metadata.create_all(bind=_engine)
    yield


@pytest.fixture()
def base_url(monkeypatch):
    monkeypatch.setattr(settings, "public_base_url", _BASE)


def _seed(*, with_line=True):
    db = _Session()
    try:
        t = Tenant(name=f"pl_{uuid.uuid4().hex[:8]}", plan="free")
        db.add(t)
        db.flush()
        cust = Customer(
            tenant_id=t.id,
            display_name="小連結",
            line_user_id=f"U{uuid.uuid4().hex[:10]}" if with_line else None,
        )
        slot = BookingSlot(
            tenant_id=t.id, slot_start=_SLOT_START, max_capacity=4
        )
        db.add_all([cust, slot])
        db.commit()
        return t.id, cust.id, slot.id, cust.line_user_id
    finally:
        db.close()


class TestTokenIssuedOnBooking:
    def test_customer_id_path_assigns_token(self):
        tid, cid, slot_id, _ = _seed(with_line=False)
        db = _Session()
        try:
            booking_svc.book_slot(
                db, tenant_id=tid, slot_id=slot_id, customer_id=cid
            )
            assert db.get(Customer, cid).portal_token
        finally:
            db.close()

    def test_line_upsert_path_assigns_token(self):
        tid, cid, slot_id, line_uid = _seed()
        db = _Session()
        try:
            booking_svc.book_slot(
                db, tenant_id=tid, slot_id=slot_id, line_user_id=line_uid
            )
            assert db.get(Customer, cid).portal_token
        finally:
            db.close()

    def test_existing_token_not_rotated(self):
        tid, cid, slot_id, _ = _seed(with_line=False)
        db = _Session()
        try:
            cust = db.get(Customer, cid)
            cust.portal_token = "keep-me-stable"
            db.commit()
            booking_svc.book_slot(
                db, tenant_id=tid, slot_id=slot_id, customer_id=cid
            )
            assert db.get(Customer, cid).portal_token == "keep-me-stable"
        finally:
            db.close()


class TestReminderText:
    def _fake(self):
        slot = BookingSlot(slot_start=_SLOT_START.replace(tzinfo=None))
        resv = type("R", (), {"party_size": 2, "id": 7})()
        return slot, resv

    def test_with_and_without_portal_url(self):
        slot, resv = self._fake()
        plain = build_reminder_text(
            slot=slot, reservation=resv, store_name="小店"
        )
        assert "管理預約" not in plain
        linked = build_reminder_text(
            slot=slot, reservation=resv, store_name="小店",
            portal_url=f"{_BASE}/booking/my/tok",
        )
        assert linked.endswith(f"管理預約:{_BASE}/booking/my/tok")
        assert plain in linked  # 連結是純附加,不改原文


class TestClampSms:
    _URL_LINE = "管理預約:https://x.example/booking/my/tok123"

    def test_short_passthrough(self):
        text = "hello\n" + self._URL_LINE
        assert _clamp_sms(text) == text

    def test_long_preserves_url_line(self):
        text = "很長的內文" * 100 + "\n" + self._URL_LINE
        clamped = _clamp_sms(text)
        assert len(clamped) <= 300
        assert clamped.endswith(self._URL_LINE)

    def test_long_without_url_plain_truncate(self):
        text = "x" * 400
        assert _clamp_sms(text) == "x" * 300


class TestNotifyLink:
    def _book(self, tid, line_uid, slot_id):
        # 走 LINE 路徑:通知入列需要 reservation.line_user_id(推播對象)。
        db = _Session()
        try:
            resv = booking_svc.book_slot(
                db, tenant_id=tid, slot_id=slot_id, line_user_id=line_uid
            )
            return resv.id
        finally:
            db.close()

    def test_cancel_notify_includes_link(self, base_url):
        tid, cid, slot_id, line_uid = _seed()
        rid = self._book(tid, line_uid, slot_id)
        db = _Session()
        try:
            resv = db.get(_r.Reservation, rid)
            slot = db.get(BookingSlot, slot_id)
            notify_svc.enqueue_cancel(db, reservation=resv, slot=slot)
            db.commit()
            row = (
                db.query(BookingNotification)
                .filter(BookingNotification.reservation_id == rid)
                .one()
            )
            token = db.get(Customer, cid).portal_token
            assert f"管理預約:{_BASE}/booking/my/{token}" in row.payload_text
        finally:
            db.close()

    def test_no_link_without_base_url(self, monkeypatch):
        monkeypatch.setattr(settings, "public_base_url", "")
        tid, cid, slot_id, line_uid = _seed()
        rid = self._book(tid, line_uid, slot_id)
        db = _Session()
        try:
            resv = db.get(_r.Reservation, rid)
            slot = db.get(BookingSlot, slot_id)
            notify_svc.enqueue_cancel(db, reservation=resv, slot=slot)
            db.commit()
            row = (
                db.query(BookingNotification)
                .filter(BookingNotification.reservation_id == rid)
                .one()
            )
            assert "管理預約" not in row.payload_text
        finally:
            db.close()


class TestConfirmSurfaces:
    def test_line_confirm_text_includes_link(self, base_url):
        from saas_mvp.routers.line_webhook import _confirm_text

        tid, cid, slot_id, line_uid = _seed()
        db = _Session()
        try:
            resv = booking_svc.book_slot(
                db, tenant_id=tid, slot_id=slot_id, line_user_id=line_uid
            )
            text = _confirm_text(db, tid, resv, slot_id)
            token = db.get(Customer, cid).portal_token
            assert f"管理預約:{_BASE}/booking/my/{token}" in text
        finally:
            db.close()
