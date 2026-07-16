"""R4-C4 — POST /tenants/me/line-config/webhook/setup(console 用 JSON 端點)。"""

from __future__ import annotations

import os
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("SAAS_RATE_LIMIT_ENABLED", "false")
os.environ.setdefault(
    "SAAS_LINE_CHANNEL_ENCRYPT_KEY",
    "ZGV2LWxpbmUtc2VjcmV0LWtleS0zMmJ5dGVzLWxvbmc=",
)

from saas_mvp.app import create_app  # noqa: E402
from saas_mvp.config import settings  # noqa: E402
from saas_mvp.db import Base, get_db  # noqa: E402
from saas_mvp.line_client import (  # noqa: E402
    LineWebhookAdminError,
    LineWebhookTestResult,
    StubLineWebhookAdminClient,
    get_webhook_admin_client,
)

_engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
_Session = sessionmaker(autocommit=False, autoflush=False, bind=_engine)


def _make_client(admin_client, monkeypatch):
    monkeypatch.setattr(settings, "public_base_url", "https://saas.example.com")
    Base.metadata.drop_all(bind=_engine)
    Base.metadata.create_all(bind=_engine)
    app = create_app()

    def override_db():
        db = _Session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_webhook_admin_client] = lambda: admin_client
    return TestClient(app)


def _auth_with_line_config(client) -> dict[str, str]:
    r = client.post("/auth/register", json={
        "email": f"wh_{uuid.uuid4().hex[:8]}@x.tw",
        "password": "Test1234!",
        "tenant_name": f"wh_{uuid.uuid4().hex[:8]}",
    })
    headers = {"Authorization": f"Bearer {r.json()['access_token']}"}
    saved = client.put(
        "/tenants/me/line-config",
        headers=headers,
        json={"channel_secret": "s" * 32, "access_token": "tok-webhook"},
    )
    assert saved.status_code == 200, saved.text
    return headers


def test_setup_success_returns_result(monkeypatch):
    admin = StubLineWebhookAdminClient()
    client = _make_client(admin, monkeypatch)
    headers = _auth_with_line_config(client)
    r = client.post("/tenants/me/line-config/webhook/setup", headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["success"] is True and body["active"] is True
    assert body["endpoint"].endswith("/line/webhook/1")
    # 用租戶保存的 token 呼叫
    assert admin.calls and admin.calls[0][1] == "tok-webhook"


def test_setup_admin_error_502(monkeypatch):
    admin = StubLineWebhookAdminClient(raises=LineWebhookAdminError("boom"))
    client = _make_client(admin, monkeypatch)
    headers = _auth_with_line_config(client)
    r = client.post("/tenants/me/line-config/webhook/setup", headers=headers)
    assert r.status_code == 502


def test_setup_without_line_config_404(monkeypatch):
    admin = StubLineWebhookAdminClient()
    client = _make_client(admin, monkeypatch)
    r = client.post("/auth/register", json={
        "email": f"wh_{uuid.uuid4().hex[:8]}@x.tw",
        "password": "Test1234!",
        "tenant_name": f"wh_{uuid.uuid4().hex[:8]}",
    })
    headers = {"Authorization": f"Bearer {r.json()['access_token']}"}
    resp = client.post("/tenants/me/line-config/webhook/setup", headers=headers)
    assert resp.status_code == 404


def test_setup_requires_https_base_url(monkeypatch):
    admin = StubLineWebhookAdminClient()
    client = _make_client(admin, monkeypatch)
    headers = _auth_with_line_config(client)
    monkeypatch.setattr(settings, "public_base_url", "")  # 無 HTTPS 對外網址
    r = client.post("/tenants/me/line-config/webhook/setup", headers=headers)
    assert r.status_code == 503


def test_setup_requires_auth(monkeypatch):
    admin = StubLineWebhookAdminClient()
    client = _make_client(admin, monkeypatch)
    assert client.post("/tenants/me/line-config/webhook/setup").status_code == 401


def test_setup_reports_failure_result(monkeypatch):
    admin = StubLineWebhookAdminClient(
        result=LineWebhookTestResult(
            endpoint="https://saas.example.com/line/webhook/1",
            success=False, active=False, status_code=403, reason="forbidden",
        )
    )
    client = _make_client(admin, monkeypatch)
    headers = _auth_with_line_config(client)
    r = client.post("/tenants/me/line-config/webhook/setup", headers=headers)
    assert r.status_code == 200
    assert r.json()["success"] is False and r.json()["status_code"] == 403
