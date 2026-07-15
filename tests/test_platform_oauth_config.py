"""平台 LINE Login 後台設定：權限、加密、即時生效與環境備援。"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from saas_mvp.app import create_app
from saas_mvp.config import settings
from saas_mvp.db import Base, get_db
from saas_mvp.models.audit_log import AuditLog
from saas_mvp.models.platform_oauth_config import PlatformOAuthConfig
from saas_mvp.models.tenant_gcal_credential import TenantGcalCredential
from saas_mvp.models.user import User
from saas_mvp.services import oauth as oauth_svc
from saas_mvp.services import platform_oauth_config as platform_oauth_svc

_engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
_Session = sessionmaker(autocommit=False, autoflush=False, bind=_engine)
_GOOGLE_CLIENT_ID = "1234567890-abcdef.apps.googleusercontent.com"
_GOOGLE_SECRET = "GOCSPX-abcdefghijklmnopqrstuv"


@pytest.fixture()
def client():
    Base.metadata.drop_all(bind=_engine)
    Base.metadata.create_all(bind=_engine)
    app = create_app()

    def override_get_db():
        with _Session() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app, follow_redirects=False) as test_client:
        yield test_client


def _register_and_login(client: TestClient, *, admin: bool) -> str:
    email = f"oauth_settings_{uuid.uuid4().hex[:8]}@example.com"
    response = client.post(
        "/auth/register",
        json={
            "email": email,
            "password": "Test1234!",
            "tenant_name": f"OAuth settings {uuid.uuid4().hex[:8]}",
        },
    )
    assert response.status_code == 201
    if admin:
        with _Session() as db:
            user = db.query(User).filter_by(email=email).one()
            user.is_admin = True
            db.commit()
    response = client.post(
        "/ui/login", data={"email": email, "password": "Test1234!"}
    )
    assert response.status_code == 303
    return email


def test_only_platform_admin_can_open_settings(client):
    _register_and_login(client, admin=False)
    response = client.get("/ui/admin/oauth-settings")
    assert response.status_code == 403


def test_login_hides_unconfigured_oauth_buttons(client, monkeypatch):
    monkeypatch.setattr(settings, "line_login_channel_id", "")
    monkeypatch.setattr(settings, "line_login_channel_secret", "")
    monkeypatch.setattr(settings, "google_oauth_client_id", "")
    monkeypatch.setattr(settings, "google_oauth_client_secret", "")
    response = client.get("/ui/login")
    assert "使用 LINE 登入" not in response.text
    assert "使用 Google 登入" not in response.text


def test_non_admin_cannot_change_google_credentials(client):
    _register_and_login(client, admin=False)
    response = client.post(
        "/ui/admin/oauth-settings/google",
        data={"client_id": _GOOGLE_CLIENT_ID, "client_secret": _GOOGLE_SECRET},
    )
    assert response.status_code == 403
    with _Session() as db:
        assert db.query(PlatformOAuthConfig).count() == 0


def test_admin_saves_encrypted_credentials_and_enables_every_user(client):
    email = _register_and_login(client, admin=True)
    response = client.get("/ui/admin/oauth-settings")
    assert response.status_code == 200
    assert "一般使用者" in response.text
    assert "docker-compose" not in response.text

    secret = "0123456789abcdef0123456789abcdef"
    response = client.post(
        "/ui/admin/oauth-settings/line",
        data={"channel_id": "1234567890", "channel_secret": secret},
    )
    assert response.status_code == 303
    assert response.headers["location"].endswith("?saved=1")

    with _Session() as db:
        row = db.query(PlatformOAuthConfig).one()
        assert row.client_id == "1234567890"
        assert row.client_secret == secret
        assert secret.encode() not in row.client_secret_enc
        assert row.updated_by_user_id == db.query(User).filter_by(email=email).one().id
        assert db.query(AuditLog).filter_by(action="platform.oauth.line.update").count() == 1
        assert oauth_svc.provider_credentials_present("line", settings=settings, db=db)
        provider = oauth_svc.get_provider("line", settings=settings, db=db)
        assert isinstance(provider, oauth_svc.LineLoginProvider)

    response = client.get("/ui/account")
    assert response.status_code == 200
    assert 'data-testid="line-link-button"' in response.text
    assert secret not in response.text

    # 憑證由平台管理員設定，但一般使用者也能連結與使用 LINE 登入。
    client.cookies.clear()
    _register_and_login(client, admin=False)
    response = client.get("/ui/account")
    assert response.status_code == 200
    assert 'data-testid="line-link-button"' in response.text
    response = client.get("/auth/oauth/line/login?link=1")
    assert response.status_code == 302
    assert "client_id=1234567890" in response.headers["location"]


def test_blank_secret_keeps_existing_encrypted_secret(client):
    _register_and_login(client, admin=True)
    original = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    with _Session() as db:
        platform_oauth_svc.save_line_credentials(
            db,
            channel_id="1234567890",
            channel_secret=original,
            actor_user_id=1,
        )
        db.commit()

    response = client.post(
        "/ui/admin/oauth-settings/line",
        data={"channel_id": "9876543210", "channel_secret": ""},
    )
    assert response.status_code == 303
    with _Session() as db:
        row = db.query(PlatformOAuthConfig).one()
        assert row.client_id == "9876543210"
        assert row.client_secret == original


def test_invalid_channel_id_does_not_store_secret(client):
    _register_and_login(client, admin=True)
    response = client.post(
        "/ui/admin/oauth-settings/line",
        data={"channel_id": "not-a-number", "channel_secret": "x" * 32},
    )
    assert response.status_code == 400
    assert "5–20 位數字" in response.text
    assert "x" * 32 not in response.text
    with _Session() as db:
        assert db.query(PlatformOAuthConfig).count() == 0


def test_database_override_can_be_removed(client, monkeypatch):
    _register_and_login(client, admin=True)
    monkeypatch.setattr(settings, "line_login_channel_id", "1111111111")
    monkeypatch.setattr(settings, "line_login_channel_secret", "fallback-secret-value")
    with _Session() as db:
        platform_oauth_svc.save_line_credentials(
            db,
            channel_id="2222222222",
            channel_secret="database-secret-value",
            actor_user_id=1,
        )
        db.commit()

    response = client.post("/ui/admin/oauth-settings/line/reset")
    assert response.status_code == 303
    with _Session() as db:
        assert db.query(PlatformOAuthConfig).count() == 0
        assert platform_oauth_svc.effective_line_credentials(db, settings) == (
            "1111111111",
            "fallback-secret-value",
        )


def test_admin_saves_google_credentials_encrypted_and_immediately_effective(client):
    email = _register_and_login(client, admin=True)
    page = client.get("/ui/admin/oauth-settings")
    assert page.status_code == 200
    assert "Google Cloud 設定教學" in page.text
    assert "http://testserver/ui/gcal/callback" in page.text
    assert "http://testserver/auth/oauth/google/callback" in page.text
    assert "docker-compose" not in page.text

    response = client.post(
        "/ui/admin/oauth-settings/google",
        data={"client_id": _GOOGLE_CLIENT_ID, "client_secret": _GOOGLE_SECRET},
    )
    assert response.status_code == 303
    assert response.headers["location"].endswith("?google_saved=1")
    saved_page = client.get("/ui/admin/oauth-settings?google_saved=1")
    assert "Google OAuth 設定已加密儲存並立即生效" in saved_page.text
    assert _GOOGLE_SECRET not in saved_page.text

    with _Session() as db:
        row = db.query(PlatformOAuthConfig).filter_by(provider="google").one()
        assert row.client_id == _GOOGLE_CLIENT_ID
        assert row.client_secret == _GOOGLE_SECRET
        assert _GOOGLE_SECRET.encode() not in row.client_secret_enc
        assert row.updated_by_user_id == db.query(User).filter_by(email=email).one().id
        assert db.query(AuditLog).filter_by(
            action="platform.oauth.google.update"
        ).count() == 1
        assert oauth_svc.provider_credentials_present("google", settings=settings, db=db)
        assert isinstance(
            oauth_svc.get_provider("google", settings=settings, db=db),
            oauth_svc.GoogleOAuthProvider,
        )
        from saas_mvp.services import gcal as gcal_svc

        gcal_client = gcal_svc.get_gcal_client(db)
        assert isinstance(gcal_client, gcal_svc.HttpGcalClient)
        assert gcal_client._client_id == _GOOGLE_CLIENT_ID

    # 不需重啟：同一個 TestClient 的下一個請求立即使用資料庫憑證。
    response = client.get("/ui/gcal/connect")
    assert response.status_code == 303
    assert f"client_id={_GOOGLE_CLIENT_ID}" in response.headers["location"]
    assert "redirect_uri=http%3A%2F%2Ftestserver%2Fui%2Fgcal%2Fcallback" in response.headers[
        "location"
    ]
    response = client.get("/auth/oauth/google/login")
    assert response.status_code == 302
    assert f"client_id={_GOOGLE_CLIENT_ID}" in response.headers["location"]
    login_page = client.get("/ui/login")
    assert "使用 Google 登入" in login_page.text


def test_blank_google_secret_keeps_existing_secret(client):
    _register_and_login(client, admin=True)
    with _Session() as db:
        platform_oauth_svc.save_google_credentials(
            db,
            client_id=_GOOGLE_CLIENT_ID,
            client_secret=_GOOGLE_SECRET,
            actor_user_id=1,
        )
        db.commit()

    replacement_id = "9876543210-newclient.apps.googleusercontent.com"
    response = client.post(
        "/ui/admin/oauth-settings/google",
        data={"client_id": replacement_id, "client_secret": ""},
    )
    assert response.status_code == 303
    with _Session() as db:
        row = db.query(PlatformOAuthConfig).filter_by(provider="google").one()
        assert row.client_id == replacement_id
        assert row.client_secret == _GOOGLE_SECRET


def test_google_calendar_callback_uses_database_credentials(client, monkeypatch):
    _register_and_login(client, admin=True)
    with _Session() as db:
        platform_oauth_svc.save_google_credentials(
            db,
            client_id=_GOOGLE_CLIENT_ID,
            client_secret=_GOOGLE_SECRET,
            actor_user_id=1,
        )
        db.commit()

    captured = {}

    def fake_post_form(url, data, **kwargs):
        captured.update(data)
        return {"refresh_token": "tenant-refresh-token"}

    monkeypatch.setattr(oauth_svc, "_post_form", fake_post_form)
    client.cookies.set("gcal_state", "state-token", path="/")
    response = client.get(
        "/ui/gcal/callback?code=auth-code&state=state-token"
    )
    assert response.status_code == 303
    assert captured["client_id"] == _GOOGLE_CLIENT_ID
    assert captured["client_secret"] == _GOOGLE_SECRET
    assert captured["redirect_uri"] == "http://testserver/ui/gcal/callback"
    with _Session() as db:
        credential = db.query(TenantGcalCredential).one()
        assert credential.refresh_token == "tenant-refresh-token"


def test_invalid_google_client_id_does_not_store_secret(client):
    _register_and_login(client, admin=True)
    response = client.post(
        "/ui/admin/oauth-settings/google",
        data={"client_id": "not-google", "client_secret": _GOOGLE_SECRET},
    )
    assert response.status_code == 400
    assert ".apps.googleusercontent.com" in response.text
    assert _GOOGLE_SECRET not in response.text
    with _Session() as db:
        assert db.query(PlatformOAuthConfig).filter_by(provider="google").count() == 0


def test_google_database_override_can_be_removed(client, monkeypatch):
    _register_and_login(client, admin=True)
    monkeypatch.setattr(settings, "google_oauth_client_id", _GOOGLE_CLIENT_ID)
    monkeypatch.setattr(settings, "google_oauth_client_secret", "environment-secret-value")
    with _Session() as db:
        platform_oauth_svc.save_google_credentials(
            db,
            client_id="2222222222-database.apps.googleusercontent.com",
            client_secret=_GOOGLE_SECRET,
            actor_user_id=1,
        )
        db.commit()

    response = client.post("/ui/admin/oauth-settings/google/reset")
    assert response.status_code == 303
    with _Session() as db:
        assert db.query(PlatformOAuthConfig).filter_by(provider="google").count() == 0
        assert platform_oauth_svc.effective_google_credentials(db, settings) == (
            _GOOGLE_CLIENT_ID,
            "environment-secret-value",
        )
        assert db.query(AuditLog).filter_by(
            action="platform.oauth.google.reset"
        ).count() == 1


def test_removing_last_google_config_marks_tenant_connections_error(
    client, monkeypatch
):
    email = _register_and_login(client, admin=True)
    monkeypatch.setattr(settings, "google_oauth_client_id", "")
    monkeypatch.setattr(settings, "google_oauth_client_secret", "")
    with _Session() as db:
        user = db.query(User).filter_by(email=email).one()
        platform_oauth_svc.save_google_credentials(
            db,
            client_id=_GOOGLE_CLIENT_ID,
            client_secret=_GOOGLE_SECRET,
            actor_user_id=user.id,
        )
        credential = TenantGcalCredential(
            tenant_id=user.tenant_id,
            calendar_id="primary",
        )
        credential.refresh_token = "existing-refresh-token"
        db.add(credential)
        db.commit()

    response = client.post("/ui/admin/oauth-settings/google/reset")
    assert response.status_code == 303
    with _Session() as db:
        credential = db.query(TenantGcalCredential).one()
        assert credential.status == "error"
        assert "平台 Google OAuth 設定已移除" in credential.last_error
