"""Task #2 驗收測試：API key 管理端點

驗收標準（逐條覆蓋）：
A. 建立 → 回應含 plain_key（只此一次）；key 格式 myapp_*
B. 列出 → 只回 key_prefix，不含 plain_key 或 key_hash
C. DB   → 只存 key_hash（sha256），不存明文
D. 撤銷 → is_active=False 軟刪除，下一次認證立即 401
E. 軟刪除後 usage 歷史仍保留（api_key_usage 列不消失）
F. 跨租戶：B 不可撤銷 A 的 key（回 404）
G. 未認證建立/列出/撤銷 → 401

全部離線，in-memory SQLite。
"""
from __future__ import annotations

import hashlib
import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("SAAS_RATE_LIMIT_ENABLED", "false")

from saas_mvp.models import tenant as _t, user as _u, note as _n, usage as _us  # noqa: F401
from saas_mvp.models import api_key as _ak, api_key_usage as _aku  # noqa: F401
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


# ── 工具函式 ──────────────────────────────────────────────────────────────────

def _register(client, email: str, password: str, tenant: str) -> str:
    resp = client.post("/auth/register", json={
        "email": email, "password": password, "tenant_name": tenant,
    })
    assert resp.status_code == 201, f"register failed: {resp.text}"
    return resp.json()["access_token"]


def _auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _create_key(client, jwt_token: str, name: str = "test-key") -> dict:
    resp = client.post(
        "/api-keys/",
        json={"name": name},
        headers=_auth_headers(jwt_token),
    )
    assert resp.status_code == 201, f"create key failed: {resp.text}"
    return resp.json()


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def alice_jwt(client):
    return _register(client, "alice_t2@test.com", "AlicePass99!", "t2-alpha")


@pytest.fixture(scope="module")
def bob_jwt(client):
    return _register(client, "bob_t2@test.com", "BobPass99!!", "t2-beta")


# ═══════════════════════════════════════════════════════════════════════════════
# A. 建立端點
# ═══════════════════════════════════════════════════════════════════════════════

class TestCreateKey:
    """POST /api-keys/ — 驗收標準 A + G"""

    def test_create_returns_201(self, client, alice_jwt):
        resp = client.post("/api-keys/", json={"name": "a1"},
                           headers=_auth_headers(alice_jwt))
        assert resp.status_code == 201

    def test_create_response_contains_plain_key(self, client, alice_jwt):
        """回應必須含 plain_key（只此一次回傳）。"""
        data = _create_key(client, alice_jwt, "a2")
        assert "plain_key" in data, "建立回應應含 plain_key"

    def test_plain_key_format_myapp_prefix(self, client, alice_jwt):
        """plain_key 必須以 myapp_ 開頭。"""
        data = _create_key(client, alice_jwt, "a3")
        assert data["plain_key"].startswith("myapp_"), \
            f"key 格式錯誤：{data['plain_key'][:10]}…"

    def test_plain_key_length_reasonable(self, client, alice_jwt):
        """myapp_（6） + token_urlsafe(32)（≈43 chars）≈ 49 chars。"""
        data = _create_key(client, alice_jwt, "a4")
        assert len(data["plain_key"]) >= 40

    def test_create_response_has_key_prefix(self, client, alice_jwt):
        """回應含 key_prefix（隨機部分前 8 字元）。"""
        data = _create_key(client, alice_jwt, "a5")
        assert "key_prefix" in data
        assert len(data["key_prefix"]) == 8
        # key_prefix == plain_key[6:14]
        assert data["key_prefix"] == data["plain_key"][6:14]

    def test_create_response_no_key_hash(self, client, alice_jwt):
        """建立回應不得含 key_hash（資安要求）。"""
        data = _create_key(client, alice_jwt, "a6")
        assert "key_hash" not in data, "回應不應洩漏 key_hash"

    def test_create_response_has_id_and_name(self, client, alice_jwt):
        data = _create_key(client, alice_jwt, "a7")
        assert "id" in data
        assert data["name"] == "a7"

    def test_create_without_auth_401(self, client):
        """未認證建立 → 401。"""
        resp = client.post("/api-keys/", json={"name": "ghost"})
        assert resp.status_code == 401


# ═══════════════════════════════════════════════════════════════════════════════
# B. 列出端點
# ═══════════════════════════════════════════════════════════════════════════════

class TestListKeys:
    """GET /api-keys/ — 驗收標準 B + G"""

    def test_list_returns_200(self, client, alice_jwt):
        resp = client.get("/api-keys/", headers=_auth_headers(alice_jwt))
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    def test_list_excludes_plain_key(self, client, alice_jwt):
        """列出回應不得含 plain_key。"""
        _create_key(client, alice_jwt, "b1")
        resp = client.get("/api-keys/", headers=_auth_headers(alice_jwt))
        for item in resp.json():
            assert "plain_key" not in item, "列出不應包含 plain_key"

    def test_list_excludes_key_hash(self, client, alice_jwt):
        """列出回應不得含 key_hash。"""
        resp = client.get("/api-keys/", headers=_auth_headers(alice_jwt))
        for item in resp.json():
            assert "key_hash" not in item, "列出不應包含 key_hash"

    def test_list_includes_key_prefix(self, client, alice_jwt):
        """列出回應必須含 key_prefix。"""
        resp = client.get("/api-keys/", headers=_auth_headers(alice_jwt))
        assert resp.json(), "應至少有一筆 key"
        for item in resp.json():
            assert "key_prefix" in item

    def test_list_includes_required_fields(self, client, alice_jwt):
        """列出項目包含 id / name / key_prefix / is_active / created_at。"""
        resp = client.get("/api-keys/", headers=_auth_headers(alice_jwt))
        for item in resp.json():
            for field in ("id", "name", "key_prefix", "is_active", "created_at"):
                assert field in item, f"列出項目缺少欄位 {field}"

    def test_list_without_auth_401(self, client):
        resp = client.get("/api-keys/")
        assert resp.status_code == 401

    def test_list_only_shows_own_tenant_keys(self, client, alice_jwt, bob_jwt):
        """Bob 的列出不應包含 Alice 的 key。"""
        alice_key = _create_key(client, alice_jwt, "b-isolation")
        alice_id = alice_key["id"]

        bob_keys = client.get("/api-keys/", headers=_auth_headers(bob_jwt)).json()
        bob_ids = {k["id"] for k in bob_keys}
        assert alice_id not in bob_ids, "Bob 列出不應看到 Alice 的 key"


# ═══════════════════════════════════════════════════════════════════════════════
# C. DB 僅存 key_hash（不含明文）
# ═══════════════════════════════════════════════════════════════════════════════

class TestDbOnlyStoresHash:
    """驗收標準 C — 直接查 DB 驗證欄位值。"""

    def test_db_stores_key_hash_not_plaintext(self, client, alice_jwt):
        """DB api_keys 表只存 key_hash，不存 plain_key。"""
        data = _create_key(client, alice_jwt, "c1")
        plain_key = data["plain_key"]
        expected_hash = hashlib.sha256(plain_key.encode()).hexdigest()
        key_id = data["id"]

        with _engine.connect() as conn:
            row = conn.execute(
                text("SELECT key_hash FROM api_keys WHERE id = :id"),
                {"id": key_id},
            ).fetchone()

        assert row is not None, "key 應在 DB 中"
        actual_hash = row[0]
        # DB 存的是 sha256 hex
        assert actual_hash == expected_hash, \
            f"DB hash 不符：expected {expected_hash[:16]}… got {actual_hash[:16]}…"
        # DB 不能存明文
        assert actual_hash != plain_key, "DB 不應存明文 key"
        assert len(actual_hash) == 64, "sha256 hex 應為 64 chars"

    def test_db_columns_no_plain_key_column(self, client):
        """api_keys 表不應有名為 plain_key 的欄位。"""
        with _engine.connect() as conn:
            cols = [row[1] for row in conn.execute(
                text("PRAGMA table_info(api_keys)")
            ).fetchall()]
        assert "plain_key" not in cols, "api_keys 表不應有 plain_key 欄位"
        assert "key_hash" in cols, "api_keys 表應有 key_hash 欄位"


# ═══════════════════════════════════════════════════════════════════════════════
# D. 撤銷 → 立即失效（401）
# ═══════════════════════════════════════════════════════════════════════════════

class TestRevokeKey:
    """驗收標準 D"""

    def test_revoke_returns_204(self, client, alice_jwt):
        key = _create_key(client, alice_jwt, "d-revoke")
        resp = client.delete(f"/api-keys/{key['id']}",
                             headers=_auth_headers(alice_jwt))
        assert resp.status_code == 204

    def test_revoked_key_immediately_401_x_api_key(self, client, alice_jwt):
        """撤銷後用 X-API-Key 呼叫 → 401（立即失效）。"""
        key = _create_key(client, alice_jwt, "d-xapi")
        plain = key["plain_key"]
        key_id = key["id"]

        # 確認撤銷前可以認證
        r_before = client.get("/notes/", headers={"X-API-Key": plain})
        assert r_before.status_code == 200, "撤銷前應可認證"

        # 撤銷
        client.delete(f"/api-keys/{key_id}", headers=_auth_headers(alice_jwt))

        # 撤銷後應失效
        r_after = client.get("/notes/", headers={"X-API-Key": plain})
        assert r_after.status_code == 401, \
            f"撤銷後應 401，實際得 {r_after.status_code}"

    def test_revoked_key_immediately_401_bearer(self, client, alice_jwt):
        """撤銷後用 Bearer <api_key> 呼叫 → 401。"""
        key = _create_key(client, alice_jwt, "d-bearer")
        plain = key["plain_key"]
        key_id = key["id"]

        client.delete(f"/api-keys/{key_id}", headers=_auth_headers(alice_jwt))

        r = client.get("/notes/", headers={"Authorization": f"Bearer {plain}"})
        assert r.status_code == 401, \
            f"撤銷後 Bearer 認證應 401，實際得 {r.status_code}"

    def test_revoke_sets_is_active_false_in_db(self, client, alice_jwt):
        """撤銷後 DB 的 is_active 應為 0（False），而非實體刪除。"""
        key = _create_key(client, alice_jwt, "d-soft")
        key_id = key["id"]

        client.delete(f"/api-keys/{key_id}", headers=_auth_headers(alice_jwt))

        with _engine.connect() as conn:
            row = conn.execute(
                text("SELECT is_active FROM api_keys WHERE id = :id"),
                {"id": key_id},
            ).fetchone()

        assert row is not None, "軟刪除：row 應仍存在 DB"
        assert row[0] == 0, f"is_active 應為 0 (False)，得 {row[0]}"

    def test_revoke_nonexistent_key_404(self, client, alice_jwt):
        resp = client.delete("/api-keys/99999",
                             headers=_auth_headers(alice_jwt))
        assert resp.status_code == 404

    def test_revoke_without_auth_401(self, client):
        resp = client.delete("/api-keys/1")
        assert resp.status_code == 401


# ═══════════════════════════════════════════════════════════════════════════════
# E. 軟刪除後 usage 歷史記錄仍保留
# ═══════════════════════════════════════════════════════════════════════════════

class TestSoftDeletePreservesUsage:
    """驗收標準 E"""

    def test_usage_row_survives_revoke(self, client, alice_jwt):
        """撤銷後，api_key_usage 中的計量列不被刪除。"""
        key = _create_key(client, alice_jwt, "e-usage-preserve")
        plain = key["plain_key"]
        key_id = key["id"]

        # 先呼叫兩次，產生 usage 記錄
        for _ in range(2):
            client.get("/notes/", headers={"X-API-Key": plain})

        # 確認 usage 存在
        with _engine.connect() as conn:
            before = conn.execute(
                text("SELECT count FROM api_key_usage WHERE api_key_id = :kid"),
                {"kid": key_id},
            ).fetchone()
        assert before is not None, "呼叫後應有 usage 記錄"
        assert before[0] == 2, f"usage count 應為 2，得 {before[0]}"

        # 撤銷
        client.delete(f"/api-keys/{key_id}", headers=_auth_headers(alice_jwt))

        # 撤銷後 usage 仍在
        with _engine.connect() as conn:
            after = conn.execute(
                text("SELECT count FROM api_key_usage WHERE api_key_id = :kid"),
                {"kid": key_id},
            ).fetchone()
        assert after is not None, "撤銷後 usage 記錄應仍保留（軟刪除）"
        assert after[0] == 2, f"撤銷後 usage count 應仍為 2，得 {after[0]}"

    def test_api_key_row_survives_revoke(self, client, alice_jwt):
        """軟刪除：api_keys 本身 row 不消失，is_active=False。"""
        key = _create_key(client, alice_jwt, "e-row-preserve")
        key_id = key["id"]

        client.delete(f"/api-keys/{key_id}", headers=_auth_headers(alice_jwt))

        with _engine.connect() as conn:
            row = conn.execute(
                text("SELECT id, is_active FROM api_keys WHERE id = :id"),
                {"id": key_id},
            ).fetchone()

        assert row is not None, "軟刪除後 row 應仍在 DB"
        assert row[0] == key_id
        assert row[1] == 0, "is_active 應為 False"


# ═══════════════════════════════════════════════════════════════════════════════
# F. 跨租戶隔離
# ═══════════════════════════════════════════════════════════════════════════════

class TestCrossTenantRevoke:
    """驗收標準 F — 跨租戶 404"""

    def test_bob_cannot_revoke_alice_key(self, client, alice_jwt, bob_jwt):
        """Bob 撤銷 Alice 的 key 應得 404（不洩漏存在性）。"""
        alice_key = _create_key(client, alice_jwt, "f-protected")
        resp = client.delete(
            f"/api-keys/{alice_key['id']}",
            headers=_auth_headers(bob_jwt),
        )
        assert resp.status_code == 404, \
            f"跨租戶撤銷應 404，實際得 {resp.status_code}"

    def test_alice_key_still_active_after_bob_attempt(self, client, alice_jwt, bob_jwt):
        """Bob 嘗試後，Alice 的 key 仍應可用。"""
        alice_key = _create_key(client, alice_jwt, "f-still-active")
        plain = alice_key["plain_key"]
        key_id = alice_key["id"]

        # Bob 嘗試撤銷
        client.delete(f"/api-keys/{key_id}", headers=_auth_headers(bob_jwt))

        # Alice 的 key 仍有效
        r = client.get("/notes/", headers={"X-API-Key": plain})
        assert r.status_code == 200, \
            f"Bob 無效撤銷後，Alice key 仍應有效，得 {r.status_code}"
