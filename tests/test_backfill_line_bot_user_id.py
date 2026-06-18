"""Offline tests for saas_mvp.ops.backfill_line_bot_user_id."""

from __future__ import annotations

import os
from io import StringIO

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault(
    "SAAS_LINE_CHANNEL_ENCRYPT_KEY",
    "ZGV2LWxpbmUtc2VjcmV0LWtleS0zMmJ5dGVzLWxvbmc=",
)

from saas_mvp.db import Base
from saas_mvp.models import user as _u, note as _n  # noqa: F401
from saas_mvp.models import api_key as _ak, api_key_usage as _aku  # noqa: F401
from saas_mvp.models import usage as _us, plan_change_history as _pch  # noqa: F401
from saas_mvp.models.line_channel_config import LineChannelConfig
from saas_mvp.models.tenant import Tenant
from saas_mvp.ops.backfill_line_bot_user_id import (
    backfill_line_bot_user_ids,
    main,
)

UID_A = "U" + "a" * 32
UID_B = "U" + "b" * 32


class FakeBotInfoClient:
    def __init__(self, responses: dict[str, str | None | Exception]) -> None:
        self.responses = responses
        self.calls: list[str] = []

    def get_user_id(self, access_token: str) -> str | None:
        self.calls.append(access_token)
        response = self.responses[access_token]
        if isinstance(response, Exception):
            raise response
        return response


@pytest.fixture()
def db_session_factory():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    yield Session
    engine.dispose()


def _tenant(db, name: str) -> Tenant:
    tenant = Tenant(name=name, plan="free")
    db.add(tenant)
    db.commit()
    db.refresh(tenant)
    return tenant


def _config(
    db,
    tenant: Tenant,
    *,
    token: str,
    secret: str = "secret",
    line_bot_user_id: str | None = None,
) -> LineChannelConfig:
    cfg = LineChannelConfig(tenant_id=tenant.id)
    cfg.channel_secret = secret
    cfg.access_token = token
    cfg.line_bot_user_id = line_bot_user_id
    db.add(cfg)
    db.commit()
    db.refresh(cfg)
    return cfg


def _stored_bot_id(Session, tenant_id: int) -> str | None:
    with Session() as db:
        cfg = (
            db.query(LineChannelConfig)
            .filter(LineChannelConfig.tenant_id == tenant_id)
            .one()
        )
        return cfg.line_bot_user_id


def test_apply_updates_null_config(db_session_factory):
    with db_session_factory() as db:
        tenant = _tenant(db, "acme")
        _config(db, tenant, token="tok-a")
        tenant_id = tenant.id

    fake = FakeBotInfoClient({"tok-a": UID_A})
    results = backfill_line_bot_user_ids(
        session_factory=db_session_factory,
        bot_info_client=fake,
        apply=True,
    )

    assert [(r.tenant_id, r.status, r.reason) for r in results] == [
        (tenant_id, "updated", "applied")
    ]
    assert fake.calls == ["tok-a"]
    assert _stored_bot_id(db_session_factory, tenant_id) == UID_A


def test_dry_run_calls_bot_info_but_does_not_commit(db_session_factory):
    with db_session_factory() as db:
        tenant = _tenant(db, "acme")
        _config(db, tenant, token="tok-a")
        tenant_id = tenant.id

    fake = FakeBotInfoClient({"tok-a": UID_A})
    results = backfill_line_bot_user_ids(
        session_factory=db_session_factory,
        bot_info_client=fake,
        apply=False,
    )

    assert results[0].status == "updated"
    assert results[0].reason == "dry_run"
    assert fake.calls == ["tok-a"]
    assert _stored_bot_id(db_session_factory, tenant_id) is None


def test_already_set_is_not_overwritten(db_session_factory):
    with db_session_factory() as db:
        tenant = _tenant(db, "acme")
        _config(db, tenant, token="tok-a", line_bot_user_id=UID_A)
        tenant_id = tenant.id

    fake = FakeBotInfoClient({"tok-a": UID_B})

    assert backfill_line_bot_user_ids(
        session_factory=db_session_factory,
        bot_info_client=fake,
        apply=True,
    ) == []

    tenant_result = backfill_line_bot_user_ids(
        session_factory=db_session_factory,
        bot_info_client=fake,
        apply=True,
        tenant_id=tenant_id,
    )

    assert tenant_result[0].status == "skipped"
    assert tenant_result[0].reason == "already_set"
    assert fake.calls == []
    assert _stored_bot_id(db_session_factory, tenant_id) == UID_A


def test_bot_info_failure_does_not_stop_other_tenants(db_session_factory):
    with db_session_factory() as db:
        bad = _tenant(db, "bad")
        good = _tenant(db, "good")
        _config(db, bad, token="tok-bad")
        _config(db, good, token="tok-good")
        bad_id = bad.id
        good_id = good.id

    fake = FakeBotInfoClient(
        {"tok-bad": RuntimeError("line unavailable"), "tok-good": UID_B}
    )
    results = backfill_line_bot_user_ids(
        session_factory=db_session_factory,
        bot_info_client=fake,
        apply=True,
    )

    assert [(r.tenant_id, r.status, r.reason) for r in results] == [
        (bad_id, "failed", "bot_info_error"),
        (good_id, "updated", "applied"),
    ]
    assert _stored_bot_id(db_session_factory, bad_id) is None
    assert _stored_bot_id(db_session_factory, good_id) == UID_B


def test_duplicate_user_id_is_reported_as_conflict(db_session_factory):
    with db_session_factory() as db:
        owner = _tenant(db, "owner")
        target = _tenant(db, "target")
        _config(db, owner, token="tok-owner", line_bot_user_id=UID_A)
        _config(db, target, token="tok-target")
        owner_id = owner.id
        target_id = target.id

    fake = FakeBotInfoClient({"tok-target": UID_A})
    results = backfill_line_bot_user_ids(
        session_factory=db_session_factory,
        bot_info_client=fake,
        apply=True,
    )

    assert len(results) == 1
    assert results[0].tenant_id == target_id
    assert results[0].status == "conflict"
    assert results[0].reason == "duplicate_line_bot_user_id"
    assert results[0].conflict_tenant_id == owner_id
    assert _stored_bot_id(db_session_factory, target_id) is None


def test_cli_report_has_summary_and_hides_secrets_tokens_user_ids(db_session_factory):
    with db_session_factory() as db:
        tenant = _tenant(db, "acme")
        _config(db, tenant, token="visible-token", secret="visible-secret")

    fake = FakeBotInfoClient({"visible-token": UID_A})
    out = StringIO()

    exit_code = main(
        ["--dry-run"],
        session_factory=db_session_factory,
        bot_info_client=fake,
        stdout=out,
    )

    text = out.getvalue()
    assert exit_code == 0
    assert "mode=dry_run" in text
    assert "summary total=1 updated=1 skipped=0 failed=0 conflict=0" in text
    assert "visible-token" not in text
    assert "visible-secret" not in text
    assert UID_A not in text
