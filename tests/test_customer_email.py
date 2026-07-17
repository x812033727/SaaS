"""R5-B3 — 顧客 email(migration 0049)+ 提醒三段 fallback(LINE→SMS→email)。

覆蓋:
- valid_email 清洗/驗證矩陣
- booking_form 選填 email:合法寫入顧客檔、無效靜默忽略不擋預約
- portal 自助填寫:儲存/清除/格式錯誤
- 提醒 fallback:LINE 失敗+SMS 未送 → email 入列(booking_reminder);無 email 略過
- CSV 匯入含 email(round-trip)、無效 email 整批拒絕(all-or-nothing)
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
import saas_mvp.models.booking_form_token as _bft  # noqa: F401,E402
import saas_mvp.models.booking_waitlist as _wl  # noqa: F401,E402
import saas_mvp.models.email_delivery as _ed  # noqa: F401,E402
import saas_mvp.models.line_channel_config as _lcm  # noqa: F401,E402

from saas_mvp.app import create_app  # noqa: E402
from saas_mvp.config import settings  # noqa: E402
from saas_mvp.db import Base, get_db  # noqa: E402
from saas_mvp.line_client import FakeLinePushClient  # noqa: E402
from saas_mvp.models.booking_slot import BookingSlot  # noqa: E402
from saas_mvp.models.customer import Customer  # noqa: E402
from saas_mvp.models.email_delivery import EmailDelivery  # noqa: E402
from saas_mvp.models.line_channel_config import LineChannelConfig  # noqa: E402
from saas_mvp.models.tenant import Tenant  # noqa: E402
from saas_mvp.ops.send_due_reminders import send_due_reminders  # noqa: E402
from saas_mvp.services import booking as booking_svc  # noqa: E402
from saas_mvp.services import booking_form as bf_svc  # noqa: E402
from saas_mvp.services import customer_import  # noqa: E402
from saas_mvp.services import customer_portal as portal_svc  # noqa: E402
from saas_mvp.services.customer_portal import valid_email  # noqa: E402

_engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
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


def _seed(*, with_line=True, line_cfg=False):
    db = _Session()
    try:
        t = Tenant(name=f"em_{uuid.uuid4().hex[:8]}", plan="free")
        db.add(t)
        db.flush()
        if line_cfg:
            cfg = LineChannelConfig(tenant_id=t.id, default_target_lang="zh-TW")
            cfg.channel_secret = "s" * 32
            cfg.access_token = "a" * 40
            cfg.bot_mode = "booking"
            db.add(cfg)
        cust = Customer(
            tenant_id=t.id,
            display_name="信箱客",
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


class TestValidEmail:
    @pytest.mark.parametrize("raw,expected", [
        ("You@Example.COM", "you@example.com"),
        ("  a@b.co  ", "a@b.co"),
        ("", None),
        (None, None),
        ("not-an-email", None),
        ("two@@x.co", None),
        ("no-tld@host", None),
        ("a" * 250 + "@x.com", None),  # 超長
    ])
    def test_matrix(self, raw, expected):
        assert valid_email(raw) == expected


class TestBookingFormEmail:
    def _token(self, tid, line_uid):
        db = _Session()
        try:
            return bf_svc.issue_token(
                db, tenant_id=tid, line_user_id=line_uid
            ).token
        finally:
            db.close()

    def test_valid_email_saved(self, client):
        tid, cid, slot_id, line_uid = _seed()
        token = self._token(tid, line_uid)
        r = client.post(f"/booking/f/{token}", data={
            "slot_id": slot_id, "party_size": 1, "email": "Cust@Mail.tw",
        })
        assert r.status_code == 200 and "預約完成" in r.text
        db = _Session()
        try:
            assert db.get(Customer, cid).email == "cust@mail.tw"
        finally:
            db.close()

    def test_invalid_email_ignored_booking_succeeds(self, client):
        tid, cid, slot_id, line_uid = _seed()
        token = self._token(tid, line_uid)
        r = client.post(f"/booking/f/{token}", data={
            "slot_id": slot_id, "party_size": 1, "email": "oops",
        })
        assert r.status_code == 200 and "預約完成" in r.text
        db = _Session()
        try:
            assert db.get(Customer, cid).email is None
        finally:
            db.close()


class TestPortalEmail:
    def _portal_token(self, cid):
        db = _Session()
        try:
            return portal_svc.ensure_portal_token(db, db.get(Customer, cid))
        finally:
            db.close()

    def test_save_clear_invalid(self, client):
        tid, cid, slot_id, _ = _seed()
        token = self._portal_token(cid)
        r = client.post(
            f"/booking/my/{token}/email",
            data={"email": "Me@Mail.tw"},
            follow_redirects=False,
        )
        assert r.status_code == 303 and "email_saved" in r.headers["location"]
        db = _Session()
        try:
            assert db.get(Customer, cid).email == "me@mail.tw"
        finally:
            db.close()

        bad = client.post(
            f"/booking/my/{token}/email",
            data={"email": "nope"},
            follow_redirects=False,
        )
        assert "email_invalid" in bad.headers["location"]
        db = _Session()
        try:
            assert db.get(Customer, cid).email == "me@mail.tw"  # 原值不動
        finally:
            db.close()

        clear = client.post(
            f"/booking/my/{token}/email", data={"email": ""},
            follow_redirects=False,
        )
        assert "email_saved" in clear.headers["location"]
        db = _Session()
        try:
            assert db.get(Customer, cid).email is None
        finally:
            db.close()

    def test_page_shows_email_form(self, client):
        tid, cid, slot_id, _ = _seed()
        token = self._portal_token(cid)
        page = client.get(f"/booking/my/{token}")
        assert "提醒設定" in page.text


class TestEmailFallback:
    def _book_and_set_email(self, tid, line_uid, slot_id, email):
        db = _Session()
        try:
            resv = booking_svc.book_slot(
                db, tenant_id=tid, slot_id=slot_id, line_user_id=line_uid
            )
            cust = db.get(Customer, resv.customer_id)
            cust.email = email
            db.commit()
            return resv.id
        finally:
            db.close()

    def _run(self, monkeypatch, *, email):
        monkeypatch.setattr(settings, "sms_fallback_enabled", False)
        tid, cid, slot_id, line_uid = _seed(line_cfg=True)
        self._book_and_set_email(tid, line_uid, slot_id, email)
        results = send_due_reminders(
            session_factory=_Session,
            push_client=FakeLinePushClient(fail=True),
            apply=True,
            now=_SLOT_START + datetime.timedelta(minutes=1),
        )
        assert any(r.status == "failed" for r in results)
        db = _Session()
        try:
            return list(db.execute(select(EmailDelivery)).scalars())
        finally:
            db.close()

    def test_line_fail_sms_off_email_queued(self, monkeypatch):
        rows = self._run(monkeypatch, email="fallback@mail.tw")
        assert rows, "email fallback 應入列"
        assert all(r.category == "booking_reminder" for r in rows)
        assert all(r.recipient == "fallback@mail.tw" for r in rows)
        assert "預約提醒" in rows[0].body

    def test_no_email_no_queue(self, monkeypatch):
        rows = self._run(monkeypatch, email=None)
        assert rows == []


class TestCsvEmail:
    def test_import_and_roundtrip(self):
        tid, *_ = _seed(with_line=False)
        csv_bytes = (
            "display_name,phone,email\n"
            "新客,0911222333,New@Mail.tw\n"
        ).encode()
        db = _Session()
        try:
            report = customer_import.import_customers(
                db, tenant_id=tid, content=csv_bytes
            )
            assert report.errors == []
            row = (
                db.query(Customer)
                .filter(Customer.tenant_id == tid, Customer.phone == "0911222333")
                .one()
            )
            assert row.email == "new@mail.tw"
        finally:
            db.close()

    def test_invalid_email_rejects_batch(self):
        tid, *_ = _seed(with_line=False)
        csv_bytes = (
            "display_name,email\n"
            "好客,ok@mail.tw\n"
            "壞客,not-an-email\n"
        ).encode()
        db = _Session()
        try:
            report = customer_import.import_customers(
                db, tenant_id=tid, content=csv_bytes
            )
            assert any("email" in e for e in report.errors)
            # all-or-nothing:整批不寫
            assert (
                db.query(Customer)
                .filter(Customer.tenant_id == tid, Customer.email.is_not(None))
                .count()
            ) == 0
        finally:
            db.close()
