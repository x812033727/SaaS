"""平台 SMTP 後台設定：權限、加密、動態 Mailer 與測試信。"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from saas_mvp.app import create_app
from saas_mvp.db import Base, get_db
from saas_mvp.models.audit_log import AuditLog
from saas_mvp.models.platform_email_config import PlatformEmailConfig
from saas_mvp.models.user import User
from saas_mvp.services import mailer as mailer_svc
from saas_mvp.services.mailer import StubMailer, get_mailer

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

    def override_db():
        with _Session() as db:
            yield db

    app.dependency_overrides[get_db] = override_db
    with TestClient(app, follow_redirects=False) as c:
        yield c


def _login(client: TestClient, *, admin: bool) -> str:
    email = f"mail_admin_{uuid.uuid4().hex[:8]}@example.com"
    response = client.post(
        "/auth/register",
        json={
            "email": email,
            "password": "Test1234!",
            "tenant_name": f"Mail {uuid.uuid4().hex[:8]}",
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


def test_regular_user_cannot_manage_smtp(client):
    _login(client, admin=False)
    assert client.get("/ui/admin/email-settings").status_code == 403


def test_admin_saves_encrypted_smtp_and_it_is_immediately_effective(client):
    _login(client, admin=True)
    password = "smtp-app-password"
    response = client.post(
        "/ui/admin/email-settings",
        data={
            "smtp_host": "smtp.example.com",
            "smtp_port": "587",
            "smtp_user": "mailer@example.com",
            "smtp_password": password,
            "smtp_from": "mailer@example.com",
        },
    )
    assert response.status_code == 303
    with _Session() as db:
        row = db.query(PlatformEmailConfig).one()
        assert row.smtp_password == password
        assert password.encode() not in row.smtp_password_enc
        assert isinstance(mailer_svc.get_mailer(db), mailer_svc.SmtpMailer)

    page = client.get("/ui/admin/email-settings")
    assert page.status_code == 200
    assert "資料庫加密設定" in page.text
    assert password not in page.text
    assert 'name="csrf_token"' in page.text


def test_admin_can_send_test_mail_without_exposing_password(client):
    email = _login(client, admin=True)
    with _Session() as db:
        from saas_mvp.services import platform_email_config as service

        service.save_email_config(
            db,
            host="smtp.example.com",
            port=587,
            user="mailer@example.com",
            password="secret-password",
            from_address="mailer@example.com",
            actor_user_id=1,
        )
        db.commit()
    stub = StubMailer()
    client.app.dependency_overrides[get_mailer] = lambda: stub
    response = client.post("/ui/admin/email-settings/test")
    assert response.status_code == 200
    assert len(stub.sent) == 1
    assert stub.sent[0].to == email
    assert "測試信已寄到" in response.text
    with _Session() as db:
        event = db.query(AuditLog).filter_by(action="platform.email.test").one()
        assert '"result": "sent"' in event.detail_json


def test_failed_test_mail_is_actionable_and_audited(client):
    _login(client, admin=True)

    class FailingMailer(mailer_svc.Mailer):
        def send(self, *, to: str, subject: str, body: str) -> None:
            raise mailer_svc.MailerError(
                "SMTP 服務暫時拒絕收信，可能正在限流或維護，請稍後再試。"
            )

    client.app.dependency_overrides[get_mailer] = lambda: FailingMailer()
    response = client.post("/ui/admin/email-settings/test")

    assert response.status_code == 502
    assert "可能正在限流或維護" in response.text
    with _Session() as db:
        event = db.query(AuditLog).filter_by(action="platform.email.test").one()
        assert '"result": "failed"' in event.detail_json
        assert "password" not in event.detail_json.lower()


def test_invalid_smtp_port_is_rejected(client):
    _login(client, admin=True)
    response = client.post(
        "/ui/admin/email-settings",
        data={
            "smtp_host": "smtp.example.com",
            "smtp_port": "70000",
            "smtp_user": "mailer@example.com",
            "smtp_password": "secret-password",
            "smtp_from": "mailer@example.com",
        },
    )
    assert response.status_code == 400
    assert "1–65535" in response.text


def test_hostinger_requires_full_email_username(client):
    _login(client, admin=True)
    response = client.post(
        "/ui/admin/email-settings",
        data={
            "smtp_host": "smtp.hostinger.com",
            "smtp_port": "465",
            "smtp_user": "",
            "smtp_password": "secret-password",
            "smtp_from": "mailer@example.com",
        },
    )
    assert response.status_code == 400
    assert "Hostinger SMTP 帳號必須填寫完整 Email" in response.text
