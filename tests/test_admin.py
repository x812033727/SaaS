"""Task #4 驗收測試：Admin API

覆蓋範圍
--------
1. 非 admin 存取 /admin/* → 403（不是 401）
2. admin 可列租戶 GET /admin/tenants
3. admin 可查租戶用量 GET /admin/tenants/{id}/usage
4. admin 停用租戶 PATCH /admin/tenants/{id} {is_active: false}
5. 被停用租戶後續請求回 403
6. admin 啟用租戶後請求恢復正常
7. admin 改方案 PATCH /admin/tenants/{id} {plan: "pro"}
8. GET /admin/api-keys 跨租戶概況
9. 查不存在的 tenant_id → 404
10. 非 admin 嘗試 PATCH /admin/tenants/{id} → 403（不需存在）

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

# 確保所有 model metadata 已載入
from saas_mvp.models import tenant as _t, user as _u, note as _n, usage as _us  # noqa: F401
from saas_mvp.models import api_key as _ak, api_key_usage as _aku  # noqa: F401
from saas_mvp.models import plan_change_history as _pch  # noqa: F401
from saas_mvp.app import create_app
from saas_mvp.db import Base, get_db

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


# ── helpers ───────────────────────────────────────────────────

def _uid() -> str:
    return uuid.uuid4().hex[:8]


def _register(client: TestClient, tenant: str | None = None) -> tuple[str, str, int]:
    """回傳 (email, token, tenant_id)"""
    email = f"user_{_uid()}@example.com"
    tn = tenant or f"tenant_{_uid()}"
    r = client.post("/auth/register", json={
        "email": email, "password": "Test1234!", "tenant_name": tn,
    })
    assert r.status_code == 201, r.text
    data = r.json()
    # 取 tenant_id
    me = client.get("/tenants/me", headers={"Authorization": f"Bearer {data['access_token']}"})
    tenant_id = me.json()["id"]
    return email, data["access_token"], tenant_id


def _make_admin(client: TestClient, token: str) -> None:
    """直接透過 DB 把 user.is_admin 設為 True。"""
    from saas_mvp.auth.security import decode_access_token
    from saas_mvp.models.user import User

    payload = decode_access_token(token)
    user_id = int(payload["sub"])
    db = _Session()
    try:
        user = db.get(User, user_id)
        user.is_admin = True
        db.commit()
    finally:
        db.close()


# ── fixtures ─────────────────────────────────────────────────

@pytest.fixture(scope="module")
def normal_user(client):
    """普通使用者 (email, token, tenant_id)"""
    return _register(client)


@pytest.fixture(scope="module")
def admin_user(client):
    """Admin 使用者 (email, token, tenant_id)"""
    email, token, tenant_id = _register(client)
    _make_admin(client, token)
    return email, token, tenant_id


@pytest.fixture(scope="module")
def target_tenant(client):
    """被操作的租戶 (email, token, tenant_id)"""
    return _register(client)


# ── Tests ─────────────────────────────────────────────────────

class TestAdminAccess:
    def test_non_admin_get_tenants_403(self, client, normal_user):
        _, token, _ = normal_user
        r = client.get("/admin/tenants", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 403
        assert r.json()["detail"] == "admin required"

    def test_non_admin_patch_tenant_403(self, client, normal_user, target_tenant):
        _, token, _ = normal_user
        _, _, tid = target_tenant
        r = client.patch(
            f"/admin/tenants/{tid}",
            json={"is_active": False},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 403

    def test_no_auth_get_tenants_401(self, client):
        r = client.get("/admin/tenants")
        assert r.status_code == 401

    def test_admin_get_tenants_200(self, client, admin_user):
        _, token, _ = admin_user
        r = client.get("/admin/tenants", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200
        assert isinstance(r.json(), list)


class TestAdminTenantList:
    def test_list_contains_all_tenants(self, client, admin_user, normal_user, target_tenant):
        _, token, _ = admin_user
        r = client.get("/admin/tenants", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200
        ids = [t["id"] for t in r.json()]
        # 三個 fixture 的 tenant 都應在列表
        _, _, tid_n = normal_user
        _, _, tid_t = target_tenant
        _, _, tid_a = admin_user
        assert tid_n in ids
        assert tid_t in ids
        assert tid_a in ids

    def test_list_has_required_fields(self, client, admin_user):
        _, token, _ = admin_user
        r = client.get("/admin/tenants", headers={"Authorization": f"Bearer {token}"})
        for t in r.json():
            assert "id" in t
            assert "name" in t
            assert "plan" in t
            assert "is_active" in t

    def test_pagination_skip_limit(self, client, admin_user):
        _, token, _ = admin_user
        r_all = client.get("/admin/tenants?limit=100", headers={"Authorization": f"Bearer {token}"})
        total = len(r_all.json())
        if total >= 2:
            r_p1 = client.get("/admin/tenants?skip=0&limit=1", headers={"Authorization": f"Bearer {token}"})
            r_p2 = client.get("/admin/tenants?skip=1&limit=1", headers={"Authorization": f"Bearer {token}"})
            assert len(r_p1.json()) == 1
            assert len(r_p2.json()) == 1
            assert r_p1.json()[0]["id"] != r_p2.json()[0]["id"]


class TestAdminTenantUsage:
    def test_usage_existing_tenant(self, client, admin_user, target_tenant):
        _, token, _ = admin_user
        _, _, tid = target_tenant
        r = client.get(f"/admin/tenants/{tid}/usage", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200
        data = r.json()
        assert data["tenant_id"] == tid
        assert "today_count" in data
        assert "limit" in data
        assert "per_key" in data
        assert isinstance(data["per_key"], list)

    def test_usage_not_found_404(self, client, admin_user):
        _, token, _ = admin_user
        r = client.get("/admin/tenants/99999/usage", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 404

    def test_non_admin_usage_403(self, client, normal_user, target_tenant):
        _, token, _ = normal_user
        _, _, tid = target_tenant
        r = client.get(f"/admin/tenants/{tid}/usage", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 403


class TestAdminPatchTenant:
    def test_disable_tenant(self, client, admin_user):
        """停用一個獨立租戶，驗證後續請求被 403。"""
        _, admin_token, _ = admin_user
        # 建立被停用的測試租戶
        victim_email, victim_token, victim_tid = _register(client)

        # 先確認能正常打 /tenants/me
        r = client.get("/tenants/me", headers={"Authorization": f"Bearer {victim_token}"})
        assert r.status_code == 200

        # Admin 停用
        r = client.patch(
            f"/admin/tenants/{victim_tid}",
            json={"is_active": False},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert r.status_code == 200
        assert r.json()["is_active"] is False

        # 被停用後請求應拒絕 403
        r = client.get("/tenants/me", headers={"Authorization": f"Bearer {victim_token}"})
        assert r.status_code == 403
        assert "disabled" in r.json()["detail"]

    def test_enable_tenant(self, client, admin_user):
        """停用後再啟用，請求恢復正常。"""
        _, admin_token, _ = admin_user
        victim_email, victim_token, victim_tid = _register(client)

        # 停用
        client.patch(
            f"/admin/tenants/{victim_tid}",
            json={"is_active": False},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        # 確認已被停用
        r = client.get("/tenants/me", headers={"Authorization": f"Bearer {victim_token}"})
        assert r.status_code == 403

        # 啟用
        r = client.patch(
            f"/admin/tenants/{victim_tid}",
            json={"is_active": True},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert r.status_code == 200
        assert r.json()["is_active"] is True

        # 恢復正常
        r = client.get("/tenants/me", headers={"Authorization": f"Bearer {victim_token}"})
        assert r.status_code == 200

    def test_change_plan_upgrade(self, client, admin_user):
        """Admin 將租戶方案從 free 升至 pro。"""
        _, admin_token, _ = admin_user
        _, _, tid = _register(client)

        r = client.patch(
            f"/admin/tenants/{tid}",
            json={"plan": "pro"},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert r.status_code == 200
        assert r.json()["plan"] == "pro"

    def test_patch_not_found_404(self, client, admin_user):
        _, token, _ = admin_user
        r = client.patch(
            "/admin/tenants/99999",
            json={"is_active": True},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 404

    def test_patch_invalid_plan_400(self, client, admin_user):
        _, admin_token, _ = admin_user
        _, _, tid = _register(client)
        r = client.patch(
            f"/admin/tenants/{tid}",
            json={"plan": "enterprise"},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert r.status_code == 400

    def test_patch_both_is_active_and_plan(self, client, admin_user):
        """同時停用並改方案，兩者都應生效。"""
        _, admin_token, _ = admin_user
        _, victim_token, victim_tid = _register(client)

        r = client.patch(
            f"/admin/tenants/{victim_tid}",
            json={"is_active": False, "plan": "pro"},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert r.status_code == 200
        data = r.json()
        assert data["is_active"] is False
        assert data["plan"] == "pro"


class TestAdminApiKeys:
    def test_list_api_keys(self, client, admin_user, normal_user):
        _, admin_token, _ = admin_user
        _, user_token, _ = normal_user

        # 先建立一個 API key
        r = client.post(
            "/api-keys",
            json={"name": "test-key"},
            headers={"Authorization": f"Bearer {user_token}"},
        )
        assert r.status_code == 201

        # Admin 列出所有 key
        r = client.get("/admin/api-keys", headers={"Authorization": f"Bearer {admin_token}"})
        assert r.status_code == 200
        keys = r.json()
        assert isinstance(keys, list)
        assert len(keys) >= 1
        # 檢查欄位
        for k in keys:
            assert "id" in k
            assert "name" in k
            assert "tenant_id" in k
            assert "is_active" in k

    def test_non_admin_api_keys_403(self, client, normal_user):
        _, token, _ = normal_user
        r = client.get("/admin/api-keys", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 403

    def test_api_keys_pagination(self, client, admin_user):
        _, token, _ = admin_user
        r = client.get("/admin/api-keys?limit=1", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200
        assert len(r.json()) <= 1
