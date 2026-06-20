"""Task #2 QA 硬化測試 — 正常路徑恰好計量 +1、超量回配額訊息且不 +1。

本檔不取代 test_line_task1_quota_billing.py，而是針對任務 #2 補強，
排除「看 reply 文字判斷未翻譯」可能的假綠：

* 用 SpyTranslator 直接「計數 translate() 實際被呼叫的次數」，
  證明：正常路徑剛好呼叫 1 次、連兩則呼叫 2 次、超量呼叫 0 次（真正不翻譯）。
* 正常路徑 reply 必為譯文、count 恰好 +1；連兩則 +2。
* 超量：HTTP 200（非 429/500）、reply 為配額訊息（非譯文）、count 不變、translate 0 次。
* 反向對照：limit-1 仍翻譯並 +1（排除「永遠不翻譯」假綠）。
* 邊界：同一 webhook 內「正常 event + redelivery event」→ 只計量 +1（redelivery 不翻不扣）。

全離線：SpyTranslator(包 StubTranslator) + FakeLineReplyClient，無金鑰、無網路、無 sleep。
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
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("SAAS_RATE_LIMIT_ENABLED", "false")

# 載入所有 model metadata
from saas_mvp.models import tenant as _t, user as _u, note as _n, usage as _us  # noqa: F401
from saas_mvp.models import api_key as _ak, api_key_usage as _aku               # noqa: F401
from saas_mvp.models import plan_change_history as _pch                          # noqa: F401
import saas_mvp.models.line_channel_config as _lcm                               # noqa: F401
import saas_mvp.models.line_user_lang as _lul                                     # noqa: F401

from saas_mvp.app import create_app
from saas_mvp.db import Base, get_db
from saas_mvp.line_client import FakeLineReplyClient, get_line_client
from saas_mvp.models.usage import ApiUsage
from saas_mvp.quota import PLAN_DAILY_LIMITS
from saas_mvp.translation import StubTranslator, TranslationResult, get_translator
from saas_mvp.translation.base import Translator

# ── In-memory SQLite ──────────────────────────────────────────────────────────

_engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
_Session = sessionmaker(autocommit=False, autoflush=False, bind=_engine)


# ── test doubles ──────────────────────────────────────────────────────────────

class SpyTranslator(Translator):
    """包 StubTranslator，計數 translate() 實際被呼叫次數（證明是否真的翻譯）。"""

    def __init__(self) -> None:
        self._inner = StubTranslator()
        self.calls: list[tuple[str, str]] = []

    def translate(self, text: str, target_lang: str) -> TranslationResult:
        self.calls.append((text, target_lang))
        return self._inner.translate(text, target_lang)

    def is_available(self) -> bool:
        return True

    def reset(self) -> None:
        self.calls.clear()

    @property
    def call_count(self) -> int:
        return len(self.calls)


_spy_translator = SpyTranslator()
_fake_line_client = FakeLineReplyClient()


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
    app.dependency_overrides[get_translator] = lambda: _spy_translator
    app.dependency_overrides[get_line_client] = lambda: _fake_line_client

    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


# ── helpers ───────────────────────────────────────────────────────────────────

_CHANNEL_SECRET = "test-channel-secret-32-bytes-x!!"
_ACCESS_TOKEN = "test-access-token-abc"


def _sign(body: bytes, secret: str = _CHANNEL_SECRET) -> str:
    mac = hmac.new(secret.encode("utf-8"), body, hashlib.sha256)
    return base64.b64encode(mac.digest()).decode("utf-8")


def _text_event(text: str, reply_token: str = "rt", line_user_id: str = "Uq001",
                redelivery: bool = False) -> dict:
    ev = {
        "type": "message",
        "replyToken": reply_token,
        "source": {"type": "user", "userId": line_user_id},
        "message": {"type": "text", "text": text},
    }
    if redelivery:
        ev["deliveryContext"] = {"isRedelivery": True}
    return ev


def _payload(*events) -> bytes:
    return json.dumps({"events": list(events)}).encode("utf-8")


def _headers(body: bytes) -> dict:
    return {"X-Line-Signature": _sign(body)}


def _new_tenant(client: TestClient) -> int:
    email = f"m_{uuid.uuid4().hex[:8]}@example.com"
    tn = f"m_tenant_{uuid.uuid4().hex[:8]}"
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
        json={"channel_secret": _CHANNEL_SECRET, "access_token": _ACCESS_TOKEN,
              "default_target_lang": "zh-TW"},
    )
    assert r2.status_code == 200, r2.text
    return tid


def _used(tid: int) -> int:
    today = datetime.date.today()
    db = _Session()
    try:
        row = db.execute(
            select(ApiUsage).where(
                ApiUsage.tenant_id == tid, ApiUsage.period == today
            )
        ).scalar_one_or_none()
        return row.count if row else 0
    finally:
        db.close()


def _seed_usage(tid: int, count: int) -> None:
    today = datetime.date.today()
    db = _Session()
    try:
        db.add(ApiUsage(tenant_id=tid, period=today, count=count))
        db.commit()
    finally:
        db.close()


@pytest.fixture(autouse=True)
def _reset():
    _fake_line_client.reset()
    _spy_translator.reset()
    yield


# ── 正常路徑：恰好 +1，translate 剛好 1 次 ───────────────────────────────────

class TestNormalPathExactlyOne:
    def test_single_success_increments_by_exactly_one(self, client):
        tid = _new_tenant(client)
        assert _used(tid) == 0

        body = _payload(_text_event("hello"))
        r = client.post(f"/line/webhook/{tid}", content=body, headers=_headers(body))

        assert r.status_code == 200
        assert _spy_translator.call_count == 1            # 真的翻譯了 1 次
        assert _spy_translator.calls[0] == ("hello", "zh-TW")
        assert _fake_line_client.call_count == 1
        assert _fake_line_client.last_text == "[ZH-TW] hello"  # reply 是譯文
        assert _used(tid) == 1                            # 恰好 +1，非 +2/+0

    def test_two_messages_increment_by_two(self, client):
        tid = _new_tenant(client)
        body = _payload(_text_event("a"), _text_event("b"))
        r = client.post(f"/line/webhook/{tid}", content=body, headers=_headers(body))

        assert r.status_code == 200
        assert _spy_translator.call_count == 2            # 兩則皆翻譯
        assert _fake_line_client.call_count == 2
        assert _used(tid) == 2                            # +2


# ── 超量：回配額訊息、HTTP 200、translate 0 次、count 不變 ────────────────────

class TestOverQuotaNoTranslateNoCharge:
    def test_over_quota_returns_200_quota_msg_no_translate_no_increment(self, client):
        tid = _new_tenant(client)
        limit = PLAN_DAILY_LIMITS["free"]
        _seed_usage(tid, limit)                           # 剛好達上限
        assert _used(tid) == limit

        body = _payload(_text_event("blocked text"))
        r = client.post(f"/line/webhook/{tid}", content=body, headers=_headers(body))

        assert r.status_code == 200                       # 非 429 / 500
        assert _spy_translator.call_count == 0            # 關鍵：完全沒翻譯
        assert _fake_line_client.call_count == 1
        reply_text = _fake_line_client.last_text
        assert "配額" in reply_text or "quota" in reply_text.lower()
        assert not reply_text.startswith("[")             # 非 StubTranslator 譯文
        assert _used(tid) == limit                        # count 不變

    def test_limit_minus_one_still_translates_and_increments(self, client):
        """反向對照：limit-1 仍翻譯並 +1 → 排除『永遠不翻譯』假綠。"""
        tid = _new_tenant(client)
        limit = PLAN_DAILY_LIMITS["free"]
        _seed_usage(tid, limit - 1)

        body = _payload(_text_event("last one"))
        r = client.post(f"/line/webhook/{tid}", content=body, headers=_headers(body))

        assert r.status_code == 200
        assert _spy_translator.call_count == 1            # 有翻譯
        assert _fake_line_client.last_text == "[ZH-TW] last one"
        assert _used(tid) == limit                        # +1 達上限


# ── 邊界：同 webhook 內正常 event + redelivery event → 只 +1 ──────────────────

class TestRedeliveryMixedExactlyOne:
    def test_normal_plus_redelivery_increments_by_one_only(self, client):
        tid = _new_tenant(client)
        # 第一則正常、第二則 isRedelivery=true（應略過不翻不扣）
        body = _payload(
            _text_event("real", reply_token="rt1"),
            _text_event("dup", reply_token="rt2", redelivery=True),
        )
        r = client.post(f"/line/webhook/{tid}", content=body, headers=_headers(body))

        assert r.status_code == 200
        assert _spy_translator.call_count == 1            # 只翻譯正常那則
        assert _spy_translator.calls[0][0] == "real"
        assert _used(tid) == 1                            # 恰好 +1，redelivery 不計量
