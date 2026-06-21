"""測試 bot 連線端點。

  POST /admin/line-configs/{tenant_id}/verify
  POST /tenants/me/line-config/verify

涵蓋：
  - stub 回 uid → credential_status=valid + 寫入 line_bot_user_id
  - stub 拋憑證錯 → credential_status=invalid（不 5xx）
  - 非 admin 打 admin verify → 403
  - 無 LINE config → 404（admin 與自助皆然）
  - 自助 verify 僅作用於自己租戶

全部離線，in-memory SQLite。verify 用 dependency_overrides 換上指定 stub。
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
from saas_mvp.line_client import (
    LineBotInfoCredentialError,
    StubLineBotInfoClient,
    get_bot_info_client,
)

_engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
_Session = sessionmaker(autocommit=False, autoflush=False, bind=_engine)

# verify 端點透過此可變 holder 取得當下要用的 stub，測試逐案替換。
_BOT_INFO_HOLDER: dict = {"client": StubLineBotInfoClient("U" + "a" * 32)}


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
    app.dependency_overrides[get_bot_info_client] = lambda: _BOT_INFO_HOLDER["client"]
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


def _make_admin(token: str) -> None:
    from saas_mvp.auth.security import decode_access_token
    from saas_mvp.models.user import User
    payload = decode_access_token(token)
    db = _Session()
    try:
        user = db.get(User, int(payload["sub"]))
        user.is_admin = True
        db.commit()
    finally:
        db.close()


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _set_config(client: TestClient, token: str) -> None:
    """以「不回填」方式設定 config（傳 stub 不影響——upsert 仍會驗證一次）。"""
    _BOT_INFO_HOLDER["client"] = StubLineBotInfoClient(None)  # upsert 不回填 uid
    r = client.put(
        "/tenants/me/line-config",
        headers=_auth(token),
        json={"channel_secret": "s" * 32, "access_token": "tok-secret"},
    )
    assert r.status_code == 200, r.text


# ── 自助 verify ───────────────────────────────────────────────────────────────

def test_self_verify_success(client):
    token, _ = _register(client)
    _set_config(client, token)
    uid = "U" + uuid.uuid4().hex
    _BOT_INFO_HOLDER["client"] = StubLineBotInfoClient(uid)
    r = client.post("/tenants/me/line-config/verify", headers=_auth(token))
    assert r.status_code == 200, r.text
    assert r.json()["credential_status"] == "valid"


def test_self_verify_credential_error_marks_invalid(client):
    token, _ = _register(client)
    _set_config(client, token)
    _BOT_INFO_HOLDER["client"] = StubLineBotInfoClient(
        raises=LineBotInfoCredentialError("bad token")
    )
    r = client.post("/tenants/me/line-config/verify", headers=_auth(token))
    assert r.status_code == 200  # 不 5xx
    assert r.json()["credential_status"] == "invalid"


def test_self_verify_no_config_404(client):
    token, _ = _register(client)
    r = client.post("/tenants/me/line-config/verify", headers=_auth(token))
    assert r.status_code == 404


# ── admin verify ──────────────────────────────────────────────────────────────

def test_admin_verify_success(client):
    admin_token, _ = _register(client)
    _make_admin(admin_token)
    token, tid = _register(client)
    _set_config(client, token)

    uid = "U" + uuid.uuid4().hex
    _BOT_INFO_HOLDER["client"] = StubLineBotInfoClient(uid)
    r = client.post(f"/admin/line-configs/{tid}/verify", headers=_auth(admin_token))
    assert r.status_code == 200, r.text
    assert r.json()["credential_status"] == "valid"

    # 確認 line_bot_user_id 已寫入
    from saas_mvp.models.line_channel_config import LineChannelConfig
    db = _Session()
    try:
        cfg = (
            db.query(LineChannelConfig)
            .filter(LineChannelConfig.tenant_id == tid)
            .one()
        )
        assert cfg.line_bot_user_id == uid
    finally:
        db.close()


def test_admin_verify_normal_user_403(client):
    token, tid = _register(client)
    r = client.post(f"/admin/line-configs/{tid}/verify", headers=_auth(token))
    assert r.status_code == 403


def test_admin_verify_no_config_404(client):
    admin_token, _ = _register(client)
    _make_admin(admin_token)
    _, tid = _register(client)
    r = client.post(f"/admin/line-configs/{tid}/verify", headers=_auth(admin_token))
    assert r.status_code == 404
