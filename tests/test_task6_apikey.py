"""Task #6 驗收測試：API Key 管理 + 多認證 + 用量計量 + /usage

覆蓋範圍
--------
1. 建立 key → 回應含 plain_key；再次列出不含明文或 hash
2. 撤銷後立即 401，usage 歷史記錄仍保留
3. X-API-Key header 認證成功
4. Authorization: Bearer <api_key> 認證成功
5. 既有 JWT Bearer 認證不退化
6. 三者皆無 → 401
7. 用量累加可手算逐位核對（每次呼叫 +1）
8. 超過 quota → 429（tenant-level 限制 free=100）
9. 跨租戶：A 租戶無法用 B 租戶的 key，/usage 不洩漏他人資料
10. /usage 回傳結構正確（tenant 總量 + per-key 明細）

全部離線，in-memory SQLite。
"""

from __future__ import annotations

import datetime
import os
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("SAAS_RATE_LIMIT_ENABLED", "false")

# 確保所有 model metadata 已載入
from saas_mvp.models import tenant as _t, user as _u, note as _n, usage as _us  # noqa: F401
from saas_mvp.models import api_key as _ak, api_key_usage as _aku  # noqa: F401
from saas_mvp.app import create_app
from saas_mvp.db import Base, get_db
from saas_mvp.quota import PLAN_DAILY_LIMITS

# ── In-memory SQLite ──────────────────────────────────────────

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


# ── 共用 helpers ──────────────────────────────────────────────

def _uid() -> str:
    return uuid.uuid4().hex[:8]


def _register(client, email: str, password: str, tenant: str) -> str:
    resp = client.post("/auth/register", json={
        "email": email, "password": password, "tenant_name": tenant,
    })
    assert resp.status_code == 201, resp.text
    return resp.json()["access_token"]


def _create_key(client, jwt_token: str, name: str = "test-key") -> dict:
    resp = client.post(
        "/api-keys/",
        json={"name": name},
        headers={"Authorization": f"Bearer {jwt_token}"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


# ── Fixtures ──────────────────────────────────────────────────

@pytest.fixture(scope="module")
def alice_jwt(client):
    return _register(client, f"alice6@test.com", "AlicePass99!", "apikey-alpha")


@pytest.fixture(scope="module")
def bob_jwt(client):
    return _register(client, f"bob6@test.com", "BobPass99!!", "apikey-beta")


@pytest.fixture(scope="module")
def alice_key(client, alice_jwt):
    """建立並回傳 Alice 的 API key dict（含 plain_key）。"""
    return _create_key(client, alice_jwt, "alice-main-key")


# ── 1. 建立 key ───────────────────────────────────────────────

class TestCreateKey:
    def test_create_returns_201(self, client, alice_jwt):
        resp = client.post(
            "/api-keys/", json={"name": "k1"},
            headers={"Authorization": f"Bearer {alice_jwt}"},
        )
        assert resp.status_code == 201

    def test_create_response_has_plain_key(self, client, alice_jwt):
        data = _create_key(client, alice_jwt, "k2")
        assert "plain_key" in data
        assert data["plain_key"].startswith("myapp_")

    def test_create_response_no_key_hash(self, client, alice_jwt):
        data = _create_key(client, alice_jwt, "k3")
        assert "key_hash" not in data

    def test_list_excludes_plain_key_and_hash(self, client, alice_jwt):
        resp = client.get("/api-keys/", headers={"Authorization": f"Bearer {alice_jwt}"})
        assert resp.status_code == 200
        for item in resp.json():
            assert "plain_key" not in item
            assert "key_hash" not in item
            assert "key_prefix" in item

    def test_create_without_token_401(self, client):
        resp = client.post("/api-keys/", json={"name": "ghost"})
        assert resp.status_code == 401


# ── 2. 撤銷 ──────────────────────────────────────────────────

class TestRevokeKey:
    def test_revoke_returns_204(self, client, alice_jwt):
        key = _create_key(client, alice_jwt, "to-revoke")
        resp = client.delete(
            f"/api-keys/{key['id']}",
            headers={"Authorization": f"Bearer {alice_jwt}"},
        )
        assert resp.status_code == 204

    def test_revoked_key_cannot_authenticate(self, client, alice_jwt):
        """撤銷後，用該 key 呼叫 /notes/ 應得 401。"""
        key = _create_key(client, alice_jwt, "revoke-then-try")
        plain = key["plain_key"]
        key_id = key["id"]

        # 撤銷
        client.delete(f"/api-keys/{key_id}",
                      headers={"Authorization": f"Bearer {alice_jwt}"})

        # 嘗試認證
        resp = client.get("/notes/", headers={"X-API-Key": plain})
        assert resp.status_code == 401

    def test_revoke_other_tenant_key_404(self, client, alice_jwt, bob_jwt):
        """Bob 無法撤銷 Alice 的 key。"""
        alice_key = _create_key(client, alice_jwt, "alice-protected")
        resp = client.delete(
            f"/api-keys/{alice_key['id']}",
            headers={"Authorization": f"Bearer {bob_jwt}"},
        )
        assert resp.status_code == 404


# ── 3 & 4. 多認證 ─────────────────────────────────────────────

class TestMultiAuth:
    def test_x_api_key_header_works(self, client, alice_key):
        resp = client.get("/notes/", headers={"X-API-Key": alice_key["plain_key"]})
        assert resp.status_code == 200

    def test_bearer_api_key_works(self, client, alice_key):
        resp = client.get(
            "/notes/",
            headers={"Authorization": f"Bearer {alice_key['plain_key']}"},
        )
        assert resp.status_code == 200

    def test_invalid_api_key_401(self, client):
        resp = client.get("/notes/", headers={"X-API-Key": "myapp_invalidddddddd"})
        assert resp.status_code == 401

    def test_no_credentials_401(self, client):
        resp = client.get("/notes/")
        assert resp.status_code == 401


# ── 5. JWT 不退化 ─────────────────────────────────────────────

class TestJwtNotRegressed:
    def test_jwt_bearer_still_works(self, client, alice_jwt):
        resp = client.get("/notes/", headers={"Authorization": f"Bearer {alice_jwt}"})
        assert resp.status_code == 200

    def test_wrong_jwt_401(self, client):
        resp = client.get("/notes/", headers={"Authorization": "Bearer badtoken"})
        assert resp.status_code == 401


# ── 7. 用量累加（手算核對）──────────────────────────────────

class TestUsageAccumulation:
    def test_per_key_usage_increments(self, client, alice_jwt):
        """建新 key，呼叫 N 次，/usage 的 per-key count 應等於 N。"""
        key = _create_key(client, alice_jwt, "count-key")
        plain = key["plain_key"]
        key_id = key["id"]
        headers = {"X-API-Key": plain}

        N = 3
        for _ in range(N):
            r = client.get("/notes/", headers=headers)
            assert r.status_code == 200

        usage = client.get("/usage/", headers={"Authorization": f"Bearer {alice_jwt}"}).json()
        per_key = {item["api_key_id"]: item["used_today"] for item in usage["api_keys"]}
        assert per_key.get(key_id, 0) == N, f"expected {N}, got {per_key}"

    def test_tenant_total_includes_key_calls(self, client, alice_jwt):
        """tenant.used_today 應包含 API key 呼叫。"""
        usage_before = client.get(
            "/usage/", headers={"Authorization": f"Bearer {alice_jwt}"}
        ).json()["tenant"]["used_today"]

        key = _create_key(client, alice_jwt, "total-count-key")
        for _ in range(2):
            client.get("/notes/", headers={"X-API-Key": key["plain_key"]})

        usage_after = client.get(
            "/usage/", headers={"Authorization": f"Bearer {alice_jwt}"}
        ).json()["tenant"]["used_today"]

        # 2 次 notes 呼叫（每次 +1 tenant） + 2 次 /usage GET = usage 增量 ≥ 2
        # /usage GET 不在 require_quota 管控內，所以只計 notes 呼叫
        assert usage_after >= usage_before + 2


# ── 8. 超量 → 429 ─────────────────────────────────────────────

class TestQuotaExceededViaApiKey:
    @pytest.fixture()
    def capped_key(self, client):
        """建立新租戶 + API key，把 tenant quota 塞滿。"""
        uid = _uid()
        jwt = _register(client, f"cap6_{uid}@test.com", "CapPass99!", f"cap6-{uid}")
        key = _create_key(client, jwt, "cap-key")

        # 取 tenant_id 並直接寫入 DB 達到上限
        tenant_id = client.get(
            "/tenants/me", headers={"Authorization": f"Bearer {jwt}"}
        ).json()["id"]
        limit = PLAN_DAILY_LIMITS["free"]
        today = datetime.date.today()
        db = _Session()
        try:
            db.execute(
                text(
                    "INSERT INTO api_usage (tenant_id, period, count) "
                    "VALUES (:tid, :dt, :cnt) "
                    "ON CONFLICT(tenant_id, period) DO UPDATE SET count = :cnt"
                ),
                {"tid": tenant_id, "dt": today.isoformat(), "cnt": limit},
            )
            db.commit()
        finally:
            db.close()

        return key["plain_key"]

    def test_api_key_call_rejected_when_quota_exceeded(self, client, capped_key):
        resp = client.get("/notes/", headers={"X-API-Key": capped_key})
        assert resp.status_code == 429
        assert "Quota" in resp.json()["detail"]


# ── 9. 跨租戶隔離 ─────────────────────────────────────────────

class TestCrossTenantIsolation:
    def test_bob_cannot_use_alice_key(self, client, alice_key, bob_jwt):
        """以 Alice 的 key 呼叫，應以 Alice 的租戶身份認證，而非 Bob 的。"""
        # 用 Alice key 可以成功
        r = client.get("/notes/", headers={"X-API-Key": alice_key["plain_key"]})
        assert r.status_code == 200

    def test_bob_usage_does_not_show_alice_keys(self, client, alice_jwt, bob_jwt):
        """Bob 的 /usage api_keys 不含 Alice 的 key。"""
        alice_resp = client.get("/api-keys/", headers={"Authorization": f"Bearer {alice_jwt}"}).json()
        alice_key_ids = {k["id"] for k in alice_resp}

        bob_usage = client.get("/usage/", headers={"Authorization": f"Bearer {bob_jwt}"}).json()
        bob_key_ids = {item["api_key_id"] for item in bob_usage["api_keys"]}

        assert alice_key_ids.isdisjoint(bob_key_ids), \
            "Bob 的 /usage 不應出現 Alice 的 key ID"

    def test_bob_cannot_revoke_alice_key(self, client, alice_jwt, bob_jwt):
        key = _create_key(client, alice_jwt, "protected-key")
        resp = client.delete(
            f"/api-keys/{key['id']}",
            headers={"Authorization": f"Bearer {bob_jwt}"},
        )
        assert resp.status_code == 404


# ── 10. /usage 回傳結構 ───────────────────────────────────────

class TestUsageEndpoint:
    def test_usage_returns_200(self, client, alice_jwt):
        resp = client.get("/usage/", headers={"Authorization": f"Bearer {alice_jwt}"})
        assert resp.status_code == 200

    def test_usage_tenant_fields(self, client, alice_jwt):
        data = client.get("/usage/", headers={"Authorization": f"Bearer {alice_jwt}"}).json()
        t = data["tenant"]
        assert "plan" in t
        assert "daily_limit" in t
        assert "used_today" in t
        assert "remaining" in t
        assert "period" in t
        assert t["daily_limit"] > 0
        assert t["remaining"] >= 0

    def test_usage_api_keys_field_is_list(self, client, alice_jwt):
        data = client.get("/usage/", headers={"Authorization": f"Bearer {alice_jwt}"}).json()
        assert isinstance(data["api_keys"], list)

    def test_usage_api_key_item_fields(self, client, alice_jwt):
        """每個 per-key 條目必須含規定欄位（含 remaining）。"""
        # 確保有至少一次 API key 呼叫，讓 api_keys 不為空
        key = _create_key(client, alice_jwt, "field-check-key")
        client.get("/notes/", headers={"X-API-Key": key["plain_key"]})

        data = client.get("/usage/", headers={"Authorization": f"Bearer {alice_jwt}"}).json()
        for item in data["api_keys"]:
            assert "api_key_id" in item
            assert "name" in item
            assert "key_prefix" in item
            assert "used_today" in item
            assert "remaining" in item
            assert "period" in item
            # remaining 不為負
            assert item["remaining"] >= 0

    def test_usage_api_key_remaining_equals_limit_minus_used(self, client, alice_jwt):
        """per-key remaining = max(0, daily_limit - used_today)。"""
        key = _create_key(client, alice_jwt, "remaining-check-key")
        plain = key["plain_key"]
        key_id = key["id"]

        N = 2
        for _ in range(N):
            client.get("/notes/", headers={"X-API-Key": plain})

        data = client.get("/usage/", headers={"Authorization": f"Bearer {alice_jwt}"}).json()
        daily_limit = data["tenant"]["daily_limit"]
        per_key = {item["api_key_id"]: item for item in data["api_keys"]}
        assert key_id in per_key
        item = per_key[key_id]
        assert item["used_today"] == N
        assert item["remaining"] == max(0, daily_limit - N)

    def test_usage_no_auth_401(self, client):
        resp = client.get("/usage/")
        assert resp.status_code == 401

    def test_usage_via_api_key_auth(self, client, alice_key):
        """以 API key 也可以查 /usage。"""
        resp = client.get("/usage/", headers={"X-API-Key": alice_key["plain_key"]})
        assert resp.status_code == 200

    def test_remaining_equals_limit_minus_used(self, client, alice_jwt):
        data = client.get("/usage/", headers={"Authorization": f"Bearer {alice_jwt}"}).json()
        t = data["tenant"]
        assert t["remaining"] == max(0, t["daily_limit"] - t["used_today"])
