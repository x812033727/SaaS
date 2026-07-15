"""Email outbox：加密、立即成功、失敗重試與最終失敗。"""

from __future__ import annotations

import datetime

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from saas_mvp.db import Base
from saas_mvp.models.email_delivery import EMAIL_FAILED, EMAIL_PENDING, EMAIL_SENT, EmailDelivery
from saas_mvp.ops.send_due_emails import send_due_emails
from saas_mvp.services import email_delivery as service
from saas_mvp.services.mailer import MailerError, StubMailer


class _FailingMailer:
    def send(self, **_kwargs):
        raise MailerError("SMTP 暫時無法使用")


def _factory():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False)


def test_success_is_recorded_and_body_is_encrypted():
    factory = _factory()
    mailer = StubMailer()
    with factory() as db:
        outcome = service.deliver_or_queue(
            db,
            mailer,
            user_id=None,
            category="verify",
            recipient="user@example.com",
            subject="驗證",
            body="secret-token-link",
        )
        row = db.execute(select(EmailDelivery)).scalar_one()
        assert outcome == EMAIL_SENT
        assert row.status == EMAIL_SENT
        assert row.attempt_count == 1
        assert row.body == "secret-token-link"
        assert b"secret-token-link" not in row.body_enc


def test_failure_is_queued_then_scheduler_retries_successfully():
    factory = _factory()
    now = datetime.datetime.now(datetime.timezone.utc)
    with factory() as db:
        outcome = service.deliver_or_queue(
            db,
            _FailingMailer(),
            user_id=None,
            category="reset",
            recipient="user@example.com",
            subject="重設密碼",
            body="body",
        )
        row = db.execute(select(EmailDelivery)).scalar_one()
        assert outcome == EMAIL_PENDING
        assert row.attempt_count == 1
        row.next_attempt_at = now
        db.commit()

    results = send_due_emails(
        apply=True,
        now=now,
        session_factory=factory,
        mailer=StubMailer(),
    )
    assert results == [(1, EMAIL_SENT)]
    with factory() as db:
        row = db.get(EmailDelivery, 1)
        assert row.status == EMAIL_SENT
        assert row.attempt_count == 2


def test_fifth_failure_stops_retrying():
    factory = _factory()
    now = datetime.datetime.now(datetime.timezone.utc)
    with factory() as db:
        row = EmailDelivery(
            category="verify",
            recipient="user@example.com",
            subject="驗證",
            body="body",
            status=EMAIL_PENDING,
            attempt_count=4,
            next_attempt_at=now,
        )
        db.add(row)
        db.commit()
        service.attempt(db, row, _FailingMailer(), now=now)
        assert row.status == EMAIL_FAILED
        assert row.attempt_count == 5
        assert row.next_attempt_at is None
