"""R5-D1 — 登入稽核(auth.login.*)+ 異常 IP email 通知(24h 冷卻)。

覆蓋:
- /auth/token 成功/失敗稽核落庫、last_login_at/ip 更新
- 失敗稽核防列舉:只存 email 雜湊,不存明文
- /ui/login 成功/失敗稽核
- OAuth callback 登入 → auth.oauth.login;未知帳號 → auth.login.failure
- 新 IP 登入 → login_alert email;同 IP 不寄;24h 冷卻防轟炸;首登不寄
- /ui/account 顯示上次登入;/ui/admin/audit 以 auth. 前綴可篩
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

from saas_mvp.models import tenant as _t, user as _u  # noqa: F401,E402
import saas_mvp.models.audit_log as _al  # noqa: F401,E402
import saas_mvp.models.email_delivery as _ed  # noqa: F401,E402

from saas_mvp.app import create_app  # noqa: E402
from saas_mvp.db import Base, get_db  # noqa: E402
from saas_mvp.models.audit_log import AuditLog  # noqa: E402
from saas_mvp.models.email_delivery import EmailDelivery  # noqa: E402
from saas_mvp.models.user import User  # noqa: E402
from saas_mvp.services.login_audit import email_digest  # noqa: E402
from saas_mvp.services.mailer import StubMailer, get_mailer  # noqa: E402

_engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
_Session = sessionmaker(autocommit=False, autoflush=False, bind=_engine)

PASSWORD = "Test1234!"


@pytest.fixture(autouse=True)
def _fresh_db():
    Base.metadata.drop_all(bind=_engine)
    Base.metadata.create_all(bind=_engine)
    yield


@pytest.fixture()
def stub_mailer():
    return StubMailer()


@pytest.fixture()
def client(stub_mailer):
    app = create_app()

    def override_db():
        s = _Session()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_mailer] = lambda: stub_mailer
    with TestClient(app, raise_server_exceptions=True, follow_redirects=False) as c:
        yield c


def _register(client, email: str) -> None:
    r = client.post(
        "/auth/register",
        json={
            "email": email,
            "password": PASSWORD,
            "tenant_name": f"t_{uuid.uuid4().hex[:8]}",
        },
    )
    assert r.status_code == 201, r.text


def _token_login(client, email: str, *, ip: str, password: str = PASSWORD):
    return client.post(
        "/auth/token",
        data={"username": email, "password": password},
        headers={"X-Forwarded-For": ip},
    )


def _audit_rows(action: str) -> list[AuditLog]:
    db = _Session()
    try:
        return (
            db.query(AuditLog).filter(AuditLog.action == action)
            .order_by(AuditLog.id).all()
        )
    finally:
        db.close()


def _alert_rows() -> list[EmailDelivery]:
    db = _Session()
    try:
        return (
            db.query(EmailDelivery)
            .filter(EmailDelivery.category == "login_alert")
            .all()
        )
    finally:
        db.close()


class TestTokenLoginAudit:
    def test_success_records_audit_and_last_login(self, client):
        email = f"a_{uuid.uuid4().hex[:8]}@example.com"
        _register(client, email)
        r = _token_login(client, email, ip="1.2.3.4")
        assert r.status_code == 200, r.text

        rows = _audit_rows("auth.login.success")
        assert len(rows) == 1
        assert rows[0].ip == "1.2.3.4"
        assert '"method": "password"' in rows[0].detail_json

        db = _Session()
        try:
            u = db.query(User).filter(User.email == email).first()
            assert u.last_login_ip == "1.2.3.4"
            assert u.last_login_at is not None
        finally:
            db.close()

    def test_failure_records_hashed_email_only(self, client):
        email = f"b_{uuid.uuid4().hex[:8]}@example.com"
        _register(client, email)
        r = _token_login(client, email, ip="1.2.3.4", password="wrong-pass")
        assert r.status_code == 401
        # 未註冊 email 也留失敗軌跡(統一路徑,無列舉差異)。
        r2 = _token_login(client, "ghost@example.com", ip="1.2.3.4")
        assert r2.status_code == 401

        rows = _audit_rows("auth.login.failure")
        assert len(rows) == 2
        for row, raw in zip(rows, (email, "ghost@example.com")):
            assert email_digest(raw) in row.detail_json
            assert raw not in row.detail_json  # 防列舉:明文不落庫

    def test_failure_does_not_touch_last_login(self, client):
        email = f"c_{uuid.uuid4().hex[:8]}@example.com"
        _register(client, email)
        _token_login(client, email, ip="1.2.3.4", password="wrong-pass")
        db = _Session()
        try:
            u = db.query(User).filter(User.email == email).first()
            assert u.last_login_at is None
            assert u.last_login_ip is None
        finally:
            db.close()


class TestUiLoginAudit:
    def test_ui_success_and_failure(self, client):
        email = f"d_{uuid.uuid4().hex[:8]}@example.com"
        _register(client, email)
        r = client.post(
            "/ui/login",
            data={"email": email, "password": "nope-nope"},
            headers={"X-Forwarded-For": "9.9.9.9"},
        )
        assert r.status_code == 401
        r = client.post(
            "/ui/login",
            data={"email": email, "password": PASSWORD},
            headers={"X-Forwarded-For": "9.9.9.9"},
        )
        assert r.status_code == 303
        assert len(_audit_rows("auth.login.failure")) == 1
        rows = _audit_rows("auth.login.success")
        assert len(rows) == 1 and rows[0].ip == "9.9.9.9"


class TestOauthLoginAudit:
    def test_oauth_login_and_unknown_user(self, client):
        email = f"e_{uuid.uuid4().hex[:8]}@example.com"
        _register(client, email)
        code = email.split("@")[0]  # StubOAuthProvider: email = f"{code}@example.com"

        r = client.get("/auth/oauth/google/login")
        assert r.status_code == 302
        state = client.cookies.get("oauth_state")
        r = client.get(f"/auth/oauth/google/callback?code={code}&state={state}")
        assert r.status_code == 303, r.text
        rows = _audit_rows("auth.oauth.login")
        assert len(rows) == 1
        assert '"method": "oauth:google"' in rows[0].detail_json

        # 未知帳號 → 403 + 失敗稽核
        r = client.get("/auth/oauth/line/login")
        state = client.cookies.get("oauth_state")
        r = client.get(
            f"/auth/oauth/line/callback?code=ghost_{uuid.uuid4().hex[:6]}&state={state}"
        )
        assert r.status_code == 403
        rows = _audit_rows("auth.login.failure")
        assert len(rows) == 1
        assert '"method": "oauth:line"' in rows[0].detail_json


class TestNewIpAlert:
    def test_first_login_no_alert(self, client, stub_mailer):
        email = f"f_{uuid.uuid4().hex[:8]}@example.com"
        _register(client, email)
        assert _token_login(client, email, ip="1.1.1.1").status_code == 200
        assert stub_mailer.sent == []
        assert _alert_rows() == []

    def test_same_ip_no_alert(self, client, stub_mailer):
        email = f"g_{uuid.uuid4().hex[:8]}@example.com"
        _register(client, email)
        _token_login(client, email, ip="1.1.1.1")
        _token_login(client, email, ip="1.1.1.1")
        assert stub_mailer.sent == []

    def test_new_ip_alert_then_cooldown(self, client, stub_mailer):
        email = f"h_{uuid.uuid4().hex[:8]}@example.com"
        _register(client, email)
        _token_login(client, email, ip="1.1.1.1")   # 首登:建立基準
        _token_login(client, email, ip="2.2.2.2")   # 新 IP → 通知
        assert len(stub_mailer.sent) == 1
        msg = stub_mailer.sent[0]
        assert msg.to == email
        assert "新位置登入" in msg.subject
        assert "2.2.2.2" in msg.body
        assert len(_alert_rows()) == 1

        _token_login(client, email, ip="3.3.3.3")   # 又換 IP:24h 冷卻內不再寄
        assert len(stub_mailer.sent) == 1
        assert len(_alert_rows()) == 1

    def test_alert_isolated_per_user(self, client, stub_mailer):
        e1 = f"i_{uuid.uuid4().hex[:8]}@example.com"
        e2 = f"j_{uuid.uuid4().hex[:8]}@example.com"
        _register(client, e1)
        _register(client, e2)
        _token_login(client, e1, ip="1.1.1.1")
        _token_login(client, e1, ip="2.2.2.2")  # e1 觸發
        _token_login(client, e2, ip="5.5.5.5")
        _token_login(client, e2, ip="6.6.6.6")  # e2 也要觸發(冷卻按 user 算)
        assert len(stub_mailer.sent) == 2
        assert {m.to for m in stub_mailer.sent} == {e1, e2}


class TestUiSurfaces:
    def test_account_page_shows_last_login(self, client):
        email = f"k_{uuid.uuid4().hex[:8]}@example.com"
        _register(client, email)
        r = client.post(
            "/ui/login",
            data={"email": email, "password": PASSWORD},
            headers={"X-Forwarded-For": "7.7.7.7"},
        )
        assert r.status_code == 303
        r = client.get("/ui/account")
        assert r.status_code == 200
        assert "上次登入" in r.text
        assert "7.7.7.7" in r.text

    def test_admin_audit_filter_auth_prefix(self, client):
        email = f"l_{uuid.uuid4().hex[:8]}@example.com"
        _register(client, email)
        db = _Session()
        try:
            u = db.query(User).filter(User.email == email).first()
            u.is_admin = True
            db.commit()
        finally:
            db.close()
        r = client.post(
            "/ui/login",
            data={"email": email, "password": PASSWORD},
            headers={"X-Forwarded-For": "8.8.8.8"},
        )
        assert r.status_code == 303
        r = client.get("/ui/admin/audit?action=auth.")
        assert r.status_code == 200
        assert "auth.login.success" in r.text
