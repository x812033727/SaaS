"""行事曆同步測試 — build_ics 正確性、STATUS:CANCELLED、google url、token feed 隔離。"""

from __future__ import annotations

import datetime
import os
import uuid
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("SAAS_RATE_LIMIT_ENABLED", "false")

from saas_mvp.models import tenant as _t, user as _u  # noqa: F401,E402
from saas_mvp.models import customer as _c, booking_slot as _bs  # noqa: F401,E402
from saas_mvp.models import reservation as _r, reservation_reminder as _rr  # noqa: F401,E402
from saas_mvp.models import staff as _staff  # noqa: F401,E402

from saas_mvp.app import create_app  # noqa: E402
from saas_mvp.db import Base, get_db  # noqa: E402
from saas_mvp.models.booking_slot import BookingSlot  # noqa: E402
from saas_mvp.models.customer import Customer  # noqa: E402
from saas_mvp.models.reservation import (  # noqa: E402
    RESERVATION_CANCELLED,
    RESERVATION_CONFIRMED,
    Reservation,
)
from saas_mvp.models.staff import Staff  # noqa: E402
from saas_mvp.models.tenant import Tenant  # noqa: E402
from saas_mvp.services.calendar_ics import (  # noqa: E402
    build_ics,
    ensure_ics_token,
    google_calendar_url,
)

_engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
_Session = sessionmaker(autocommit=False, autoflush=False, bind=_engine)

_UTC = datetime.timezone.utc
_START = datetime.datetime(2030, 6, 1, 18, 0, tzinfo=_UTC)
_END = datetime.datetime(2030, 6, 1, 19, 0, tzinfo=_UTC)


@pytest.fixture(scope="module")
def client():
    Base.metadata.create_all(bind=_engine)
    app = create_app()

    def override_get_db():
        db = _Session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


def _uid() -> str:
    return uuid.uuid4().hex[:8]


# ── 純函式測試 ───────────────────────────────────────────────────────────────

class TestBuildICS:
    def test_valid_vcalendar(self):
        ics = build_ics([
            {
                "uid": "resv-1@x",
                "summary": "測試 預約",
                "start": _START,
                "end": _END,
                "status": "confirmed",
                "sequence": 3,
                "location": "台北店",
                "description": "人數：2 位",
            }
        ])
        assert ics.startswith("BEGIN:VCALENDAR\r\n")
        assert "VERSION:2.0" in ics
        assert "METHOD:PUBLISH" in ics
        assert "BEGIN:VEVENT" in ics and "END:VEVENT" in ics
        assert "UID:resv-1@x" in ics
        assert "DTSTART:20300601T180000Z" in ics
        assert "DTEND:20300601T190000Z" in ics
        assert "SEQUENCE:3" in ics
        assert "STATUS:CONFIRMED" in ics
        assert ics.rstrip().endswith("END:VCALENDAR")

    def test_cancelled_event(self):
        ics = build_ics([
            {
                "uid": "resv-2@x",
                "summary": "取消的預約",
                "start": _START,
                "end": _END,
                "status": "cancelled",
                "sequence": 5,
            }
        ])
        assert "METHOD:CANCEL" in ics
        assert "STATUS:CANCELLED" in ics

    def test_end_defaults_to_start(self):
        ics = build_ics([
            {"uid": "u@x", "summary": "s", "start": _START}
        ])
        assert "DTSTART:20300601T180000Z" in ics
        assert "DTEND:20300601T180000Z" in ics


class TestGoogleURL:
    def test_well_formed(self):
        url = google_calendar_url(
            title="預約 #5",
            start=_START,
            end=_END,
            details="人數 2",
            location="台北店",
        )
        parsed = urlparse(url)
        assert parsed.scheme == "https"
        assert parsed.netloc == "calendar.google.com"
        assert parsed.path == "/calendar/render"
        qs = parse_qs(parsed.query)
        assert qs["action"] == ["TEMPLATE"]
        assert qs["text"] == ["預約 #5"]
        assert qs["dates"] == ["20300601T180000Z/20300601T190000Z"]
        assert qs["details"] == ["人數 2"]
        assert qs["location"] == ["台北店"]


# ── token feed 測試 ──────────────────────────────────────────────────────────

def _seed(*, staff: bool = False, customer: bool = False):
    """建一租戶 + 一未來時段 + 一筆 confirmed 預約；回傳 dict of ids/tokens。"""
    db = _Session()
    try:
        t = Tenant(name=f"cal_{_uid()}", plan="free")
        db.add(t)
        db.flush()
        slot = BookingSlot(
            tenant_id=t.id, slot_start=_START, slot_end=_END, max_capacity=10
        )
        db.add(slot)
        db.flush()
        cust = None
        if customer:
            cust = Customer(tenant_id=t.id, line_user_id=f"U{_uid()}")
            db.add(cust)
            db.flush()
        stf = None
        if staff:
            stf = Staff(tenant_id=t.id, name="員工", access_token=f"stk_{_uid()}")
            db.add(stf)
            db.flush()
        resv = Reservation(
            tenant_id=t.id,
            slot_id=slot.id,
            customer_id=cust.id if cust else None,
            staff_id=stf.id if stf else None,
            party_size=2,
            status=RESERVATION_CONFIRMED,
        )
        db.add(resv)
        db.commit()
        out = {
            "tenant_id": t.id,
            "tenant_token": ensure_ics_token(db, t),
            "resv_id": resv.id,
        }
        if cust:
            db.refresh(cust)
            out["customer_token"] = ensure_ics_token(db, cust)
            out["customer_id"] = cust.id
        if stf:
            out["staff_token"] = stf.access_token
            out["staff_id"] = stf.id
        return out
    finally:
        db.close()


class TestShopFeed:
    def test_resolves(self, client):
        seed = _seed()
        r = client.get(f"/calendar/shop/{seed['tenant_token']}.ics")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/calendar")
        assert "BEGIN:VCALENDAR" in r.text
        assert f"resv-{seed['resv_id']}@saas-mvp" in r.text

    def test_unknown_token_404(self, client):
        assert client.get("/calendar/shop/nope.ics").status_code == 404

    def test_tenant_isolation(self, client):
        a = _seed()
        b = _seed()
        # A 的 feed 不應含 B 的預約 uid。
        r = client.get(f"/calendar/shop/{a['tenant_token']}.ics")
        assert f"resv-{b['resv_id']}@saas-mvp" not in r.text

    def test_cancelled_reservation_status(self, client):
        seed = _seed()
        db = _Session()
        try:
            resv = db.get(Reservation, seed["resv_id"])
            resv.status = RESERVATION_CANCELLED
            db.commit()
        finally:
            db.close()
        r = client.get(f"/calendar/shop/{seed['tenant_token']}.ics")
        assert "STATUS:CANCELLED" in r.text
        assert "METHOD:CANCEL" in r.text


class TestStaffFeed:
    def test_resolves(self, client):
        seed = _seed(staff=True)
        r = client.get(f"/calendar/staff/{seed['staff_token']}.ics")
        assert r.status_code == 200
        assert f"resv-{seed['resv_id']}@saas-mvp" in r.text

    def test_unknown_token_404(self, client):
        assert client.get("/calendar/staff/nope.ics").status_code == 404


class TestCustomerFeed:
    def test_resolves(self, client):
        seed = _seed(customer=True)
        r = client.get(f"/calendar/customer/{seed['customer_token']}.ics")
        assert r.status_code == 200
        assert f"resv-{seed['resv_id']}@saas-mvp" in r.text

    def test_unknown_token_404(self, client):
        assert client.get("/calendar/customer/nope.ics").status_code == 404
