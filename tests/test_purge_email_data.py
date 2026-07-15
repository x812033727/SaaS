"""Email 稽核紀錄與 token 保留期限清理。"""

from __future__ import annotations

import datetime

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from saas_mvp.db import Base
from saas_mvp.models.email_delivery import EMAIL_PENDING, EMAIL_SENT, EmailDelivery
from saas_mvp.models.email_token import EmailToken, hash_token
from saas_mvp.ops.purge_email_data import purge_email_data

_NOW = datetime.datetime(2030, 6, 15, 12, 0, tzinfo=datetime.timezone.utc)


def _factory():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False)


def _seed(factory):
    with factory() as db:
        db.add_all([
            EmailDelivery(
                category="verify", recipient="old@example.com", subject="old",
                body="body", status=EMAIL_SENT,
                created_at=_NOW - datetime.timedelta(days=100),
                updated_at=_NOW - datetime.timedelta(days=100),
            ),
            EmailDelivery(
                category="verify", recipient="pending@example.com", subject="pending",
                body="body", status=EMAIL_PENDING,
                created_at=_NOW - datetime.timedelta(days=100),
                updated_at=_NOW - datetime.timedelta(days=100),
                next_attempt_at=_NOW,
            ),
            EmailDelivery(
                category="reset", recipient="recent@example.com", subject="recent",
                body="body", status=EMAIL_SENT,
                created_at=_NOW - datetime.timedelta(days=2),
                updated_at=_NOW - datetime.timedelta(days=2),
            ),
        ])
        db.add_all([
            EmailToken(
                user_id=1, purpose="verify", token_hash=hash_token("old-expired"),
                created_at=_NOW - datetime.timedelta(days=20),
                expires_at=_NOW - datetime.timedelta(days=10),
            ),
            EmailToken(
                user_id=1, purpose="reset", token_hash=hash_token("recent-expired"),
                created_at=_NOW - datetime.timedelta(days=2),
                expires_at=_NOW - datetime.timedelta(days=1),
            ),
            EmailToken(
                user_id=1, purpose="verify", token_hash=hash_token("active"),
                created_at=_NOW,
                expires_at=_NOW + datetime.timedelta(days=1),
            ),
        ])
        db.commit()


def test_dry_run_counts_without_deleting():
    factory = _factory()
    _seed(factory)
    result = purge_email_data(session_factory=factory, now=_NOW)
    assert result.dry_run
    assert (result.deliveries_purged, result.tokens_purged) == (1, 1)
    with factory() as db:
        assert db.scalar(select(func.count(EmailDelivery.id))) == 3
        assert db.scalar(select(func.count(EmailToken.id))) == 3


def test_apply_deletes_only_terminal_old_data():
    factory = _factory()
    _seed(factory)
    result = purge_email_data(session_factory=factory, apply=True, now=_NOW)
    assert not result.dry_run
    assert result.total == 2
    with factory() as db:
        recipients = set(db.execute(select(EmailDelivery.recipient)).scalars())
        token_hashes = set(db.execute(select(EmailToken.token_hash)).scalars())
    assert recipients == {"pending@example.com", "recent@example.com"}
    assert hash_token("old-expired") not in token_hashes
    assert hash_token("recent-expired") in token_hashes
    assert hash_token("active") in token_hashes
