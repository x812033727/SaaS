"""分店（multi-location）REST 測試。"""

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
from saas_mvp.models import location as _loc  # noqa: F401,E402
from saas_mvp.models import tenant_feature as _tf  # noqa: F401,E402
from saas_mvp.models import feature_change_history as _fch  # noqa: F401,E402

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


class TestLocations:
    def test_crud(self, client):
        token = _register(client)
        r = client.post("/booking/locations/", headers=_auth(token), json={
            "name": "信義店", "address": "台北市信義區", "phone": "0212345678",
        })
        assert r.status_code == 201, r.text
        lid = r.json()["id"]
        assert r.json()["timezone"] == "Asia/Taipei"
        assert client.get("/booking/locations/", headers=_auth(token)).status_code == 200
        got = client.get(f"/booking/locations/{lid}", headers=_auth(token))
        assert got.json()["name"] == "信義店"
        upd = client.put(f"/booking/locations/{lid}", headers=_auth(token),
                         json={"name": "信義旗艦店"})
        assert upd.json()["name"] == "信義旗艦店"
        d = client.delete(f"/booking/locations/{lid}", headers=_auth(token))
        assert d.status_code == 204
        assert client.get(f"/booking/locations/{lid}", headers=_auth(token)).json()["is_active"] is False

    def test_cap_of_5_returns_409(self, client):
        token = _register(client)
        for i in range(5):
            r = client.post("/booking/locations/", headers=_auth(token),
                            json={"name": f"店{i}"})
            assert r.status_code == 201, r.text
        over = client.post("/booking/locations/", headers=_auth(token), json={"name": "第六店"})
        assert over.status_code == 409, over.text

    def test_deactivate_frees_slot(self, client):
        """停用一家分店後可再建一家（上限算的是啟用中）。"""
        token = _register(client)
        ids = []
        for i in range(5):
            ids.append(client.post("/booking/locations/", headers=_auth(token),
                                   json={"name": f"x{i}"}).json()["id"])
        # 停用一家
        client.delete(f"/booking/locations/{ids[0]}", headers=_auth(token))
        r = client.post("/booking/locations/", headers=_auth(token), json={"name": "replacement"})
        assert r.status_code == 201, r.text

    def test_tenant_isolation(self, client):
        token_a = _register(client)
        lid = client.post("/booking/locations/", headers=_auth(token_a),
                          json={"name": "A店"}).json()["id"]
        token_b = _register(client)
        # B 看不到 A 的分店清單
        assert client.get("/booking/locations/", headers=_auth(token_b)).json() == []
        # B 取 A 的分店 → 404（不洩漏存在）
        assert client.get(f"/booking/locations/{lid}", headers=_auth(token_b)).status_code == 404

    def test_unauth_401(self, client):
        assert client.get("/booking/locations/").status_code == 401
