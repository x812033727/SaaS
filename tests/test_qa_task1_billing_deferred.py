"""QA 獨立驗收 — Task #1：計費點後移，消除下游失敗白扣。

驗收標準（任務 #1）
-------------------
* translate 或 reply 拋例外時，當日 quota used 不增加（以失敗 stub 驗證）。
* 正常路徑 quota 仍恰 +1。
* 超量時不翻譯、回覆原配額訊息、且不 +1，HTTP 200。

設計：正向＋反向黑樣本對照，證明真判別力（非假綠）：
  - 若計費點被整個移除 → 正向 (+1) 測試會紅。
  - 若計費點仍在 translate/reply 之前 → 反向 (translate/reply 失敗不扣) 測試會紅。
  - reply 失敗仍不扣 → 證明計費點排在 reply「之後」。

全離線：StubTranslator + FakeLineReplyClient，無真實金鑰、無 time.sleep。
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

# 載入 model metadata
from saas_mvp.models import tenant as _t, user as _u, note as _n, usage as _us  # noqa: F401,E402
from saas_mvp.models import api_key as _ak, api_key_usage as _aku                # noqa: F401,E402
from saas_mvp.models import plan_change_history as _pch                          # noqa: F401,E402
import saas_mvp.models.line_channel_config as _lcm                              # noqa: F401,E402
import saas_mvp.models.line_user_lang as _lul                                   # noqa: F401,E402

from saas_mvp.app import create_app                                             # noqa: E402
from saas_mvp.db import Base, get_db                                            # noqa: E402
from saas_mvp.line_client import FakeLineReplyClient, get_line_client          # noqa: E402
from saas_mvp.translation import StubTranslator, get_translator               # noqa: E402
from saas_mvp.translation.base import TranslationResult                        # noqa: E402
from saas_mvp.models.usage import ApiUsage                                      # noqa: E402
from saas_mvp.quota import PLAN_DAILY_LIMITS                                    # noqa: E402

# ── 獨立 in-memory DB（與既有測試檔隔離） ─────────────────────────────────────
_engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
_Session = sessionmaker(autocommit=False, autoflush=False, bind=_engine)

_stub_translator = StubTranslator()
_fake_client = FakeLineReplyClient()

_CHANNEL_SECRET = "qa-task1-channel-secret-32bytes!"
_ACCESS_TOKEN = "qa-task1-access-token"


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
    app.dependency_overrides[get_line_client] = lambda: _fake_client
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


@pytest.fixture(autouse=True)
def _reset():
    _fake_client.reset()
    yield


# ── helpers ───────────────────────────────────────────────────────────────────
def _sign(body: bytes, secret: str = _CHANNEL_SECRET) -> str:
    return base64.b64encode(
        hmac.new(secret.encode(), body, hashlib.sha256).digest()
    ).decode()


def _text_event(text: str, rt: str = "rt", uid: str = "Uqa1") -> dict:
    return {
        "type": "message",
        "replyToken": rt,
        "source": {"type": "user", "userId": uid},
        "message": {"type": "text", "text": text},
    }


def _payload(*events) -> bytes:
    return json.dumps({"events": list(events)}).encode()


def _headers(body: bytes, secret: str = _CHANNEL_SECRET) -> dict:
    return {"X-Line-Signature": _sign(body, secret)}


def _register(client: TestClient) -> int:
    email = f"qa1_{uuid.uuid4().hex[:8]}@example.com"
    tn = f"qa1_{uuid.uuid4().hex[:8]}"
    r = client.post("/auth/register", json={
        "email": email, "password": "Test1234!", "tenant_name": tn})
    assert r.status_code == 201, r.text
    token = r.json()["access_token"]
    tid = client.get("/tenants/me",
                     headers={"Authorization": f"Bearer {token}"}).json()["id"]

    from saas_mvp.auth.security import decode_access_token
    from saas_mvp.models.user import User
    sub = int(decode_access_token(token)["sub"])
    db = _Session()
    try:
        db.get(User, sub).is_admin = True
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
    db = _Session()
    try:
        row = db.execute(
            ApiUsage.__table__.select().where(
                (ApiUsage.tenant_id == tid)
                & (ApiUsage.period == datetime.date.today())
            )
        ).first()
        return row.count if row else 0
    finally:
        db.close()


def _set_used(tid: int, count: int) -> None:
    db = _Session()
    try:
        db.add(ApiUsage(tenant_id=tid, period=datetime.date.today(), count=count))
        db.commit()
    finally:
        db.close()


# ── 失敗 stub ────────────────────────────────────────────────────────────────
class _BoomTranslator(StubTranslator):
    def translate(self, text: str, target_lang: str) -> TranslationResult:
        raise RuntimeError("translate backend down")


class _BoomReply(FakeLineReplyClient):
    def reply(self, reply_token: str, text: str, *, access_token: str) -> None:
        raise RuntimeError("LINE reply API down")


# ── 測試 ─────────────────────────────────────────────────────────────────────
class TestTask1BillingDeferred:
    def test_positive_success_increments_exactly_one(self, client):
        """正向黑樣本：translate + reply 都成功 → used 恰 +1（守住「計費點被移除」）。"""
        tid = _register(client)
        before = _used(tid)
        body = _payload(_text_event("hello", "rt-ok"))
        r = client.post(f"/line/webhook/{tid}", content=body, headers=_headers(body))
        assert r.status_code == 200
        assert _fake_client.call_count == 1
        assert _fake_client.last_text == "[ZH-TW] hello"
        assert _used(tid) == before + 1

    def test_positive_two_messages_increment_two(self, client):
        """正向：兩則文字 → used +2（計費點確實在每則成功後生效）。"""
        tid = _register(client)
        before = _used(tid)
        body = _payload(_text_event("a", "rt-a"), _text_event("b", "rt-b"))
        r = client.post(f"/line/webhook/{tid}", content=body, headers=_headers(body))
        assert r.status_code == 200
        assert _used(tid) == before + 2

    def test_reverse_translate_failure_no_charge(self, client):
        """反向：translate 拋例外 → handler 仍回 200、used 不變（task #5 契約）。

        背景化前：handler 同步拋例外 → 500、測試用 ``pytest.raises`` 收。
        背景化後：handler 立即回 200，翻譯在 background 內炸被
        ``_process_events`` 的 ``try/except`` 攔下只 log。語意保留
        （used 不白扣）+ 新契約（response 仍 200）。
        """
        tid = _register(client)
        before = _used(tid)
        app = client.app
        app.dependency_overrides[get_translator] = lambda: _BoomTranslator()
        try:
            body = _payload(_text_event("boom", "rt-tx"))
            r = client.post(f"/line/webhook/{tid}", content=body, headers=_headers(body))
            assert r.status_code == 200
            assert r.json() == {"status": "ok"}
        finally:
            app.dependency_overrides[get_translator] = lambda: _stub_translator
        assert _used(tid) == before
        assert _fake_client.call_count == 0  # 未送出任何 reply

    def test_reverse_reply_failure_no_charge(self, client):
        """反向：translate 成功但 reply 拋例外 → handler 仍回 200、used 不變（task #5 契約）。

        reply 在 background 內炸、handler 已送出 200、背景 try/except
        攔下只 log。line_client.reply 失敗前 increment 還沒跑（後扣
        骨架）→ used 維持原值。
        """
        tid = _register(client)
        before = _used(tid)
        app = client.app
        app.dependency_overrides[get_line_client] = lambda: _BoomReply()
        try:
            body = _payload(_text_event("boom2", "rt-rp"))
            r = client.post(f"/line/webhook/{tid}", content=body, headers=_headers(body))
            assert r.status_code == 200
            assert r.json() == {"status": "ok"}
        finally:
            app.dependency_overrides[get_line_client] = lambda: _fake_client
        assert _used(tid) == before

    def test_boundary_over_quota_no_increment_replies_msg(self, client):
        """邊界：已達上限 → 回配額訊息、used 不再 +1、HTTP 200。"""
        tid = _register(client)
        limit = PLAN_DAILY_LIMITS["free"]
        _set_used(tid, limit)
        body = _payload(_text_event("over", "rt-over"))
        r = client.post(f"/line/webhook/{tid}", content=body, headers=_headers(body))
        assert r.status_code == 200
        assert _fake_client.call_count == 1
        txt = _fake_client.last_text
        assert "配額" in txt or "quota" in txt.lower()
        assert _used(tid) == limit  # 沒有再 +1

    def test_boundary_one_below_limit_succeeds_to_limit(self, client):
        """邊界：used = limit-1 → 成功翻譯並 +1 至 limit（非遞增檢查不誤殺最後一格）。"""
        tid = _register(client)
        limit = PLAN_DAILY_LIMITS["free"]
        _set_used(tid, limit - 1)
        body = _payload(_text_event("last slot", "rt-last"))
        r = client.post(f"/line/webhook/{tid}", content=body, headers=_headers(body))
        assert r.status_code == 200
        assert _fake_client.last_text == "[ZH-TW] last slot"
        assert _used(tid) == limit
