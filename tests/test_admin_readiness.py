"""平台上線檢查中心：權限、友善說明、設定連結與統計。"""

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
from saas_mvp.models.user import User
from saas_mvp.ops.check_readiness import Check
from saas_mvp.services import platform_email_config as email_config_svc
from saas_mvp.services import readiness_dashboard as dashboard_svc

_engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
_Session = sessionmaker(autocommit=False, autoflush=False, bind=_engine)


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(settings, "ui_csrf_enabled", True)
    monkeypatch.setattr(settings, "payment_provider", "stub")
    monkeypatch.setattr(settings, "invoice_provider", "stub")
    monkeypatch.setattr(settings, "smtp_host", "")
    monkeypatch.setattr(settings, "sentry_dsn", "")
    monkeypatch.setattr(settings, "google_oauth_client_id", "")
    monkeypatch.setattr(settings, "google_oauth_client_secret", "")
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
    email = f"ready_{uuid.uuid4().hex[:8]}@example.com"
    response = client.post(
        "/auth/register",
        json={
            "email": email,
            "password": "Test1234!",
            "tenant_name": f"Ready {uuid.uuid4().hex[:8]}",
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


def test_regular_user_cannot_view_platform_readiness(client):
    _login(client, admin=False)
    assert client.get("/ui/admin/readiness").status_code == 403


def test_admin_page_is_actionable_and_contains_no_terminal_instructions(client):
    _login(client, admin=True)
    response = client.get("/ui/admin/readiness")
    assert response.status_code == 200
    assert "商用上線檢查" in response.text
    assert "前往金流設定" in response.text
    assert "前往發票設定" in response.text
    assert "前往寄信設定" in response.text
    assert "前往 AI 設定" in response.text
    assert "前往登入設定" in response.text
    assert "備份" in response.text and "主機健康機制" in response.text
    for forbidden in ("SAAS_", ".env", "docker-compose", "python -m", "終端機"):
        assert forbidden not in response.text


def test_overview_shows_readiness_summary_and_link(client):
    _login(client, admin=True)
    response = client.get("/ui/admin")
    assert response.status_code == 200
    assert "商用上線狀態" in response.text
    assert "完成度" in response.text
    assert 'href="/ui/admin/readiness"' in response.text


def test_database_smtp_is_reflected_immediately(client):
    email = _login(client, admin=True)
    with _Session() as db:
        user = db.query(User).filter_by(email=email).one()
        email_config_svc.save_email_config(
            db,
            host="smtp.example.com",
            port=587,
            user="mailer@example.com",
            password="secret-password",
            from_address="mailer@example.com",
            actor_user_id=user.id,
        )
        db.commit()
    response = client.get("/ui/admin/readiness")
    assert response.status_code == 200
    assert "Email 寄送服務已設定。" in response.text
    assert "secret-password" not in response.text


def test_dashboard_omits_host_only_checks_and_sorts_urgent_first():
    # 使用獨立 session，並以固定檢查結果驗證 UI 彙整，不依賴主機檔案狀態。
    Base.metadata.drop_all(bind=_engine)
    Base.metadata.create_all(bind=_engine)
    checks = [
        Check("smtp", "PASS", "secret raw detail"),
        Check("backup", "WARN", "host-only"),
        Check("payment", "WARN", "provider=stub"),
        Check("ui_csrf", "FAIL", "technical raw detail"),
        Check("scheduler", "FAIL", "host-only"),
    ]
    with pytest.MonkeyPatch.context() as patcher:
        patcher.setattr(dashboard_svc, "run_checks", lambda **kwargs: checks)
        with _Session() as session:
            result = dashboard_svc.build_dashboard(session)
    assert [item.key for item in result.items] == ["ui_csrf", "payment", "smtp"]
    assert (result.fails, result.warns, result.passes, result.progress) == (1, 1, 1, 33)
    assert "technical raw detail" not in result.items[0].detail
    assert result.launch_safe is False
