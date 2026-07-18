"""R8-1 — console JSON API:LINE 自動回覆規則。"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from saas_mvp.app import create_app
from saas_mvp.db import Base, get_db
from saas_mvp.models.flex_menu import FlexMenu


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


def _register(client: TestClient, prefix: str = "ar") -> tuple[int, dict[str, str]]:
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


def _mk_flex_menu(session_factory, tenant_id: int, title="主選單") -> int:
    db = session_factory()
    try:
        menu = FlexMenu(tenant_id=tenant_id, title=title, is_active=True)
        db.add(menu)
        db.commit()
        return menu.id
    finally:
        db.close()


class TestAutoReplyConsole:
    def test_envelope_defaults(self, v1_client):
        client, _ = v1_client
        _, headers = _register(client)
        r = client.get("/api/v1/auto-reply", headers=headers)
        assert r.status_code == 200, r.text
        env = r.json()
        assert env["rules"] == []
        assert env["flex_menus"] == []
        # 未設 LINE → 預設 translation,前端據此顯示模式警示
        assert env["bot_mode"] == "translation"

    def test_crud_text_rule(self, v1_client):
        client, _ = v1_client
        _, headers = _register(client)
        r = client.post(
            "/api/v1/auto-reply",
            json={"keyword": "營業時間", "reply_text": "每日 10:00-20:00", "priority": 5},
            headers=headers,
        )
        assert r.status_code == 201, r.text
        rule = r.json()
        assert rule["match_type"] == "contains"
        assert rule["is_active"] is True
        # 更新(含 toggle)
        r2 = client.put(
            f"/api/v1/auto-reply/{rule['id']}",
            json={
                "keyword": "營業時間",
                "match_type": "exact",
                "reply_type": "text",
                "reply_text": "改 11:00 開",
                "flex_menu_id": None,
                "priority": 1,
                "is_active": False,
            },
            headers=headers,
        )
        assert r2.status_code == 200, r2.text
        assert r2.json()["match_type"] == "exact"
        assert r2.json()["is_active"] is False
        # 刪除回 envelope
        r3 = client.delete(f"/api/v1/auto-reply/{rule['id']}", headers=headers)
        assert r3.status_code == 200
        assert r3.json()["rules"] == []

    def test_flex_rule_requires_owned_menu(self, v1_client):
        client, sf = v1_client
        tid, headers = _register(client)
        menu_id = _mk_flex_menu(sf, tid)
        r = client.post(
            "/api/v1/auto-reply",
            json={"keyword": "菜單", "reply_type": "flex", "flex_menu_id": menu_id},
            headers=headers,
        )
        assert r.status_code == 201, r.text
        assert r.json()["reply_text"] is None
        env = client.get("/api/v1/auto-reply", headers=headers).json()
        assert env["flex_menus"] == [{"id": menu_id, "name": "主選單"}]
        # 他租戶的 menu → 404
        tid2, headers2 = _register(client, prefix="other")
        r2 = client.post(
            "/api/v1/auto-reply",
            json={"keyword": "菜單", "reply_type": "flex", "flex_menu_id": menu_id},
            headers=headers2,
        )
        assert r2.status_code == 404

    def test_validation_422(self, v1_client):
        client, _ = v1_client
        _, headers = _register(client)
        # text 型缺 reply_text
        assert (
            client.post(
                "/api/v1/auto-reply", json={"keyword": "hi"}, headers=headers
            ).status_code
            == 422
        )
        # 空 keyword
        assert (
            client.post(
                "/api/v1/auto-reply",
                json={"keyword": "  ", "reply_text": "x"},
                headers=headers,
            ).status_code
            == 422
        )

    def test_tenant_isolation_404(self, v1_client):
        client, _ = v1_client
        _, headers = _register(client)
        rule_id = client.post(
            "/api/v1/auto-reply",
            json={"keyword": "hi", "reply_text": "hello"},
            headers=headers,
        ).json()["id"]
        _, headers2 = _register(client, prefix="other")
        assert (
            client.delete(
                f"/api/v1/auto-reply/{rule_id}", headers=headers2
            ).status_code
            == 404
        )
