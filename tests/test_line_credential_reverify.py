from __future__ import annotations

import datetime

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from saas_mvp.config import settings
from saas_mvp.db import Base
from saas_mvp.line_client import StubLineBotInfoClient
from saas_mvp.models.line_channel_config import LineChannelConfig
from saas_mvp.models.tenant import Tenant
from saas_mvp.ops.reverify_line_credentials import reverify_stale_credentials
from saas_mvp.routers.tenants import TenantLineConfigResponse
from saas_mvp.services import line_config as service

_UID = "U" + "c" * 32
_NOW = datetime.datetime(2026, 7, 16, tzinfo=datetime.timezone.utc)


@pytest.fixture()
def factory():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(autocommit=False, autoflush=False, bind=engine)


def _config(factory, *, name: str, checked_at, status="valid") -> int:
    with factory() as db:
        tenant = Tenant(name=name, plan="free")
        db.add(tenant)
        db.flush()
        cfg = LineChannelConfig(
            tenant_id=tenant.id,
            credential_status=status,
            credential_checked_at=checked_at,
        )
        cfg.channel_secret = "secret"
        cfg.access_token = f"token-{name}"
        db.add(cfg)
        db.commit()
        return cfg.id


def test_credential_status_rejects_invalid_values(factory):
    with pytest.raises(ValueError):
        LineChannelConfig(credential_status="invlaid")
    with pytest.raises(ValidationError):
        TenantLineConfigResponse(
            tenant_id=1,
            has_channel_secret=True,
            has_access_token=True,
            default_target_lang="zh-TW",
            credential_status="invlaid",
            webhook_url="/line/webhook/1",
        )


def test_verify_budget_blocks_fourth_attempt_and_resets(factory, monkeypatch):
    config_id = _config(factory, name="budget", checked_at=_NOW)
    monkeypatch.setattr(settings, "line_verify_max_attempts_per_hour", 3)
    stub = StubLineBotInfoClient(_UID)
    with factory() as db:
        cfg = db.get(LineChannelConfig, config_id)
        for _ in range(3):
            assert service.verify_config_row(db, cfg, bot_info_client=stub) == "valid"
        assert service.verify_config_row(db, cfg, bot_info_client=stub) == "rate_limited"
        assert len(stub.calls) == 3
        assert "rate_limited" in cfg.credential_last_error

        cfg.verify_attempt_window_start = datetime.datetime.now(
            datetime.timezone.utc
        ) - datetime.timedelta(hours=2)
        db.commit()
        assert service.verify_config_row(db, cfg, bot_info_client=stub) == "valid"
        assert len(stub.calls) == 4


def test_reverify_only_stale_valid_credentials(factory):
    stale_id = _config(
        factory, name="stale", checked_at=_NOW - datetime.timedelta(hours=25)
    )
    fresh_id = _config(
        factory, name="fresh", checked_at=_NOW - datetime.timedelta(hours=2)
    )
    invalid_id = _config(
        factory,
        name="invalid",
        checked_at=_NOW - datetime.timedelta(hours=25),
        status="invalid",
    )
    stub = StubLineBotInfoClient(_UID)
    report = reverify_stale_credentials(
        session_factory=factory,
        bot_info_client=stub,
        now=_NOW,
        throttle_seconds=0,
    )
    assert report.scanned == 1 and report.valid == 1
    assert stub.calls == ["token-stale"]
    with factory() as db:
        assert db.get(LineChannelConfig, stale_id).credential_status == "valid"
        assert db.get(LineChannelConfig, fresh_id).credential_status == "valid"
        assert db.get(LineChannelConfig, invalid_id).credential_status == "invalid"


def test_reverify_circuit_breaker_stops_batch(factory, caplog):
    for i in range(4):
        _config(
            factory,
            name=f"broken-{i}",
            checked_at=_NOW - datetime.timedelta(hours=30),
        )
    stub = StubLineBotInfoClient(_UID, raises=True)
    report = reverify_stale_credentials(
        session_factory=factory,
        bot_info_client=stub,
        now=_NOW,
        throttle_seconds=0,
        circuit_breaker_failures=3,
    )
    assert report.scanned == 3
    assert report.error == 3
    assert report.circuit_open is True
    assert "circuit breaker opened" in caplog.text
