"""商品/訂單 REST 測試。"""

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
from saas_mvp.models import customer as _c  # noqa: F401,E402
from saas_mvp.models import product as _p, order as _o, order_item as _oi  # noqa: F401,E402

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


def _product(client, token, *, price=100, stock=10):
    return client.post("/booking/products/", headers=_auth(token), json={
        "name": "商品", "price_cents": price, "stock": stock,
    }).json()


class TestProductRest:
    def test_crud(self, client):
        token = _register(client)
        p = _product(client, token, price=250, stock=5)
        assert p["price_cents"] == 250
        assert client.get(f"/booking/products/{p['id']}", headers=_auth(token)).status_code == 200
        u = client.put(f"/booking/products/{p['id']}", headers=_auth(token), json={"stock": 20})
        assert u.json()["stock"] == 20
        d = client.delete(f"/booking/products/{p['id']}", headers=_auth(token))
        assert d.status_code == 204

    def test_invalid_price_422(self, client):
        token = _register(client)
        r = client.post("/booking/products/", headers=_auth(token), json={"name": "x", "price_cents": -5})
        assert r.status_code == 422

    def test_cross_tenant_404(self, client):
        token_a = _register(client)
        pid = _product(client, token_a)["id"]
        token_b = _register(client)
        assert client.get(f"/booking/products/{pid}", headers=_auth(token_b)).status_code == 404

    def test_unauth_401(self, client):
        assert client.get("/booking/products/").status_code == 401


class TestOrderRest:
    def test_create_with_checkout_then_pay(self, client):
        token = _register(client)
        pid = _product(client, token, price=100, stock=10)["id"]
        r = client.post("/booking/orders/", headers=_auth(token), json={
            "items": [{"product_id": pid, "qty": 2}], "line_user_id": "U1",
        })
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["total_cents"] == 200
        assert body["checkout_url"] and "order=" in body["checkout_url"]
        assert len(body["items"]) == 1
        oid = body["id"]
        paid = client.post(f"/booking/orders/{oid}/pay", headers=_auth(token))
        assert paid.json()["status"] == "paid"

    def test_oversell_409(self, client):
        token = _register(client)
        pid = _product(client, token, stock=1)["id"]
        r = client.post("/booking/orders/", headers=_auth(token), json={
            "items": [{"product_id": pid, "qty": 5}],
        })
        assert r.status_code == 409

    def test_cancel_restores_stock(self, client):
        token = _register(client)
        pid = _product(client, token, stock=10)["id"]
        oid = client.post("/booking/orders/", headers=_auth(token), json={
            "items": [{"product_id": pid, "qty": 4}],
        }).json()["id"]
        client.post(f"/booking/orders/{oid}/cancel", headers=_auth(token))
        assert client.get(f"/booking/products/{pid}", headers=_auth(token)).json()["stock"] == 10
