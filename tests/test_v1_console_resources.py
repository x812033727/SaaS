"""R7-C3 — console JSON API:房間/設備資源(五子實體)。"""

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


def _register(client: TestClient, prefix: str = "res") -> tuple[int, dict[str, str]]:
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
            features_svc.BOOKABLE_RESOURCES,
            enabled,
            actor_user_id=None,
            source="admin",
        )
        db.commit()
    finally:
        db.close()


def _add_service(session_factory, tenant_id: int, name: str = "雷射") -> int:
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


def _mk_type(client, headers, name="美容室") -> int:
    r = client.post(
        "/api/v1/resources/types", json={"name": name}, headers=headers
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _mk_resource(client, headers, type_id: int, name="1 號室") -> int:
    r = client.post(
        "/api/v1/resources",
        json={"resource_type_id": type_id, "name": name, "capacity": 1},
        headers=headers,
    )
    assert r.status_code == 201, r.text
    return next(x["id"] for x in r.json()["resources"] if x["name"] == name)


class TestResourcesConsole:
    def test_feature_gate_403(self, v1_client):
        client, sf = v1_client
        tid, headers = _register(client)
        _set_feature(sf, tid, False)
        assert (
            client.get("/api/v1/resources/overview", headers=headers).status_code
            == 403
        )
        assert (
            client.post(
                "/api/v1/resources/types", json={"name": "x"}, headers=headers
            ).status_code
            == 403
        )

    def test_type_create_duplicate_and_toggle(self, v1_client):
        client, sf = v1_client
        tid, headers = _register(client)
        _set_feature(sf, tid)
        type_id = _mk_type(client, headers)
        assert (
            client.post(
                "/api/v1/resources/types", json={"name": "美容室"}, headers=headers
            ).status_code
            == 422
        )
        r = client.post(
            f"/api/v1/resources/types/{type_id}/active",
            json={"active": False},
            headers=headers,
        )
        assert r.status_code == 200
        assert r.json()["is_active"] is False

    def test_resource_crud_and_overview(self, v1_client):
        client, sf = v1_client
        tid, headers = _register(client)
        _set_feature(sf, tid)
        type_id = _mk_type(client, headers)
        resource_id = _mk_resource(client, headers, type_id)
        # update
        r = client.patch(
            f"/api/v1/resources/{resource_id}",
            json={"name": "VIP 室", "capacity": 2},
            headers=headers,
        )
        assert r.status_code == 200, r.text
        row = next(x for x in r.json()["resources"] if x["id"] == resource_id)
        assert row["name"] == "VIP 室"
        assert row["capacity"] == 2
        # active toggle
        r2 = client.post(
            f"/api/v1/resources/{resource_id}/active",
            json={"active": False},
            headers=headers,
        )
        row2 = next(x for x in r2.json()["resources"] if x["id"] == resource_id)
        assert row2["is_active"] is False
        # overview aggregates
        ov = client.get("/api/v1/resources/overview", headers=headers).json()
        assert len(ov["types"]) == 1
        assert len(ov["resources"]) == 1

    def test_requirement_set_and_delete(self, v1_client):
        client, sf = v1_client
        tid, headers = _register(client)
        _set_feature(sf, tid)
        service_id = _add_service(sf, tid)
        type_id = _mk_type(client, headers)
        r = client.post(
            "/api/v1/resources/requirements",
            json={"service_id": service_id, "resource_type_id": type_id, "quantity": 2},
            headers=headers,
        )
        assert r.status_code == 200, r.text
        req = r.json()["requirements"][0]
        assert req["quantity"] == 2
        assert req["service_name"] == "雷射"
        r2 = client.delete(
            f"/api/v1/resources/requirements/{req['id']}", headers=headers
        )
        assert r2.status_code == 200
        assert r2.json()["requirements"] == []

    def test_availability_overlap_and_block_validation(self, v1_client):
        client, sf = v1_client
        tid, headers = _register(client)
        _set_feature(sf, tid)
        type_id = _mk_type(client, headers)
        resource_id = _mk_resource(client, headers, type_id)
        win = {"weekday": 0, "start_time": "09:00", "end_time": "12:00"}
        r = client.post(
            f"/api/v1/resources/{resource_id}/availability", json=win, headers=headers
        )
        assert r.status_code == 201, r.text
        window = next(
            x for x in r.json()["resources"] if x["id"] == resource_id
        )["windows"][0]
        # 重疊 → 422
        assert (
            client.post(
                f"/api/v1/resources/{resource_id}/availability",
                json={"weekday": 0, "start_time": "10:00", "end_time": "13:00"},
                headers=headers,
            ).status_code
            == 422
        )
        assert (
            client.delete(
                f"/api/v1/resources/availability/{window['id']}", headers=headers
            ).status_code
            == 200
        )
        # block:結束早於開始 → 422
        assert (
            client.post(
                f"/api/v1/resources/{resource_id}/blocks",
                json={"starts_at": "2031-01-02T10:00", "ends_at": "2031-01-02T09:00"},
                headers=headers,
            ).status_code
            == 422
        )
        r3 = client.post(
            f"/api/v1/resources/{resource_id}/blocks",
            json={
                "starts_at": "2031-01-02T09:00",
                "ends_at": "2031-01-03T18:00",
                "reason": "年度保養",
            },
            headers=headers,
        )
        assert r3.status_code == 201
        block = next(
            x for x in r3.json()["resources"] if x["id"] == resource_id
        )["blocks"][0]
        assert block["reason"] == "年度保養"
        assert (
            client.delete(
                f"/api/v1/resources/blocks/{block['id']}", headers=headers
            ).status_code
            == 200
        )

    def test_staff_cannot_mutate(self, v1_client):
        client, sf = v1_client
        tid, headers = _register(client)
        _set_feature(sf, tid)
        staff = _staff_headers(sf, tid)
        assert (
            client.post(
                "/api/v1/resources/types", json={"name": "x"}, headers=staff
            ).status_code
            == 403
        )
        assert (
            client.get("/api/v1/resources/overview", headers=staff).status_code == 200
        )

    def test_tenant_isolation_404(self, v1_client):
        client, sf = v1_client
        tid, headers = _register(client)
        _set_feature(sf, tid)
        type_id = _mk_type(client, headers)
        resource_id = _mk_resource(client, headers, type_id)
        tid2, headers2 = _register(client, prefix="other")
        _set_feature(sf, tid2)
        assert (
            client.patch(
                f"/api/v1/resources/{resource_id}",
                json={"name": "偷改"},
                headers=headers2,
            ).status_code
            == 404
        )
        assert (
            client.post(
                f"/api/v1/resources/{resource_id}/availability",
                json={"weekday": 1, "start_time": "09:00", "end_time": "10:00"},
                headers=headers2,
            ).status_code
            == 404
        )
