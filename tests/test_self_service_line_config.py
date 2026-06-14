"""Task #1 驗收測試 — 租戶自助 LINE Channel Config 端點。

端點涵蓋：
  GET    /tenants/me/line-config
  PUT    /tenants/me/line-config
  DELETE /tenants/me/line-config

驗收標準：
  * CRUD happy path（建立/查詢/更新/刪除），回應不含 secret/token 明文
  * GET 回傳 webhook_url == "/line/webhook/{自己的 tenant_id}"
  * 無效 default_target_lang → 400
  * 未設定時 GET / DELETE → 404
  * 未登入 → 401
  * 隔離：tenant_id 一律取自 current_user，A 操作只影響 A，讀不到 B

全部離線，in-memory SQLite，不需真實 LINE 金鑰。
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

# 確保所有 model metadata 已載入
from saas_mvp.models import tenant as _t, user as _u, note as _n, usage as _us  # noqa: F401,E402
from saas_mvp.models import api_key as _ak, api_key_usage as _aku               # noqa: F401,E402
from saas_mvp.models import plan_change_history as _pch                          # noqa: F401,E402
import saas_mvp.models.line_channel_config as _lcm                               # noqa: F401,E402

from saas_mvp.app import create_app  # noqa: E402
from saas_mvp.db import Base, get_db  # noqa: E402

# ── In-memory SQLite ──────────────────────────────────────────────────────────

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
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


# ── helpers ───────────────────────────────────────────────────────────────────

def _uid() -> str:
    return uuid.uuid4().hex[:8]


def _register(client: TestClient) -> tuple[str, int]:
    """回傳 (token, tenant_id)。"""
    email = f"user_{_uid()}@example.com"
    tn = f"tenant_{_uid()}"
    r = client.post("/auth/register", json={
        "email": email, "password": "Test1234!", "tenant_name": tn,
    })
    assert r.status_code == 201, r.text
    token = r.json()["access_token"]
    me = client.get("/tenants/me", headers={"Authorization": f"Bearer {token}"})
    return token, me.json()["id"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


PATH = "/tenants/me/line-config"


@pytest.fixture
def tenant_a(client):
    return _register(client)


@pytest.fixture
def tenant_b(client):
    return _register(client)


# ── 未登入 ────────────────────────────────────────────────────────────────────

class TestNoAuth:
    def test_get_401(self, client):
        assert client.get(PATH).status_code == 401

    def test_put_401(self, client):
        r = client.put(PATH, json={"channel_secret": "s", "access_token": "t"})
        assert r.status_code == 401

    def test_delete_401(self, client):
        assert client.delete(PATH).status_code == 401


# ── 未設定狀態 ────────────────────────────────────────────────────────────────

class TestUnconfigured:
    def test_get_404(self, client, tenant_a):
        token, _ = tenant_a
        assert client.get(PATH, headers=_auth(token)).status_code == 404

    def test_delete_404(self, client, tenant_a):
        token, _ = tenant_a
        assert client.delete(PATH, headers=_auth(token)).status_code == 404


# ── CRUD happy path ───────────────────────────────────────────────────────────

class TestCrud:
    def test_put_creates_and_masks(self, client, tenant_a):
        token, tid = tenant_a
        r = client.put(PATH, headers=_auth(token), json={
            "channel_secret": "my-secret", "access_token": "my-token",
            "default_target_lang": "ja",
        })
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["tenant_id"] == tid
        assert body["has_channel_secret"] is True
        assert body["has_access_token"] is True
        assert body["default_target_lang"] == "ja"
        assert body["webhook_url"] == f"/line/webhook/{tid}"
        # 絕不洩漏明文
        assert "channel_secret" not in body
        assert "access_token" not in body
        assert "my-secret" not in r.text
        assert "my-token" not in r.text

    def test_get_returns_config(self, client, tenant_a):
        token, tid = tenant_a
        client.put(PATH, headers=_auth(token), json={
            "channel_secret": "s", "access_token": "t",
        })
        r = client.get(PATH, headers=_auth(token))
        assert r.status_code == 200
        body = r.json()
        assert body["tenant_id"] == tid
        assert body["webhook_url"] == f"/line/webhook/{tid}"
        assert body["has_channel_secret"] is True

    def test_put_updates_existing(self, client, tenant_a):
        token, _ = tenant_a
        client.put(PATH, headers=_auth(token), json={
            "channel_secret": "s1", "access_token": "t1", "default_target_lang": "ja",
        })
        r = client.put(PATH, headers=_auth(token), json={
            "channel_secret": "s2", "access_token": "t2", "default_target_lang": "ko",
        })
        assert r.status_code == 200
        assert r.json()["default_target_lang"] == "ko"

    def test_delete_204_then_404(self, client, tenant_a):
        token, _ = tenant_a
        client.put(PATH, headers=_auth(token), json={
            "channel_secret": "s", "access_token": "t",
        })
        r = client.delete(PATH, headers=_auth(token))
        assert r.status_code == 204
        assert r.content == b""
        # 再查已不存在
        assert client.get(PATH, headers=_auth(token)).status_code == 404


# ── 驗證 ──────────────────────────────────────────────────────────────────────

class TestValidation:
    def test_invalid_lang_400(self, client, tenant_a):
        token, _ = tenant_a
        r = client.put(PATH, headers=_auth(token), json={
            "channel_secret": "s", "access_token": "t",
            "default_target_lang": "not-a-lang!!",
        })
        assert r.status_code == 400

    def test_empty_secret_422(self, client, tenant_a):
        token, _ = tenant_a
        r = client.put(PATH, headers=_auth(token), json={
            "channel_secret": "", "access_token": "t",
        })
        assert r.status_code == 422

    def test_missing_field_422(self, client, tenant_a):
        token, _ = tenant_a
        r = client.put(PATH, headers=_auth(token), json={"channel_secret": "s"})
        assert r.status_code == 422

    def test_oversized_secret_422(self, client, tenant_a):
        """超過 max_length 的 secret 應在 router 層被攔（防儲存 DoS）。"""
        token, _ = tenant_a
        r = client.put(PATH, headers=_auth(token), json={
            "channel_secret": "x" * 65, "access_token": "t",
        })
        assert r.status_code == 422

    def test_oversized_token_422(self, client, tenant_a):
        token, _ = tenant_a
        r = client.put(PATH, headers=_auth(token), json={
            "channel_secret": "s", "access_token": "y" * 1025,
        })
        assert r.status_code == 422


# ── 隔離 ──────────────────────────────────────────────────────────────────────

class TestIsolation:
    def test_each_tenant_sees_own_webhook_url(self, client, tenant_a, tenant_b):
        ta, tid_a = tenant_a
        tb, tid_b = tenant_b
        client.put(PATH, headers=_auth(ta), json={
            "channel_secret": "a-secret", "access_token": "a-token",
        })
        r = client.get(PATH, headers=_auth(ta))
        assert r.json()["tenant_id"] == tid_a
        assert r.json()["webhook_url"] == f"/line/webhook/{tid_a}"
        # B 未設定 → 看不到 A 的資料，回自己的 404
        assert client.get(PATH, headers=_auth(tb)).status_code == 404

    def test_b_put_does_not_touch_a(self, client, tenant_a, tenant_b):
        ta, tid_a = tenant_a
        tb, tid_b = tenant_b
        client.put(PATH, headers=_auth(ta), json={
            "channel_secret": "a", "access_token": "a", "default_target_lang": "ja",
        })
        client.put(PATH, headers=_auth(tb), json={
            "channel_secret": "b", "access_token": "b", "default_target_lang": "ko",
        })
        # A 仍是 ja，未被 B 影響
        ra = client.get(PATH, headers=_auth(ta))
        assert ra.json()["default_target_lang"] == "ja"
        assert ra.json()["tenant_id"] == tid_a

    def test_a_delete_does_not_touch_b(self, client, tenant_a, tenant_b):
        ta, _ = tenant_a
        tb, tid_b = tenant_b
        client.put(PATH, headers=_auth(ta), json={
            "channel_secret": "a", "access_token": "a",
        })
        client.put(PATH, headers=_auth(tb), json={
            "channel_secret": "b", "access_token": "b",
        })
        assert client.delete(PATH, headers=_auth(ta)).status_code == 204
        # B 仍在
        assert client.get(PATH, headers=_auth(tb)).status_code == 200
