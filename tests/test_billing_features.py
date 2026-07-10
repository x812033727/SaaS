"""進階功能訂閱（self-service）+ admin 覆寫 REST 測試。"""

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
from saas_mvp.models import tenant_feature as _tf, feature_change_history as _fch  # noqa: F401,E402

from saas_mvp.app import create_app  # noqa: E402
from saas_mvp.db import Base, get_db  # noqa: E402
from saas_mvp.models.user import User  # noqa: E402

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


def _register(client) -> tuple[str, str]:
    email = f"u_{uuid.uuid4().hex[:8]}@example.com"
    r = client.post("/auth/register", json={
        "email": email, "password": "Test1234!", "tenant_name": f"t_{uuid.uuid4().hex[:8]}",
    })
    assert r.status_code == 201, r.text
    return email, r.json()["access_token"]


def _auth(t):
    return {"Authorization": f"Bearer {t}"}


def _make_admin(email):
    db = _Session()
    try:
        u = db.query(User).filter(User.email == email).first()
        u.is_admin = True
        db.commit()
        return u.tenant_id
    finally:
        db.close()


class TestSelfService:
    def test_list_features(self, client):
        _, token = _register(client)
        r = client.get("/billing/features", headers=_auth(token))
        assert r.status_code == 200
        keys = {row["key"] for row in r.json()}
        assert keys == {
            "AUTO_REMINDER", "COUPON_SYSTEM", "PRODUCT_SALES",
            "STAFF_SCHEDULING", "MULTI_LOCATION", "SERVICE_CATALOG",
            "BOOKING_NOTIFY", "PUBLIC_PROFILE",
            "MARKETING_AUTO", "AI_ASSISTANT",
            "PRIVACY_MODE", "ADVANCED_REPORTING",
            "FLEX_MENU", "PUSH_BOOST", "UNLIMITED_STAFF",
            "WEB_BOOKING", "FEEDBACK_SURVEY",
            "AI_BOOKING_AGENT", "AI_BOOST", "DEPOSIT_PAYMENT",
        }

    def test_subscribe_returns_payment_id(self, client):
        _, token = _register(client)
        client.post("/billing/features/COUPON_SYSTEM/unsubscribe", headers=_auth(token))
        r = client.post("/billing/features/COUPON_SYSTEM/subscribe", headers=_auth(token))
        assert r.status_code == 200
        body = r.json()
        assert body["enabled"] is True and body["payment_id"].startswith("simulated_")
        # 讀回為開通
        feats = {f["key"]: f for f in client.get("/billing/features", headers=_auth(token)).json()}
        assert feats["COUPON_SYSTEM"]["enabled"] is True

    def test_unsubscribe(self, client):
        _, token = _register(client)
        r = client.post("/billing/features/PRODUCT_SALES/unsubscribe", headers=_auth(token))
        assert r.status_code == 200 and r.json()["enabled"] is False

    def test_unknown_feature_400(self, client):
        _, token = _register(client)
        assert client.post("/billing/features/NOPE/subscribe", headers=_auth(token)).status_code == 400


class TestAdminOverride:
    def test_admin_set_feature(self, client):
        email, token = _register(client)
        tid = _make_admin(email)
        # admin 列出
        r = client.get(f"/admin/tenants/{tid}/features", headers=_auth(token))
        assert r.status_code == 200
        # 關閉 PRODUCT_SALES
        r = client.put(f"/admin/tenants/{tid}/features/PRODUCT_SALES",
                       headers=_auth(token), json={"enabled": False})
        assert r.status_code == 200
        feats = {f["key"]: f for f in r.json()}
        assert feats["PRODUCT_SALES"]["enabled"] is False

    def test_non_admin_403(self, client):
        _, token = _register(client)  # 非 admin
        assert client.get("/admin/tenants/1/features", headers=_auth(token)).status_code == 403

    def test_unknown_feature_400(self, client):
        email, token = _register(client)
        tid = _make_admin(email)
        assert client.put(f"/admin/tenants/{tid}/features/NOPE",
                          headers=_auth(token), json={"enabled": True}).status_code == 400
