"""LINE Webhook 冪等去重（webhookEventId）

驗收標準
--------
- 相同 webhookEventId 第二次進入 → fake client 未收到 reply、quota used 不變、回 200
- isRedelivery=true 但 webhookEventId 首次出現 → 行為與現狀一致（翻譯 + 回覆 + 計量）
- 缺 deliveryContext 欄位 → 視為首投，正常處理
- 多 event 混合：僅重複 ID 被略過，首投者照常處理（反向對照）

全部離線：StubTranslator + FakeLineReplyClient。
"""

from __future__ import annotations

import base64
import datetime
import hashlib
import hmac
import json
import os
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("SAAS_RATE_LIMIT_ENABLED", "false")

# 載入所有 model metadata
from saas_mvp.models import tenant as _t, user as _u, note as _n, usage as _us  # noqa: F401,E402
from saas_mvp.models import api_key as _ak, api_key_usage as _aku               # noqa: F401,E402
from saas_mvp.models import plan_change_history as _pch                          # noqa: F401,E402
import saas_mvp.models.line_channel_config as _lcm                               # noqa: F401,E402
import saas_mvp.models.line_user_lang as _lul                                     # noqa: F401,E402

from saas_mvp.app import create_app                                              # noqa: E402
from saas_mvp.db import Base, get_db                                             # noqa: E402
from saas_mvp.line_client import FakeLineReplyClient, get_line_client           # noqa: E402
from saas_mvp.models.line_webhook_event import LineWebhookEvent                 # noqa: E402
from saas_mvp.translation import StubTranslator, get_translator                 # noqa: E402
from saas_mvp.models.usage import ApiUsage                                       # noqa: E402

# ── In-memory SQLite ──────────────────────────────────────────────────────────

_engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
_Session = sessionmaker(autocommit=False, autoflush=False, bind=_engine)

_stub_translator = StubTranslator()
_fake_line_client = FakeLineReplyClient()

_CHANNEL_SECRET = "test-channel-secret-32-bytes-x!!"
_ACCESS_TOKEN = "test-access-token-abc"


@pytest.fixture(scope="module")
def client():
    Base.metadata.create_all(bind=_engine)
    app = create_app()

    def override_db():
        db = _Session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_translator] = lambda: _stub_translator
    app.dependency_overrides[get_line_client] = lambda: _fake_line_client

    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


# ── helpers ───────────────────────────────────────────────────────────────────


def _sign(body: bytes, secret: str = _CHANNEL_SECRET) -> str:
    mac = hmac.new(secret.encode("utf-8"), body, hashlib.sha256)
    return base64.b64encode(mac.digest()).decode("utf-8")


def _headers(body: bytes, secret: str = _CHANNEL_SECRET) -> dict:
    return {"X-Line-Signature": _sign(body, secret)}


def _text_event(
    text: str,
    reply_token: str,
    *,
    is_redelivery=None,
    user_id="Uredeliver",
    webhook_event_id: str | None = None,
) -> dict:
    ev = {
        "type": "message",
        "replyToken": reply_token,
        "source": {"type": "user", "userId": user_id},
        "message": {"type": "text", "text": text},
    }
    if webhook_event_id is not None:
        ev["webhookEventId"] = webhook_event_id
    if is_redelivery is not None:
        ev["deliveryContext"] = {"isRedelivery": is_redelivery}
    return ev


def _payload(*events) -> bytes:
    return json.dumps({"events": list(events)}).encode("utf-8")


def _register_with_config(client: TestClient) -> int:
    email = f"rd_{uuid.uuid4().hex[:8]}@example.com"
    tn = f"rd_tenant_{uuid.uuid4().hex[:8]}"
    r = client.post("/auth/register", json={
        "email": email, "password": "Test1234!", "tenant_name": tn,
    })
    assert r.status_code == 201, r.text
    token = r.json()["access_token"]
    me = client.get("/tenants/me", headers={"Authorization": f"Bearer {token}"})
    tid = me.json()["id"]

    from saas_mvp.auth.security import decode_access_token
    from saas_mvp.models.user import User
    payload = decode_access_token(token)
    db = _Session()
    try:
        user = db.get(User, int(payload["sub"]))
        user.is_admin = True
        db.commit()
    finally:
        db.close()

    r2 = client.put(
        f"/admin/line-configs/{tid}",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "channel_secret": _CHANNEL_SECRET,
            "access_token": _ACCESS_TOKEN,
            "default_target_lang": "zh-TW",
        },
    )
    assert r2.status_code == 200, r2.text
    return tid


def _usage_count(tid: int) -> int:
    db = _Session()
    try:
        row = db.query(ApiUsage).filter(
            ApiUsage.tenant_id == tid,
            ApiUsage.period == datetime.date.today(),
        ).one_or_none()
        return row.count if row else 0
    finally:
        db.close()


def _webhook_event_rows(tid: int, webhook_event_id: str | None = None) -> list[tuple[str, str]]:
    db = _Session()
    try:
        query = db.query(LineWebhookEvent).filter(LineWebhookEvent.tenant_id == tid)
        if webhook_event_id is not None:
            query = query.filter(LineWebhookEvent.webhook_event_id == webhook_event_id)
        return [
            (row.webhook_event_id, row.status)
            for row in query.order_by(LineWebhookEvent.webhook_event_id).all()
        ]
    finally:
        db.close()


@pytest.fixture(autouse=True)
def reset_fake_client():
    _fake_line_client.reset()
    yield


@pytest.fixture(scope="module")
def tid(client):
    return _register_with_config(client)


# ── 測試：重送去重 ────────────────────────────────────────────────────────────


class TestRedeliveryDedup:
    def test_duplicate_webhook_event_id_skipped_no_reply_no_quota(self, client, tid):
        """相同 webhookEventId 第二次進入 → 不翻譯、不回覆、quota 不變。"""
        first = _payload(
            _text_event("first", "rt-first-dup", webhook_event_id="evt-redelivery-dup")
        )
        r1 = client.post(f"/line/webhook/{tid}", content=first, headers=_headers(first))
        assert r1.status_code == 200
        assert _fake_line_client.call_count == 1

        _fake_line_client.reset()
        before = _usage_count(tid)
        body = _payload(
            _text_event(
                "重送訊息",
                "rt-redeliver",
                is_redelivery=True,
                webhook_event_id="evt-redelivery-dup",
            )
        )
        r = client.post(f"/line/webhook/{tid}", content=body, headers=_headers(body))
        assert r.status_code == 200
        assert _fake_line_client.call_count == 0          # 未回覆
        assert _usage_count(tid) == before                # quota 不變

        rows = _webhook_event_rows(tid, "evt-redelivery-dup")
        assert rows == [("evt-redelivery-dup", "processed")]

    def test_duplicate_webhook_event_id_skipped_even_when_redelivery_false(self, client, tid):
        """同 webhookEventId 第二次進入，即使 isRedelivery=false 也略過。"""
        first = _payload(
            _text_event(
                "first false",
                "rt-first-false",
                is_redelivery=False,
                webhook_event_id="evt-redelivery-false-dup",
            )
        )
        r1 = client.post(f"/line/webhook/{tid}", content=first, headers=_headers(first))
        assert r1.status_code == 200
        assert _fake_line_client.call_count == 1

        _fake_line_client.reset()
        before = _usage_count(tid)
        second = _payload(
            _text_event(
                "second false should skip",
                "rt-second-false",
                is_redelivery=False,
                webhook_event_id="evt-redelivery-false-dup",
            )
        )
        r2 = client.post(f"/line/webhook/{tid}", content=second, headers=_headers(second))

        assert r2.status_code == 200
        assert _fake_line_client.call_count == 0
        assert _usage_count(tid) == before

        rows = _webhook_event_rows(tid, "evt-redelivery-false-dup")
        assert rows == [("evt-redelivery-false-dup", "processed")]

    def test_redelivery_true_new_webhook_event_id_processed_normally(self, client, tid):
        """isRedelivery=true 但 webhookEventId 首次出現 → 正常處理。"""
        before = _usage_count(tid)
        body = _payload(
            _text_event(
                "hello",
                "rt-first",
                is_redelivery=True,
                webhook_event_id="evt-redelivery-new",
            )
        )
        r = client.post(f"/line/webhook/{tid}", content=body, headers=_headers(body))
        assert r.status_code == 200
        assert _fake_line_client.call_count == 1
        assert _fake_line_client.last_text == "[ZH-TW] hello"
        assert _usage_count(tid) == before + 1            # quota +1

    def test_missing_delivery_context_treated_as_first_delivery(self, client, tid):
        """缺 deliveryContext 欄位 → 視為首投，正常處理。"""
        body = _payload(_text_event("world", "rt-nofield"))  # 無 deliveryContext
        r = client.post(f"/line/webhook/{tid}", content=body, headers=_headers(body))
        assert r.status_code == 200
        assert _fake_line_client.call_count == 1
        assert _fake_line_client.last_text == "[ZH-TW] world"

    def test_missing_webhook_event_id_falls_back_to_direct_processing(self, client, tid):
        """缺 webhookEventId 時不做冪等 claim；同 payload 兩筆都照常處理。"""
        before = _usage_count(tid)
        rows_before = len(_webhook_event_rows(tid))
        body = _payload(
            _text_event("no id one", "rt-no-id-1"),
            _text_event("no id two", "rt-no-id-2"),
        )
        r = client.post(f"/line/webhook/{tid}", content=body, headers=_headers(body))

        assert r.status_code == 200
        assert _fake_line_client.call_count == 2
        assert [sent.text for sent in _fake_line_client.sent] == [
            "[ZH-TW] no id one",
            "[ZH-TW] no id two",
        ]
        assert _usage_count(tid) == before + 2
        assert len(_webhook_event_rows(tid)) == rows_before

    def test_delivery_context_without_flag_treated_as_first_delivery(self, client, tid):
        """有 deliveryContext 但無 isRedelivery 鍵 → 視為首投。"""
        ev = {
            "type": "message",
            "replyToken": "rt-emptyctx",
            "source": {"type": "user", "userId": "Uredeliver"},
            "message": {"type": "text", "text": "ping"},
            "deliveryContext": {},
        }
        body = json.dumps({"events": [ev]}).encode("utf-8")
        r = client.post(f"/line/webhook/{tid}", content=body, headers=_headers(body))
        assert r.status_code == 200
        assert _fake_line_client.call_count == 1
        assert _fake_line_client.last_text == "[ZH-TW] ping"

    def test_mixed_only_duplicate_id_skipped(self, client, tid):
        """混合：重複 ID + 首投 → 僅首投被翻譯回覆，重複 ID 被略過。"""
        seed = _payload(
            _text_event("seed", "rt-mix-seed", webhook_event_id="evt-mix-dup")
        )
        r0 = client.post(f"/line/webhook/{tid}", content=seed, headers=_headers(seed))
        assert r0.status_code == 200
        _fake_line_client.reset()

        before = _usage_count(tid)
        body = _payload(
            _text_event(
                "redelivered-msg",
                "rt-mix-2",
                is_redelivery=True,
                webhook_event_id="evt-mix-dup",
            ),
            _text_event("first-msg", "rt-mix-1", webhook_event_id="evt-mix-fresh"),
        )
        r = client.post(f"/line/webhook/{tid}", content=body, headers=_headers(body))
        assert r.status_code == 200
        assert _fake_line_client.call_count == 1           # 只有首投
        assert _fake_line_client.last_text == "[ZH-TW] first-msg"
        assert _usage_count(tid) == before + 1             # 只計一次
