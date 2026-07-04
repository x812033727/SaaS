"""AI 客服測試 — factory 選擇、stub 決定性、faq.match、/ai/ask 閘門、LINE webhook fallback。

絕不呼叫真實 Anthropic API：factory 測試 monkeypatch settings；其餘走 StubAIAssistant。
"""

from __future__ import annotations

import json
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from saas_mvp.models import tenant as _t  # noqa: F401
from saas_mvp.models import user as _u  # noqa: F401
from saas_mvp.models import faq_entry as _faq  # noqa: F401
from saas_mvp.models import tenant_feature as _tf  # noqa: F401
from saas_mvp.models import feature_change_history as _fch  # noqa: F401
import saas_mvp.models.line_channel_config as _lcm  # noqa: F401
import saas_mvp.models.customer as _cust  # noqa: F401

from saas_mvp.ai import AnthropicAssistant, StubAIAssistant, get_assistant
from saas_mvp.app import create_app
from saas_mvp.db import Base, get_db
from saas_mvp.line_client import FakeLineReplyClient, get_line_client
from saas_mvp.models.faq_entry import FAQEntry
from saas_mvp.models.line_channel_config import LineChannelConfig
from saas_mvp.models.tenant import Tenant
from saas_mvp.services import faq as faq_svc
from saas_mvp.services import features as features_svc

_engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
_Session = sessionmaker(autocommit=False, autoflush=False, bind=_engine)


@pytest.fixture()
def db():
    Base.metadata.drop_all(bind=_engine)
    Base.metadata.create_all(bind=_engine)
    s = _Session()
    try:
        yield s
    finally:
        s.close()


# ── factory ──────────────────────────────────────────────────────────────────

def test_factory_returns_stub_when_no_key(monkeypatch):
    from saas_mvp import config as cfg
    monkeypatch.setattr(cfg.settings, "anthropic_api_key", "")
    assert isinstance(get_assistant(), StubAIAssistant)


def test_factory_returns_anthropic_when_key_set(monkeypatch):
    from saas_mvp import config as cfg
    monkeypatch.setattr(cfg.settings, "anthropic_api_key", "sk-test")
    assistant = get_assistant()
    assert isinstance(assistant, AnthropicAssistant)
    assert assistant.is_available() is True


# ── stub determinism ─────────────────────────────────────────────────────────

def test_stub_deterministic():
    stub = StubAIAssistant()
    r1 = stub.answer("營業時間？", "")
    r2 = stub.answer("營業時間？", "")
    assert r1 == r2
    assert r1.source == "stub"
    # context present → echoed
    r3 = stub.answer("營業時間？", "Q: 營業時間\nA: 10:00-22:00")
    assert "10:00-22:00" in r3.answer


def test_context_budget_per_backend():
    # stub 只回最相關 1 筆（否則會「問一個列一堆」）；真 LLM 可吃多筆綜合。
    assert StubAIAssistant().context_max_entries == 1
    assert AnthropicAssistant().context_max_entries > 1


# ── faq.match ────────────────────────────────────────────────────────────────

def test_faq_match(db):
    t = Tenant(name="t", plan="free")
    db.add(t)
    db.flush()
    tid = t.id
    faq_svc.create_faq(db, tenant_id=tid, question="營業時間", answer="10:00-22:00")
    faq_svc.create_faq(db, tenant_id=tid, question="停車資訊", answer="有附設停車場")
    matched = faq_svc.match(db, tid, "請問營業時間是幾點")
    assert len(matched) == 1
    assert matched[0].answer == "10:00-22:00"
    # no match
    assert faq_svc.match(db, tid, "完全無關的問題XYZ") == []


# ── REST /ai/ask + gating ────────────────────────────────────────────────────

@pytest.fixture()
def client():
    Base.metadata.drop_all(bind=_engine)
    Base.metadata.create_all(bind=_engine)
    app = create_app()

    def override_get_db():
        s = _Session()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


def _register(client) -> str:
    email = f"u_{uuid.uuid4().hex[:8]}@example.com"
    r = client.post("/auth/register", json={
        "email": email, "password": "Test1234!", "tenant_name": f"t_{uuid.uuid4().hex[:8]}",
    })
    assert r.status_code == 201, r.text
    return r.json()["access_token"]


def _auth(t):
    return {"Authorization": f"Bearer {t}"}


def test_ai_ask_403_when_disabled(client):
    token = _register(client)
    client.post("/billing/features/AI_ASSISTANT/unsubscribe", headers=_auth(token))
    r = client.post("/ai/ask", headers=_auth(token), json={"question": "hi"})
    assert r.status_code == 403


def test_ai_ask_returns_answer_with_stub(client, monkeypatch):
    from saas_mvp import config as cfg
    monkeypatch.setattr(cfg.settings, "anthropic_api_key", "")  # force stub
    token = _register(client)
    client.post("/billing/features/AI_ASSISTANT/subscribe", headers=_auth(token))
    # seed a FAQ → echoed in context
    client.post("/ai/faq", headers=_auth(token), json={
        "question": "營業時間", "answer": "10:00-22:00",
    })
    r = client.post("/ai/ask", headers=_auth(token), json={"question": "營業時間"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["source"] == "stub"
    assert "10:00-22:00" in body["answer"]


def test_faq_get_one(client):
    token = _register(client)
    client.post("/billing/features/AI_ASSISTANT/subscribe", headers=_auth(token))
    faq_id = client.post("/ai/faq", headers=_auth(token), json={
        "question": "退款政策", "answer": "七天內可退",
    }).json()["id"]
    r = client.get(f"/ai/faq/{faq_id}", headers=_auth(token))
    assert r.status_code == 200, r.text
    assert r.json()["question"] == "退款政策" and r.json()["answer"] == "七天內可退"
    # 查無 → 404
    assert client.get("/ai/faq/999999", headers=_auth(token)).status_code == 404
    # 跨租戶 → 404
    token_b = _register(client)
    client.post("/billing/features/AI_ASSISTANT/subscribe", headers=_auth(token_b))
    assert client.get(f"/ai/faq/{faq_id}", headers=_auth(token_b)).status_code == 404


def test_ai_ask_stub_returns_only_top_faq(client, monkeypatch):
    """多筆 FAQ 都相關時，stub 只回最相關那筆，不把整排 FAQ 全列出來。"""
    from saas_mvp import config as cfg
    monkeypatch.setattr(cfg.settings, "anthropic_api_key", "")  # force stub
    token = _register(client)
    client.post("/billing/features/AI_ASSISTANT/subscribe", headers=_auth(token))
    # 三筆都含「營業」→ 都會 match；只有最相關的「營業時間」該被回。
    client.post("/ai/faq", headers=_auth(token), json={
        "question": "營業時間", "answer": "平日10:00-22:00",
    })
    client.post("/ai/faq", headers=_auth(token), json={
        "question": "營業地點", "answer": "台北市信義區",
    })
    client.post("/ai/faq", headers=_auth(token), json={
        "question": "營業項目", "answer": "美容美髮",
    })
    r = client.post("/ai/ask", headers=_auth(token), json={"question": "請問營業時間"})
    assert r.status_code == 200, r.text
    answer = r.json()["answer"]
    assert "10:00-22:00" in answer
    # 不該把其他 FAQ 也一併列出
    assert "台北市信義區" not in answer
    assert "美容美髮" not in answer


# ── LINE webhook AI fallback ──────────────────────────────────────────────────

import base64  # noqa: E402
import hashlib  # noqa: E402
import hmac  # noqa: E402


def _seed_booking_tenant(db, *, ai_enabled: bool) -> int:
    t = Tenant(name="webhook_t", plan="free")
    db.add(t)
    db.flush()
    cfg = LineChannelConfig(tenant_id=t.id, default_target_lang="zh-TW")
    cfg.channel_secret = "s" * 32
    cfg.access_token = "a" * 40
    cfg.bot_mode = "booking"
    db.add(cfg)
    db.flush()
    features_svc.set_enabled(
        db, t.id, features_svc.AI_ASSISTANT, ai_enabled,
        actor_user_id=None, source="admin",
    )
    # seed FAQ so the stub echoes it
    db.add(FAQEntry(tenant_id=t.id, question="退貨", answer="七天內可退貨"))
    db.commit()
    return t.id


def _sign(secret: str, body: bytes) -> str:
    mac = hmac.new(secret.encode(), body, hashlib.sha256)
    return base64.b64encode(mac.digest()).decode()


def _webhook_post(client, tid, text):
    payload = {
        "destination": "U" + "0" * 32,
        "events": [
            {
                "type": "message",
                "replyToken": "rt1",
                "source": {"userId": "Uvisitor"},
                "message": {"type": "text", "text": text},
            }
        ],
    }
    body = json.dumps(payload).encode()
    sig = _sign("s" * 32, body)
    return client.post(
        f"/line/webhook/{tid}",
        content=body,
        headers={"X-Line-Signature": sig, "Content-Type": "application/json"},
    )


def test_webhook_ai_fallback_replies_when_enabled(client, monkeypatch):
    from saas_mvp import config as cfg
    monkeypatch.setattr(cfg.settings, "anthropic_api_key", "")  # stub path
    fake = FakeLineReplyClient()
    client.app.dependency_overrides[get_line_client] = lambda: fake

    db = _Session()
    try:
        tid = _seed_booking_tenant(db, ai_enabled=True)
    finally:
        db.close()

    r = _webhook_post(client, tid, "我想問退貨怎麼處理")
    assert r.status_code == 200
    assert fake.call_count == 1
    # stub echoes matched FAQ answer
    assert "七天內可退貨" in fake.last_text


def test_webhook_no_ai_fallback_when_disabled(client):
    fake = FakeLineReplyClient()
    client.app.dependency_overrides[get_line_client] = lambda: fake

    db = _Session()
    try:
        tid = _seed_booking_tenant(db, ai_enabled=False)
    finally:
        db.close()

    r = _webhook_post(client, tid, "我想問退貨怎麼處理")
    assert r.status_code == 200
    # AI disabled → falls back to booking help, not the FAQ answer
    assert fake.call_count == 1
    assert "七天內可退貨" not in fake.last_text
