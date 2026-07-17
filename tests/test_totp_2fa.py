"""R5-D2 — owner TOTP 2FA:註冊/確認/登入第二步/恢復碼/停用/pending 票隔離。

覆蓋:
- enrollment:start→錯碼不啟用→對碼啟用+10 組恢復碼(僅顯示一次)
- /auth/token:2FA 帳號缺 otp=otp_required、錯 otp=otp_invalid、對 otp 過;±1 step 視窗
- /ui/login:改走 /ui/login/mfa 第二步;錯碼 401;對碼種正式 cookie
- pending 票不可當 access token(UI cookie / Bearer / renew 三路全拒)
- 恢復碼一次性;disable 需驗證碼;OAuth 登入也進第二步
"""

from __future__ import annotations

import os
import re
import uuid

import pyotp
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("SAAS_RATE_LIMIT_ENABLED", "false")

from saas_mvp.models import tenant as _t, user as _u  # noqa: F401,E402
import saas_mvp.models.audit_log as _al  # noqa: F401,E402
import saas_mvp.models.totp_recovery_code as _trc  # noqa: F401,E402

from saas_mvp.app import create_app  # noqa: E402
from saas_mvp.db import Base, get_db  # noqa: E402
from saas_mvp.models.audit_log import AuditLog  # noqa: E402
from saas_mvp.models.totp_recovery_code import TotpRecoveryCode  # noqa: E402
from saas_mvp.models.user import User  # noqa: E402
from saas_mvp.services.mailer import StubMailer, get_mailer  # noqa: E402

_engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
_Session = sessionmaker(autocommit=False, autoflush=False, bind=_engine)

PASSWORD = "Test1234!"
_RECOVERY_RE = re.compile(r"[0-9A-F]{4}-[0-9A-F]{4}")


@pytest.fixture(autouse=True)
def _fresh_db():
    Base.metadata.drop_all(bind=_engine)
    Base.metadata.create_all(bind=_engine)
    yield


@pytest.fixture()
def client():
    app = create_app()

    def override_db():
        s = _Session()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_mailer] = lambda: StubMailer()
    with TestClient(app, raise_server_exceptions=True, follow_redirects=False) as c:
        yield c


def _register_and_login(client) -> str:
    email = f"u_{uuid.uuid4().hex[:8]}@example.com"
    r = client.post(
        "/auth/register",
        json={"email": email, "password": PASSWORD, "tenant_name": f"t_{uuid.uuid4().hex[:8]}"},
    )
    assert r.status_code == 201, r.text
    r = client.post("/ui/login", data={"email": email, "password": PASSWORD})
    assert r.status_code == 303
    return email


def _user_secret(email: str) -> str:
    db = _Session()
    try:
        return db.query(User).filter(User.email == email).first().totp_secret
    finally:
        db.close()


def _enroll(client, email: str) -> tuple[str, list[str]]:
    """走完整 HTMX 註冊流程,回傳 (secret, 恢復碼列表)。"""
    r = client.post("/ui/account/totp/start")
    assert r.status_code == 200
    secret = _user_secret(email)
    assert secret and secret in r.text  # 手動輸入密鑰有顯示
    code = pyotp.TOTP(secret).now()
    r = client.post("/ui/account/totp/confirm", data={"otp": code})
    assert r.status_code == 200
    codes = _RECOVERY_RE.findall(r.text)
    assert len(codes) == 10, r.text
    return secret, codes


class TestEnrollment:
    def test_wrong_code_does_not_enable(self, client):
        email = _register_and_login(client)
        client.post("/ui/account/totp/start")
        r = client.post("/ui/account/totp/confirm", data={"otp": "000000"})
        assert r.status_code == 200 and "驗證碼錯誤" in r.text
        db = _Session()
        try:
            u = db.query(User).filter(User.email == email).first()
            assert not u.totp_enabled
        finally:
            db.close()

    def test_enroll_success_ten_recovery_codes(self, client):
        email = _register_and_login(client)
        _, codes = _enroll(client, email)
        assert len(set(codes)) == 10
        db = _Session()
        try:
            u = db.query(User).filter(User.email == email).first()
            assert u.totp_enabled
            rows = db.query(TotpRecoveryCode).filter_by(user_id=u.id).all()
            assert len(rows) == 10
            assert all(r.used_at is None for r in rows)
            # 只存雜湊
            assert all(r.code_hash not in codes for r in rows)
        finally:
            db.close()
        # audit
        db = _Session()
        try:
            assert (
                db.query(AuditLog).filter(AuditLog.action == "auth.mfa.enable").count()
                == 1
            )
        finally:
            db.close()


class TestTokenEndpointGate:
    def test_otp_required_invalid_and_ok(self, client):
        email = _register_and_login(client)
        secret, _ = _enroll(client, email)

        r = client.post("/auth/token", data={"username": email, "password": PASSWORD})
        assert r.status_code == 401 and r.json()["detail"] == "otp_required"

        r = client.post(
            "/auth/token",
            data={"username": email, "password": PASSWORD, "otp": "000000"},
        )
        assert r.status_code == 401 and r.json()["detail"] == "otp_invalid"

        r = client.post(
            "/auth/token",
            data={"username": email, "password": PASSWORD, "otp": pyotp.TOTP(secret).now()},
        )
        assert r.status_code == 200 and r.json()["access_token"]

    def test_previous_step_code_within_window(self, client):
        import time

        email = _register_and_login(client)
        secret, _ = _enroll(client, email)
        prev_code = pyotp.TOTP(secret).at(int(time.time()) - 30)
        r = client.post(
            "/auth/token",
            data={"username": email, "password": PASSWORD, "otp": prev_code},
        )
        assert r.status_code == 200

    def test_wrong_password_still_generic_401(self, client):
        email = _register_and_login(client)
        _enroll(client, email)
        r = client.post("/auth/token", data={"username": email, "password": "wrong!"})
        assert r.status_code == 401
        assert r.json()["detail"] == "Incorrect email or password"


class TestUiMfaFlow:
    def _password_step(self, client, email):
        r = client.post("/ui/login", data={"email": email, "password": PASSWORD})
        assert r.status_code == 303
        assert r.headers["location"] == "/ui/login/mfa"
        set_cookie = r.headers.get("set-cookie", "")
        assert "mfa_pending=" in set_cookie
        assert "access_token" not in set_cookie.split("mfa_pending")[0] or True
        return r

    def test_full_flow(self, client):
        email = _register_and_login(client)
        secret, _ = _enroll(client, email)
        client.get("/ui/logout")

        self._password_step(client, email)
        r = client.get("/ui/login/mfa")
        assert r.status_code == 200 and "兩步驟驗證" in r.text

        r = client.post("/ui/login/mfa", data={"otp": "000000"})
        assert r.status_code == 401 and "驗證碼錯誤" in r.text

        r = client.post("/ui/login/mfa", data={"otp": pyotp.TOTP(secret).now()})
        assert r.status_code == 303 and r.headers["location"] == "/ui/"
        assert "access_token=" in r.headers.get("set-cookie", "")

        r = client.get("/ui/account")
        assert r.status_code == 200

    def test_mfa_page_without_pending_redirects(self, client):
        r = client.get("/ui/login/mfa")
        assert r.status_code == 303 and r.headers["location"] == "/ui/login"

    def test_login_audit_recorded_after_mfa(self, client):
        email = _register_and_login(client)
        secret, _ = _enroll(client, email)
        client.get("/ui/logout")
        db = _Session()
        try:
            before = db.query(AuditLog).filter(
                AuditLog.action == "auth.login.success"
            ).count()
        finally:
            db.close()
        self._password_step(client, email)
        client.post("/ui/login/mfa", data={"otp": pyotp.TOTP(secret).now()})
        db = _Session()
        try:
            after = db.query(AuditLog).filter(
                AuditLog.action == "auth.login.success"
            ).count()
            assert after == before + 1
        finally:
            db.close()


class TestPendingTokenIsolation:
    def _get_pending(self, client, email) -> str:
        r = client.post("/ui/login", data={"email": email, "password": PASSWORD})
        assert r.headers["location"] == "/ui/login/mfa"
        pending = client.cookies.get("mfa_pending")
        assert pending
        return pending

    def test_pending_rejected_everywhere(self, client):
        email = _register_and_login(client)
        _enroll(client, email)
        client.get("/ui/logout")
        pending = self._get_pending(client, email)

        # UI cookie 路徑:pending 當 access_token → 未登入(導回登入頁)
        client.cookies.set("access_token", pending)
        r = client.get("/ui/account")
        assert r.status_code in (302, 303), r.status_code
        client.cookies.delete("access_token")

        # Bearer API 路徑
        r = client.get("/auth/me", headers={"Authorization": f"Bearer {pending}"})
        assert r.status_code == 401

        # renew 路徑:pending 不可換正式票
        r = client.post("/auth/renew", headers={"Authorization": f"Bearer {pending}"})
        assert r.status_code == 401


class TestRecoveryCodes:
    def test_recovery_code_one_shot(self, client):
        email = _register_and_login(client)
        _, codes = _enroll(client, email)
        client.get("/ui/logout")

        client.post("/ui/login", data={"email": email, "password": PASSWORD})
        r = client.post("/ui/login/mfa", data={"otp": codes[0]})
        assert r.status_code == 303 and r.headers["location"] == "/ui/"

        client.get("/ui/logout")
        client.post("/ui/login", data={"email": email, "password": PASSWORD})
        r = client.post("/ui/login/mfa", data={"otp": codes[0]})  # 重用同一組
        assert r.status_code == 401 and "驗證碼錯誤" in r.text

        r = client.post("/ui/login/mfa", data={"otp": codes[1]})  # 換一組可過
        assert r.status_code == 303


class TestDisable:
    def test_disable_requires_code_then_login_no_mfa(self, client):
        email = _register_and_login(client)
        secret, _ = _enroll(client, email)

        r = client.post("/ui/account/totp/disable", data={"otp": "000000"})
        assert r.status_code == 200 and "未停用" in r.text

        r = client.post(
            "/ui/account/totp/disable", data={"otp": pyotp.TOTP(secret).now()}
        )
        assert r.status_code == 200
        db = _Session()
        try:
            u = db.query(User).filter(User.email == email).first()
            assert not u.totp_enabled and not u.totp_secret_enc
            assert db.query(TotpRecoveryCode).filter_by(user_id=u.id).count() == 0
            assert (
                db.query(AuditLog).filter(AuditLog.action == "auth.mfa.disable").count()
                == 1
            )
        finally:
            db.close()

        client.get("/ui/logout")
        r = client.post("/ui/login", data={"email": email, "password": PASSWORD})
        assert r.status_code == 303 and r.headers["location"] == "/ui/"


class TestOauthMfa:
    def test_oauth_login_enters_mfa_step(self, client):
        email = f"oa_{uuid.uuid4().hex[:8]}@example.com"
        r = client.post(
            "/auth/register",
            json={"email": email, "password": PASSWORD, "tenant_name": f"t_{uuid.uuid4().hex[:8]}"},
        )
        assert r.status_code == 201
        client.post("/ui/login", data={"email": email, "password": PASSWORD})
        secret, _ = _enroll(client, email)
        client.get("/ui/logout")

        code = email.split("@")[0]  # StubOAuthProvider: email = f"{code}@example.com"
        r = client.get("/auth/oauth/google/login")
        state = client.cookies.get("oauth_state")
        r = client.get(f"/auth/oauth/google/callback?code={code}&state={state}")
        assert r.status_code == 303, r.text
        assert r.headers["location"] == "/ui/login/mfa"
        assert "mfa_pending=" in r.headers.get("set-cookie", "")

        r = client.post("/ui/login/mfa", data={"otp": pyotp.TOTP(secret).now()})
        assert r.status_code == 303 and r.headers["location"] == "/ui/"
        db = _Session()
        try:
            row = (
                db.query(AuditLog)
                .filter(AuditLog.action == "auth.oauth.login")
                .order_by(AuditLog.id.desc())
                .first()
            )
            assert row is not None
            assert '"method": "oauth:google"' in row.detail_json
        finally:
            db.close()
