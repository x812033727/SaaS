"""進階功能閘門測試（REST 403 + 提醒不入列；每拒絕配控制組）。"""

from __future__ import annotations

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
from saas_mvp.models import reservation as _r, reservation_reminder as _rr  # noqa: F401,E402
from saas_mvp.models import coupon as _cp, coupon_redemption as _cr, point_transaction as _pt  # noqa: F401,E402
from saas_mvp.models import product as _p, order as _o, order_item as _oi  # noqa: F401,E402
from saas_mvp.models import tenant_feature as _tf, feature_change_history as _fch  # noqa: F401,E402

from saas_mvp.app import create_app  # noqa: E402
from saas_mvp.db import Base, get_db  # noqa: E402
from saas_mvp.models.reservation_reminder import ReservationReminder  # noqa: E402

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


def _unsub(client, token, feature):
    r = client.post(f"/billing/features/{feature}/unsubscribe", headers=_auth(token))
    assert r.status_code == 200, r.text


def _sub(client, token, feature):
    r = client.post(f"/billing/features/{feature}/subscribe", headers=_auth(token))
    assert r.status_code == 200, r.text


class TestCouponGating:
    def test_disabled_403_then_enabled_200(self, client):
        token = _register(client)
        # 預設開通 → 可建券（控制組）
        body = {"code": f"C{uuid.uuid4().hex[:6]}", "name": "x",
                "discount_type": "amount", "discount_value": 10}
        assert client.post("/booking/coupons/", headers=_auth(token), json=body).status_code == 201
        # 退訂 → 403
        _unsub(client, token, "COUPON_SYSTEM")
        assert client.get("/booking/coupons/", headers=_auth(token)).status_code == 403
        # 重新訂閱 → 恢復
        _sub(client, token, "COUPON_SYSTEM")
        assert client.get("/booking/coupons/", headers=_auth(token)).status_code == 200


class TestShopGating:
    def test_products_and_orders_403_when_disabled(self, client):
        token = _register(client)
        p = client.post("/booking/products/", headers=_auth(token),
                        json={"name": "x", "price_cents": 100, "stock": 5})
        assert p.status_code == 201
        _unsub(client, token, "PRODUCT_SALES")
        assert client.get("/booking/products/", headers=_auth(token)).status_code == 403
        assert client.post("/booking/orders/", headers=_auth(token),
                           json={"items": [{"product_id": p.json()["id"], "qty": 1}]}).status_code == 403
        _sub(client, token, "PRODUCT_SALES")
        assert client.get("/booking/products/", headers=_auth(token)).status_code == 200


class TestReminderGating:
    def _slot(self, client, token):
        return client.post("/booking/slots/", headers=_auth(token), json={
            "slot_start": "2030-09-01T18:00:00+00:00", "max_capacity": 10,
        }).json()["id"]

    def _reminders_for(self, line_user_id):
        db = _Session()
        try:
            return db.execute(
                select(ReservationReminder).where(ReservationReminder.line_user_id == line_user_id)
            ).scalars().all()
        finally:
            db.close()

    def test_no_reminders_when_disabled(self, client):
        token = _register(client)
        sid = self._slot(client, token)
        _unsub(client, token, "AUTO_REMINDER")
        u_off = f"Uoff{uuid.uuid4().hex[:6]}"
        client.post("/booking/reservations/", headers=_auth(token),
                    json={"slot_id": sid, "party_size": 1, "line_user_id": u_off})
        assert self._reminders_for(u_off) == []
        # 控制組：開通後入列
        _sub(client, token, "AUTO_REMINDER")
        u_on = f"Uon{uuid.uuid4().hex[:6]}"
        client.post("/booking/reservations/", headers=_auth(token),
                    json={"slot_id": sid, "party_size": 1, "line_user_id": u_on})
        assert len(self._reminders_for(u_on)) == 2
