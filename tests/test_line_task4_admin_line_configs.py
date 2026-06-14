"""Task #4 驗收測試 — Admin LINE Channel Config 端點

端點涵蓋：
  GET    /admin/line-configs/{tenant_id}
  PUT    /admin/line-configs/{tenant_id}
  DELETE /admin/line-configs/{tenant_id}

驗收標準：
  1. 非 admin → 403
  2. Admin 可建立 LINE 設定（PUT），回應不含 secret/token 明文
  3. Admin 可查詢 LINE 設定（GET），has_channel_secret / has_access_token 正確
  4. Admin 可更新 LINE 設定（PUT 再次呼叫），設定覆寫
  5. Admin 可刪除 LINE 設定（DELETE）
  6. 查詢不存在的 tenant → 404
  7. 查詢未設定 LINE config 的 tenant → 404
  8. 刪除未設定 LINE config 的 tenant → 404
  9. 無效 BCP-47 lang → 400
 10. 跨租戶隔離：無法用他人 tenant_id 讀取（admin 例外因可查全部）
 11. 未登入 → 401
 12. 建立後 has_channel_secret/has_access_token 均為 true
 13. response 絕對不含 channel_secret / access_token 明文欄位

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
from saas_mvp.models import tenant as _t, user as _u, note as _n, usage as _us  # noqa: F401
from saas_mvp.models import api_key as _ak, api_key_usage as _aku               # noqa: F401
from saas_mvp.models import plan_change_history as _pch                          # noqa: F401
import saas_mvp.models.line_channel_config as _lcm                               # noqa: F401

from saas_mvp.app import create_app
from saas_mvp.db import Base, get_db

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


def _register(client: TestClient) -> tuple[str, str, int]:
    """(email, token, tenant_id)"""
    email = f"user_{_uid()}@example.com"
    tn = f"tenant_{_uid()}"
    r = client.post("/auth/register", json={
        "email": email, "password": "Test1234!", "tenant_name": tn,
    })
    assert r.status_code == 201, r.text
    token = r.json()["access_token"]
    me = client.get("/tenants/me", headers={"Authorization": f"Bearer {token}"})
    tenant_id = me.json()["id"]
    return email, token, tenant_id


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


# ── fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def admin(client):
    _, token, tid = _register(client)
    _make_admin(token)
    return token, tid


@pytest.fixture(scope="module")
def normal(client):
    _, token, tid = _register(client)
    return token, tid


@pytest.fixture(scope="module")
def target_tenant(client):
    """被操作的租戶（無 LINE config）"""
    _, token, tid = _register(client)
    return token, tid


# ── 授權邊界 ──────────────────────────────────────────────────────────────────

class TestAuthBoundary:
    def test_no_auth_get_401(self, client, target_tenant):
        _, tid = target_tenant
        r = client.get(f"/admin/line-configs/{tid}")
        assert r.status_code == 401

    def test_no_auth_put_401(self, client, target_tenant):
        _, tid = target_tenant
        r = client.put(f"/admin/line-configs/{tid}", json={
            "channel_secret": "s", "access_token": "t",
        })
        assert r.status_code == 401

    def test_normal_user_get_403(self, client, normal, target_tenant):
        token, _ = normal
        _, tid = target_tenant
        r = client.get(f"/admin/line-configs/{tid}", headers=_auth(token))
        assert r.status_code == 403

    def test_normal_user_put_403(self, client, normal, target_tenant):
        token, _ = normal
        _, tid = target_tenant
        r = client.put(f"/admin/line-configs/{tid}", headers=_auth(token), json={
            "channel_secret": "s", "access_token": "t",
        })
        assert r.status_code == 403

    def test_normal_user_delete_403(self, client, normal, target_tenant):
        token, _ = normal
        _, tid = target_tenant
        r = client.delete(f"/admin/line-configs/{tid}", headers=_auth(token))
        assert r.status_code == 403


# ── 404 場景 ──────────────────────────────────────────────────────────────────

class TestNotFound:
    def test_get_nonexistent_tenant_404(self, client, admin):
        token, _ = admin
        r = client.get("/admin/line-configs/99999", headers=_auth(token))
        assert r.status_code == 404

    def test_put_nonexistent_tenant_404(self, client, admin):
        token, _ = admin
        r = client.put("/admin/line-configs/99999", headers=_auth(token), json={
            "channel_secret": "s", "access_token": "t",
        })
        assert r.status_code == 404

    def test_delete_nonexistent_tenant_404(self, client, admin):
        token, _ = admin
        r = client.delete("/admin/line-configs/99999", headers=_auth(token))
        assert r.status_code == 404

    def test_get_tenant_without_config_404(self, client, admin, target_tenant):
        """存在的 tenant 但尚未設定 LINE config → 404"""
        token, _ = admin
        _, tid = target_tenant
        r = client.get(f"/admin/line-configs/{tid}", headers=_auth(token))
        assert r.status_code == 404
        assert "line channel config" in r.json()["detail"].lower()

    def test_delete_tenant_without_config_404(self, client, admin, target_tenant):
        token, _ = admin
        _, tid = target_tenant
        r = client.delete(f"/admin/line-configs/{tid}", headers=_auth(token))
        assert r.status_code == 404


# ── 建立與查詢 ─────────────────────────────────────────────────────────────────

class TestUpsertAndGet:
    @pytest.fixture(scope="class")
    def setup_config(self, client, admin, target_tenant):
        """建立 LINE config，回傳 (admin_token, tenant_id, response_data)"""
        token, _ = admin
        _, tid = target_tenant
        r = client.put(
            f"/admin/line-configs/{tid}",
            headers=_auth(token),
            json={
                "channel_secret": "my-channel-secret-123",
                "access_token": "my-access-token-456",
                "default_target_lang": "ja",
            },
        )
        assert r.status_code == 200, r.text
        return token, tid, r.json()

    def test_put_returns_200(self, client, admin, target_tenant):
        token, _ = admin
        _, tid = target_tenant
        r = client.put(
            f"/admin/line-configs/{tid}",
            headers=_auth(token),
            json={"channel_secret": "s", "access_token": "t"},
        )
        assert r.status_code == 200

    def test_response_has_correct_tenant_id(self, client, setup_config):
        _, tid, data = setup_config
        assert data["tenant_id"] == tid

    def test_response_has_secret_masked(self, client, setup_config):
        _, _, data = setup_config
        assert data["has_channel_secret"] is True
        assert data["has_access_token"] is True

    def test_response_no_plaintext_secret(self, client, setup_config):
        """絕對不能有 channel_secret / access_token 明文欄位"""
        _, _, data = setup_config
        assert "channel_secret" not in data
        assert "access_token" not in data

    def test_response_default_lang(self, client, setup_config):
        _, _, data = setup_config
        assert data["default_target_lang"] == "ja"

    def test_response_has_timestamps(self, client, setup_config):
        _, _, data = setup_config
        assert data["created_at"] is not None
        assert data["updated_at"] is not None

    def test_get_after_put_200(self, client, admin, target_tenant):
        token, _ = admin
        _, tid = target_tenant
        # PUT first
        client.put(
            f"/admin/line-configs/{tid}",
            headers=_auth(token),
            json={"channel_secret": "s2", "access_token": "t2", "default_target_lang": "en"},
        )
        # GET
        r = client.get(f"/admin/line-configs/{tid}", headers=_auth(token))
        assert r.status_code == 200
        data = r.json()
        assert data["tenant_id"] == tid
        assert data["has_channel_secret"] is True
        assert data["has_access_token"] is True
        assert "channel_secret" not in data
        assert "access_token" not in data

    def test_get_reflects_updated_lang(self, client, admin, target_tenant):
        token, _ = admin
        _, tid = target_tenant
        client.put(
            f"/admin/line-configs/{tid}",
            headers=_auth(token),
            json={"channel_secret": "s", "access_token": "t", "default_target_lang": "ko"},
        )
        r = client.get(f"/admin/line-configs/{tid}", headers=_auth(token))
        assert r.json()["default_target_lang"] == "ko"


# ── 更新語義（upsert） ────────────────────────────────────────────────────────

class TestUpsertSemantics:
    def test_second_put_overwrites(self, client, admin):
        """同一 tenant PUT 兩次不應重複建立，而是更新。"""
        token, _ = admin
        _, _, fresh_tid = _register(client)

        # 第一次
        r1 = client.put(
            f"/admin/line-configs/{fresh_tid}",
            headers=_auth(token),
            json={"channel_secret": "first-secret", "access_token": "first-token",
                  "default_target_lang": "zh-TW"},
        )
        assert r1.status_code == 200

        # 第二次（更新）
        r2 = client.put(
            f"/admin/line-configs/{fresh_tid}",
            headers=_auth(token),
            json={"channel_secret": "second-secret", "access_token": "second-token",
                  "default_target_lang": "en"},
        )
        assert r2.status_code == 200
        assert r2.json()["default_target_lang"] == "en"

        # GET 仍只有一筆
        r_get = client.get(f"/admin/line-configs/{fresh_tid}", headers=_auth(token))
        assert r_get.status_code == 200
        assert r_get.json()["default_target_lang"] == "en"

    def test_default_lang_defaults_to_zh_tw(self, client, admin):
        token, _ = admin
        _, _, fresh_tid = _register(client)
        r = client.put(
            f"/admin/line-configs/{fresh_tid}",
            headers=_auth(token),
            json={"channel_secret": "s", "access_token": "t"},
        )
        assert r.status_code == 200
        assert r.json()["default_target_lang"] == "zh-TW"


# ── 刪除 ──────────────────────────────────────────────────────────────────────

class TestDelete:
    def test_delete_existing_config(self, client, admin):
        token, _ = admin
        _, _, fresh_tid = _register(client)

        # 先建立
        client.put(
            f"/admin/line-configs/{fresh_tid}",
            headers=_auth(token),
            json={"channel_secret": "s", "access_token": "t"},
        )

        # 刪除
        r = client.delete(f"/admin/line-configs/{fresh_tid}", headers=_auth(token))
        assert r.status_code == 200
        assert r.json()["tenant_id"] == fresh_tid

        # 再查應 404
        r2 = client.get(f"/admin/line-configs/{fresh_tid}", headers=_auth(token))
        assert r2.status_code == 404

    def test_delete_twice_second_is_404(self, client, admin):
        token, _ = admin
        _, _, fresh_tid = _register(client)
        client.put(
            f"/admin/line-configs/{fresh_tid}",
            headers=_auth(token),
            json={"channel_secret": "s", "access_token": "t"},
        )
        client.delete(f"/admin/line-configs/{fresh_tid}", headers=_auth(token))
        r = client.delete(f"/admin/line-configs/{fresh_tid}", headers=_auth(token))
        assert r.status_code == 404


# ── BCP-47 驗證 ───────────────────────────────────────────────────────────────

class TestBCP47Validation:
    def test_invalid_lang_400(self, client, admin):
        token, _ = admin
        _, _, fresh_tid = _register(client)
        r = client.put(
            f"/admin/line-configs/{fresh_tid}",
            headers=_auth(token),
            json={"channel_secret": "s", "access_token": "t",
                  "default_target_lang": "not valid!"},
        )
        assert r.status_code == 400
        assert "BCP-47" in r.json()["detail"] or "Invalid" in r.json()["detail"]

    def test_empty_lang_400(self, client, admin):
        token, _ = admin
        _, _, fresh_tid = _register(client)
        r = client.put(
            f"/admin/line-configs/{fresh_tid}",
            headers=_auth(token),
            json={"channel_secret": "s", "access_token": "t",
                  "default_target_lang": ""},
        )
        assert r.status_code == 400

    def test_valid_lang_en_200(self, client, admin):
        token, _ = admin
        _, _, fresh_tid = _register(client)
        r = client.put(
            f"/admin/line-configs/{fresh_tid}",
            headers=_auth(token),
            json={"channel_secret": "s", "access_token": "t",
                  "default_target_lang": "en"},
        )
        assert r.status_code == 200

    def test_valid_lang_zh_tw_200(self, client, admin):
        token, _ = admin
        _, _, fresh_tid = _register(client)
        r = client.put(
            f"/admin/line-configs/{fresh_tid}",
            headers=_auth(token),
            json={"channel_secret": "s", "access_token": "t",
                  "default_target_lang": "zh-TW"},
        )
        assert r.status_code == 200


# ── PUT body 欄位驗證（Pydantic min_length） ──────────────────────────────────

class TestPutBodyValidation:
    def test_empty_channel_secret_422(self, client, admin):
        """channel_secret 空字串 → Pydantic min_length=1 → 422。"""
        token, _ = admin
        _, _, fresh_tid = _register(client)
        r = client.put(
            f"/admin/line-configs/{fresh_tid}",
            headers=_auth(token),
            json={"channel_secret": "", "access_token": "valid-token"},
        )
        assert r.status_code == 422

    def test_empty_access_token_422(self, client, admin):
        """access_token 空字串 → Pydantic min_length=1 → 422。"""
        token, _ = admin
        _, _, fresh_tid = _register(client)
        r = client.put(
            f"/admin/line-configs/{fresh_tid}",
            headers=_auth(token),
            json={"channel_secret": "valid-secret", "access_token": ""},
        )
        assert r.status_code == 422

    def test_both_empty_422(self, client, admin):
        """兩欄位皆空 → 422。"""
        token, _ = admin
        _, _, fresh_tid = _register(client)
        r = client.put(
            f"/admin/line-configs/{fresh_tid}",
            headers=_auth(token),
            json={"channel_secret": "", "access_token": ""},
        )
        assert r.status_code == 422

    def test_missing_channel_secret_422(self, client, admin):
        """缺少必要欄位 channel_secret → 422。"""
        token, _ = admin
        _, _, fresh_tid = _register(client)
        r = client.put(
            f"/admin/line-configs/{fresh_tid}",
            headers=_auth(token),
            json={"access_token": "t"},
        )
        assert r.status_code == 422

    def test_missing_access_token_422(self, client, admin):
        """缺少必要欄位 access_token → 422。"""
        token, _ = admin
        _, _, fresh_tid = _register(client)
        r = client.put(
            f"/admin/line-configs/{fresh_tid}",
            headers=_auth(token),
            json={"channel_secret": "s"},
        )
        assert r.status_code == 422


# ── 跨租戶隔離 ────────────────────────────────────────────────────────────────

class TestCrossTenantIsolation:
    def test_admin_can_read_any_tenant_config(self, client, admin):
        """Admin 可讀取任意租戶的設定（不限自己的 tenant）。"""
        token, _ = admin
        _, _, other_tid = _register(client)
        # 建立 other tenant 的設定
        client.put(
            f"/admin/line-configs/{other_tid}",
            headers=_auth(token),
            json={"channel_secret": "x", "access_token": "y"},
        )
        # Admin 可讀到
        r = client.get(f"/admin/line-configs/{other_tid}", headers=_auth(token))
        assert r.status_code == 200

    def test_normal_user_cannot_read_own_config_via_admin_route(self, client, normal):
        """普通使用者不能走 /admin 路由讀自己的設定。"""
        token, tid = normal
        r = client.get(f"/admin/line-configs/{tid}", headers=_auth(token))
        assert r.status_code == 403

    def test_configs_are_per_tenant(self, client, admin):
        """兩個租戶各自建立設定，互不干擾。"""
        token, _ = admin
        _, _, tid_a = _register(client)
        _, _, tid_b = _register(client)

        client.put(f"/admin/line-configs/{tid_a}", headers=_auth(token),
                   json={"channel_secret": "secret-a", "access_token": "token-a",
                         "default_target_lang": "ja"})
        client.put(f"/admin/line-configs/{tid_b}", headers=_auth(token),
                   json={"channel_secret": "secret-b", "access_token": "token-b",
                         "default_target_lang": "en"})

        ra = client.get(f"/admin/line-configs/{tid_a}", headers=_auth(token))
        rb = client.get(f"/admin/line-configs/{tid_b}", headers=_auth(token))

        assert ra.json()["default_target_lang"] == "ja"
        assert rb.json()["default_target_lang"] == "en"
        assert ra.json()["tenant_id"] == tid_a
        assert rb.json()["tenant_id"] == tid_b
