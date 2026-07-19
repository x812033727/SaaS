"""R12-C2 — console 營運能力補齊:顧客 CSV 匯入、顧客套票操作。

缺口審計第二批的 console JSON 端點,語意鏡像原 /ui handler。
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from saas_mvp.app import create_app
from saas_mvp.db import Base, get_db
from saas_mvp.models.customer import Customer
from saas_mvp.models.service import Service
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


def _register(client: TestClient, prefix: str = "og") -> tuple[int, dict[str, str]]:
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


def _enable_packages(session_factory, tenant_id: int) -> None:
    db = session_factory()
    try:
        features_svc.set_enabled(
            db, tenant_id, features_svc.SERVICE_PACKAGES, True,
            actor_user_id=None, source="test",
        )
        db.commit()
    finally:
        db.close()


class TestCustomerImport:
    def test_import_creates_customers(self, v1_client):
        client, sf = v1_client
        tid, headers = _register(client)
        csv = "display_name,phone\n王小明,0912345678\n李小美,0987654321\n"
        r = client.post(
            "/api/v1/customers/import",
            json={"content": csv},
            headers=headers,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True and body["created"] == 2
        db = sf()
        try:
            assert (
                db.query(Customer).filter(Customer.tenant_id == tid).count() == 2
            )
        finally:
            db.close()

    def test_import_all_or_nothing_on_error(self, v1_client):
        client, sf = v1_client
        tid, headers = _register(client)
        csv = "display_name,phone\n王小明,0912345678\n,badrow\n"
        r = client.post(
            "/api/v1/customers/import", json={"content": csv}, headers=headers
        )
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is False and body["errors"]
        db = sf()
        try:
            assert (
                db.query(Customer).filter(Customer.tenant_id == tid).count() == 0
            )
        finally:
            db.close()

    def test_import_missing_header(self, v1_client):
        client, _sf = v1_client
        _tid, headers = _register(client)
        r = client.post(
            "/api/v1/customers/import",
            json={"content": "phone\n0912345678\n"},
            headers=headers,
        )
        assert r.status_code == 200 and r.json()["ok"] is False


class TestCustomerPackages:
    def _seed(self, client, sf):
        tid, headers = _register(client)
        _enable_packages(sf, tid)
        db = sf()
        try:
            svc = Service(
                tenant_id=tid, name="按摩", duration_minutes=60, price_cents=100000
            )
            cust = Customer(tenant_id=tid, line_user_id=None, display_name="套票客")
            db.add_all([svc, cust])
            db.commit()
            service_id, customer_id = svc.id, cust.id
        finally:
            db.close()
        # 用既有 console packages API 建套票定義+項目
        r = client.post(
            "/api/v1/packages",
            json={"name": "十次卡", "price_twd": 8000, "validity_days": 180},
            headers=headers,
        )
        assert r.status_code in (200, 201), r.text
        pkg_id = r.json()["id"]
        r2 = client.post(
            f"/api/v1/packages/{pkg_id}/items",
            json={"service_id": service_id, "included_quantity": 10},
            headers=headers,
        )
        assert r2.status_code in (200, 201), r2.text
        return tid, headers, customer_id, pkg_id

    def test_issue_wallet_cancel_roundtrip(self, v1_client):
        client, sf = v1_client
        _tid, headers, cid, pkg_id = self._seed(client, sf)
        r = client.post(
            f"/api/v1/customers/{cid}/packages",
            json={"package_id": pkg_id, "issuance_key": "t-1"},
            headers=headers,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert len(body["wallet"]) == 1
        assert body["wallet"][0]["remaining"] == 10
        assert any(row["kind"] == "issue" for row in body["ledger"])
        cpid = body["wallet"][0]["customer_package_id"]
        r2 = client.post(
            f"/api/v1/customers/{cid}/packages/{cpid}/cancel",
            json={"note": "客訴退費"},
            headers=headers,
        )
        assert r2.status_code == 200, r2.text
        assert r2.json()["wallet"] == []

    def test_issue_requires_owner_403_for_staff(self, v1_client):
        from saas_mvp.auth.security import create_access_token
        from saas_mvp.models.user import User

        client, sf = v1_client
        tid, headers, cid, pkg_id = self._seed(client, sf)
        db = sf()
        try:
            u = User(
                email=f"st-{uuid.uuid4().hex[:6]}@example.com",
                hashed_password="x", tenant_id=tid, role="staff",
            )
            db.add(u)
            db.commit()
            db.refresh(u)
            staff = {"Authorization": f"Bearer {create_access_token(u.id, tid)}"}
        finally:
            db.close()
        r = client.post(
            f"/api/v1/customers/{cid}/packages",
            json={"package_id": pkg_id, "issuance_key": "t-2"},
            headers=staff,
        )
        assert r.status_code == 403

    def test_cross_tenant_customer_isolated(self, v1_client):
        client, sf = v1_client
        _tid_a, headers_a, cid_a, _pkg = self._seed(client, sf)
        tid_b, headers_b = _register(client, "b")
        _enable_packages(sf, tid_b)
        # B 租戶查 A 的顧客套票:空(tenant_query 隔離)
        r = client.get(
            f"/api/v1/customers/{cid_a}/packages", headers=headers_b
        )
        assert r.status_code == 200
        assert r.json() == {"wallet": [], "ledger": []}
