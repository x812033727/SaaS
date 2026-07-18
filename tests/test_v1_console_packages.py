"""R7-C2 — console JSON API:服務套票定義 CRUD。"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from saas_mvp.app import create_app
from saas_mvp.auth.security import create_access_token
from saas_mvp.db import Base, get_db
from saas_mvp.models.service import Service
from saas_mvp.models.user import User
from saas_mvp.services import features as features_svc


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


def _register(client: TestClient, prefix: str = "pkg") -> tuple[int, dict[str, str]]:
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
    headers = {"Authorization": f"Bearer {r.json()['access_token']}"}
    ctx = client.get("/api/v1/context", headers=headers).json()
    return ctx["tenant"]["id"], headers


def _set_feature(session_factory, tenant_id: int, enabled: bool = True) -> None:
    db = session_factory()
    try:
        features_svc.set_enabled(
            db,
            tenant_id,
            features_svc.SERVICE_PACKAGES,
            enabled,
            actor_user_id=None,
            source="admin",
        )
        db.commit()
    finally:
        db.close()


def _add_service(session_factory, tenant_id: int, name: str = "課程") -> int:
    db = session_factory()
    try:
        svc = Service(
            tenant_id=tenant_id, name=name, duration_minutes=60, price_cents=100000
        )
        db.add(svc)
        db.commit()
        return svc.id
    finally:
        db.close()


def _staff_headers(session_factory, tenant_id: int) -> dict[str, str]:
    db = session_factory()
    try:
        user = User(
            email=f"staff-{uuid.uuid4().hex[:8]}@example.com",
            hashed_password="x",
            tenant_id=tenant_id,
            role="staff",
        )
        db.add(user)
        db.commit()
        token = create_access_token(user.id, tenant_id)
    finally:
        db.close()
    return {"Authorization": f"Bearer {token}"}


_PKG = {"name": "十次卡", "price_twd": 8800, "validity_days": 365}


class TestPackages:
    def test_feature_gate_403(self, v1_client):
        client, sf = v1_client
        tid, headers = _register(client)
        _set_feature(sf, tid, False)
        assert client.get("/api/v1/packages", headers=headers).status_code == 403
        assert (
            client.post("/api/v1/packages", json=_PKG, headers=headers).status_code
            == 403
        )

    def test_create_add_item_and_toggle(self, v1_client):
        client, sf = v1_client
        tid, headers = _register(client)
        _set_feature(sf, tid)
        service_id = _add_service(sf, tid)
        r = client.post("/api/v1/packages", json=_PKG, headers=headers)
        assert r.status_code == 201, r.text
        pkg = r.json()
        assert pkg["price_cents"] == 880000
        assert pkg["is_active"] is True
        assert pkg["items"] == []
        # upsert item:新增 → 更新
        r2 = client.post(
            f"/api/v1/packages/{pkg['id']}/items",
            json={"service_id": service_id, "included_quantity": 10},
            headers=headers,
        )
        assert r2.status_code == 200, r2.text
        assert r2.json()["items"] == [
            {"service_id": service_id, "service_name": "課程", "included_quantity": 10}
        ]
        r3 = client.post(
            f"/api/v1/packages/{pkg['id']}/items",
            json={"service_id": service_id, "included_quantity": 12},
            headers=headers,
        )
        assert r3.json()["items"][0]["included_quantity"] == 12
        # 停售
        r4 = client.post(
            f"/api/v1/packages/{pkg['id']}/active",
            json={"active": False},
            headers=headers,
        )
        assert r4.status_code == 200
        assert r4.json()["is_active"] is False
        rows = client.get("/api/v1/packages", headers=headers)
        assert rows.headers["X-Total-Count"] == "1"

    def test_duplicate_name_and_bad_quantity_422(self, v1_client):
        client, sf = v1_client
        tid, headers = _register(client)
        _set_feature(sf, tid)
        service_id = _add_service(sf, tid)
        pkg_id = client.post("/api/v1/packages", json=_PKG, headers=headers).json()["id"]
        assert (
            client.post("/api/v1/packages", json=_PKG, headers=headers).status_code
            == 422
        )
        assert (
            client.post(
                f"/api/v1/packages/{pkg_id}/items",
                json={"service_id": service_id, "included_quantity": 0},
                headers=headers,
            ).status_code
            == 422
        )

    def test_staff_cannot_mutate(self, v1_client):
        client, sf = v1_client
        tid, headers = _register(client)
        _set_feature(sf, tid)
        staff = _staff_headers(sf, tid)
        assert (
            client.post("/api/v1/packages", json=_PKG, headers=staff).status_code
            == 403
        )
        assert client.get("/api/v1/packages", headers=staff).status_code == 200

    def test_tenant_isolation_404(self, v1_client):
        client, sf = v1_client
        tid, headers = _register(client)
        _set_feature(sf, tid)
        service_id = _add_service(sf, tid)
        pkg_id = client.post("/api/v1/packages", json=_PKG, headers=headers).json()["id"]
        tid2, headers2 = _register(client, prefix="other")
        _set_feature(sf, tid2)
        r = client.post(
            f"/api/v1/packages/{pkg_id}/items",
            json={"service_id": service_id, "included_quantity": 5},
            headers=headers2,
        )
        assert r.status_code == 404
        assert (
            client.post(
                f"/api/v1/packages/{pkg_id}/active",
                json={"active": False},
                headers=headers2,
            ).status_code
            == 404
        )
