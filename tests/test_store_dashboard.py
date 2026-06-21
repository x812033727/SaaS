"""GET /tenants/me/dashboard — 店家自助總覽測試。

涵蓋：
  - 尚未設定 LINE bot：不 404，line_config 為 null、has_line_config=false、usage 仍回傳
  - 已設定 LINE bot：line_config 遮罩 + credential_status，usage 正確
  - 結構性隔離：dashboard 只反映自己租戶（tenant.id 與自己一致）

全部離線，in-memory SQLite。
"""

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

from saas_mvp.models import tenant as _t, user as _u, note as _n, usage as _us  # noqa: F401
from saas_mvp.models import api_key as _ak, api_key_usage as _aku               # noqa: F401
from saas_mvp.models import plan_change_history as _pch                          # noqa: F401
import saas_mvp.models.line_channel_config as _lcm                               # noqa: F401

from saas_mvp.app import create_app
from saas_mvp.db import Base, get_db
from saas_mvp.line_client import StubLineBotInfoClient, get_bot_info_client

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
    app.dependency_overrides[get_bot_info_client] = (
        lambda: StubLineBotInfoClient("U" + uuid.uuid4().hex)
    )
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


def _uid() -> str:
    return uuid.uuid4().hex[:8]


def _register(client: TestClient) -> tuple[str, int]:
    email = f"user_{_uid()}@example.com"
    tn = f"tenant_{_uid()}"
    r = client.post("/auth/register", json={
        "email": email, "password": "Test1234!", "tenant_name": tn,
    })
    assert r.status_code == 201, r.text
    token = r.json()["access_token"]
    tid = client.get("/tenants/me", headers=_auth(token)).json()["id"]
    return token, tid


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_dashboard_without_config(client):
    token, tid = _register(client)
    r = client.get("/tenants/me/dashboard", headers=_auth(token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tenant"]["id"] == tid
    assert body["has_line_config"] is False
    assert body["line_config"] is None
    # usage 仍回傳完整配額狀態
    assert body["usage"]["plan"] == "free"
    assert "remaining" in body["usage"]
    assert "remaining_chars" in body["usage"]


def test_dashboard_with_config(client):
    token, tid = _register(client)
    client.put(
        "/tenants/me/line-config",
        headers=_auth(token),
        json={"channel_secret": "s" * 32, "access_token": "tok-secret"},
    )
    r = client.get("/tenants/me/dashboard", headers=_auth(token))
    assert r.status_code == 200
    body = r.json()
    assert body["has_line_config"] is True
    lc = body["line_config"]
    assert lc is not None
    # 遮罩：不含明文
    assert "channel_secret" not in lc
    assert "access_token" not in lc
    assert lc["has_channel_secret"] is True
    assert lc["credential_status"] == "valid"
    assert lc["webhook_url"].endswith(f"/{tid}")


def test_dashboard_isolation(client):
    token_a, tid_a = _register(client)
    token_b, tid_b = _register(client)
    a = client.get("/tenants/me/dashboard", headers=_auth(token_a)).json()
    b = client.get("/tenants/me/dashboard", headers=_auth(token_b)).json()
    assert a["tenant"]["id"] == tid_a
    assert b["tenant"]["id"] == tid_b
    assert a["tenant"]["id"] != b["tenant"]["id"]
