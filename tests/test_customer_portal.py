"""顧客自助入口網(R5-B1「我的預約」)測試。

覆蓋:
- token 簽發(惰性/冪等)、輪替(舊連結 404)、解析失敗 404
- 入口頁:未來/歷史分區、跨顧客隔離
- 取消:容量回補、冪等、他人預約拒絕
- 確認出席:冪等、無身分拒絕(service 層)
- 改期:兩步流程、容量、他人預約拒絕
- 候補:LINE 綁定顧客可見+可取消;無 LINE 顧客區塊不出現
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
import saas_mvp.models.booking_waitlist as _wl  # noqa: F401,E402
import saas_mvp.models.line_channel_config as _lcm  # noqa: F401,E402

from saas_mvp.app import create_app  # noqa: E402
from saas_mvp.db import Base, get_db  # noqa: E402
from saas_mvp.models.booking_slot import BookingSlot  # noqa: E402
from saas_mvp.models.booking_waitlist import WaitlistEntry  # noqa: E402
from saas_mvp.models.customer import Customer  # noqa: E402
from saas_mvp.models.reservation import Reservation  # noqa: E402
from saas_mvp.models.service import Service  # noqa: E402
from saas_mvp.models.tenant import Tenant  # noqa: E402
from saas_mvp.services import booking as booking_svc  # noqa: E402
from saas_mvp.services import customer_portal as portal_svc  # noqa: E402
from saas_mvp.services import waitlist as waitlist_svc  # noqa: E402

_engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
_Session = sessionmaker(autocommit=False, autoflush=False, bind=_engine)

_SLOT_START = datetime.datetime(2030, 6, 1, 18, 0, tzinfo=datetime.timezone.utc)


@pytest.fixture()
def client():
    Base.metadata.drop_all(bind=_engine)
    Base.metadata.create_all(bind=_engine)
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


def _seed(*, with_line=True, capacity=4):
    """回傳 (tenant_id, customer_id, service_id, slot_id, slot2_id)。"""
    db = _Session()
    try:
        t = Tenant(name=f"cp_{uuid.uuid4().hex[:8]}", plan="free")
        db.add(t)
        db.flush()
        cust = Customer(
            tenant_id=t.id,
            display_name="小美",
            line_user_id=f"U{uuid.uuid4().hex[:12]}" if with_line else None,
            phone="0912345678",
        )
        svc = Service(
            tenant_id=t.id, name="剪髮", duration_minutes=60, price_cents=80000
        )
        db.add_all([cust, svc])
        db.flush()
        slot = BookingSlot(
            tenant_id=t.id,
            slot_start=_SLOT_START,
            slot_end=_SLOT_START + datetime.timedelta(hours=1),
            max_capacity=capacity,
        )
        slot2 = BookingSlot(
            tenant_id=t.id,
            slot_start=_SLOT_START + datetime.timedelta(days=1),
            slot_end=_SLOT_START + datetime.timedelta(days=1, hours=1),
            max_capacity=capacity,
        )
        db.add_all([slot, slot2])
        db.flush()
        ids = (t.id, cust.id, svc.id, slot.id, slot2.id)
        db.commit()
        return ids
    finally:
        db.close()


def _book(tenant_id, customer_id, slot_id, *, service_id=None, party=1) -> int:
    db = _Session()
    try:
        resv = booking_svc.book_slot(
            db,
            tenant_id=tenant_id,
            slot_id=slot_id,
            customer_id=customer_id,
            service_id=service_id,
            party_size=party,
        )
        return resv.id
    finally:
        db.close()


def _token(customer_id) -> str:
    db = _Session()
    try:
        cust = db.get(Customer, customer_id)
        return portal_svc.ensure_portal_token(db, cust)
    finally:
        db.close()


# ── token 生命週期 ────────────────────────────────────────────────────────────


class TestPortalToken:
    def test_ensure_idempotent_and_rotate_invalidates(self, client):
        tid, cid, sid, slot_id, _ = _seed()
        token = _token(cid)
        assert _token(cid) == token  # 冪等:不重產

        assert client.get(f"/booking/my/{token}").status_code == 200

        db = _Session()
        try:
            cust = db.get(Customer, cid)
            new_token = portal_svc.rotate_portal_token(db, cust)
        finally:
            db.close()
        assert new_token != token
        assert client.get(f"/booking/my/{token}").status_code == 404
        assert client.get(f"/booking/my/{new_token}").status_code == 200

    def test_unknown_token_404(self, client):
        assert client.get("/booking/my/no-such-token").status_code == 404
        assert client.get(f"/booking/my/{'x' * 65}").status_code == 404

    def test_portal_url_requires_base_and_token(self, client):
        from saas_mvp.config import settings

        tid, cid, *_ = _seed()
        db = _Session()
        try:
            cust = db.get(Customer, cid)
            old = settings.public_base_url
            try:
                settings.public_base_url = ""
                assert portal_svc.portal_url(cust) is None
                settings.public_base_url = "https://x.example"
                assert portal_svc.portal_url(cust) is None  # 尚無 token 不簽發
                portal_svc.ensure_portal_token(db, cust)
                url = portal_svc.portal_url(cust)
                assert url.startswith("https://x.example/booking/my/")
            finally:
                settings.public_base_url = old
        finally:
            db.close()


# ── 入口頁 ────────────────────────────────────────────────────────────────────


class TestPortalPage:
    def test_upcoming_and_history_sections(self, client):
        tid, cid, sid, slot_id, slot2_id = _seed()
        _book(tid, cid, slot_id, service_id=sid)
        rid2 = _book(tid, cid, slot2_id)
        db = _Session()
        try:
            booking_svc.cancel_reservation(
                db, tenant_id=tid, reservation_id=rid2
            )
        finally:
            db.close()
        token = _token(cid)
        page = client.get(f"/booking/my/{token}")
        assert page.status_code == 200
        assert "我的預約" in page.text
        assert "剪髮" in page.text          # upcoming 帶服務名
        assert "已取消" in page.text        # history 帶取消標記

    def test_cross_customer_isolation(self, client):
        tid, cid, sid, slot_id, _ = _seed()
        tid2, cid2, sid2, slot_id2, _ = _seed()
        _book(tid, cid, slot_id, service_id=sid)
        token2 = _token(cid2)
        page = client.get(f"/booking/my/{token2}")
        assert "剪髮" not in page.text
        assert "目前沒有即將到來的預約" in page.text


# ── 取消 / 確認 ───────────────────────────────────────────────────────────────


class TestPortalActions:
    def test_cancel_releases_capacity_and_idempotent(self, client):
        tid, cid, sid, slot_id, _ = _seed()
        rid = _book(tid, cid, slot_id, party=2)
        token = _token(cid)
        r = client.post(
            f"/booking/my/{token}/reservations/{rid}/cancel",
            follow_redirects=False,
        )
        assert r.status_code == 303
        db = _Session()
        try:
            assert db.get(Reservation, rid).status == "cancelled"
            assert db.get(BookingSlot, slot_id).booked_count == 0
        finally:
            db.close()
        # 冪等:再取消仍 303、容量不再回補
        client.post(f"/booking/my/{token}/reservations/{rid}/cancel")
        db = _Session()
        try:
            assert db.get(BookingSlot, slot_id).booked_count == 0
        finally:
            db.close()

    def test_cannot_touch_other_customers_reservation(self, client):
        tid, cid, sid, slot_id, _ = _seed()
        _tid2, cid2, _sid2, _s, _s2 = _seed()
        rid = _book(tid, cid, slot_id)
        token2 = _token(cid2)
        r = client.post(
            f"/booking/my/{token2}/reservations/{rid}/cancel",
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert "msg=error" in r.headers["location"]
        db = _Session()
        try:
            assert db.get(Reservation, rid).status == "confirmed"
        finally:
            db.close()

    def test_confirm_idempotent(self, client):
        tid, cid, sid, slot_id, _ = _seed()
        rid = _book(tid, cid, slot_id)
        token = _token(cid)
        r = client.post(
            f"/booking/my/{token}/reservations/{rid}/confirm",
            follow_redirects=False,
        )
        assert r.status_code == 303 and "msg=confirmed" in r.headers["location"]
        db = _Session()
        try:
            first = db.get(Reservation, rid).customer_confirmed_at
            assert first is not None
        finally:
            db.close()
        client.post(f"/booking/my/{token}/reservations/{rid}/confirm")
        db = _Session()
        try:
            assert db.get(Reservation, rid).customer_confirmed_at == first
        finally:
            db.close()

    def test_confirm_requires_identity_at_service_layer(self, client):
        tid, cid, sid, slot_id, _ = _seed()
        rid = _book(tid, cid, slot_id)
        db = _Session()
        try:
            with pytest.raises(booking_svc.ReservationPermissionError):
                booking_svc.confirm_reservation(
                    db, tenant_id=tid, reservation_id=rid
                )
        finally:
            db.close()


# ── 改期 ─────────────────────────────────────────────────────────────────────


class TestPortalReschedule:
    def test_two_step_flow_and_capacity_moves(self, client):
        tid, cid, sid, slot_id, slot2_id = _seed()
        rid = _book(tid, cid, slot_id, service_id=sid, party=2)
        token = _token(cid)

        step1 = client.get(
            f"/booking/my/{token}/reservations/{rid}/reschedule"
        )
        assert step1.status_code == 200
        assert "選擇新日期" in step1.text

        date2 = (_SLOT_START + datetime.timedelta(days=1)).date().isoformat()
        step2 = client.get(
            f"/booking/my/{token}/reservations/{rid}/reschedule?date={date2}"
        )
        assert step2.status_code == 200
        assert "選擇時段" in step2.text

        r = client.post(
            f"/booking/my/{token}/reservations/{rid}/reschedule",
            data={"slot_id": slot2_id},
            follow_redirects=False,
        )
        assert r.status_code == 303 and "msg=rescheduled" in r.headers["location"]
        db = _Session()
        try:
            assert db.get(Reservation, rid).slot_id == slot2_id
            assert db.get(BookingSlot, slot_id).booked_count == 0
            assert db.get(BookingSlot, slot2_id).booked_count == 2
        finally:
            db.close()

    def test_reschedule_to_full_slot_shows_error(self, client):
        tid, cid, sid, slot_id, slot2_id = _seed(capacity=1)
        rid = _book(tid, cid, slot_id)
        # 另一位顧客占滿 slot2
        db = _Session()
        try:
            other = Customer(tenant_id=tid, display_name="佔位")
            db.add(other)
            db.flush()
            booking_svc.book_slot(
                db, tenant_id=tid, slot_id=slot2_id, customer_id=other.id
            )
        finally:
            db.close()
        token = _token(cid)
        r = client.post(
            f"/booking/my/{token}/reservations/{rid}/reschedule",
            data={"slot_id": slot2_id},
            follow_redirects=False,
        )
        assert r.status_code == 303 and "msg=slot_full" in r.headers["location"]

    def test_reschedule_other_customers_reservation_denied(self, client):
        tid, cid, sid, slot_id, slot2_id = _seed()
        _t2, cid2, *_ = _seed()
        rid = _book(tid, cid, slot_id)
        token2 = _token(cid2)
        page = client.get(
            f"/booking/my/{token2}/reservations/{rid}/reschedule",
            follow_redirects=False,
        )
        assert page.status_code == 303 and "msg=error" in page.headers["location"]


# ── 候補 ─────────────────────────────────────────────────────────────────────


class TestPortalWaitlist:
    def _fill_and_join(self, tid, cid, slot_id, line_user_id):
        db = _Session()
        try:
            other = Customer(tenant_id=tid, display_name="佔位")
            db.add(other)
            db.flush()
            booking_svc.book_slot(
                db, tenant_id=tid, slot_id=slot_id, customer_id=other.id
            )
            entry = waitlist_svc.join_waitlist(
                db,
                tenant_id=tid,
                slot_id=slot_id,
                line_user_id=line_user_id,
                display_name="小美",
            )
            return entry.id
        finally:
            db.close()

    def test_line_customer_sees_and_cancels_waitlist(self, client):
        tid, cid, sid, slot_id, _ = _seed(capacity=1)
        db = _Session()
        try:
            line_uid = db.get(Customer, cid).line_user_id
        finally:
            db.close()
        wid = self._fill_and_join(tid, cid, slot_id, line_uid)
        token = _token(cid)
        page = client.get(f"/booking/my/{token}")
        assert "候補中" in page.text
        r = client.post(
            f"/booking/my/{token}/waitlist/{wid}/cancel", follow_redirects=False
        )
        assert r.status_code == 303 and "msg=waitlist_cancelled" in r.headers["location"]
        db = _Session()
        try:
            assert db.get(WaitlistEntry, wid).status == "cancelled"
        finally:
            db.close()

    def test_customer_without_line_has_no_waitlist_section(self, client):
        tid, cid, sid, slot_id, _ = _seed(with_line=False)
        _book(tid, cid, slot_id)
        token = _token(cid)
        page = client.get(f"/booking/my/{token}")
        assert page.status_code == 200
        assert "候補中" not in page.text
