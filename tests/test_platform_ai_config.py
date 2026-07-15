"""平台 AI 後台設定：權限、加密、動態生效與安全測試。"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from saas_mvp.ai import AnthropicAssistant, StubAIAssistant, get_assistant
from saas_mvp.ai.agent import AnthropicAgent, StubAgent, get_agent
from saas_mvp.app import create_app
from saas_mvp.config import settings
from saas_mvp.db import Base, get_db
from saas_mvp.models.audit_log import AuditLog
from saas_mvp.models.platform_ai_config import PlatformAIConfig
from saas_mvp.models.user import User
from saas_mvp.services import platform_ai_config as service

_engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
_Session = sessionmaker(autocommit=False, autoflush=False, bind=_engine)


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    monkeypatch.setattr(settings, "ai_model", "claude-sonnet-4-6")
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
    email = f"ai_admin_{uuid.uuid4().hex[:8]}@example.com"
    response = client.post(
        "/auth/register",
        json={
            "email": email,
            "password": "Test1234!",
            "tenant_name": f"AI {uuid.uuid4().hex[:8]}",
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


def test_regular_user_cannot_manage_platform_ai(client):
    _login(client, admin=False)
    assert client.get("/ui/admin/ai-settings").status_code == 403
    assert client.post(
        "/ui/admin/ai-settings",
        data={"api_key": "sk-ant-" + "x" * 32, "model": "claude-sonnet-4-6"},
    ).status_code == 403


def test_admin_saves_encrypted_key_and_all_ai_paths_change_immediately(client):
    email = _login(client, admin=True)
    key = "sk-ant-api03-" + "a" * 40
    response = client.post(
        "/ui/admin/ai-settings",
        data={"api_key": key, "model": "claude-sonnet-4-6"},
    )
    assert response.status_code == 303
    assert response.headers["location"].endswith("?saved=1")

    with _Session() as db:
        row = db.query(PlatformAIConfig).one()
        assert row.api_key == key
        assert key.encode() not in row.api_key_enc
        assert row.updated_by_user_id == db.query(User).filter_by(email=email).one().id
        assert isinstance(get_assistant(db), AnthropicAssistant)
        assert isinstance(get_agent(db), AnthropicAgent)
        assert db.query(AuditLog).filter_by(action="platform.ai.update").count() == 1

    page = client.get("/ui/admin/ai-settings")
    assert page.status_code == 200
    assert "Claude 已啟用" in page.text
    assert "資料庫加密設定" in page.text
    assert key not in page.text
    assert key[-4:] in page.text
    assert "不需修改 .env、重建或重啟" in page.text


def test_blank_key_preserves_existing_key_but_updates_model(client):
    _login(client, admin=True)
    original = "sk-ant-api03-" + "b" * 40
    with _Session() as db:
        service.save_ai_config(
            db,
            api_key=original,
            model="claude-sonnet-4-6",
            actor_user_id=1,
        )
        db.commit()

    response = client.post(
        "/ui/admin/ai-settings",
        data={"api_key": "", "model": "claude-opus-4-6"},
    )
    assert response.status_code == 303
    with _Session() as db:
        row = db.query(PlatformAIConfig).one()
        assert row.api_key == original
        assert row.model == "claude-opus-4-6"


def test_invalid_model_does_not_replace_configuration(client):
    _login(client, admin=True)
    response = client.post(
        "/ui/admin/ai-settings",
        data={"api_key": "sk-ant-api03-" + "c" * 40, "model": "not-a-model"},
    )
    assert response.status_code == 400
    assert "模型 ID" in response.text
    with _Session() as db:
        assert db.query(PlatformAIConfig).count() == 0


def test_test_connection_success_is_audited_without_key(client, monkeypatch):
    _login(client, admin=True)
    key = "sk-ant-api03-" + "d" * 40
    with _Session() as db:
        service.save_ai_config(
            db,
            api_key=key,
            model="claude-sonnet-4-6",
            actor_user_id=1,
        )
        db.commit()

    monkeypatch.setattr(service, "test_ai_config", lambda db, settings: None)
    response = client.post("/ui/admin/ai-settings/test")
    assert response.status_code == 200
    assert "連線、API Key 與模型測試成功" in response.text
    assert key not in response.text
    with _Session() as db:
        audit = db.query(AuditLog).filter_by(action="platform.ai.test").one()
        assert key not in str(audit.detail_json)


def test_reset_uses_environment_fallback_or_safe_stub(client, monkeypatch):
    _login(client, admin=True)
    with _Session() as db:
        service.save_ai_config(
            db,
            api_key="sk-ant-api03-" + "e" * 40,
            model="claude-sonnet-4-6",
            actor_user_id=1,
        )
        db.commit()

    response = client.post("/ui/admin/ai-settings/reset")
    assert response.status_code == 303
    with _Session() as db:
        assert db.query(PlatformAIConfig).count() == 0
        assert isinstance(get_assistant(db), StubAIAssistant)
        assert isinstance(get_agent(db), StubAgent)

    monkeypatch.setattr(settings, "anthropic_api_key", "sk-ant-env-" + "z" * 32)
    with _Session() as db:
        status = service.ai_status(db, settings)
        assert status["source"] == "environment"
        assert isinstance(get_assistant(db), AnthropicAssistant)


def test_unconfigured_page_explains_faq_fallback(client):
    _login(client, admin=True)
    response = client.get("/ui/admin/ai-settings")
    assert response.status_code == 200
    assert "FAQ 規則模式" in response.text
    assert "Anthropic Console" in response.text
