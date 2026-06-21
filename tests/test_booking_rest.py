"""預約 REST API 測試（slots / reservations / customers）。

涵蓋：CRUD happy path、跨租戶 404（+ 同租戶控制組）、未認證 401、
容量下修低於已訂量 409。
"""

from __future__ import annotations

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


def _uid() -> str:
    return uuid.uuid4().hex[:8]


def _register(client) -> str:
    r = client.post("/auth/register", json={
        "email": f"u_{_uid()}@example.com",
        "password": "Test1234!",
        "tenant_name": f"t_{_uid()}",
    })
    assert r.status_code == 201, r.text
    return r.json()["access_token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


_FUTURE = "2030-06-01T18:00:00+00:00"


def _make_slot(client, token, *, max_capacity=4, walkin_reserved=0, start=_FUTURE):
    r = client.post(
        "/booking/slots/",
        headers=_auth(token),
        json={
            "slot_start": start,
            "max_capacity": max_capacity,
            "walkin_reserved": walkin_reserved,
        },
    )
    assert r.status_code == 201, r.text
    return r.json()


class TestSlots:
    def test_create_and_get(self, client):
        token = _register(client)
        slot = _make_slot(client, token, max_capacity=4, walkin_reserved=1)
        assert slot["online_available"] == 3
        got = client.get(f"/booking/slots/{slot['id']}", headers=_auth(token))
        assert got.status_code == 200
        assert got.json()["max_capacity"] == 4

    def test_list(self, client):
        token = _register(client)
        _make_slot(client, token, start="2030-07-01T12:00:00+00:00")
        r = client.get("/booking/slots/", headers=_auth(token))
        assert r.status_code == 200
        assert len(r.json()) >= 1

    def test_update_capacity(self, client):
        token = _register(client)
        slot = _make_slot(client, token, max_capacity=4)
        r = client.put(
            f"/booking/slots/{slot['id']}",
            headers=_auth(token),
            json={"max_capacity": 6},
        )
        assert r.status_code == 200
        assert r.json()["max_capacity"] == 6

    def test_shrink_below_booked_409(self, client):
        token = _register(client)
        slot = _make_slot(client, token, max_capacity=4)
        # 訂 3 位
        client.post(
            "/booking/reservations/",
            headers=_auth(token),
            json={"slot_id": slot["id"], "party_size": 3},
        )
        # 下修到 2 < 3 → 409
        r = client.put(
            f"/booking/slots/{slot['id']}",
            headers=_auth(token),
            json={"max_capacity": 2},
        )
        assert r.status_code == 409, r.text
        # 控制組：下修到 3 == 已訂量，允許
        ok = client.put(
            f"/booking/slots/{slot['id']}",
            headers=_auth(token),
            json={"max_capacity": 3},
        )
        assert ok.status_code == 200

    def test_delete_soft(self, client):
        token = _register(client)
        slot = _make_slot(client, token)
        r = client.delete(f"/booking/slots/{slot['id']}", headers=_auth(token))
        assert r.status_code == 204
        got = client.get(f"/booking/slots/{slot['id']}", headers=_auth(token))
        assert got.json()["is_active"] is False

    def test_unauth_401(self, client):
        r = client.get("/booking/slots/")
        assert r.status_code == 401

    def test_cross_tenant_404(self, client):
        token_a = _register(client)
        slot = _make_slot(client, token_a)
        token_b = _register(client)
        # B 讀 A 的 slot → 404
        r = client.get(f"/booking/slots/{slot['id']}", headers=_auth(token_b))
        assert r.status_code == 404
        # 控制組：A 自己讀得到
        assert client.get(f"/booking/slots/{slot['id']}", headers=_auth(token_a)).status_code == 200


class TestReservations:
    def test_create_list_cancel(self, client):
        token = _register(client)
        slot = _make_slot(client, token, max_capacity=4)
        r = client.post(
            "/booking/reservations/",
            headers=_auth(token),
            json={"slot_id": slot["id"], "party_size": 2, "line_user_id": "Ux"},
        )
        assert r.status_code == 201, r.text
        rid = r.json()["id"]
        # list
        lst = client.get("/booking/reservations/", headers=_auth(token))
        assert lst.status_code == 200 and len(lst.json()) == 1
        # cancel
        c = client.post(f"/booking/reservations/{rid}/cancel", headers=_auth(token))
        assert c.status_code == 200 and c.json()["status"] == "cancelled"

    def test_book_full_slot_409(self, client):
        token = _register(client)
        slot = _make_slot(client, token, max_capacity=1)
        client.post(
            "/booking/reservations/",
            headers=_auth(token),
            json={"slot_id": slot["id"], "party_size": 1},
        )
        r = client.post(
            "/booking/reservations/",
            headers=_auth(token),
            json={"slot_id": slot["id"], "party_size": 1},
        )
        assert r.status_code == 409

    def test_book_missing_slot_404(self, client):
        token = _register(client)
        r = client.post(
            "/booking/reservations/",
            headers=_auth(token),
            json={"slot_id": 999999, "party_size": 1},
        )
        assert r.status_code == 404

    def test_cross_tenant_reservation_404(self, client):
        token_a = _register(client)
        slot = _make_slot(client, token_a, max_capacity=4)
        ra = client.post(
            "/booking/reservations/",
            headers=_auth(token_a),
            json={"slot_id": slot["id"], "party_size": 1},
        )
        rid = ra.json()["id"]
        token_b = _register(client)
        r = client.get(f"/booking/reservations/{rid}", headers=_auth(token_b))
        assert r.status_code == 404


class TestCustomers:
    def test_customer_listed_and_patch(self, client):
        token = _register(client)
        slot = _make_slot(client, token, max_capacity=4)
        client.post(
            "/booking/reservations/",
            headers=_auth(token),
            json={"slot_id": slot["id"], "party_size": 1, "line_user_id": "Ucrm", "display_name": "客A"},
        )
        lst = client.get("/booking/customers/", headers=_auth(token))
        assert lst.status_code == 200
        rows = [c for c in lst.json() if c["line_user_id"] == "Ucrm"]
        assert len(rows) == 1 and rows[0]["booking_count"] == 1
        cid = rows[0]["id"]
        # PATCH phone/note
        p = client.patch(
            f"/booking/customers/{cid}",
            headers=_auth(token),
            json={"phone": "0912345678", "note": "靠窗"},
        )
        assert p.status_code == 200 and p.json()["phone"] == "0912345678"

    def test_cross_tenant_customer_404(self, client):
        token_a = _register(client)
        slot = _make_slot(client, token_a, max_capacity=4)
        client.post(
            "/booking/reservations/",
            headers=_auth(token_a),
            json={"slot_id": slot["id"], "party_size": 1, "line_user_id": "Uiso"},
        )
        cid = [c for c in client.get("/booking/customers/", headers=_auth(token_a)).json()
               if c["line_user_id"] == "Uiso"][0]["id"]
        token_b = _register(client)
        assert client.get(f"/booking/customers/{cid}", headers=_auth(token_b)).status_code == 404
