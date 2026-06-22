"""服務目錄（service catalog）REST 測試 — 分類/服務 CRUD + 員工指派 + 分店篩選 + 閘門。"""

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
from saas_mvp.models import location as _loc, staff as _staff  # noqa: F401,E402
from saas_mvp.models import service_category as _sc, service as _svc  # noqa: F401,E402
from saas_mvp.models import service_staff as _svcs  # noqa: F401,E402
from saas_mvp.models import tenant_feature as _tf, feature_change_history as _fch  # noqa: F401,E402

from saas_mvp.app import create_app  # noqa: E402
from saas_mvp.db import Base, get_db  # noqa: E402
from saas_mvp.services import features as features_svc  # noqa: E402

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


def _tenant_id_of(token) -> int:
    db = _Session()
    try:
        from saas_mvp.models.user import User
        from saas_mvp.auth.security import decode_access_token
        payload = decode_access_token(token)
        return db.query(User).filter(User.id == int(payload["sub"])).first().tenant_id
    finally:
        db.close()


class TestCatalog:
    def test_category_and_service_crud(self, client):
        token = _register(client)
        cat = client.post("/booking/services/categories", headers=_auth(token),
                          json={"name": "美髮", "sort_order": 1})
        assert cat.status_code == 201, cat.text
        cid = cat.json()["id"]
        assert client.get("/booking/services/categories", headers=_auth(token)).status_code == 200
        svc = client.post("/booking/services/", headers=_auth(token), json={
            "name": "剪髮", "category_id": cid, "duration_minutes": 45, "price_cents": 50000,
        })
        assert svc.status_code == 201, svc.text
        sid = svc.json()["id"]
        assert svc.json()["duration_minutes"] == 45
        got = client.get(f"/booking/services/{sid}", headers=_auth(token))
        assert got.json()["name"] == "剪髮"
        upd = client.put(f"/booking/services/{sid}", headers=_auth(token),
                         json={"price_cents": 60000})
        assert upd.json()["price_cents"] == 60000

    def test_staff_assignment(self, client):
        token = _register(client)
        sid = client.post("/booking/services/", headers=_auth(token),
                          json={"name": "染髮"}).json()["id"]
        staff_id = client.post("/booking/staff/", headers=_auth(token),
                               json={"name": "設計師A"}).json()["id"]
        a = client.post(f"/booking/services/{sid}/staff", headers=_auth(token),
                        json={"staff_id": staff_id})
        assert a.status_code == 201, a.text
        # 重複指派 → 409
        dup = client.post(f"/booking/services/{sid}/staff", headers=_auth(token),
                          json={"staff_id": staff_id})
        assert dup.status_code == 409
        lst = client.get(f"/booking/services/{sid}/staff", headers=_auth(token))
        assert len(lst.json()) == 1
        d = client.delete(f"/booking/services/{sid}/staff/{staff_id}", headers=_auth(token))
        assert d.status_code == 204
        assert client.get(f"/booking/services/{sid}/staff", headers=_auth(token)).json() == []

    def test_location_scoping_filter(self, client):
        token = _register(client)
        loc = client.post("/booking/locations/", headers=_auth(token),
                          json={"name": "南店"}).json()
        lid = loc["id"]
        # 全店通用（location_id=None）+ 綁定南店
        client.post("/booking/services/", headers=_auth(token), json={"name": "通用服務"})
        client.post("/booking/services/", headers=_auth(token),
                    json={"name": "南店限定", "location_id": lid})
        # 指定該分店 → 看到「通用」+「南店限定」
        scoped = client.get(f"/booking/services/?location_id={lid}", headers=_auth(token)).json()
        names = {s["name"] for s in scoped}
        assert "通用服務" in names and "南店限定" in names
        # 指定別的分店 id（999）→ 只看到通用（NULL）
        other = client.get("/booking/services/?location_id=999", headers=_auth(token)).json()
        onames = {s["name"] for s in other}
        assert "通用服務" in onames and "南店限定" not in onames

    def test_tenant_isolation(self, client):
        token_a = _register(client)
        sid = client.post("/booking/services/", headers=_auth(token_a),
                          json={"name": "A服務"}).json()["id"]
        token_b = _register(client)
        assert client.get(f"/booking/services/{sid}", headers=_auth(token_b)).status_code == 404

    def test_feature_gate_403_when_disabled(self, client):
        token = _register(client)
        tid = _tenant_id_of(token)
        db = _Session()
        try:
            features_svc.set_enabled(
                db, tid, features_svc.SERVICE_CATALOG, False,
                actor_user_id=None, source="admin",
            )
        finally:
            db.close()
        r = client.get("/booking/services/categories", headers=_auth(token))
        assert r.status_code == 403, r.text
