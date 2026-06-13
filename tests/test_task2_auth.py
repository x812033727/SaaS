"""Task #2 驗收測試：帳號模組（註冊/登入/token 驗證）

所有測試使用 in-memory SQLite + FastAPI TestClient，完全離線。
速率限制在測試中透過 env 變數停用（SAAS_RATE_LIMIT_ENABLED=false）。
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# 先 import models 讓 Base.metadata 知道所有 table
from saas_mvp.models import tenant as _t, user as _u, note as _n  # noqa: F401
from saas_mvp.app import create_app
from saas_mvp.db import Base, get_db

# 停用速率限制，避免測試因累積請求數而 429
os.environ.setdefault("SAAS_RATE_LIMIT_ENABLED", "false")

# ──────────────────────────── 測試用 DB 設定 ─────────────────────────────────

TEST_DB_URL = "sqlite:///:memory:"


@pytest.fixture(scope="module")
def client():
    # StaticPool：所有連線共用同一個 in-memory connection
    engine = create_engine(
        TEST_DB_URL,
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
        yield c, engine


@pytest.fixture(scope="module")
def registered(client):
    """預先註冊 alice（新 tenant acme），供後續測試複用。"""
    c, _ = client
    resp = c.post("/auth/register", json={
        "email": "alice@example.com",
        "password": "s3cr3t!Pass",   # ≥ 8 chars
        "tenant_name": "acme",
    })
    assert resp.status_code == 201, resp.text
    return resp.json()


# ─────────────────────────── 1. 註冊 ─────────────────────────────────────────

class TestRegister:
    def test_register_success(self, client):
        c, _ = client
        resp = c.post("/auth/register", json={
            "email": "bob@example.com",
            "password": "p@ssw0rd!",   # ≥ 8 chars
            "tenant_name": "bob-corp",
        })
        assert resp.status_code == 201, resp.text
        data = resp.json()
        assert "access_token" in data
        assert data["token_type"] == "bearer"

    def test_register_duplicate_email(self, client, registered):
        c, _ = client
        resp = c.post("/auth/register", json={
            "email": "alice@example.com",   # already registered
            "password": "anotherPass1",
            "tenant_name": "other-tenant",
        })
        assert resp.status_code == 400
        assert "already registered" in resp.json()["detail"].lower()

    # ── 安全修正 3：Tenant 名稱保護 ─────────────────────────────────────────
    def test_register_existing_tenant_rejected(self, client, registered):
        """已存在的 tenant 名稱不可被其他人搶入（防範 tenant 搶佔攻擊）。"""
        c, _ = client
        resp = c.post("/auth/register", json={
            "email": "intruder@example.com",
            "password": "intruderPass9",
            "tenant_name": "acme",          # 已被 alice 創建
        })
        assert resp.status_code == 400
        assert "tenant name already taken" in resp.json()["detail"].lower()

    # ── 安全修正 1：密碼最短長度 ────────────────────────────────────────────
    def test_register_password_too_short_rejected(self, client):
        """密碼少於 8 個字元，Pydantic 422 驗證失敗。"""
        c, _ = client
        resp = c.post("/auth/register", json={
            "email": "short@example.com",
            "password": "abc123",           # 6 chars — too short
            "tenant_name": "short-corp",
        })
        assert resp.status_code == 422

    def test_register_empty_password_rejected(self, client):
        """空字串密碼應被拒絕（422）。"""
        c, _ = client
        resp = c.post("/auth/register", json={
            "email": "empty@example.com",
            "password": "",
            "tenant_name": "empty-corp",
        })
        assert resp.status_code == 422


# ─────────────────────────── 2. 密碼儲存安全 ─────────────────────────────────

class TestPasswordSecurity:
    def test_no_plaintext_password_in_db(self, client, registered):
        """DB 的 hashed_password 欄位不得含明文密碼。"""
        _, engine = client
        with engine.connect() as conn:
            rows = conn.execute(text("SELECT hashed_password FROM users")).fetchall()
        assert rows, "DB 中沒有使用者"
        for (hp,) in rows:
            assert "s3cr3t!Pass" not in hp
            assert "p@ssw0rd!" not in hp
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
            "password": "s3cr3t!Pass",
        })
        assert resp.status_code == 200, resp.text
        assert "access_token" in resp.json()

    def test_login_wrong_password(self, client, registered):
        c, _ = client
        resp = c.post("/auth/token", data={
            "username": "alice@example.com",
            "password": "wrongpassword",
        })
        assert resp.status_code == 401

    def test_login_unknown_email(self, client):
        c, _ = client
        resp = c.post("/auth/token", data={
            "username": "nobody@example.com",
            "password": "whatever123",
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
            "password": "s3cr3t!Pass",
        })
        token = login_resp.json()["access_token"]
        me_resp = c.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert me_resp.status_code == 200
        assert me_resp.json()["email"] == "alice@example.com"

    # ── 安全修正 2：速率限制驗證（啟用後觸發 429）──────────────────────────
    def test_rate_limiter_blocks_excess_requests(self, client):
        """當速率限制啟用時，超量請求應收到 429。"""
        from saas_mvp.auth.ratelimit import token_limiter
        # 暫時將 max_calls 設為 1，驗證 429 行為
        original = token_limiter._max_calls
        token_limiter._max_calls = 1
        # 清除紀錄讓計數從零開始
        token_limiter._log.clear()

        from saas_mvp.config import settings
        original_enabled = settings.rate_limit_enabled
        settings.rate_limit_enabled = True

        c, _ = client
        try:
            # 第 1 次（允許）
            r1 = c.post("/auth/token", data={
                "username": "nobody@rate.com", "password": "whatever"
            })
            assert r1.status_code != 429, "第 1 次不應被限流"
            # 第 2 次（應被攔截）
            r2 = c.post("/auth/token", data={
                "username": "nobody@rate.com", "password": "whatever"
            })
            assert r2.status_code == 429
        finally:
            token_limiter._max_calls = original
            token_limiter._log.clear()
            settings.rate_limit_enabled = original_enabled
