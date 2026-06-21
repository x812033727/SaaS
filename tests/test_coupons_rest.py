"""優惠券 + 會員點數 REST 測試。"""

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
from saas_mvp.models import coupon as _cp, coupon_redemption as _cr  # noqa: F401,E402
from saas_mvp.models import point_transaction as _pt  # noqa: F401,E402

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


class TestCouponRest:
    def test_crud(self, client):
        token = _register(client)
        r = client.post("/booking/coupons/", headers=_auth(token), json={
            "code": f"C{uuid.uuid4().hex[:6]}", "name": "夏季折扣",
            "discount_type": "percent", "discount_value": 15, "max_redemptions": 100,
        })
        assert r.status_code == 201, r.text
        cid = r.json()["id"]
        assert client.get("/booking/coupons/", headers=_auth(token)).status_code == 200
        assert client.get(f"/booking/coupons/{cid}", headers=_auth(token)).json()["discount_value"] == 15
        # 改名 + 停用
        u = client.put(f"/booking/coupons/{cid}", headers=_auth(token), json={"is_active": False})
        assert u.json()["is_active"] is False

    def test_duplicate_code_409(self, client):
        token = _register(client)
        body = {"code": "DUP123", "name": "x", "discount_type": "amount", "discount_value": 50}
        assert client.post("/booking/coupons/", headers=_auth(token), json=body).status_code == 201
        assert client.post("/booking/coupons/", headers=_auth(token), json=body).status_code == 409

    def test_invalid_percent_422(self, client):
        token = _register(client)
        r = client.post("/booking/coupons/", headers=_auth(token), json={
            "code": "BAD", "name": "x", "discount_type": "percent", "discount_value": 150,
        })
        assert r.status_code == 422

    def test_cross_tenant_404(self, client):
        token_a = _register(client)
        cid = client.post("/booking/coupons/", headers=_auth(token_a), json={
            "code": f"A{uuid.uuid4().hex[:6]}", "name": "a", "discount_type": "amount", "discount_value": 10,
        }).json()["id"]
        token_b = _register(client)
        assert client.get(f"/booking/coupons/{cid}", headers=_auth(token_b)).status_code == 404

    def test_unauth_401(self, client):
        assert client.get("/booking/coupons/").status_code == 401


class TestCustomerPointsRest:
    def _make_customer(self, client, token) -> int:
        # 透過建時段 + 建單（帶 line_user_id）自動建顧客
        slot = client.post("/booking/slots/", headers=_auth(token), json={
            "slot_start": "2030-06-01T18:00:00+00:00", "max_capacity": 5,
        }).json()
        client.post("/booking/reservations/", headers=_auth(token), json={
            "slot_id": slot["id"], "party_size": 1, "line_user_id": f"U{uuid.uuid4().hex[:8]}",
        })
        return client.get("/booking/customers/", headers=_auth(token)).json()[0]["id"]

    def test_points_shown_and_ledger(self, client):
        token = _register(client)
        cid = self._make_customer(client, token)
        cust = client.get(f"/booking/customers/{cid}", headers=_auth(token)).json()
        assert cust["points_balance"] == 10 and cust["tier"] == "regular"
        ledger = client.get(f"/booking/customers/{cid}/points", headers=_auth(token)).json()
        assert len(ledger) == 1 and ledger[0]["delta"] == 10

    def test_manual_adjust_and_insufficient(self, client):
        token = _register(client)
        cid = self._make_customer(client, token)
        # 加 200 點 → tier silver
        r = client.post(f"/booking/customers/{cid}/points", headers=_auth(token),
                        json={"delta": 200, "reason": "promo"})
        assert r.json()["points_balance"] == 210 and r.json()["tier"] == "silver"
        # 扣超過餘額 → 409
        bad = client.post(f"/booking/customers/{cid}/points", headers=_auth(token),
                          json={"delta": -9999})
        assert bad.status_code == 409
