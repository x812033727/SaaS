"""Task #3 驗收測試：多租戶隔離

測試重點：
1. GET /tenants/me 回傳正確租戶資訊
2. 跨租戶讀/寫/刪除一律 404（不洩漏 ID 存在性）
3. 各租戶 LIST 只看得到自己的資料
4. tenant_query() helper 單元測試（無 HTTP）
5. require_same_tenant() 403 防線單元測試
"""

from __future__ import annotations

import os

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("SAAS_RATE_LIMIT_ENABLED", "false")

# 先 import models 讓 Base.metadata 知道所有 table
from saas_mvp.models import tenant as _t, user as _u, note as _n  # noqa: F401
from saas_mvp.app import create_app
from saas_mvp.auth.security import hash_password
from saas_mvp.db import Base, get_db
from saas_mvp.models.note import Note
from saas_mvp.models.tenant import Tenant
from saas_mvp.models.user import User
from saas_mvp.services.tenants import require_same_tenant, tenant_query


# ──────────────────────────── Fixtures ───────────────────────────────────────

@pytest.fixture(scope="module")
def client():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)

    app = create_app()

    def override_get_db():
        db = TestingSession()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db

    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


@pytest.fixture(scope="module")
def alice_token(client):
    """Alice 屬於 alpha-corp。"""
    resp = client.post("/auth/register", json={
        "email": "alice3@example.com",
        "password": "AlicePass99!",
        "tenant_name": "alpha-corp",
    })
    assert resp.status_code == 201, resp.text
    return resp.json()["access_token"]


@pytest.fixture(scope="module")
def bob_token(client):
    """Bob 屬於 beta-corp（不同租戶）。"""
    resp = client.post("/auth/register", json={
        "email": "bob3@example.com",
        "password": "BobPass99!!",
        "tenant_name": "beta-corp",
    })
    assert resp.status_code == 201, resp.text
    return resp.json()["access_token"]


@pytest.fixture(scope="module")
def alice_note_id(client, alice_token):
    """Alice 建立一筆 note，回傳其 id。"""
    resp = client.post(
        "/notes/",
        json={"title": "Alice's Secret", "content": "do not share"},
        headers={"Authorization": f"Bearer {alice_token}"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


# ──────────────────────────── 1. GET /tenants/me ─────────────────────────────

class TestTenantMe:
    def test_get_my_tenant_alice(self, client, alice_token):
        resp = client.get("/tenants/me", headers={"Authorization": f"Bearer {alice_token}"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "alpha-corp"
        assert data["plan"] == "free"
        assert "id" in data

    def test_get_my_tenant_bob(self, client, bob_token):
        resp = client.get("/tenants/me", headers={"Authorization": f"Bearer {bob_token}"})
        assert resp.status_code == 200
        assert resp.json()["name"] == "beta-corp"

    def test_tenants_me_without_token_returns_401(self, client):
        resp = client.get("/tenants/me")
        assert resp.status_code == 401

    def test_tenant_ids_differ(self, client, alice_token, bob_token):
        """Alice 與 Bob 應屬於不同租戶。"""
        alice_tid = client.get("/tenants/me", headers={"Authorization": f"Bearer {alice_token}"}).json()["id"]
        bob_tid   = client.get("/tenants/me", headers={"Authorization": f"Bearer {bob_token}"}).json()["id"]
        assert alice_tid != bob_tid


# ──────────────────────────── 2. 跨租戶讀寫拒絕 ─────────────────────────────

class TestCrossTenantIsolation:
    def test_bob_cannot_read_alice_note(self, client, bob_token, alice_note_id):
        """Bob 讀取 Alice 的 note → 404（不洩漏 ID 存在性）。"""
        resp = client.get(
            f"/notes/{alice_note_id}",
            headers={"Authorization": f"Bearer {bob_token}"},
        )
        assert resp.status_code == 404

    def test_bob_cannot_update_alice_note(self, client, bob_token, alice_note_id):
        """Bob 嘗試 PUT Alice 的 note → 404。"""
        resp = client.put(
            f"/notes/{alice_note_id}",
            json={"title": "Hacked!", "content": "pwned"},
            headers={"Authorization": f"Bearer {bob_token}"},
        )
        assert resp.status_code == 404

    def test_bob_cannot_delete_alice_note(self, client, bob_token, alice_note_id):
        """Bob 嘗試 DELETE Alice 的 note → 404。"""
        resp = client.delete(
            f"/notes/{alice_note_id}",
            headers={"Authorization": f"Bearer {bob_token}"},
        )
        assert resp.status_code == 404

    def test_alice_can_read_own_note(self, client, alice_token, alice_note_id):
        """Alice 讀取自己的 note → 200。"""
        resp = client.get(
            f"/notes/{alice_note_id}",
            headers={"Authorization": f"Bearer {alice_token}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["title"] == "Alice's Secret"
        assert data["content"] == "do not share"

    def test_unauthenticated_cannot_read_note(self, client, alice_note_id):
        """無 token → 401。"""
        resp = client.get(f"/notes/{alice_note_id}")
        assert resp.status_code == 401


# ──────────────────────────── 3. LIST 隔離 ───────────────────────────────────

class TestListIsolation:
    def test_alice_list_excludes_bob_notes(self, client, alice_token, bob_token):
        """Alice list 不包含 Bob 的 note。"""
        # Bob 先建立一筆
        client.post(
            "/notes/",
            json={"title": "Bob's Private Note", "content": "bob only"},
            headers={"Authorization": f"Bearer {bob_token}"},
        )
        resp = client.get("/notes/", headers={"Authorization": f"Bearer {alice_token}"})
        assert resp.status_code == 200
        titles = [n["title"] for n in resp.json()]
        assert "Bob's Private Note" not in titles
        assert "Alice's Secret" in titles

    def test_bob_list_excludes_alice_notes(self, client, alice_token, bob_token):
        """Bob list 不包含 Alice 的 note。"""
        resp = client.get("/notes/", headers={"Authorization": f"Bearer {bob_token}"})
        assert resp.status_code == 200
        titles = [n["title"] for n in resp.json()]
        assert "Alice's Secret" not in titles

    def test_list_tenant_id_consistent(self, client, alice_token):
        """Alice list 回傳的每筆 note.tenant_id 都相同。"""
        me = client.get("/tenants/me", headers={"Authorization": f"Bearer {alice_token}"}).json()
        notes = client.get("/notes/", headers={"Authorization": f"Bearer {alice_token}"}).json()
        assert all(n["tenant_id"] == me["id"] for n in notes)


# ──────────────────────────── 4. tenant_query 單元測試 ───────────────────────

class TestTenantQueryHelper:
    """直接操作 DB，不過 HTTP stack，驗證 helper 的過濾邏輯。"""

    @pytest.fixture(scope="class")
    def session(self):
        engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(bind=engine)
        s = sessionmaker(bind=engine)()

        t1 = Tenant(name="unit-t1", plan="free")
        t2 = Tenant(name="unit-t2", plan="pro")
        s.add_all([t1, t2])
        s.flush()

        u1 = User(email="u1@unit.com", hashed_password=hash_password("pass1234"), tenant_id=t1.id)
        u2 = User(email="u2@unit.com", hashed_password=hash_password("pass1234"), tenant_id=t2.id)
        s.add_all([u1, u2])
        s.flush()

        # T1: 2 notes, T2: 1 note
        s.add_all([
            Note(title="T1-A", content="", owner_id=u1.id, tenant_id=t1.id),
            Note(title="T1-B", content="", owner_id=u1.id, tenant_id=t1.id),
            Note(title="T2-A", content="", owner_id=u2.id, tenant_id=t2.id),
        ])
        s.commit()

        yield s, t1.id, t2.id
        s.close()

    def test_t1_sees_only_own_notes(self, session):
        s, t1_id, _ = session
        notes = tenant_query(s, Note, t1_id).all()
        assert len(notes) == 2
        assert {n.title for n in notes} == {"T1-A", "T1-B"}

    def test_t2_sees_only_own_notes(self, session):
        s, _, t2_id = session
        notes = tenant_query(s, Note, t2_id).all()
        assert len(notes) == 1
        assert notes[0].title == "T2-A"

    def test_cross_tenant_filter_returns_empty(self, session):
        """用 T1 的 id 去 filter T2 的 note → 空集合。"""
        s, t1_id, t2_id = session
        t2_note_id = tenant_query(s, Note, t2_id).first().id
        result = tenant_query(s, Note, t1_id).filter(Note.id == t2_note_id).first()
        assert result is None  # T1 看不到 T2 的 note


# ──────────────────────────── 5. require_same_tenant 單元測試 ────────────────

class TestRequireSameTenant:
    def test_same_tenant_passes(self):
        require_same_tenant(42, 42)  # 不應拋例外

    def test_different_tenant_raises_403(self):
        with pytest.raises(HTTPException) as exc_info:
            require_same_tenant(1, 2)
        assert exc_info.value.status_code == 403
        assert "different tenant" in exc_info.value.detail

    def test_zero_tenant_id_raises_403(self):
        with pytest.raises(HTTPException) as exc_info:
            require_same_tenant(0, 1)
        assert exc_info.value.status_code == 403
