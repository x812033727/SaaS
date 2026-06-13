"""Task #2 驗收測試：帳號模組（註冊/登入/token 驗證）

所有測試使用 in-memory SQLite + FastAPI TestClient，完全離線。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# 先 import models 讓 Base.metadata 知道所有 table
from saas_mvp.models import tenant as _t, user as _u, note as _n  # noqa: F401
from saas_mvp.app import create_app
from saas_mvp.db import Base, get_db

# ──────────────────────────── 測試用 DB 設定 ─────────────────────────────────

TEST_DB_URL = "sqlite:///:memory:"


@pytest.fixture(scope="module")
def client():
    # StaticPool：所有連線共用同一個 in-memory connection
    # （否則 sqlite:///:memory: 每次開新連線都是空白 DB，表格消失）
    engine = create_engine(
        TEST_DB_URL,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    # 所有 models 已在頂部 import，Base.metadata 已完整 → 建表
    Base.metadata.create_all(bind=engine)

    app = create_app()

    def override_get_db():
        db = TestingSession()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db

    # TestClient 進入 context 時 lifespan 會呼叫 init_db()，
    # 但 get_db 已被覆寫，HTTP 請求一律走 test engine。
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c, engine  # 也傳 engine 給需要直查 DB 的測試


@pytest.fixture(scope="module")
def registered(client):
    """預先註冊一個帳號，供後續測試複用。"""
    c, _ = client
    resp = c.post("/auth/register", json={
        "email": "alice@example.com",
        "password": "s3cr3t!",
        "tenant_name": "acme",
    })
    assert resp.status_code == 201, resp.text
    return resp.json()  # {"access_token": "...", "token_type": "bearer"}


# ─────────────────────────── 1. 註冊 ─────────────────────────────────────────

class TestRegister:
    def test_register_success(self, client):
        c, _ = client
        resp = c.post("/auth/register", json={
            "email": "bob@example.com",
            "password": "p@ssword",
            "tenant_name": "bob-corp",
        })
        assert resp.status_code == 201, resp.text
        data = resp.json()
        assert "access_token" in data
        assert data["token_type"] == "bearer"

    def test_register_duplicate_email(self, client, registered):
        c, _ = client
        resp = c.post("/auth/register", json={
            "email": "alice@example.com",  # already registered
            "password": "other",
            "tenant_name": "other-tenant",
        })
        assert resp.status_code == 400
        assert "already registered" in resp.json()["detail"].lower()

    def test_register_same_tenant_different_user(self, client):
        """同租戶可有多位使用者。"""
        c, _ = client
        resp = c.post("/auth/register", json={
            "email": "carol@example.com",
            "password": "carol-pw",
            "tenant_name": "acme",  # same tenant as alice
        })
        assert resp.status_code == 201, resp.text


# ─────────────────────────── 2. 密碼儲存安全 ─────────────────────────────────

class TestPasswordSecurity:
    def test_no_plaintext_password_in_db(self, client, registered):
        """DB 的 hashed_password 欄位不得含明文密碼。"""
        _, engine = client
        with engine.connect() as conn:
            rows = conn.execute(text("SELECT hashed_password FROM users")).fetchall()
        assert rows, "DB 中沒有使用者"
        for (hp,) in rows:
            assert "s3cr3t!" not in hp, "明文密碼洩漏進 DB"
            assert "p@ssword" not in hp
            assert "carol-pw" not in hp
            # bcrypt hash 以 $2b$ 或 $2a$ 開頭
            assert hp.startswith("$2"), f"不像 bcrypt hash: {hp!r}"

    def test_password_column_name_is_hashed_password(self, client):
        """欄位名稱確認是 hashed_password 而不是 password。"""
        _, engine = client
        with engine.connect() as conn:
            cols = [row[1] for row in conn.execute(
                text("PRAGMA table_info(users)")
            ).fetchall()]
        assert "hashed_password" in cols
        assert "password" not in cols


# ─────────────────────────── 3. 登入 ─────────────────────────────────────────

class TestLogin:
    def test_login_success(self, client, registered):
        c, _ = client
        resp = c.post("/auth/token", data={
            "username": "alice@example.com",
            "password": "s3cr3t!",
        })
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert "access_token" in data

    def test_login_wrong_password(self, client, registered):
        c, _ = client
        resp = c.post("/auth/token", data={
            "username": "alice@example.com",
            "password": "wrong!",
        })
        assert resp.status_code == 401

    def test_login_unknown_email(self, client):
        c, _ = client
        resp = c.post("/auth/token", data={
            "username": "nobody@example.com",
            "password": "whatever",
        })
        assert resp.status_code == 401


# ─────────────────────────── 4. Token 驗證 ───────────────────────────────────

class TestTokenValidation:
    def test_whoami_with_valid_token(self, client, registered):
        c, _ = client
        token = registered["access_token"]
        resp = c.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["email"] == "alice@example.com"
        assert "tenant_id" in data
        assert data["tenant_name"] == "acme"

    def test_forged_token_rejected(self, client):
        c, _ = client
        fake = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiI5OTkifQ.bad-sig"
        resp = c.get("/auth/me", headers={"Authorization": f"Bearer {fake}"})
        assert resp.status_code == 401

    def test_tampered_token_rejected(self, client, registered):
        """篡改 payload 後簽名不合法，必須 401。"""
        c, _ = client
        token = registered["access_token"]
        parts = token.split(".")
        assert len(parts) == 3
        tampered = parts[0] + ".dGFtcGVyZWQ.badbadbad"
        resp = c.get("/auth/me", headers={"Authorization": f"Bearer {tampered}"})
        assert resp.status_code == 401

    def test_expired_token_rejected(self, client):
        """手動建立一個已過期的 token，應被拒絕。"""
        from datetime import timedelta
        from saas_mvp.auth.security import create_access_token
        expired_token = create_access_token(
            user_id=1, tenant_id=1, expires_delta=timedelta(seconds=-1)
        )
        c, _ = client
        resp = c.get("/auth/me", headers={"Authorization": f"Bearer {expired_token}"})
        assert resp.status_code == 401

    def test_no_token_rejected(self, client):
        c, _ = client
        resp = c.get("/auth/me")
        assert resp.status_code == 401

    def test_login_token_is_valid_for_whoami(self, client, registered):
        """登入取得的 token 也能通過 /auth/me 驗證。"""
        c, _ = client
        login_resp = c.post("/auth/token", data={
            "username": "alice@example.com",
            "password": "s3cr3t!",
        })
        token = login_resp.json()["access_token"]
        me_resp = c.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert me_resp.status_code == 200
        assert me_resp.json()["email"] == "alice@example.com"
