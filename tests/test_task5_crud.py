"""Task #5 驗收測試：核心資源 CRUD REST API（Notes）

覆蓋範圍
--------
1. CREATE  POST /notes/       → 201、欄位正確
2. READ    GET  /notes/{id}   → 200、404（不存在）
3. LIST    GET  /notes/       → 200、列表結構
4. UPDATE  PUT  /notes/{id}   → 200、部分更新、404
5. DELETE  DEL  /notes/{id}   → 204、再刪 404
6. Auth 防線：所有端點無 token → 401
7. Quota 防線：所有端點超量 → 429（包括讀/更新/刪除）
8. 租戶隔離：跨租戶 CRUD 全部 404
9. 輸入驗證：欄位格式錯誤 → 422

全部離線、使用 in-memory SQLite。
"""

from __future__ import annotations

import datetime
import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("SAAS_RATE_LIMIT_ENABLED", "false")

# 確保所有 model metadata 已載入
from saas_mvp.models import tenant as _t, user as _u, note as _n, usage as _us  # noqa: F401
from saas_mvp.app import create_app
from saas_mvp.db import Base, get_db
from saas_mvp.quota import PLAN_DAILY_LIMITS


# ──────────────────────────── 共用 Fixtures ──────────────────────────────────

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


def _register(client, email: str, password: str, tenant: str) -> str:
    """註冊並回傳 access_token。"""
    resp = client.post("/auth/register", json={
        "email": email,
        "password": password,
        "tenant_name": tenant,
    })
    assert resp.status_code == 201, resp.text
    return resp.json()["access_token"]


@pytest.fixture(scope="module")
def alice_token(client):
    return _register(client, "alice5@test.com", "AlicePass99!", "crud-alpha")


@pytest.fixture(scope="module")
def bob_token(client):
    return _register(client, "bob5@test.com", "BobPass99!!", "crud-beta")


# ──────────────────────────── 1. CREATE ──────────────────────────────────────

class TestCreate:
    def test_create_returns_201(self, client, alice_token):
        resp = client.post(
            "/notes/",
            json={"title": "First Note", "content": "Hello World"},
            headers={"Authorization": f"Bearer {alice_token}"},
        )
        assert resp.status_code == 201

    def test_create_response_fields(self, client, alice_token):
        resp = client.post(
            "/notes/",
            json={"title": "Field Check", "content": "content here"},
            headers={"Authorization": f"Bearer {alice_token}"},
        )
        data = resp.json()
        assert "id" in data
        assert data["title"] == "Field Check"
        assert data["content"] == "content here"
        assert "owner_id" in data
        assert "tenant_id" in data

    def test_create_default_empty_content(self, client, alice_token):
        """content 可省略，預設為空字串。"""
        resp = client.post(
            "/notes/",
            json={"title": "No Content Note"},
            headers={"Authorization": f"Bearer {alice_token}"},
        )
        assert resp.status_code == 201
        assert resp.json()["content"] == ""

    def test_create_without_token_returns_401(self, client):
        resp = client.post("/notes/", json={"title": "Ghost Note"})
        assert resp.status_code == 401

    def test_create_empty_title_returns_422(self, client, alice_token):
        resp = client.post(
            "/notes/",
            json={"title": "", "content": "x"},
            headers={"Authorization": f"Bearer {alice_token}"},
        )
        assert resp.status_code == 422

    def test_create_missing_title_returns_422(self, client, alice_token):
        resp = client.post(
            "/notes/",
            json={"content": "no title"},
            headers={"Authorization": f"Bearer {alice_token}"},
        )
        assert resp.status_code == 422


# ──────────────────────────── 2. READ (single) ───────────────────────────────

class TestReadSingle:
    @pytest.fixture(scope="class")
    def note_id(self, client, alice_token):
        resp = client.post(
            "/notes/",
            json={"title": "Readable Note", "content": "read me"},
            headers={"Authorization": f"Bearer {alice_token}"},
        )
        return resp.json()["id"]

    def test_get_existing_note_returns_200(self, client, alice_token, note_id):
        resp = client.get(
            f"/notes/{note_id}",
            headers={"Authorization": f"Bearer {alice_token}"},
        )
        assert resp.status_code == 200

    def test_get_note_correct_fields(self, client, alice_token, note_id):
        data = client.get(
            f"/notes/{note_id}",
            headers={"Authorization": f"Bearer {alice_token}"},
        ).json()
        assert data["id"] == note_id
        assert data["title"] == "Readable Note"
        assert data["content"] == "read me"

    def test_get_nonexistent_returns_404(self, client, alice_token):
        resp = client.get(
            "/notes/999999",
            headers={"Authorization": f"Bearer {alice_token}"},
        )
        assert resp.status_code == 404

    def test_get_without_token_returns_401(self, client, note_id):
        resp = client.get(f"/notes/{note_id}")
        assert resp.status_code == 401


# ──────────────────────────── 3. LIST ────────────────────────────────────────

class TestList:
    def test_list_returns_200(self, client, alice_token):
        resp = client.get("/notes/", headers={"Authorization": f"Bearer {alice_token}"})
        assert resp.status_code == 200

    def test_list_returns_array(self, client, alice_token):
        resp = client.get("/notes/", headers={"Authorization": f"Bearer {alice_token}"})
        assert isinstance(resp.json(), list)

    def test_list_contains_own_notes(self, client, alice_token):
        """建立後確認 list 包含它。"""
        create_resp = client.post(
            "/notes/",
            json={"title": "Listed Note", "content": "yes"},
            headers={"Authorization": f"Bearer {alice_token}"},
        )
        note_id = create_resp.json()["id"]
        notes = client.get("/notes/", headers={"Authorization": f"Bearer {alice_token}"}).json()
        ids = [n["id"] for n in notes]
        assert note_id in ids

    def test_list_items_have_correct_schema(self, client, alice_token):
        notes = client.get("/notes/", headers={"Authorization": f"Bearer {alice_token}"}).json()
        assert len(notes) > 0
        for note in notes:
            assert "id" in note
            assert "title" in note
            assert "content" in note
            assert "owner_id" in note
            assert "tenant_id" in note

    def test_list_without_token_returns_401(self, client):
        resp = client.get("/notes/")
        assert resp.status_code == 401


# ──────────────────────────── 4. UPDATE ──────────────────────────────────────

class TestUpdate:
    @pytest.fixture(scope="class")
    def note_id(self, client, alice_token):
        resp = client.post(
            "/notes/",
            json={"title": "Original Title", "content": "original content"},
            headers={"Authorization": f"Bearer {alice_token}"},
        )
        return resp.json()["id"]

    def test_update_returns_200(self, client, alice_token, note_id):
        resp = client.put(
            f"/notes/{note_id}",
            json={"title": "Updated Title"},
            headers={"Authorization": f"Bearer {alice_token}"},
        )
        assert resp.status_code == 200

    def test_update_title_only(self, client, alice_token, note_id):
        resp = client.put(
            f"/notes/{note_id}",
            json={"title": "New Title Only"},
            headers={"Authorization": f"Bearer {alice_token}"},
        )
        data = resp.json()
        assert data["title"] == "New Title Only"
        assert data["content"] == "original content"  # content 未改動

    def test_update_content_only(self, client, alice_token, note_id):
        resp = client.put(
            f"/notes/{note_id}",
            json={"content": "new content only"},
            headers={"Authorization": f"Bearer {alice_token}"},
        )
        data = resp.json()
        assert data["content"] == "new content only"

    def test_update_both_fields(self, client, alice_token, note_id):
        resp = client.put(
            f"/notes/{note_id}",
            json={"title": "Full Update", "content": "full content"},
            headers={"Authorization": f"Bearer {alice_token}"},
        )
        data = resp.json()
        assert data["title"] == "Full Update"
        assert data["content"] == "full content"

    def test_update_nonexistent_returns_404(self, client, alice_token):
        resp = client.put(
            "/notes/999999",
            json={"title": "Ghost"},
            headers={"Authorization": f"Bearer {alice_token}"},
        )
        assert resp.status_code == 404

    def test_update_without_token_returns_401(self, client, note_id):
        resp = client.put(f"/notes/{note_id}", json={"title": "Hack"})
        assert resp.status_code == 401

    def test_update_empty_title_returns_422(self, client, alice_token, note_id):
        resp = client.put(
            f"/notes/{note_id}",
            json={"title": ""},
            headers={"Authorization": f"Bearer {alice_token}"},
        )
        assert resp.status_code == 422


# ──────────────────────────── 5. DELETE ──────────────────────────────────────

class TestDelete:
    def test_delete_returns_204(self, client, alice_token):
        note_id = client.post(
            "/notes/",
            json={"title": "To Delete", "content": "bye"},
            headers={"Authorization": f"Bearer {alice_token}"},
        ).json()["id"]

        resp = client.delete(
            f"/notes/{note_id}",
            headers={"Authorization": f"Bearer {alice_token}"},
        )
        assert resp.status_code == 204
        assert resp.content == b""

    def test_delete_then_get_returns_404(self, client, alice_token):
        note_id = client.post(
            "/notes/",
            json={"title": "Delete Me", "content": "x"},
            headers={"Authorization": f"Bearer {alice_token}"},
        ).json()["id"]

        client.delete(f"/notes/{note_id}", headers={"Authorization": f"Bearer {alice_token}"})
        resp = client.get(f"/notes/{note_id}", headers={"Authorization": f"Bearer {alice_token}"})
        assert resp.status_code == 404

    def test_delete_nonexistent_returns_404(self, client, alice_token):
        resp = client.delete(
            "/notes/999999",
            headers={"Authorization": f"Bearer {alice_token}"},
        )
        assert resp.status_code == 404

    def test_delete_without_token_returns_401(self, client):
        resp = client.delete("/notes/1")
        assert resp.status_code == 401

    def test_delete_removes_from_list(self, client, alice_token):
        note_id = client.post(
            "/notes/",
            json={"title": "Remove From List", "content": "gone"},
            headers={"Authorization": f"Bearer {alice_token}"},
        ).json()["id"]

        client.delete(f"/notes/{note_id}", headers={"Authorization": f"Bearer {alice_token}"})
        notes = client.get("/notes/", headers={"Authorization": f"Bearer {alice_token}"}).json()
        assert note_id not in [n["id"] for n in notes]


# ──────────────────────────── 6. 完整 CRUD 流程 ──────────────────────────────

class TestFullCrudLifecycle:
    """建立 → 讀取 → 更新 → 確認 → 刪除 → 確認消失。"""

    def test_full_lifecycle(self, client, alice_token):
        # CREATE
        create_resp = client.post(
            "/notes/",
            json={"title": "Lifecycle", "content": "v1"},
            headers={"Authorization": f"Bearer {alice_token}"},
        )
        assert create_resp.status_code == 201
        note_id = create_resp.json()["id"]

        # READ
        get_resp = client.get(
            f"/notes/{note_id}",
            headers={"Authorization": f"Bearer {alice_token}"},
        )
        assert get_resp.status_code == 200
        assert get_resp.json()["content"] == "v1"

        # UPDATE
        put_resp = client.put(
            f"/notes/{note_id}",
            json={"content": "v2"},
            headers={"Authorization": f"Bearer {alice_token}"},
        )
        assert put_resp.status_code == 200
        assert put_resp.json()["content"] == "v2"

        # LIST includes it
        list_resp = client.get("/notes/", headers={"Authorization": f"Bearer {alice_token}"})
        assert note_id in [n["id"] for n in list_resp.json()]

        # DELETE
        del_resp = client.delete(
            f"/notes/{note_id}",
            headers={"Authorization": f"Bearer {alice_token}"},
        )
        assert del_resp.status_code == 204

        # CONFIRM GONE
        assert client.get(
            f"/notes/{note_id}",
            headers={"Authorization": f"Bearer {alice_token}"},
        ).status_code == 404


# ──────────────────────────── 7. 租戶隔離（CRUD 維度）────────────────────────

class TestCrossTenantCrud:
    """驗證 Bob 無法對 Alice 的 note 做任何 CRUD 操作。"""

    @pytest.fixture(scope="class")
    def alice_note(self, client, alice_token):
        return client.post(
            "/notes/",
            json={"title": "Alice Private", "content": "secret"},
            headers={"Authorization": f"Bearer {alice_token}"},
        ).json()["id"]

    def test_bob_get_alice_note_404(self, client, bob_token, alice_note):
        assert client.get(
            f"/notes/{alice_note}",
            headers={"Authorization": f"Bearer {bob_token}"},
        ).status_code == 404

    def test_bob_put_alice_note_404(self, client, bob_token, alice_note):
        assert client.put(
            f"/notes/{alice_note}",
            json={"title": "Hijack"},
            headers={"Authorization": f"Bearer {bob_token}"},
        ).status_code == 404

    def test_bob_delete_alice_note_404(self, client, bob_token, alice_note):
        assert client.delete(
            f"/notes/{alice_note}",
            headers={"Authorization": f"Bearer {bob_token}"},
        ).status_code == 404

    def test_alice_note_still_exists_after_bob_attempts(self, client, alice_token, alice_note):
        """Bob 的嘗試不應影響 Alice 的資料。"""
        resp = client.get(
            f"/notes/{alice_note}",
            headers={"Authorization": f"Bearer {alice_token}"},
        )
        assert resp.status_code == 200
        assert resp.json()["title"] == "Alice Private"

    def test_bob_list_excludes_alice_notes(self, client, alice_token, bob_token, alice_note):
        alice_notes = client.get("/notes/", headers={"Authorization": f"Bearer {alice_token}"}).json()
        bob_notes   = client.get("/notes/", headers={"Authorization": f"Bearer {bob_token}"}).json()
        alice_ids = {n["id"] for n in alice_notes}
        bob_ids   = {n["id"] for n in bob_notes}
        assert alice_ids.isdisjoint(bob_ids), "兩租戶 note ID 集合不應交叉"


# ──────────────────────────── 8. Quota 防線（所有端點）──────────────────────

class TestQuotaOnAllEndpoints:
    """所有 CRUD 端點超量後都應回 429，包括讀/更新/刪除。"""

    @pytest.fixture()
    def capped_user(self, client):
        """建立一個新 tenant，直接把 quota 塞滿後回傳 (token, note_id)。"""
        import uuid
        uid = uuid.uuid4().hex[:8]
        token = _register(client, f"cap_{uid}@test.com", "CapPass99!", f"cap-corp-{uid}")
        auth = {"Authorization": f"Bearer {token}"}

        # 先建一筆 note（消耗 1 quota）
        note_id = client.post(
            "/notes/", json={"title": "pre-cap", "content": "x"}, headers=auth,
        ).json()["id"]

        # 取 tenant_id 並將 count 設到上限
        tenant_id = client.get("/tenants/me", headers=auth).json()["id"]
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

        return token, note_id

    def test_create_quota_exceeded_429(self, client, capped_user):
        token, _ = capped_user
        resp = client.post(
            "/notes/",
            json={"title": "over-limit"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 429
        assert "Quota" in resp.json()["detail"]

    def test_list_quota_exceeded_429(self, client, capped_user):
        token, _ = capped_user
        resp = client.get("/notes/", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 429

    def test_get_single_quota_exceeded_429(self, client, capped_user):
        token, note_id = capped_user
        resp = client.get(f"/notes/{note_id}", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 429

    def test_update_quota_exceeded_429(self, client, capped_user):
        token, note_id = capped_user
        resp = client.put(
            f"/notes/{note_id}",
            json={"title": "no"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 429

    def test_delete_quota_exceeded_429(self, client, capped_user):
        token, note_id = capped_user
        resp = client.delete(f"/notes/{note_id}", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 429

    def test_quota_message_is_informative(self, client, capped_user):
        """429 detail 必須有明確說明（不只是空字串或 null）。"""
        token, _ = capped_user
        resp = client.get("/notes/", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 429
        detail = resp.json().get("detail", "")
        assert len(detail) > 10, f"detail 太短或無資訊: {detail!r}"
