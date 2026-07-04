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
from saas_mvp.models.line_webhook_event import LineWebhookEvent
from saas_mvp.models.tenant import Tenant
from saas_mvp.models.usage import ApiUsage
from saas_mvp.translation import (
    StubTranslator,
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
        return TranslationResult(
            text=f"[{target_lang.upper()}] {text}",
            detected_lang=None,
            skipped=False,
        )

    def is_available(self) -> bool:
        return True


class SkipsFirstTranslator(Translator):
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def translate(self, text: str, target_lang: str) -> TranslationResult:
        self.calls.append((text, target_lang))
        if len(self.calls) == 1:
            return TranslationResult(
                text=text,
                detected_lang=target_lang,
                skipped=True,
            )
        return TranslationResult(
            text=f"[{target_lang.upper()}] {text}",
            detected_lang="EN",
            skipped=False,
        )

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


@pytest.fixture()
def skip_app_client():
    Base.metadata.drop_all(bind=_engine)
    Base.metadata.create_all(bind=_engine)
    translator = StubTranslator(source_lang="zh-TW")
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
        yield client, line_client


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


def _text_event(
    text: str,
    reply_token: str,
    line_user_id: str,
    *,
    webhook_event_id: str | None = None,
    is_redelivery: bool = False,
) -> dict:
    ev = {
        "type": "message",
        "replyToken": reply_token,
        "source": {"type": "user", "userId": line_user_id},
        "message": {"type": "text", "text": text},
    }
    if webhook_event_id is not None:
        ev["webhookEventId"] = webhook_event_id
    if is_redelivery:
        ev["deliveryContext"] = {"isRedelivery": True}
    return ev


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


def _usage_stats(tenant_id: int) -> tuple[int, int]:
    db = _Session()
    try:
        row = db.execute(
            select(ApiUsage).where(
                ApiUsage.tenant_id == tenant_id,
                ApiUsage.period == datetime.date.today(),
            )
        ).scalar_one_or_none()
        if row is None:
            return 0, 0
        return row.count, row.char_count
    finally:
        db.close()


def _webhook_event_rows(tenant_id: int) -> list[tuple[str, str, str | None, bool]]:
    db = _Session()
    try:
        rows = db.execute(
            select(LineWebhookEvent)
            .where(LineWebhookEvent.tenant_id == tenant_id)
            .order_by(LineWebhookEvent.webhook_event_id)
        ).scalars().all()
        return [
            (
                row.webhook_event_id,
                row.status,
                row.last_stage,
                row.processed_at is not None,
            )
            for row in rows
        ]
    finally:
        db.close()


def _webhook_event_attempt_count(tenant_id: int, webhook_event_id: str) -> int:
    db = _Session()
    try:
        return db.execute(
            select(LineWebhookEvent.attempt_count).where(
                LineWebhookEvent.tenant_id == tenant_id,
                LineWebhookEvent.webhook_event_id == webhook_event_id,
            )
        ).scalar_one()
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


def test_skip_event_does_not_stop_next_normal_text_event(app_client):
    client, _, line_client = app_client
    translator = SkipsFirstTranslator()
    client.app.dependency_overrides[get_translator] = lambda: translator
    tenant_id = _seed_tenant()

    body = _payload(
        _text_event("same-language", "rt-skip", "Uskip"),
        _text_event("normal", "rt-normal", "Unormal"),
    )

    response = client.post(
        f"/line/webhook/{tenant_id}",
        content=body,
        headers=_headers(body),
    )

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert translator.calls == [("same-language", "zh-TW"), ("normal", "zh-TW")]
    assert line_client.call_count == 1
    assert line_client.sent[0].reply_token == "rt-normal"
    assert line_client.sent[0].text == "[ZH-TW] normal"
    assert _usage_stats(tenant_id) == (1, len("[ZH-TW] normal"))


def test_same_language_event_skips_reply_and_usage_then_next_event_runs(skip_app_client):
    client, line_client = skip_app_client
    tenant_id = _seed_tenant()

    body = _payload(
        _text_event("同語言", "rt-skip", "Uskip"),
        _text_event("/lang ja translate me", "rt-normal", "Unormal"),
    )

    response = client.post(
        f"/line/webhook/{tenant_id}",
        content=body,
        headers=_headers(body),
    )

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert line_client.call_count == 1
    assert line_client.sent[0].reply_token == "rt-normal"
    assert line_client.sent[0].text == "[JA] translate me"
    assert _usage_stats(tenant_id) == (1, len("[JA] translate me"))


def test_duplicate_webhook_event_id_skips_second_reply_and_usage(app_client):
    client, _, line_client = app_client
    client.app.dependency_overrides[get_translator] = lambda: StubTranslator()
    tenant_id = _seed_tenant()

    first = _payload(
        _text_event(
            "first",
            "rt-first",
            "Ufirst",
            webhook_event_id="evt-idem-dup",
        )
    )
    response = client.post(
        f"/line/webhook/{tenant_id}",
        content=first,
        headers=_headers(first),
    )
    assert response.status_code == 200
    assert line_client.call_count == 1
    assert _usage_count(tenant_id) == 1

    line_client.reset()
    second = _payload(
        _text_event(
            "second",
            "rt-second",
            "Ufirst",
            webhook_event_id="evt-idem-dup",
            is_redelivery=True,
        )
    )
    response = client.post(
        f"/line/webhook/{tenant_id}",
        content=second,
        headers=_headers(second),
    )

    assert response.status_code == 200
    assert line_client.call_count == 0
    assert _usage_count(tenant_id) == 1
    assert _webhook_event_rows(tenant_id) == [
        ("evt-idem-dup", "processed", "usage_incremented", True)
    ]


def test_redelivery_true_with_new_webhook_event_id_is_processed(app_client):
    client, _, line_client = app_client
    client.app.dependency_overrides[get_translator] = lambda: StubTranslator()
    tenant_id = _seed_tenant()

    body = _payload(
        _text_event(
            "fresh",
            "rt-redelivery-fresh",
            "Ufresh",
            webhook_event_id="evt-redelivery-fresh",
            is_redelivery=True,
        )
    )
    response = client.post(
        f"/line/webhook/{tenant_id}",
        content=body,
        headers=_headers(body),
    )

    assert response.status_code == 200
    assert line_client.call_count == 1
    assert line_client.sent[0].text == "[ZH-TW] fresh"
    assert _usage_count(tenant_id) == 1
    assert _webhook_event_rows(tenant_id) == [
        ("evt-redelivery-fresh", "processed", "usage_incremented", True)
    ]


def test_missing_webhook_event_id_falls_back_to_direct_processing(app_client):
    client, _, line_client = app_client
    client.app.dependency_overrides[get_translator] = lambda: StubTranslator()
    tenant_id = _seed_tenant()

    body1 = _payload(_text_event("one", "rt-no-id-1", "Unoid"))
    body2 = _payload(_text_event("two", "rt-no-id-2", "Unoid"))
    assert client.post(
        f"/line/webhook/{tenant_id}",
        content=body1,
        headers=_headers(body1),
    ).status_code == 200
    assert client.post(
        f"/line/webhook/{tenant_id}",
        content=body2,
        headers=_headers(body2),
    ).status_code == 200

    assert line_client.call_count == 2
    assert _usage_count(tenant_id) == 2
    assert _webhook_event_rows(tenant_id) == []


def test_failed_event_is_marked_failed_and_next_event_processed(app_client):
    client, translator, line_client = app_client
    tenant_id = _seed_tenant()

    body = _payload(
        _text_event(
            "first",
            "rt-failed",
            "Ufailed",
            webhook_event_id="evt-failed",
        ),
        _text_event(
            "second",
            "rt-ok",
            "Uok",
            webhook_event_id="evt-ok",
        ),
    )
    response = client.post(
        f"/line/webhook/{tenant_id}",
        content=body,
        headers=_headers(body),
    )

    assert response.status_code == 200
    assert translator.calls == [("first", "zh-TW"), ("second", "zh-TW")]
    assert line_client.call_count == 1
    assert line_client.sent[0].reply_token == "rt-ok"
    assert _usage_count(tenant_id) == 1
    assert _webhook_event_rows(tenant_id) == [
        ("evt-failed", "failed", "quota_checked", False),
        ("evt-ok", "processed", "usage_incremented", True),
    ]


def test_failed_event_before_reply_is_retried_on_same_webhook_event_id(app_client):
    client, translator, line_client = app_client
    tenant_id = _seed_tenant()

    first = _payload(
        _text_event(
            "first",
            "rt-retry-first",
            "Uretry",
            webhook_event_id="evt-retry-failed",
        )
    )
    response = client.post(
        f"/line/webhook/{tenant_id}",
        content=first,
        headers=_headers(first),
    )
    assert response.status_code == 200
    assert translator.calls == [("first", "zh-TW")]
    assert line_client.call_count == 0
    assert _webhook_event_rows(tenant_id) == [
        ("evt-retry-failed", "failed", "quota_checked", False)
    ]

    second = _payload(
        _text_event(
            "second",
            "rt-retry-second",
            "Uretry",
            webhook_event_id="evt-retry-failed",
            is_redelivery=True,
        )
    )
    response = client.post(
        f"/line/webhook/{tenant_id}",
        content=second,
        headers=_headers(second),
    )

    assert response.status_code == 200
    assert translator.calls == [("first", "zh-TW"), ("second", "zh-TW")]
    assert line_client.call_count == 1
    assert line_client.sent[0].reply_token == "rt-retry-second"
    assert line_client.sent[0].text == "[ZH-TW] second"
    assert _usage_count(tenant_id) == 1
    assert _webhook_event_rows(tenant_id) == [
        ("evt-retry-failed", "processed", "usage_incremented", True)
    ]


@pytest.mark.parametrize(
    "failed_stage",
    ["claimed", "quota_checked", "translated"],
)
def test_failed_pre_reply_stages_are_retried_on_same_webhook_event_id(
    app_client,
    failed_stage,
):
    client, _, line_client = app_client
    client.app.dependency_overrides[get_translator] = lambda: StubTranslator()
    tenant_id = _seed_tenant()
    webhook_event_id = f"evt-{failed_stage}-failed"

    db = _Session()
    try:
        db.add(
            LineWebhookEvent(
                tenant_id=tenant_id,
                webhook_event_id=webhook_event_id,
                status="failed",
                attempt_count=2,
                last_error="TranslationError",
                last_stage=failed_stage,
            )
        )
        db.commit()
    finally:
        db.close()

    body = _payload(
        _text_event(
            f"retry-{failed_stage}",
            f"rt-{failed_stage}-retry",
            "UretryStage",
            webhook_event_id=webhook_event_id,
            is_redelivery=True,
        )
    )
    response = client.post(
        f"/line/webhook/{tenant_id}",
        content=body,
        headers=_headers(body),
    )

    assert response.status_code == 200
    assert line_client.call_count == 1
    assert line_client.sent[0].reply_token == f"rt-{failed_stage}-retry"
    assert line_client.sent[0].text == f"[ZH-TW] retry-{failed_stage}"
    assert _usage_count(tenant_id) == 1
    assert _webhook_event_attempt_count(tenant_id, webhook_event_id) == 3
    assert _webhook_event_rows(tenant_id) == [
        (webhook_event_id, "processed", "usage_incremented", True)
    ]


def test_failed_reply_sent_event_skips_redelivery_without_duplicate_reply(app_client):
    client, _, line_client = app_client
    client.app.dependency_overrides[get_translator] = lambda: StubTranslator()
    tenant_id = _seed_tenant()

    db = _Session()
    try:
        db.add(
            LineWebhookEvent(
                tenant_id=tenant_id,
                webhook_event_id="evt-reply-sent-failed",
                status="failed",
                last_stage="reply_sent",
            )
        )
        db.commit()
    finally:
        db.close()

    body = _payload(
        _text_event(
            "do-not-reply-again",
            "rt-reply-sent-retry",
            "Ureplysent",
            webhook_event_id="evt-reply-sent-failed",
            is_redelivery=True,
        )
    )
    response = client.post(
        f"/line/webhook/{tenant_id}",
        content=body,
        headers=_headers(body),
    )

    assert response.status_code == 200
    assert line_client.call_count == 0
    assert _usage_count(tenant_id) == 0
    assert _webhook_event_rows(tenant_id) == [
        ("evt-reply-sent-failed", "failed", "reply_sent", False)
    ]
