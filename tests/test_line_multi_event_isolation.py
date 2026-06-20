"""LINE webhook multi-event isolation tests."""

from __future__ import annotations

import base64
import datetime
import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from saas_mvp.app import create_app
from saas_mvp.db import Base, get_db
from saas_mvp.line_client import FakeLineReplyClient, get_line_client
from saas_mvp.models import api_key as _ak, api_key_usage as _aku  # noqa: F401
from saas_mvp.models import note as _n, plan_change_history as _pch  # noqa: F401
from saas_mvp.models import tenant as _t, usage as _us, user as _u  # noqa: F401
import saas_mvp.models.line_channel_config as _lcm
import saas_mvp.models.line_user_lang as _lul  # noqa: F401
from saas_mvp.models.tenant import Tenant
from saas_mvp.models.usage import ApiUsage
from saas_mvp.translation import (
    TranslationError,
    TranslationResult,
    Translator,
    get_translator,
)


_engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
_Session = sessionmaker(autocommit=False, autoflush=False, bind=_engine)

_CHANNEL_SECRET = "test-channel-secret-32-bytes-x!!"
_ACCESS_TOKEN = "test-access-token-abc"


class FailsFirstTranslator(Translator):
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def translate(self, text: str, target_lang: str) -> TranslationResult:
        self.calls.append((text, target_lang))
        if len(self.calls) == 1:
            raise TranslationError("first event failed")
        return TranslationResult(f"[{target_lang.upper()}] {text}", None, False)

    def is_available(self) -> bool:
        return True


@pytest.fixture()
def app_client():
    Base.metadata.drop_all(bind=_engine)
    Base.metadata.create_all(bind=_engine)
    translator = FailsFirstTranslator()
    line_client = FakeLineReplyClient()
    app = create_app()

    def override_db():
        db = _Session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_translator] = lambda: translator
    app.dependency_overrides[get_line_client] = lambda: line_client

    with TestClient(app, raise_server_exceptions=True) as client:
        yield client, translator, line_client


def _seed_tenant() -> int:
    db = _Session()
    try:
        tenant = Tenant(name="line_multi_event_isolation", plan="free")
        db.add(tenant)
        db.flush()

        cfg = _lcm.LineChannelConfig(
            tenant_id=tenant.id,
            default_target_lang="zh-TW",
        )
        cfg.channel_secret = _CHANNEL_SECRET
        cfg.access_token = _ACCESS_TOKEN
        db.add(cfg)
        db.commit()
        return tenant.id
    finally:
        db.close()


def _text_event(text: str, reply_token: str, line_user_id: str) -> dict:
    return {
        "type": "message",
        "replyToken": reply_token,
        "source": {"type": "user", "userId": line_user_id},
        "message": {"type": "text", "text": text},
    }


def _payload(*events: dict) -> bytes:
    return json.dumps({"events": list(events)}).encode("utf-8")


def _headers(body: bytes) -> dict:
    mac = hmac.new(_CHANNEL_SECRET.encode("utf-8"), body, hashlib.sha256)
    return {
        "X-Line-Signature": base64.b64encode(mac.digest()).decode("utf-8")
    }


def _usage_count(tenant_id: int) -> int:
    db = _Session()
    try:
        row = db.execute(
            select(ApiUsage).where(
                ApiUsage.tenant_id == tenant_id,
                ApiUsage.period == datetime.date.today(),
            )
        ).scalar_one_or_none()
        return row.count if row else 0
    finally:
        db.close()


def test_first_event_translate_failure_does_not_stop_second_event(app_client):
    client, translator, line_client = app_client
    tenant_id = _seed_tenant()

    body = _payload(
        _text_event("first", "rt-first", "Ufirst"),
        _text_event("second", "rt-second", "Usecond"),
    )

    response = client.post(
        f"/line/webhook/{tenant_id}",
        content=body,
        headers=_headers(body),
    )

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert translator.calls == [("first", "zh-TW"), ("second", "zh-TW")]
    assert line_client.call_count == 1
    assert line_client.sent[0].reply_token == "rt-second"
    assert line_client.sent[0].text == "[ZH-TW] second"
    assert _usage_count(tenant_id) == 1
