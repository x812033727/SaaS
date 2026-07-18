"""R8-4 — console JSON API:方案/帳單/進階功能(金錢面)。"""

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
from saas_mvp.models.user import User


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


def _register(client: TestClient, prefix: str = "bil") -> tuple[int, dict[str, str]]:
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


class TestPlanConsole:
    def test_overview_owner_only(self, v1_client):
        client, sf = v1_client
        tid, headers = _register(client)
        staff = _staff_headers(sf, tid)
        assert client.get("/api/v1/plan", headers=staff).status_code == 403
        r = client.get("/api/v1/plan", headers=headers)
        assert r.status_code == 200, r.text
        env = r.json()
        assert {p["key"] for p in env["plans"]} >= {"free", "standard", "pro"}
        assert env["plan_info"]["paid"] == "free"

    def test_subscribe_stub_and_unsubscribe(self, v1_client):
        client, _ = v1_client
        _, headers = _register(client)
        # 測試環境 provider=stub → 立即生效、無 checkout_url
        r = client.post("/api/v1/plan/standard/subscribe", json={}, headers=headers)
        assert r.status_code == 200, r.text
        assert r.json()["mode"] == "stub"
        assert r.json()["checkout_url"] is None
        env = client.get("/api/v1/plan", headers=headers).json()
        assert env["plan_info"]["paid"] == "standard"
        # 重複訂閱同方案 → 400(service HTTPException 透傳)
        assert (
            client.post(
                "/api/v1/plan/standard/subscribe", json={}, headers=headers
            ).status_code
            == 400
        )
        # 未知方案 → 422
        assert (
            client.post(
                "/api/v1/plan/bogus/subscribe", json={}, headers=headers
            ).status_code
            == 422
        )
        # 退訂
        r2 = client.post("/api/v1/plan/unsubscribe", json={}, headers=headers)
        assert r2.status_code == 200
        assert r2.json()["plan_info"]["paid"] == "free"

    def test_subscribe_staff_403(self, v1_client):
        client, sf = v1_client
        tid, _ = _register(client)
        staff = _staff_headers(sf, tid)
        assert (
            client.post(
                "/api/v1/plan/standard/subscribe", json={}, headers=staff
            ).status_code
            == 403
        )


class TestBillingConsole:
    def test_overview_shapes(self, v1_client):
        client, _ = v1_client
        _, headers = _register(client)
        r = client.get("/api/v1/billing", headers=headers)
        assert r.status_code == 200, r.text
        env = r.json()
        assert env["subscription"] is None
        assert env["charges"] == []
        assert env["einvoice_config"]["configured"] is False
        assert env["invoice_profile"]["configured"] is False

    def test_invoice_profile_save_and_masking(self, v1_client):
        client, _ = v1_client
        _, headers = _register(client)
        r = client.post(
            "/api/v1/billing/invoice-profile",
            json={"mode": "personal", "carrier_type": "mobile", "carrier_number": "/ABC1234"},
            headers=headers,
        )
        assert r.status_code == 200, r.text
        profile = r.json()["invoice_profile"]
        assert profile["configured"] is True
        assert profile["has_carrier_number"] is True
        # 明碼載具不得回流
        assert "/ABC1234" not in r.text
        # 非法模式 → 422
        assert (
            client.post(
                "/api/v1/billing/invoice-profile",
                json={"mode": "bogus"},
                headers=headers,
            ).status_code
            == 422
        )

    def test_einvoice_config_write_only_secrets(self, v1_client):
        client, _ = v1_client
        _, headers = _register(client)
        r = client.post(
            "/api/v1/billing/einvoice-config",
            json={
                "merchant_id": "2000132",
                "hash_key": "test-key-abc",
                "hash_iv": "test-iv-def",
                "environment": "stage",
                "enabled": True,
            },
            headers=headers,
        )
        assert r.status_code == 200, r.text
        cfg = r.json()["einvoice_config"]
        assert cfg["configured"] is True
        assert cfg["enabled"] is True
        assert cfg["has_hash_key"] is True
        # 憑證明碼不得回流
        assert "test-key-abc" not in r.text
        assert "test-iv-def" not in r.text
        # 留空=沿用既有值
        r2 = client.post(
            "/api/v1/billing/einvoice-config",
            json={"merchant_id": "2000132", "environment": "stage", "enabled": False},
            headers=headers,
        )
        assert r2.json()["einvoice_config"]["has_hash_key"] is True
        assert r2.json()["einvoice_config"]["enabled"] is False

    def test_owner_only(self, v1_client):
        client, sf = v1_client
        tid, _ = _register(client)
        staff = _staff_headers(sf, tid)
        assert client.get("/api/v1/billing", headers=staff).status_code == 403


class TestFeaturesConsole:
    def test_list_visible_to_staff_mutation_owner_only(self, v1_client):
        client, sf = v1_client
        tid, headers = _register(client)
        staff = _staff_headers(sf, tid)
        # 唯讀:任一成員可看(比照 /ui)
        r = client.get("/api/v1/features", headers=staff)
        assert r.status_code == 200, r.text
        assert r.json()["is_owner"] is False
        assert len(r.json()["features"]) >= 20
        # mutation 限 owner
        key = r.json()["features"][0]["key"]
        assert (
            client.post(
                f"/api/v1/features/{key}/subscribe", json={}, headers=staff
            ).status_code
            == 403
        )
        assert client.get("/api/v1/features", headers=headers).json()["is_owner"] is True

    def test_subscribe_unsubscribe_stub(self, v1_client):
        from saas_mvp.services import features as features_svc

        client, sf = v1_client
        tid, headers = _register(client)
        # 試用 bundle 讓全功能 enabled;先顯式關閉一項,才有「未開通→訂閱」路徑
        db = sf()
        try:
            features_svc.set_enabled(
                db, tid, features_svc.GIFT_CARDS, False,
                actor_user_id=None, source="admin",
            )
            db.commit()
        finally:
            db.close()
        env = client.get("/api/v1/features", headers=headers).json()
        target = next(f for f in env["features"] if not f["enabled"])
        assert target["key"] == features_svc.GIFT_CARDS
        r = client.post(
            f"/api/v1/features/{target['key']}/subscribe", json={}, headers=headers
        )
        assert r.status_code == 200, r.text
        assert r.json()["enabled"] is True
        env2 = client.get("/api/v1/features", headers=headers).json()
        assert next(
            f for f in env2["features"] if f["key"] == target["key"]
        )["enabled"] is True
        # 退訂
        r2 = client.post(
            f"/api/v1/features/{target['key']}/unsubscribe", json={}, headers=headers
        )
        assert r2.status_code == 200
        assert next(
            f for f in r2.json()["features"] if f["key"] == target["key"]
        )["enabled"] is False
        # 未知 feature → 422
        assert (
            client.post(
                "/api/v1/features/BOGUS/subscribe", json={}, headers=headers
            ).status_code
            == 422
        )
