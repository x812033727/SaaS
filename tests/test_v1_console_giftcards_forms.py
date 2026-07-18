"""R7-C1 — console JSON API:電子禮物卡 + 顧客表單/同意書。"""

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


def _register(client: TestClient, prefix: str = "c1") -> tuple[int, dict[str, str]]:
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


def _set_feature(
    session_factory, tenant_id: int, feature: str, enabled: bool = True
) -> None:
    db = session_factory()
    try:
        features_svc.set_enabled(
            db, tenant_id, feature, enabled, actor_user_id=None, source="admin"
        )
        db.commit()
    finally:
        db.close()


def _enable(session_factory, tenant_id: int, feature: str) -> None:
    _set_feature(session_factory, tenant_id, feature, True)


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


_ISSUE = {
    "amount_twd": 1000,
    "fulfillment_guarantee": "履約保障:本店已投保履約保證保險,保障消費者權益。",
    "compliance_ack": True,
}


def _issue_body(**over) -> dict:
    return {**_ISSUE, "issuance_key": uuid.uuid4().hex, **over}


class TestGiftCards:
    def test_feature_gate_403(self, v1_client):
        client, sf = v1_client
        tid, headers = _register(client)
        # 新租戶(試用)可能 bundle 全開;顯式關閉後驗 403
        _set_feature(sf, tid, features_svc.GIFT_CARDS, False)
        assert client.get("/api/v1/gift-cards", headers=headers).status_code == 403
        assert (
            client.post(
                "/api/v1/gift-cards", json=_issue_body(), headers=headers
            ).status_code
            == 403
        )

    def test_issue_list_and_idempotent_replay(self, v1_client):
        client, sf = v1_client
        tid, headers = _register(client)
        _enable(sf, tid, features_svc.GIFT_CARDS)
        body = _issue_body()
        r = client.post("/api/v1/gift-cards", json=body, headers=headers)
        assert r.status_code == 201, r.text
        first = r.json()
        assert first["created"] is True
        assert first["code"]
        assert first["card"]["balance_cents"] == 100000
        # 同 issuance_key 重放:冪等,不再回明碼
        r2 = client.post("/api/v1/gift-cards", json=body, headers=headers)
        assert r2.status_code == 201
        assert r2.json()["created"] is False
        assert r2.json()["code"] is None
        rows = client.get("/api/v1/gift-cards", headers=headers)
        assert rows.status_code == 200
        assert rows.headers["X-Total-Count"] == "1"
        assert rows.json()[0]["id"] == first["card"]["id"]

    def test_compliance_ack_required(self, v1_client):
        client, sf = v1_client
        tid, headers = _register(client)
        _enable(sf, tid, features_svc.GIFT_CARDS)
        r = client.post(
            "/api/v1/gift-cards",
            json=_issue_body(compliance_ack=False),
            headers=headers,
        )
        assert r.status_code == 422

    def test_staff_cannot_issue_or_void(self, v1_client):
        client, sf = v1_client
        tid, headers = _register(client)
        _enable(sf, tid, features_svc.GIFT_CARDS)
        card_id = (
            client.post("/api/v1/gift-cards", json=_issue_body(), headers=headers)
            .json()["card"]["id"]
        )
        staff = _staff_headers(sf, tid)
        assert (
            client.post(
                "/api/v1/gift-cards", json=_issue_body(), headers=staff
            ).status_code
            == 403
        )
        assert (
            client.post(
                f"/api/v1/gift-cards/{card_id}/void", json={}, headers=staff
            ).status_code
            == 403
        )
        # 唯讀列表 staff 可用
        assert client.get("/api/v1/gift-cards", headers=staff).status_code == 200

    def test_void_zeroes_balance(self, v1_client):
        client, sf = v1_client
        tid, headers = _register(client)
        _enable(sf, tid, features_svc.GIFT_CARDS)
        card_id = (
            client.post("/api/v1/gift-cards", json=_issue_body(), headers=headers)
            .json()["card"]["id"]
        )
        r = client.post(
            f"/api/v1/gift-cards/{card_id}/void",
            json={"note": "測試作廢"},
            headers=headers,
        )
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "void"
        assert r.json()["balance_cents"] == 0

    def test_void_tenant_isolation(self, v1_client):
        client, sf = v1_client
        tid, headers = _register(client)
        _enable(sf, tid, features_svc.GIFT_CARDS)
        card_id = (
            client.post("/api/v1/gift-cards", json=_issue_body(), headers=headers)
            .json()["card"]["id"]
        )
        tid2, headers2 = _register(client, prefix="other")
        _enable(sf, tid2, features_svc.GIFT_CARDS)
        r = client.post(
            f"/api/v1/gift-cards/{card_id}/void", json={}, headers=headers2
        )
        assert r.status_code == 404


_FORM = {
    "name": "術前同意書",
    "consent_text": "本人已詳閱並同意上述療程說明與風險告知。",
}


class TestClientForms:
    def test_feature_gate_403(self, v1_client):
        client, sf = v1_client
        tid, headers = _register(client)
        _set_feature(sf, tid, features_svc.CLIENT_FORMS, False)
        assert client.get("/api/v1/client-forms", headers=headers).status_code == 403
        assert (
            client.post(
                "/api/v1/client-forms", json=_FORM, headers=headers
            ).status_code
            == 403
        )

    def test_create_add_question_activate(self, v1_client):
        client, sf = v1_client
        tid, headers = _register(client)
        _enable(sf, tid, features_svc.CLIENT_FORMS)
        r = client.post("/api/v1/client-forms", json=_FORM, headers=headers)
        assert r.status_code == 201, r.text
        template = r.json()
        assert template["is_active"] is False
        assert template["questions"] == []
        # 未有題目前不可啟用
        r2 = client.post(
            f"/api/v1/client-forms/{template['id']}/active",
            json={"active": True},
            headers=headers,
        )
        assert r2.status_code == 422
        # 加題(select 需至少兩選項)
        bad = client.post(
            f"/api/v1/client-forms/{template['id']}/questions",
            json={"label": "膚質", "field_type": "select", "options": "油性"},
            headers=headers,
        )
        assert bad.status_code == 422
        ok = client.post(
            f"/api/v1/client-forms/{template['id']}/questions",
            json={
                "label": "膚質",
                "field_type": "select",
                "required": True,
                "options": "油性\n乾性",
            },
            headers=headers,
        )
        assert ok.status_code == 201, ok.text
        q = ok.json()["questions"][0]
        assert q["options"] == ["油性", "乾性"]
        assert q["is_required"] is True
        # 啟用
        r3 = client.post(
            f"/api/v1/client-forms/{template['id']}/active",
            json={"active": True},
            headers=headers,
        )
        assert r3.status_code == 200
        assert r3.json()["is_active"] is True
        rows = client.get("/api/v1/client-forms", headers=headers)
        assert rows.headers["X-Total-Count"] == "1"

    def test_duplicate_name_422(self, v1_client):
        client, sf = v1_client
        tid, headers = _register(client)
        _enable(sf, tid, features_svc.CLIENT_FORMS)
        assert (
            client.post("/api/v1/client-forms", json=_FORM, headers=headers).status_code
            == 201
        )
        assert (
            client.post("/api/v1/client-forms", json=_FORM, headers=headers).status_code
            == 422
        )

    def test_staff_cannot_mutate(self, v1_client):
        client, sf = v1_client
        tid, headers = _register(client)
        _enable(sf, tid, features_svc.CLIENT_FORMS)
        staff = _staff_headers(sf, tid)
        assert (
            client.post("/api/v1/client-forms", json=_FORM, headers=staff).status_code
            == 403
        )
        assert client.get("/api/v1/client-forms", headers=staff).status_code == 200

    def test_tenant_isolation_404(self, v1_client):
        client, sf = v1_client
        tid, headers = _register(client)
        _enable(sf, tid, features_svc.CLIENT_FORMS)
        template_id = (
            client.post("/api/v1/client-forms", json=_FORM, headers=headers)
            .json()["id"]
        )
        tid2, headers2 = _register(client, prefix="other")
        _enable(sf, tid2, features_svc.CLIENT_FORMS)
        r = client.post(
            f"/api/v1/client-forms/{template_id}/questions",
            json={"label": "備註", "field_type": "text"},
            headers=headers2,
        )
        assert r.status_code == 404
