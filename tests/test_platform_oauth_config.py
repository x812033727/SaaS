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
from saas_mvp.models.user import User
from saas_mvp.services import oauth as oauth_svc
from saas_mvp.services import platform_oauth_config as platform_oauth_svc

_engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
_Session = sessionmaker(autocommit=False, autoflush=False, bind=_engine)


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
