"""R8-2 — /booking/rich-menu/preview.png JSON API(console 預覽用)。"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from saas_mvp.app import create_app
from saas_mvp.db import Base, get_db


@pytest.fixture()
def client():
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
    with TestClient(app) as c:
        yield c


def _headers(client: TestClient) -> dict[str, str]:
    unique = uuid.uuid4().hex[:8]
    r = client.post(
        "/auth/register",
        json={
            "email": f"rm-{unique}@example.com",
            "password": "safe-password-123",
            "tenant_name": f"rm-{unique}",
        },
    )
    assert r.status_code == 201, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


class TestRichMenuPreview:
    def test_requires_auth(self, client):
        r = client.get(
            "/booking/rich-menu/preview.png",
            params={"template": "booking4", "theme": "brand"},
        )
        assert r.status_code == 401

    def test_returns_png(self, client):
        headers = _headers(client)
        r = client.get(
            "/booking/rich-menu/preview.png",
            params={"template": "booking4", "theme": "brand"},
            headers=headers,
        )
        assert r.status_code == 200, r.text
        assert r.headers["content-type"] == "image/png"
        assert r.content.startswith(b"\x89PNG")
        assert r.headers["cache-control"] == "no-store"

    def test_unknown_template_400(self, client):
        headers = _headers(client)
        r = client.get(
            "/booking/rich-menu/preview.png",
            params={"template": "bogus", "theme": "brand"},
            headers=headers,
        )
        assert r.status_code == 400

    def test_options_include_template_labels(self, client):
        r = client.get("/booking/rich-menu/options", headers=_headers(client))
        assert r.status_code == 200
        body = r.json()
        assert "booking4" in body["templates"]
        assert body["template_labels"]["booking4"]
