"""Task #1 驗收測試 — quota 計費點後移（副作用成功後才計量）。

驗收標準
--------
1. translate 拋例外 → 當日 quota `used` 不增加（失敗 stub）、回覆未送出。
2. reply 拋例外 → 當日 quota `used` 不增加（失敗 client）。
3. 正常路徑（translate + reply 皆成功）→ quota `used` 恰好 +1。
4. 超量（used 已達 limit）→ 不翻譯、回覆原配額訊息、`used` 不變（仍 == limit）。

反向樣本（對照組）：每個「不應 +1」案例都搭配一個「正常路徑必 +1」對照，
證明計量機制真的有作用、非假綠。

全部離線：StubTranslator / 失敗 stub + FakeLineReplyClient / 失敗 client，
無真實金鑰、無 time.sleep。
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
from saas_mvp.line_client.base import LineReplyError
from saas_mvp.models.usage import ApiUsage
from saas_mvp.quota import PLAN_DAILY_LIMITS
from saas_mvp.translation import StubTranslator, get_translator
from saas_mvp.translation.base import TranslationError, TranslationResult, Translator

# ── In-memory SQLite ──────────────────────────────────────────────────────────

_engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
_Session = sessionmaker(autocommit=False, autoflush=False, bind=_engine)


# ── test doubles ──────────────────────────────────────────────────────────────

class FailingTranslator(Translator):
    """每次 translate 都拋 TranslationError 的失敗 stub。"""

    def translate(self, text: str, target_lang: str) -> TranslationResult:
        raise TranslationError("simulated translate failure")

    def is_available(self) -> bool:
        return True


class FailingLineReplyClient(FakeLineReplyClient):
    """reply 一律拋 LineReplyError，但仍記錄被呼叫的次數。"""

    def __init__(self) -> None:
        super().__init__()
        self.attempts: list[str] = []

    def reply(self, reply_token: str, text: str, *, access_token: str) -> None:
        self.attempts.append(text)
        raise LineReplyError("simulated reply failure")

    def reset(self) -> None:
        """同步清 sent 與 attempts，避免漏同步（高工審查建議）。"""
        super().reset()
        self.attempts.clear()


_stub_translator = StubTranslator()
_failing_translator = FailingTranslator()
_fake_line_client = FakeLineReplyClient()
_failing_line_client = FailingLineReplyClient()

# 由各測試切換要注入哪個 translator / client
_active = {"translator": _stub_translator, "client": _fake_line_client}


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
    app.dependency_overrides[get_translator] = lambda: _active["translator"]
    app.dependency_overrides[get_line_client] = lambda: _active["client"]

    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


# ── helpers ───────────────────────────────────────────────────────────────────

_CHANNEL_SECRET = "test-channel-secret-32-bytes-x!!"
_ACCESS_TOKEN = "test-access-token-abc"


def _sign(body: bytes, secret: str = _CHANNEL_SECRET) -> str:
    mac = hmac.new(secret.encode("utf-8"), body, hashlib.sha256)
    return base64.b64encode(mac.digest()).decode("utf-8")


def _text_event(text: str, reply_token: str = "rt", line_user_id: str = "Uq001") -> dict:
    return {
        "type": "message",
        "replyToken": reply_token,
        "source": {"type": "user", "userId": line_user_id},
        "message": {"type": "text", "text": text},
    }


def _payload(*events) -> bytes:
    return json.dumps({"events": list(events)}).encode("utf-8")


def _headers(body: bytes) -> dict:
    return {"X-Line-Signature": _sign(body)}


def _new_tenant(client: TestClient) -> int:
    """建立租戶 + LINE config，回傳 tenant_id。"""
    email = f"q_{uuid.uuid4().hex[:8]}@example.com"
    tn = f"q_tenant_{uuid.uuid4().hex[:8]}"
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
    """讀今日 ApiUsage.count；無列回 0。"""
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
    _failing_line_client.reset()  # override 後一併清 attempts
    _active["translator"] = _stub_translator
    _active["client"] = _fake_line_client
    yield


# ── 反向樣本基準：正常路徑必 +1 ──────────────────────────────────────────────

class TestNormalPathIncrements:
    def test_success_increments_used_by_one(self, client):
        tid = _new_tenant(client)
        assert _used(tid) == 0
        body = _payload(_text_event("hello"))
        r = client.post(f"/line/webhook/{tid}", content=body, headers=_headers(body))
        assert r.status_code == 200
        assert _fake_line_client.call_count == 1
        assert _fake_line_client.last_text == "[ZH-TW] hello"
        assert _used(tid) == 1  # 正常路徑：恰好 +1

    def test_two_success_increments_to_two(self, client):
        tid = _new_tenant(client)
        body = _payload(_text_event("a"), _text_event("b"))
        r = client.post(f"/line/webhook/{tid}", content=body, headers=_headers(body))
        assert r.status_code == 200
        assert _used(tid) == 2  # 兩則皆成功 → +2


# ── #1：translate 失敗 → 不計量 ──────────────────────────────────────────────

class TestTranslateFailureNoCharge:
    def test_translate_failure_does_not_increment(self, client):
        """翻譯拋例外 → handler 仍回 200、quota 不被白扣（task #5 契約）。

        背景化前：handler 同步拋例外 → 500、測試用 ``pytest.raises`` 收。
        背景化後：handler 立即回 200，翻譯在 background 內炸被
        ``_process_events`` 的 ``try/except`` 攔下只 log。語意保留
        （quota 不被白扣）+ 新契約（response 仍 200）需在此同時斷言。
        """
        tid = _new_tenant(client)
        assert _used(tid) == 0
        _active["translator"] = _failing_translator  # 失敗 stub

        body = _payload(_text_event("hello"))
        r = client.post(f"/line/webhook/{tid}", content=body, headers=_headers(body))
        # task #5：背景化後 handler 立即回 200，例外被吞
        assert r.status_code == 200, (
            f"翻譯炸時 handler 應回 200（背景例外只 log），"
            f"got {r.status_code} body={r.text!r}"
        )
        assert r.json() == {"status": "ok"}

        # 關鍵斷言：quota 未被白扣
        assert _used(tid) == 0
        # 反向對照：換回正常 translator，同租戶應能正常 +1
        _active["translator"] = _stub_translator
        r = client.post(f"/line/webhook/{tid}", content=body, headers=_headers(body))
        assert r.status_code == 200
        assert _used(tid) == 1


# ── #1：reply 失敗 → 不計量 ──────────────────────────────────────────────────

class TestReplyFailureNoCharge:
    def test_reply_failure_does_not_increment(self, client):
        """回覆拋例外 → handler 仍回 200、quota 不被白扣（task #5 契約）。

        reply 在 background 內炸、handler 已送出 200、背景 try/except
        攔下只 log。翻譯已成功 → translator 紀錄會留 trace（attempts），
        但 line_client.reply 失敗前 increment 還沒跑（後扣骨架保證）→
        quota 維持 0。
        """
        tid = _new_tenant(client)
        assert _used(tid) == 0
        _active["client"] = _failing_line_client  # 失敗 client

        body = _payload(_text_event("hello"))
        r = client.post(f"/line/webhook/{tid}", content=body, headers=_headers(body))
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}

        # reply 真的被嘗試過（翻譯已成功），但因 reply 失敗 → 不計量
        assert _failing_line_client.attempts == ["[ZH-TW] hello"]
        assert _used(tid) == 0
        # 反向對照：換回正常 client，同租戶應能正常 +1
        _active["client"] = _fake_line_client
        r = client.post(f"/line/webhook/{tid}", content=body, headers=_headers(body))
        assert r.status_code == 200
        assert _used(tid) == 1


# ── #1：超量 → 不翻譯、回配額訊息、不 +1 ─────────────────────────────────────

class TestQuotaExceededNoCharge:
    def test_over_quota_replies_and_does_not_increment(self, client):
        tid = _new_tenant(client)
        limit = PLAN_DAILY_LIMITS["free"]
        _seed_usage(tid, limit)  # 剛好達上限
        assert _used(tid) == limit

        body = _payload(_text_event("over quota"))
        r = client.post(f"/line/webhook/{tid}", content=body, headers=_headers(body))
        assert r.status_code == 200  # 不是 429 / 500

        # 回覆的是配額訊息，不是翻譯結果
        assert _fake_line_client.call_count == 1
        reply_text = _fake_line_client.last_text
        assert "配額" in reply_text or "quota" in reply_text.lower()
        assert not reply_text.startswith("[")  # 非 StubTranslator 譯文

        # used 不變（仍 == limit），未再 +1
        assert _used(tid) == limit

    def test_just_below_limit_still_increments(self, client):
        """反向對照：limit-1 時仍應翻譯並 +1，達到 limit。"""
        tid = _new_tenant(client)
        limit = PLAN_DAILY_LIMITS["free"]
        _seed_usage(tid, limit - 1)

        body = _payload(_text_event("last one"))
        r = client.post(f"/line/webhook/{tid}", content=body, headers=_headers(body))
        assert r.status_code == 200
        assert _fake_line_client.last_text == "[ZH-TW] last one"  # 有翻譯
        assert _used(tid) == limit  # +1 達上限


# ── #1：increment_usage 鎖內重驗 limit（TOCTOU 防護，資安審查建議） ──────────

class TestIncrementUsageRecheck:
    def test_increment_does_not_exceed_limit_when_plan_given(self, client):
        """模擬 TOCTOU：count 已達 limit 時，increment_usage(plan) 不再 +1。"""
        from saas_mvp.quota import increment_usage
        tid = _new_tenant(client)
        limit = PLAN_DAILY_LIMITS["free"]
        _seed_usage(tid, limit)

        db = _Session()
        try:
            result = increment_usage(db, tid, plan="free")
        finally:
            db.close()
        assert result == limit       # 未遞增
        assert _used(tid) == limit   # DB 仍為 limit，永不超賣

    def test_increment_without_plan_still_increments(self, client):
        """反向對照：未傳 plan（無重驗）時仍 +1，證明重驗確實由 plan 觸發。"""
        from saas_mvp.quota import increment_usage
        tid = _new_tenant(client)
        limit = PLAN_DAILY_LIMITS["free"]
        _seed_usage(tid, limit)

        db = _Session()
        try:
            result = increment_usage(db, tid)  # 無 plan → 不重驗
        finally:
            db.close()
        assert result == limit + 1
        assert _used(tid) == limit + 1
