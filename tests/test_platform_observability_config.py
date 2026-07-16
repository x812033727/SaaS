"""Platform Sentry settings: authorization, encryption and runtime reload."""

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
from saas_mvp.models.platform_observability_config import PlatformObservabilityConfig
from saas_mvp.models.user import User
from saas_mvp.services import platform_observability_config as service

_engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
_Session = sessionmaker(autocommit=False, autoflush=False, bind=_engine)


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(settings, "sentry_dsn", "")
    Base.metadata.drop_all(bind=_engine)
    Base.metadata.create_all(bind=_engine)
    app = create_app()

    def override_db():
        with _Session() as db:
            yield db

    app.dependency_overrides[get_db] = override_db
    with TestClient(app, follow_redirects=False) as test_client:
        yield test_client


def _login(client: TestClient, *, admin: bool) -> str:
    email = f"obs_{uuid.uuid4().hex[:8]}@example.com"
    response = client.post(
        "/auth/register",
        json={
            "email": email,
            "password": "Test1234!",
            "tenant_name": f"Obs {uuid.uuid4().hex[:8]}",
        },
    )
    assert response.status_code == 201
    if admin:
        with _Session() as db:
            user = db.query(User).filter_by(email=email).one()
            user.is_admin = True
            db.commit()
    assert client.post(
        "/ui/login", data={"email": email, "password": "Test1234!"}
    ).status_code == 303
    return email


def test_regular_user_cannot_manage_observability(client):
    _login(client, admin=False)
    assert client.get("/ui/admin/observability-settings").status_code == 403
    assert client.post(
        "/ui/admin/observability-settings",
        data={"sentry_dsn": "https://public@example.ingest.sentry.io/123"},
    ).status_code == 403


def test_admin_saves_encrypted_dsn_and_reloads_runtime(client, monkeypatch):
    email = _login(client, admin=True)
    dsn = "https://public@example.ingest.sentry.io/123456"
    initialized = []
    monkeypatch.setattr(
        "saas_mvp.obs.alerts.init_sentry", lambda value=None: initialized.append(value) or True
    )

    response = client.post(
        "/ui/admin/observability-settings", data={"sentry_dsn": dsn}
    )
    assert response.status_code == 303
    assert response.headers["location"].endswith("?saved=1")
    assert initialized == [dsn]

    with _Session() as db:
        row = db.query(PlatformObservabilityConfig).one()
        assert row.sentry_dsn == dsn
        assert dsn.encode() not in row.sentry_dsn_enc
        assert row.updated_by_user_id == db.query(User).filter_by(email=email).one().id
        assert (
            db.query(AuditLog)
            .filter_by(action="platform.observability.update")
            .count()
            == 1
        )

    page = client.get("/ui/admin/observability-settings")
    assert page.status_code == 200
    assert "錯誤監控已啟用" in page.text
    assert "資料庫加密設定" in page.text
    assert dsn not in page.text


def test_invalid_dsn_is_rejected(client):
    _login(client, admin=True)
    response = client.post(
        "/ui/admin/observability-settings", data={"sentry_dsn": "not-a-dsn"}
    )
    assert response.status_code == 400
    assert "DSN 格式不正確" in response.text
    with _Session() as db:
        assert db.query(PlatformObservabilityConfig).count() == 0


def test_test_event_is_audited_without_exposing_dsn(client, monkeypatch):
    _login(client, admin=True)
    dsn = "https://public@example.ingest.sentry.io/789"
    with _Session() as db:
        service.save_observability_config(db, sentry_dsn=dsn, actor_user_id=1)
        db.commit()
    monkeypatch.setattr(service, "send_test_event", lambda db, settings: None)

    response = client.post("/ui/admin/observability-settings/test")
    assert response.status_code == 200
    assert "測試事件已送出" in response.text
    assert dsn not in response.text
    with _Session() as db:
        audit = (
            db.query(AuditLog)
            .filter_by(action="platform.observability.test")
            .one()
        )
        assert dsn not in str(audit.detail_json)


def test_reset_reloads_environment_fallback(client, monkeypatch):
    _login(client, admin=True)
    with _Session() as db:
        service.save_observability_config(
            db,
            sentry_dsn="https://public@example.ingest.sentry.io/456",
            actor_user_id=1,
        )
        db.commit()
    env_dsn = "https://env@example.ingest.sentry.io/999"
    monkeypatch.setattr(settings, "sentry_dsn", env_dsn)
    initialized = []
    monkeypatch.setattr(
        "saas_mvp.obs.alerts.init_sentry", lambda value=None: initialized.append(value) or True
    )

    response = client.post("/ui/admin/observability-settings/reset")
    assert response.status_code == 303
    assert initialized == [env_dsn]
    with _Session() as db:
        assert db.query(PlatformObservabilityConfig).count() == 0
        assert service.observability_status(db, settings)["source"] == "environment"
