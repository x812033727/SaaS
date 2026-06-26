"""訂單套用優惠券 + 四種票券類型測試（對標 vibeaico）。"""

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
from saas_mvp.models import coupon as _cp, coupon_redemption as _cr  # noqa: F401,E402
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


def _product(client, token, *, price, stock=100):
    return client.post("/booking/products/", headers=_auth(token), json={
        "name": "商品", "price_cents": price, "stock": stock,
    }).json()


def _coupon(client, token, **kw):
    body = {"code": f"C{uuid.uuid4().hex[:6]}", "name": "券"}
    body.update(kw)
    r = client.post("/booking/coupons/", headers=_auth(token), json=body)
    assert r.status_code == 201, r.text
    return r.json()


def _order(client, token, pid, qty=1, *, line_user_id, coupon_code=None):
    body = {"items": [{"product_id": pid, "qty": qty}], "line_user_id": line_user_id}
    if coupon_code is not None:
        body["coupon_code"] = coupon_code
    return client.post("/booking/orders/", headers=_auth(token), json=body)


class TestCouponTypes:
    def test_create_all_four_types(self, client):
        token = _register(client)
        for dtype, val in [("percent", 10), ("amount", 5000), ("gift", 0), ("upsell", 3000)]:
            c = _coupon(client, token, discount_type=dtype, discount_value=val)
            assert c["discount_type"] == dtype

    def test_invalid_type_rejected(self, client):
        token = _register(client)
        r = client.post("/booking/coupons/", headers=_auth(token), json={
            "code": "BAD", "name": "x", "discount_type": "weird", "discount_value": 1,
        })
        assert r.status_code == 422


class TestOrderCouponApply:
    def test_percent_discount(self, client):
        token = _register(client)
        p = _product(client, token, price=1000)
        c = _coupon(client, token, discount_type="percent", discount_value=20)
        r = _order(client, token, p["id"], 2, line_user_id="U_pct", coupon_code=c["code"])
        assert r.status_code == 201, r.text
        body = r.json()
        # 小計 2000 → 20% 折 400 → 實付 1600
        assert body["discount_cents"] == 400
        assert body["total_cents"] == 1600
        assert body["coupon_code"] == c["code"]

    def test_amount_discount_clamped(self, client):
        token = _register(client)
        p = _product(client, token, price=300)
        c = _coupon(client, token, discount_type="amount", discount_value=5000)
        r = _order(client, token, p["id"], 1, line_user_id="U_amt", coupon_code=c["code"])
        assert r.status_code == 201, r.text
        body = r.json()
        # 折抵不得超過小計 300 → total 0
        assert body["discount_cents"] == 300
        assert body["total_cents"] == 0

    def test_gift_no_monetary_discount_but_redeemed(self, client):
        token = _register(client)
        p = _product(client, token, price=800)
        c = _coupon(client, token, discount_type="gift", discount_value=0)
        r = _order(client, token, p["id"], 1, line_user_id="U_gift", coupon_code=c["code"])
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["discount_cents"] == 0
        assert body["total_cents"] == 800
        assert body["coupon_code"] == c["code"]
        # 券已核銷一次
        got = client.get(f"/booking/coupons/{c['id']}", headers=_auth(token)).json()
        assert got["redeemed_count"] == 1

    def test_min_spend_not_met_409(self, client):
        token = _register(client)
        p = _product(client, token, price=500)
        c = _coupon(client, token, discount_type="amount", discount_value=100,
                    min_spend_cents=1000)
        r = _order(client, token, p["id"], 1, line_user_id="U_min", coupon_code=c["code"])
        assert r.status_code == 409, r.text
        # 未達門檻 → 不應建立核銷紀錄（券仍可用）
        got = client.get(f"/booking/coupons/{c['id']}", headers=_auth(token)).json()
        assert got["redeemed_count"] == 0

    def test_one_per_user_second_use_409(self, client):
        token = _register(client)
        p = _product(client, token, price=1000, stock=100)
        c = _coupon(client, token, discount_type="amount", discount_value=100)
        r1 = _order(client, token, p["id"], 1, line_user_id="U_dup", coupon_code=c["code"])
        assert r1.status_code == 201, r1.text
        r2 = _order(client, token, p["id"], 1, line_user_id="U_dup", coupon_code=c["code"])
        assert r2.status_code == 409, r2.text

    def test_unknown_coupon_409(self, client):
        token = _register(client)
        p = _product(client, token, price=1000)
        r = _order(client, token, p["id"], 1, line_user_id="U_nf", coupon_code="NOPE")
        assert r.status_code == 409, r.text

    def test_coupon_without_line_user_422(self, client):
        token = _register(client)
        p = _product(client, token, price=1000)
        c = _coupon(client, token, discount_type="amount", discount_value=100)
        body = {"items": [{"product_id": p["id"], "qty": 1}], "coupon_code": c["code"]}
        r = client.post("/booking/orders/", headers=_auth(token), json=body)
        assert r.status_code == 422, r.text
