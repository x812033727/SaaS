"""R8-3 — console JSON API:成員管理 + 帳號設定(auth 熱路徑)。"""

from __future__ import annotations

import uuid

import pyotp
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from saas_mvp.app import create_app
from saas_mvp.auth.security import create_access_token
from saas_mvp.db import Base, get_db
from saas_mvp.models.user import User


@pytest.fixture()
def v1_client():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    Base.metadata.create_all(engine)
    app = create_app()

    def override_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_db
    with TestClient(app) as client:
        yield client, session_factory


def _register(client: TestClient, prefix: str = "ma") -> tuple[int, dict[str, str]]:
    unique = uuid.uuid4().hex[:8]
    r = client.post(
        "/auth/register",
        json={
            "email": f"{prefix}-{unique}@example.com",
            "password": "safe-password-123",
            "tenant_name": f"{prefix}-{unique}",
        },
    )
    assert r.status_code == 201, r.text
    headers = {"Authorization": f"Bearer {r.json()['access_token']}"}
    ctx = client.get("/api/v1/context", headers=headers).json()
    return ctx["tenant"]["id"], headers


def _add_member(
    session_factory, tenant_id: int, role: str = "staff"
) -> tuple[int, dict[str, str]]:
    db = session_factory()
    try:
        user = User(
            email=f"m-{uuid.uuid4().hex[:8]}@example.com",
            hashed_password="x",
            tenant_id=tenant_id,
            role=role,
        )
        db.add(user)
        db.commit()
        token = create_access_token(user.id, tenant_id)
        return user.id, {"Authorization": f"Bearer {token}"}
    finally:
        db.close()


class TestMembersConsole:
    def test_owner_only(self, v1_client):
        client, sf = v1_client
        tid, headers = _register(client)
        _, staff_headers = _add_member(sf, tid)
        assert client.get("/api/v1/members", headers=staff_headers).status_code == 403
        assert client.get("/api/v1/members", headers=headers).status_code == 200

    def test_invite_url(self, v1_client):
        client, _ = v1_client
        _, headers = _register(client)
        r = client.post("/api/v1/members/invite", json={}, headers=headers)
        assert r.status_code == 201, r.text
        assert "/ui/join/" in r.json()["invite_url"]

    def test_role_disable_enable_remove(self, v1_client):
        client, sf = v1_client
        tid, headers = _register(client)
        member_id, member_headers = _add_member(sf, tid)
        # 升 owner
        rows = client.post(
            f"/api/v1/members/{member_id}/role",
            json={"role": "owner"},
            headers=headers,
        )
        assert rows.status_code == 200, rows.text
        target = next(m for m in rows.json() if m["id"] == member_id)
        assert target["role"] == "owner"
        # 降回 staff、停用 → 該成員的票即刻失效(token_version bump)
        client.post(
            f"/api/v1/members/{member_id}/role", json={"role": "staff"}, headers=headers
        )
        rows2 = client.post(
            f"/api/v1/members/{member_id}/disable", json={}, headers=headers
        )
        assert next(m for m in rows2.json() if m["id"] == member_id)["disabled"] is True
        assert (
            client.get("/api/v1/account", headers=member_headers).status_code == 401
        )
        # 啟用 → 移除
        client.post(f"/api/v1/members/{member_id}/enable", json={}, headers=headers)
        rows3 = client.delete(f"/api/v1/members/{member_id}", headers=headers)
        assert rows3.status_code == 200
        assert all(m["id"] != member_id for m in rows3.json())

    def test_last_owner_and_self_protection(self, v1_client):
        client, sf = v1_client
        tid, headers = _register(client)
        ctx = client.get("/api/v1/context", headers=headers).json()
        my_id = ctx["user"]["id"] if "user" in ctx else None
        rows = client.get("/api/v1/members", headers=headers).json()
        me = next(m for m in rows if m["is_self"])
        # 對自己操作一律拒絕
        assert (
            client.post(
                f"/api/v1/members/{me['id']}/disable", json={}, headers=headers
            ).status_code
            == 422
        )
        # 最後一位 owner 不可降級(自我保護已擋;建第二位 staff 再驗降級最後 owner)
        member_id, _ = _add_member(sf, tid)
        r = client.post(
            f"/api/v1/members/{me['id']}/role", json={"role": "staff"}, headers=headers
        )
        assert r.status_code == 422
        assert my_id is None or me["id"] == my_id

    def test_tenant_isolation(self, v1_client):
        client, sf = v1_client
        tid, headers = _register(client)
        member_id, _ = _add_member(sf, tid)
        _, headers2 = _register(client, prefix="other")
        assert (
            client.post(
                f"/api/v1/members/{member_id}/disable", json={}, headers=headers2
            ).status_code
            == 422
        )


class TestAccountConsole:
    def test_summary(self, v1_client):
        client, _ = v1_client
        _, headers = _register(client)
        r = client.get("/api/v1/account", headers=headers)
        assert r.status_code == 200, r.text
        s = r.json()
        assert s["email_verified"] is False
        assert s["totp_enabled"] is False
        assert s["oauth_provider"] is None

    def test_password_change_rotates_token(self, v1_client):
        client, _ = v1_client
        _, headers = _register(client)
        r = client.post(
            "/api/v1/account/password",
            json={
                "current_password": "safe-password-123",
                "new_password": "new-password-456",
                "confirm_password": "new-password-456",
            },
            headers=headers,
        )
        assert r.status_code == 200, r.text
        new_token = r.json()["access_token"]
        # 舊票已被 token_version 撤銷;新票有效
        assert client.get("/api/v1/account", headers=headers).status_code == 401
        assert (
            client.get(
                "/api/v1/account",
                headers={"Authorization": f"Bearer {new_token}"},
            ).status_code
            == 200
        )

    def test_password_change_validation(self, v1_client):
        client, _ = v1_client
        _, headers = _register(client)
        base = {
            "new_password": "new-password-456",
            "confirm_password": "new-password-456",
        }
        assert (
            client.post(
                "/api/v1/account/password",
                json={**base, "current_password": "wrong"},
                headers=headers,
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/api/v1/account/password",
                json={
                    "current_password": "safe-password-123",
                    "new_password": "short",
                    "confirm_password": "short",
                },
                headers=headers,
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/api/v1/account/password",
                json={
                    "current_password": "safe-password-123",
                    "new_password": "new-password-456",
                    "confirm_password": "different-999",
                },
                headers=headers,
            ).status_code
            == 422
        )

    def test_logout_all_rotates_token(self, v1_client):
        client, _ = v1_client
        _, headers = _register(client)
        r = client.post("/api/v1/account/logout-all", json={}, headers=headers)
        assert r.status_code == 200, r.text
        new_token = r.json()["access_token"]
        assert client.get("/api/v1/account", headers=headers).status_code == 401
        assert (
            client.get(
                "/api/v1/account",
                headers={"Authorization": f"Bearer {new_token}"},
            ).status_code
            == 200
        )

    def test_totp_enroll_confirm_disable(self, v1_client):
        client, _ = v1_client
        _, headers = _register(client)
        start = client.post("/api/v1/account/totp/start", json={}, headers=headers)
        assert start.status_code == 200, start.text
        body = start.json()
        assert body["qr_svg"].lstrip().startswith("<svg")
        secret = body["secret"]
        # 錯誤驗證碼 → 422
        assert (
            client.post(
                "/api/v1/account/totp/confirm",
                json={"otp": "000000"},
                headers=headers,
            ).status_code
            == 422
        )
        # 正確驗證碼 → 恢復碼一次性回傳
        otp = pyotp.TOTP(secret).now()
        r = client.post(
            "/api/v1/account/totp/confirm", json={"otp": otp}, headers=headers
        )
        assert r.status_code == 200, r.text
        codes = r.json()["recovery_codes"]
        assert len(codes) == 10
        s = client.get("/api/v1/account", headers=headers).json()
        assert s["totp_enabled"] is True
        assert s["remaining_recovery_codes"] == 10
        # 已啟用再 start → 409
        assert (
            client.post(
                "/api/v1/account/totp/start", json={}, headers=headers
            ).status_code
            == 409
        )
        # 恢復碼可停用
        r2 = client.post(
            "/api/v1/account/totp/disable",
            json={"otp": codes[0]},
            headers=headers,
        )
        assert r2.status_code == 200, r2.text
        assert client.get("/api/v1/account", headers=headers).json()["totp_enabled"] is False

    def test_oauth_unlink(self, v1_client):
        client, sf = v1_client
        tid, headers = _register(client)
        db = sf()
        try:
            user = (
                db.query(User).filter(User.tenant_id == tid).order_by(User.id).first()
            )
            user.oauth_provider = "line"
            user.oauth_subject = "U123"
            db.commit()
        finally:
            db.close()
        assert client.get("/api/v1/account", headers=headers).json()["oauth_provider"] == "line"
        r = client.post("/api/v1/account/oauth/unlink", json={}, headers=headers)
        assert r.status_code == 200
        assert client.get("/api/v1/account", headers=headers).json()["oauth_provider"] is None


class TestAccountAudits:
    def test_password_change_and_unlink_write_audit_rows(self, v1_client):
        """R9-2:改密碼與解除社群連結須落稽核(auth.password_change / auth.oauth.unlink)。"""
        from saas_mvp.models.audit_log import AuditLog

        client, sf = v1_client
        tid, headers = _register(client)
        r = client.post(
            "/api/v1/account/password",
            json={
                "current_password": "safe-password-123",
                "new_password": "new-password-456",
                "confirm_password": "new-password-456",
            },
            headers=headers,
        )
        assert r.status_code == 200, r.text
        new_headers = {"Authorization": f"Bearer {r.json()['access_token']}"}
        # 種 oauth 綁定再解除
        db = sf()
        try:
            user = (
                db.query(User).filter(User.tenant_id == tid).order_by(User.id).first()
            )
            user.oauth_provider = "line"
            user.oauth_subject = "U-audit"
            db.commit()
            user_id = user.id
        finally:
            db.close()
        assert (
            client.post(
                "/api/v1/account/oauth/unlink", json={}, headers=new_headers
            ).status_code
            == 200
        )
        db = sf()
        try:
            actions = {
                row.action
                for row in db.query(AuditLog)
                .filter(AuditLog.actor_user_id == user_id)
                .all()
            }
            assert "auth.password_change" in actions
            assert "auth.oauth.unlink" in actions
        finally:
            db.close()
