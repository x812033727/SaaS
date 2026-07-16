"""R3-C1 — console JSON API(/api/v1 reservations/calendar/dashboard)契約測試。"""

from __future__ import annotations

import datetime
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from saas_mvp.app import create_app
from saas_mvp.db import Base, get_db
from saas_mvp.models.booking_slot import BookingSlot
from saas_mvp.models.booking_waitlist import WaitlistEntry
from saas_mvp.models.customer import Customer
from saas_mvp.models.reservation import Reservation
from saas_mvp.models.service import Service
from saas_mvp.models.staff import Staff
from saas_mvp.models.user import User

_DAY1 = datetime.datetime(2031, 3, 10, 10, 0)
_DAY2 = datetime.datetime(2031, 3, 12, 14, 0)


@pytest.fixture()
def v1_client():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    Base.metadata.create_all(engine)
    app = create_app()

    def override_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_db
    with TestClient(app) as client:
        yield client, session_factory


def _register(client: TestClient, prefix: str = "con") -> tuple[int, dict[str, str]]:
    unique = uuid.uuid4().hex[:8]
    r = client.post(
        "/auth/register",
        json={
            "email": f"{prefix}-{unique}@example.com",
            "password": "safe-password-123",
            "tenant_name": f"{prefix}-{unique}",
        },
    )
    assert r.status_code == 201, r.text
    token = r.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    ctx = client.get("/api/v1/context", headers=headers).json()
    return ctx["tenant"]["id"], headers


def _seed(session_factory, tenant_id: int) -> dict:
    """兩天各一筆預約:day1 含顧客/員工/服務 + 已過時段未點名;day2 純預約。
    另加 1 筆 waiting 候補與 day1 預約的 pending 定金。"""
    db = session_factory()
    try:
        cust = Customer(tenant_id=tenant_id, line_user_id="Uc1",
                        display_name="王小美", phone="0912345678")
        staff = Staff(tenant_id=tenant_id, name="Amy")
        svc = Service(tenant_id=tenant_id, name="剪髮", duration_minutes=60,
                      price_cents=80000)
        db.add_all([cust, staff, svc])
        db.flush()
        s1 = BookingSlot(tenant_id=tenant_id, slot_start=_DAY1, max_capacity=4)
        s2 = BookingSlot(tenant_id=tenant_id, slot_start=_DAY2, max_capacity=4)
        db.add_all([s1, s2])
        db.flush()
        r1 = Reservation(
            tenant_id=tenant_id, slot_id=s1.id, party_size=2, status="confirmed",
            line_user_id="Uc1", customer_id=cust.id, staff_id=staff.id,
            service_id=svc.id, deposit_status="pending", deposit_cents=20000,
        )
        r2 = Reservation(
            tenant_id=tenant_id, slot_id=s2.id, party_size=1, status="cancelled",
            line_user_id="Uc2",
        )
        wl = WaitlistEntry(tenant_id=tenant_id, slot_id=s1.id,
                           line_user_id="Uw1", party_size=1)
        db.add_all([r1, r2, wl])
        db.commit()
        return {"customer_id": cust.id, "r1": r1.id, "r2": r2.id}
    finally:
        db.close()


class TestReservationsEnriched:
    def test_requires_auth(self, v1_client):
        client, _ = v1_client
        assert client.get("/api/v1/reservations").status_code == 401

    def test_enriched_fields_and_total_count(self, v1_client):
        client, sf = v1_client
        tid, headers = _register(client)
        seeded = _seed(sf, tid)
        r = client.get("/api/v1/reservations", headers=headers)
        assert r.status_code == 200, r.text
        assert r.headers["X-Total-Count"] == "2"
        rows = r.json()
        assert len(rows) == 2
        # slot_start desc:day2 在前
        assert rows[0]["id"] == seeded["r2"]
        enriched = rows[1]
        assert enriched["customer_name"] == "王小美"
        assert enriched["customer_phone"] == "0912345678"
        assert enriched["staff_name"] == "Amy"
        assert enriched["service_name"] == "剪髮"
        assert enriched["slot_start"].startswith("2031-03-10T10:00")

    def test_date_range_filter(self, v1_client):
        client, sf = v1_client
        tid, headers = _register(client)
        seeded = _seed(sf, tid)
        # from 含、to 不含
        r = client.get(
            "/api/v1/reservations?date_from=2031-03-10&date_to=2031-03-11",
            headers=headers,
        )
        assert [row["id"] for row in r.json()] == [seeded["r1"]]
        assert r.headers["X-Total-Count"] == "1"
        r2 = client.get(
            "/api/v1/reservations?date_from=2031-03-11", headers=headers
        )
        assert [row["id"] for row in r2.json()] == [seeded["r2"]]

    def test_status_filter(self, v1_client):
        client, sf = v1_client
        tid, headers = _register(client)
        seeded = _seed(sf, tid)
        r = client.get("/api/v1/reservations?status=cancelled", headers=headers)
        assert [row["id"] for row in r.json()] == [seeded["r2"]]

    def test_bad_date_422(self, v1_client):
        client, _ = v1_client
        _, headers = _register(client)
        assert client.get(
            "/api/v1/reservations?date_from=03/10", headers=headers
        ).status_code == 422

    def test_tenant_isolation(self, v1_client):
        client, sf = v1_client
        tid_a, headers_a = _register(client, "ta")
        _tid_b, headers_b = _register(client, "tb")
        _seed(sf, tid_a)
        assert client.get(
            "/api/v1/reservations", headers=headers_b
        ).json() == []
        assert len(client.get("/api/v1/reservations", headers=headers_a).json()) == 2


class TestCustomerReservations:
    def test_history_and_cross_tenant_404(self, v1_client):
        client, sf = v1_client
        tid_a, headers_a = _register(client, "ca")
        _tid_b, headers_b = _register(client, "cb")
        seeded = _seed(sf, tid_a)
        cid = seeded["customer_id"]
        r = client.get(f"/api/v1/customers/{cid}/reservations", headers=headers_a)
        assert r.status_code == 200
        assert [row["id"] for row in r.json()] == [seeded["r1"]]
        # 他租戶看不到(404 而非空列,防枚舉)
        assert client.get(
            f"/api/v1/customers/{cid}/reservations", headers=headers_b
        ).status_code == 404


class TestCalendar:
    def test_month_and_week_shapes(self, v1_client):
        client, sf = v1_client
        tid, headers = _register(client)
        _seed(sf, tid)
        m = client.get("/api/v1/calendar/month?year=2031&month=3", headers=headers)
        assert m.status_code == 200, m.text
        body = m.json()
        assert "weeks" in body
        w = client.get("/api/v1/calendar/week?anchor=2031-03-10", headers=headers)
        assert w.status_code == 200
        assert len(w.json()["days"]) == 7

    def test_requires_auth(self, v1_client):
        client, _ = v1_client
        assert client.get("/api/v1/calendar/month?year=2031&month=3").status_code == 401


class TestDashboardToday:
    def test_aggregates(self, v1_client):
        client, sf = v1_client
        tid, headers = _register(client)
        _seed(sf, tid)
        r = client.get("/api/v1/dashboard/today?date=2031-03-10", headers=headers)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["date"] == "2031-03-10"
        assert len(body["reservations"]) == 1
        assert body["reservations"][0]["customer_name"] == "王小美"
        assert body["summary"]["total"] == 1
        assert body["pending"]["waitlist_waiting"] == 1
        assert body["pending"]["deposits_pending"] == 1
        # 2031 未過 → 未點名數 0;若 date 在過去則會計入
        assert body["pending"]["attendance_unmarked"] == 0

    def test_past_day_counts_unmarked_attendance(self, v1_client):
        client, sf = v1_client
        tid, headers = _register(client)
        db = sf()
        try:
            past = datetime.datetime(2020, 1, 6, 10, 0)
            slot = BookingSlot(tenant_id=tid, slot_start=past, max_capacity=2)
            db.add(slot)
            db.flush()
            db.add(Reservation(tenant_id=tid, slot_id=slot.id, party_size=1,
                               status="confirmed", line_user_id="Up1"))
            db.commit()
        finally:
            db.close()
        r = client.get("/api/v1/dashboard/today?date=2020-01-06", headers=headers)
        assert r.json()["pending"]["attendance_unmarked"] == 1


class TestCustomerSearch:
    def test_q_param_exposed(self, v1_client):
        client, sf = v1_client
        tid, headers = _register(client)
        _seed(sf, tid)
        r = client.get("/booking/customers/?q=小美", headers=headers)
        assert r.status_code == 200, r.text
        rows = r.json()
        assert len(rows) == 1 and rows[0]["display_name"] == "王小美"
        assert r.headers["X-Total-Count"] == "1"
        assert client.get("/booking/customers/?q=不存在", headers=headers).json() == []
