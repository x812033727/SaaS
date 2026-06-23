"""OAuth 登入測試 — 既有 email 登入設 cookie、未知 email 403（不建租戶）、
CSRF state 防護、factory stub/real 切換、未知 provider 404。
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

from saas_mvp.app import create_app  # noqa: E402
from saas_mvp.db import Base, get_db  # noqa: E402
from saas_mvp.models.tenant import Tenant  # noqa: E402
from saas_mvp.models.user import User  # noqa: E402
from saas_mvp.services import oauth as oauth_svc  # noqa: E402

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
    # follow_redirects=False 讓我們檢查 302/303 + Set-Cookie。
    with TestClient(app, raise_server_exceptions=True, follow_redirects=False) as c:
        yield c


def _register(client, email: str) -> None:
    r = client.post("/auth/register", json={
        "email": email,
        "password": "Test1234!",
        "tenant_name": f"t_{uuid.uuid4().hex[:8]}",
    })
    assert r.status_code == 201, r.text


def _login_and_get_state(client, provider: str) -> str:
    """打 login 端點，回傳種下的 oauth_state 值（並讓 client jar 持有該 cookie）。"""
    r = client.get(f"/auth/oauth/{provider}/login")
    assert r.status_code == 302, r.text
    state = client.cookies.get("oauth_state")
    assert state
    return state


class TestOAuthLogin:
    def test_existing_email_logs_in(self, client):
        email = f"alice_{uuid.uuid4().hex[:8]}@example.com"
        _register(client, email)
        # stub: code == email 的 local part 之外，需讓 exchange 回該 email。
        # StubOAuthProvider 由 code 推導 email=f"{code}@example.com"，
        # 故 code 取 email 的 local part。
        code = email.split("@")[0]

        state = _login_and_get_state(client, "google")
        r = client.get(
            f"/auth/oauth/google/callback?code={code}&state={state}"
        )
        assert r.status_code == 303, r.text
        assert r.headers["location"] == "/ui/"
        # 設定登入 cookie
        assert "access_token" in r.headers.get("set-cookie", "")

        # 使用者被補上 oauth 連結
        db = _Session()
        try:
            u = db.query(User).filter(User.email == email).first()
            assert u.oauth_provider == "google"
            assert u.oauth_subject == f"stub-{code}"
        finally:
            db.close()

    def test_unknown_email_403_no_tenant_created(self, client):
        db = _Session()
        try:
            before = db.query(Tenant).count()
        finally:
            db.close()

        code = f"ghost_{uuid.uuid4().hex[:8]}"  # 該 email 從未註冊
        state = _login_and_get_state(client, "line")
        r = client.get(f"/auth/oauth/line/callback?code={code}&state={state}")
        assert r.status_code == 403

        db = _Session()
        try:
            after = db.query(Tenant).count()
        finally:
            db.close()
        # 關鍵：未知 email 不得自動建立租戶
        assert after == before

    def test_unverified_email_no_login(self, client, monkeypatch):
        """H2 回歸：provider 回未驗證 email → exchange 拋 OAuthError → callback 拒登入：
        不設 cookie、不建租戶。"""
        # 先註冊一個既有 email，證明即使該 email 已存在、未驗證仍不得登入。
        email = f"victim_{uuid.uuid4().hex[:8]}@example.com"
        _register(client, email)
        code = email.split("@")[0]

        db = _Session()
        try:
            tenants_before = db.query(Tenant).count()
        finally:
            db.close()

        # 旋鈕：讓 get_provider 回 email_verified=False 的 stub。
        def _unverified_provider(name, *, settings):
            return oauth_svc.StubOAuthProvider(name=name, email_verified=False)

        monkeypatch.setattr(oauth_svc, "get_provider", _unverified_provider)

        state = _login_and_get_state(client, "google")
        r = client.get(f"/auth/oauth/google/callback?code={code}&state={state}")
        # exchange 失敗 → callback 轉 400，不登入。
        assert r.status_code == 400
        assert "access_token" not in r.headers.get("set-cookie", "")

        db = _Session()
        try:
            assert db.query(Tenant).count() == tenants_before
        finally:
            db.close()

    def test_csrf_state_mismatch_rejected(self, client):
        _login_and_get_state(client, "google")
        # 故意送錯 state
        r = client.get("/auth/oauth/google/callback?code=whoever&state=WRONG")
        assert r.status_code == 400

    def test_missing_state_rejected(self, client):
        # 清掉 cookie jar 後直接打 callback（無 state cookie）
        client.cookies.clear()
        r = client.get("/auth/oauth/google/callback?code=x&state=y")
        assert r.status_code == 400

    def test_unknown_provider_404(self, client):
        assert client.get("/auth/oauth/wechat/login").status_code == 404
        assert client.get(
            "/auth/oauth/wechat/callback?code=a&state=b"
        ).status_code == 404


def _ui_login(client, email: str, password: str = "Test1234!") -> None:
    """以 UI 表單登入，讓 client jar 持有 access_token cookie。"""
    r = client.post(
        "/ui/login", data={"email": email, "password": password}
    )
    assert r.status_code == 303, r.text
    assert client.cookies.get("access_token")


def _link_login_and_get_state(client, provider: str) -> str:
    """打 login?link=1，回傳 state（並讓 jar 持有 oauth_state + oauth_intent）。"""
    r = client.get(f"/auth/oauth/{provider}/login?link=1")
    assert r.status_code == 302, r.text
    assert client.cookies.get("oauth_intent") == "link"
    state = client.cookies.get("oauth_state")
    assert state
    return state


class TestOAuthLink:
    def test_link_binds_to_current_user_regardless_of_email(self, client):
        """綁定模式：已登入者連結 LINE，即使 LINE 信箱 ≠ 後台信箱也綁到本人。"""
        email = f"owner_{uuid.uuid4().hex[:8]}@example.com"
        _register(client, email)
        _ui_login(client, email)

        # LINE 端身分用完全不同的 code（→ 不同 email），證明綁定不靠 email 配對。
        code = f"lineid_{uuid.uuid4().hex[:8]}"
        state = _link_login_and_get_state(client, "line")
        r = client.get(f"/auth/oauth/line/callback?code={code}&state={state}")
        assert r.status_code == 303, r.text
        assert r.headers["location"] == "/ui/account?linked=line"

        db = _Session()
        try:
            u = db.query(User).filter(User.email == email).first()
            assert u.oauth_provider == "line"
            assert u.oauth_subject == f"stub-{code}"
        finally:
            db.close()
        client.cookies.clear()

    def test_linked_subject_allows_login_with_different_email(self, client):
        """登入模式以 (provider, subject) 優先：LINE 信箱與後台信箱不同也能登入。"""
        email = f"owner2_{uuid.uuid4().hex[:8]}@example.com"
        _register(client, email)
        _ui_login(client, email)
        code = f"lineid2_{uuid.uuid4().hex[:8]}"
        state = _link_login_and_get_state(client, "line")
        assert client.get(
            f"/auth/oauth/line/callback?code={code}&state={state}"
        ).status_code == 303
        client.cookies.clear()

        # 之後純以 LINE 登入（同 subject，但 LINE email≠後台 email）→ 命中本人。
        state = _login_and_get_state(client, "line")
        r = client.get(f"/auth/oauth/line/callback?code={code}&state={state}")
        assert r.status_code == 303, r.text
        assert r.headers["location"] == "/ui/"
        assert "access_token" in r.headers.get("set-cookie", "")
        client.cookies.clear()

    def test_link_rejects_subject_already_bound_to_other_user(self, client):
        """帳號接管防護：同一 LINE 身分不得綁到第二個後台帳號。"""
        code = f"shared_{uuid.uuid4().hex[:8]}"

        # 使用者 A 先綁定該 LINE 身分。
        email_a = f"a_{uuid.uuid4().hex[:8]}@example.com"
        _register(client, email_a)
        _ui_login(client, email_a)
        state = _link_login_and_get_state(client, "line")
        assert client.get(
            f"/auth/oauth/line/callback?code={code}&state={state}"
        ).status_code == 303
        client.cookies.clear()

        # 使用者 B 嘗試綁定同一 LINE 身分 → 導回 account 帶 in_use 錯誤，且未綁定。
        email_b = f"b_{uuid.uuid4().hex[:8]}@example.com"
        _register(client, email_b)
        _ui_login(client, email_b)
        state = _link_login_and_get_state(client, "line")
        r = client.get(f"/auth/oauth/line/callback?code={code}&state={state}")
        assert r.status_code == 303
        assert r.headers["location"] == "/ui/account?oauth_error=in_use"

        db = _Session()
        try:
            b = db.query(User).filter(User.email == email_b).first()
            assert b.oauth_provider is None
            assert b.oauth_subject is None
        finally:
            db.close()
        client.cookies.clear()

    def test_unlink_clears_oauth_identity(self, client):
        email = f"unlink_{uuid.uuid4().hex[:8]}@example.com"
        _register(client, email)
        _ui_login(client, email)
        code = f"u_{uuid.uuid4().hex[:8]}"
        state = _link_login_and_get_state(client, "line")
        assert client.get(
            f"/auth/oauth/line/callback?code={code}&state={state}"
        ).status_code == 303

        r = client.post("/ui/account/oauth/unlink")
        assert r.status_code == 303
        assert r.headers["location"] == "/ui/account"

        db = _Session()
        try:
            u = db.query(User).filter(User.email == email).first()
            assert u.oauth_provider is None
            assert u.oauth_subject is None
        finally:
            db.close()
        client.cookies.clear()


class TestFactory:
    def test_returns_stub_when_unconfigured(self):
        class _S:
            line_login_channel_id = ""
            line_login_channel_secret = ""
            google_oauth_client_id = ""
            google_oauth_client_secret = ""

        assert isinstance(
            oauth_svc.get_provider("line", settings=_S()),
            oauth_svc.StubOAuthProvider,
        )
        assert isinstance(
            oauth_svc.get_provider("google", settings=_S()),
            oauth_svc.StubOAuthProvider,
        )

    def test_returns_real_when_configured(self):
        class _S:
            line_login_channel_id = "cid"
            line_login_channel_secret = "csecret"
            google_oauth_client_id = "gid"
            google_oauth_client_secret = "gsecret"

        assert isinstance(
            oauth_svc.get_provider("line", settings=_S()),
            oauth_svc.LineLoginProvider,
        )
        assert isinstance(
            oauth_svc.get_provider("google", settings=_S()),
            oauth_svc.GoogleOAuthProvider,
        )

    def test_unknown_provider_raises(self):
        class _S:
            pass

        with pytest.raises(oauth_svc.OAuthError):
            oauth_svc.get_provider("nope", settings=_S())

    def test_stub_deterministic(self):
        p = oauth_svc.StubOAuthProvider(name="google")
        out = p.exchange_code("alice", "https://cb")
        assert out == {
            "email": "alice@example.com",
            "subject": "stub-alice",
            "name": "alice",
        }
