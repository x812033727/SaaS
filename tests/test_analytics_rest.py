"""報表 REST 測試（summary / utilization / customers / CSV / attendance）。"""

from __future__ import annotations

import csv
import io
import os
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("SAAS_RATE_LIMIT_ENABLED", "false")

from saas_mvp.models import tenant as _t, user as _u  # noqa: F401,E402
from saas_mvp.models import customer as _c, booking_slot as _bs  # noqa: F401,E402
from saas_mvp.models import reservation as _r, reservation_reminder as _rr  # noqa: F401,E402
from saas_mvp.models import coupon as _cp, coupon_redemption as _cr, point_transaction as _pt  # noqa: F401,E402

from saas_mvp.app import create_app  # noqa: E402
from saas_mvp.db import Base, get_db  # noqa: E402

_engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
_Session = sessionmaker(autocommit=False, autoflush=False, bind=_engine)


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


def _register(client) -> str:
    r = client.post("/auth/register", json={
        "email": f"u_{uuid.uuid4().hex[:8]}@example.com",
        "password": "Test1234!",
        "tenant_name": f"t_{uuid.uuid4().hex[:8]}",
    })
    assert r.status_code == 201, r.text
    return r.json()["access_token"]


def _auth(t):
    return {"Authorization": f"Bearer {t}"}


def _seed(client, token):
    slot = client.post("/booking/slots/", headers=_auth(token), json={
        "slot_start": "2030-06-01T18:00:00+00:00", "max_capacity": 10,
    }).json()
    r = client.post("/booking/reservations/", headers=_auth(token), json={
        "slot_id": slot["id"], "party_size": 2, "line_user_id": "U1",
    }).json()
    return slot["id"], r["id"]


class TestAnalyticsRest:
    def test_summary(self, client):
        token = _register(client)
        _seed(client, token)
        r = client.get("/booking/analytics/summary", headers=_auth(token))
        assert r.status_code == 200
        body = r.json()
        assert body["total"] == 1 and body["confirmed"] == 1
        assert body["no_show_rate"] is None

    def test_attendance_then_summary(self, client):
        token = _register(client)
        _, rid = _seed(client, token)
        a = client.post(f"/booking/reservations/{rid}/attendance", headers=_auth(token),
                        json={"attended": True})
        assert a.status_code == 200 and a.json()["attended"] is True
        body = client.get("/booking/analytics/summary", headers=_auth(token)).json()
        assert body["attended"] == 1 and body["no_show_rate"] == 0.0

    def test_utilization_and_customers(self, client):
        token = _register(client)
        _seed(client, token)
        u = client.get("/booking/analytics/utilization", headers=_auth(token))
        assert u.status_code == 200 and any(row["hour"] == 18 for row in u.json())
        c = client.get("/booking/analytics/customers", headers=_auth(token))
        assert c.status_code == 200 and len(c.json()) >= 1

    def test_export_csv(self, client):
        token = _register(client)
        _seed(client, token)
        r = client.get("/booking/analytics/export.csv", headers=_auth(token))
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/csv")
        assert "attachment" in r.headers.get("content-disposition", "")
        rows = list(csv.DictReader(io.StringIO(r.text)))
        assert rows and rows[0]["party_size"] == "2"

    def test_unauth_401(self, client):
        assert client.get("/booking/analytics/summary").status_code == 401

    def test_attendance_cross_tenant_404(self, client):
        token_a = _register(client)
        _, rid = _seed(client, token_a)
        token_b = _register(client)
        r = client.post(f"/booking/reservations/{rid}/attendance", headers=_auth(token_b),
                        json={"attended": True})
        assert r.status_code == 404
