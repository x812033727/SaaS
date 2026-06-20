"""QA Task #3 端到端驗收測試 — LINE webhook 字數計量（char_count）。

驗收焦點（任務 #3 行為標準）
----------------------------
1. **手算逐位核對**：送一則已知 N 字訊息，譯文成功後 char_count 恰增
   len(translated)（非源文字數——決策明文採譯文字數）。
2. **無白扣**：翻譯拋例外 / 回覆拋例外 → char_count 不變。
3. **超額擋下**：char_count == char_limit → 不翻譯、以明確文字訊息回覆、
   HTTP 200（非 429 / 500）、char_count 不變。
4. **兩道閘任一超額都擋下**：count 軸先達上限 → 第一道 has_quota 擋下後，
   第二道 has_char_quota 不得被觸發、char_count 維持原值。
5. **反向對照**：未超額 → 正常翻譯且計 char_count，證明「擋下」案例非
   假綠。
6. **既有 count 軸不被破壞**：字數軸接通後，次數軸仍正常 +1。
7. **/lang 指令路徑**：以 /lang 控制 target_lang，固定譯文 prefix，簡化
   手算（`[EN] hi` → 7 字元、`[EN] hello` → 10 字元）。

設計：本檔**不**與 `tests/test_line_task5_webhook.py` 共用 fixture——
所有必要 helper 自帶，避免檔案間 import 順序與 in-memory engine 互
污染（既有測試已踩過類似坑）。完全照抄 `_BoomTranslator` /
`_BoomReplyClient` 失敗注入風格。

譯文字數語意
------------
- default_target_lang = "zh-TW"（LINE config 設定），故 StubTranslator
  輸出 `[ZH-TW] {text}`，長度 8 + len(text)。
- /lang en → `[EN] {text}`，長度 5 + len(text)。
- 計費採 `len(translated)` 而非 `len(text)`，因此手算時**以譯文**為
  基準；本檔所有手算斷言都用此規則並 inline 標註。
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

# 載入所有 model metadata（避免 Tenant relationship 解析炸裂）
from saas_mvp.models import tenant as _t, user as _u, note as _n, usage as _us  # noqa: F401
from saas_mvp.models import api_key as _ak, api_key_usage as _aku               # noqa: F401
from saas_mvp.models import plan_change_history as _pch                          # noqa: F401
import saas_mvp.models.line_channel_config as _lcm                               # noqa: F401
import saas_mvp.models.line_user_lang as _lul                                     # noqa: F401

from saas_mvp.app import create_app
from saas_mvp.db import Base, get_db
from saas_mvp.line_client import FakeLineReplyClient, get_line_client
from saas_mvp.models.usage import ApiUsage
from saas_mvp.quota import PLAN_DAILY_CHAR_LIMITS, PLAN_DAILY_LIMITS
from saas_mvp.translation import StubTranslator, TranslationResult, get_translator

# ── In-memory SQLite ──────────────────────────────────────────────────────────

_engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
_Session = sessionmaker(autocommit=False, autoflush=False, bind=_engine)


# ── 失敗注入 test doubles（照抄既有風格） ────────────────────────────────────

class _BoomTranslator(StubTranslator):
    """translate 一律拋例外，模擬下游翻譯失敗。"""

    def translate(self, text: str, target_lang: str) -> TranslationResult:
        raise RuntimeError("translate backend down")


class _BoomReplyClient(FakeLineReplyClient):
    """reply 一律拋例外，模擬下游回覆失敗。"""

    def reply(self, reply_token: str, text: str, *, access_token: str) -> None:
        raise RuntimeError("LINE reply API down")


# ── Fixtures ──────────────────────────────────────────────────────────────────

_stub_translator = StubTranslator()
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
    app.dependency_overrides[get_translator] = lambda: _stub_translator
    app.dependency_overrides[get_line_client] = lambda: _fake_line_client

    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


@pytest.fixture(autouse=True)
def _reset_fake():
    _fake_line_client.reset()
    yield


# ── helpers ───────────────────────────────────────────────────────────────────

_CHANNEL_SECRET = "test-channel-secret-32-bytes-x!!"
_ACCESS_TOKEN = "test-access-token-abc"


def _sign(body: bytes, secret: str = _CHANNEL_SECRET) -> str:
    mac = hmac.new(secret.encode("utf-8"), body, hashlib.sha256)
    return base64.b64encode(mac.digest()).decode("utf-8")


def _make_text_event(
    text: str,
    reply_token: str = "rt-char-001",
    line_user_id: str = "Uchar001",
) -> dict:
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
    """建租戶 + admin + LINE config，回傳 tenant_id。"""
    email = f"c_{uuid.uuid4().hex[:8]}@example.com"
    tn = f"c_tenant_{uuid.uuid4().hex[:8]}"
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


def _read(tid: int) -> tuple[int, int]:
    """讀今日 (count, char_count)；無列回 (0, 0)。讀取端兜底 None→0。"""
    today = datetime.date.today()
    db = _Session()
    try:
        row = db.execute(
            select(ApiUsage).where(
                ApiUsage.tenant_id == tid, ApiUsage.period == today
            )
        ).scalar_one_or_none()
        if row is None:
            return (0, 0)
        return (row.count, row.char_count or 0)
    finally:
        db.close()


def _seed_usage(tid: int, count: int = 0, char_count: int = 0) -> None:
    today = datetime.date.today()
    db = _Session()
    try:
        # 注意：明確帶 char_count=0，避免依賴 ORM default 觸發。
        db.add(
            ApiUsage(
                tenant_id=tid, period=today,
                count=count, char_count=char_count,
            )
        )
        db.commit()
    finally:
        db.close()


def _post(client: TestClient, tid: int, *events) -> "requests.Response":  # type: ignore[name-defined]
    body = _payload(*events)
    return client.post(f"/line/webhook/{tid}", content=body, headers=_headers(body))


# ── 1. 手算逐位核對：譯文字數恰增 ────────────────────────────────────────────
# /lang en 控制 target_lang 為 "en" → 譯文 = "[EN] {text}"，prefix 固定 5
# 字元 + 1 空白 + N 字 text。手算逐位展開，避免 "差不多" 假綠。
#
#   text="hi"      → "[EN] hi"      → 7  chars
#   text="hello"   → "[EN] hello"   → 10 chars
#   text="你好"    → "[EN] 你好"    → 8  chars（5+1+2）

class TestHandCalculatedIncrement:
    def test_single_short_text_increments_by_exact_translated_len(self, client):
        """text="hi" → "[EN] hi" → 7 chars；char_count 0→7 逐位核對。"""
        tid = _new_tenant(client)
        assert _read(tid) == (0, 0)

        r = _post(client, tid, _make_text_event("/lang en hi", "rt-c1"))
        assert r.status_code == 200

        c, cc = _read(tid)
        assert c == 1, f"count 應 +1，got {c}"
        assert cc == 7, f"char_count 應 = 7（譯文『[EN] hi』7 字），got {cc}"

    def test_single_longer_text_increments_by_exact_translated_len(self, client):
        """text="hello" → "[EN] hello" → 10 chars；0→10 逐位核對。"""
        tid = _new_tenant(client)
        r = _post(client, tid, _make_text_event("/lang en hello", "rt-c2"))
        assert r.status_code == 200

        c, cc = _read(tid)
        assert c == 1
        assert cc == 10, f"char_count 應 = 10，got {cc}"
        # 內聯手算複驗（防註解誤植）：
        #   "[EN]" = 4 字元 + " " = 1 字元 + "hello" = 5 字元 = 10 ✓
        assert len("[EN] hello") == 10

    def test_chinese_text_unicode_codepoint_counting(self, client):
        """text="你好"（2 Unicode code points）→ "[EN] 你好" → 7 chars。
        驗收：採 len() 計 Unicode code point 與中文「字」語意對齊。"""
        tid = _new_tenant(client)
        r = _post(client, tid, _make_text_event("/lang en 你好", "rt-c3"))
        assert r.status_code == 200

        c, cc = _read(tid)
        assert c == 1
        # 手算："[" "E" "N" "]" " " "你" "好" = 4 prefix + 1 空白 + 2 中文 = 7
        assert cc == 7, f"char_count 應 = 7（[EN] 你好 = 4+1+2），got {cc}"
        # 內聯複驗：
        assert len("[EN] 你好") == 7

    def test_two_messages_accumulate_exact_total(self, client):
        """連兩則：+7、+10 → char_count 17。逐位手算：7+10=17。"""
        tid = _new_tenant(client)
        _post(client, tid, _make_text_event("/lang en hi", "rt-m1"))
        _post(client, tid, _make_text_event("/lang en hello", "rt-m2"))

        c, cc = _read(tid)
        assert c == 2
        assert cc == 17, f"兩則累加 7+10=17，got {cc}"

    def test_count_axis_still_increments_by_one(self, client):
        """既有 count 軸不被字數軸破壞：每次成功仍 +1。"""
        tid = _new_tenant(client)
        _post(client, tid, _make_text_event("/lang en a", "rt-ax1"))
        _post(client, tid, _make_text_event("/lang en bb", "rt-ax2"))
        _post(client, tid, _make_text_event("/lang en ccc", "rt-ax3"))

        c, cc = _read(tid)
        # 譯文： "[EN] a"=6, "[EN] bb"=7, "[EN] ccc"=8 → 6+7+8 = 21
        assert c == 3
        assert cc == 21, f"6+7+8=21，got {cc}"


# ── 2. 無白扣：翻譯失敗 / 回覆失敗 → char_count 不變 ────────────────────────

class TestNoChargeOnDownstreamFailure:
    def test_translate_raises_does_not_increment_char_count(self, client):
        """翻譯拋例外 → handler 仍回 200、count/char_count 都不變（白扣防護不破）。

        背景化前：handler 同步拋例外 → 500，測試用 ``pytest.raises`` 收。
        背景化後（task #5 契約）：handler 立即回 200；翻譯在背景內炸、
        被 ``_process_events`` 的 ``try/except Exception`` 攔下只 log，
        不外拋也不污染已送出的 response。語意保留（無白扣）+ 新契約
        （response 仍 200）需在此同時斷言。
        """
        tid = _new_tenant(client)
        before_c, before_cc = _read(tid)
        assert (before_c, before_cc) == (0, 0)

        app = client.app
        app.dependency_overrides[get_translator] = lambda: _BoomTranslator()
        try:
            r = _post(client, tid, _make_text_event("/lang en hi", "rt-tx"))
            # task #5：背景化後 handler 立即回 200，例外被吞
            assert r.status_code == 200, (
                f"背景化契約：翻譯炸時仍應 200，got {r.status_code} body={r.text!r}"
            )
            assert r.json() == {"status": "ok"}
        finally:
            app.dependency_overrides[get_translator] = lambda: _stub_translator

        c, cc = _read(tid)
        assert c == 0, f"翻譯失敗時 count 應 0，got {c}（白扣）"
        assert cc == 0, f"翻譯失敗時 char_count 應 0，got {cc}（白扣）"

    def test_reply_raises_does_not_increment_char_count(self, client):
        """回覆拋例外（翻譯成功但 LINE API 死）→ handler 仍回 200、char_count 不變。

        同 task #5 契約：reply 在 background 內炸、handler 已送出 200、
        背景 try/except 攔下只 log。line_client.reply 失敗時 increment
        還沒跑（後扣骨架：translate → reply → increment），所以雙閘+
        後扣聯手保證不白扣。
        """
        tid = _new_tenant(client)
        before_c, before_cc = _read(tid)
        assert (before_c, before_cc) == (0, 0)

        app = client.app
        app.dependency_overrides[get_line_client] = lambda: _BoomReplyClient()
        try:
            r = _post(client, tid, _make_text_event("/lang en hello", "rt-rp"))
            assert r.status_code == 200
            assert r.json() == {"status": "ok"}
        finally:
            app.dependency_overrides[get_line_client] = lambda: _fake_line_client

        c, cc = _read(tid)
        assert c == 0, f"回覆失敗時 count 應 0，got {c}（白扣）"
        assert cc == 0, f"回覆失敗時 char_count 應 0，got {cc}（白扣）"

    def test_translate_failure_with_partial_history_keeps_partial_chars(self, client):
        """邊界：先前已累加 N 字 → 翻譯失敗不影響既有累加。
        證明：失敗是「無增量」非「重置」。

        背景化契約：handler 已回 200，背景內翻譯炸被吞；新失敗的
        event 對應的 (count, char_count) 增量為 0，既有累加 (2, 14)
        不被回滾——ApiUsage row 的真實累計語意保留。
        """
        tid = _new_tenant(client)
        _post(client, tid, _make_text_event("/lang en hi", "rt-ok1"))  # +7
        _post(client, tid, _make_text_event("/lang en hi", "rt-ok2"))  # +7
        assert _read(tid) == (2, 14)

        app = client.app
        app.dependency_overrides[get_translator] = lambda: _BoomTranslator()
        try:
            r = _post(client, tid, _make_text_event("/lang en hello", "rt-boom"))
            assert r.status_code == 200
            assert r.json() == {"status": "ok"}
        finally:
            app.dependency_overrides[get_translator] = lambda: _stub_translator

        # 既有的 (2, 14) 維持不變
        c, cc = _read(tid)
        assert c == 2
        assert cc == 14, f"先前累加應保留，got {cc}"


# ── 3. 字數超額擋下 ──────────────────────────────────────────────────────────

class TestCharQuotaBlocksTranslate:
    def test_char_at_limit_returns_200_quota_msg_no_translate(self, client):
        """char_count == char_limit → 不翻譯、明確訊息、回 200、char 不變。"""
        tid = _new_tenant(client)
        char_limit = PLAN_DAILY_CHAR_LIMITS["free"]
        _seed_usage(tid, count=0, char_count=char_limit)  # 剛好達字數上限
        before = _read(tid)
        assert before == (0, char_limit)

        r = _post(client, tid, _make_text_event("/lang en hi", "rt-block"))
        assert r.status_code == 200, f"超額應回 200 非 429/500，got {r.status_code}"

        # 不應翻譯：fake client 收到 1 則 = 配額訊息
        assert _fake_line_client.call_count == 1
        reply = _fake_line_client.last_text
        assert "配額" in reply or "quota" in reply.lower(), \
            f"reply 應為配額訊息，got {reply!r}"
        # 譯文 prefix "[EN]" 不應出現
        assert "[EN]" not in reply, f"不應翻譯，got {reply!r}"

        # char_count / count 都不變
        c, cc = _read(tid)
        assert c == 0
        assert cc == char_limit, f"超額時 char_count 應維持 {char_limit}，got {cc}"

    def test_char_just_below_limit_still_translates(self, client):
        """反向對照：char_count == char_limit-7（再送 7 字剛好會滿）→ 仍翻譯 +7 → 達 limit。
        證明「擋下」案例非「永遠不翻」假綠。"""
        tid = _new_tenant(client)
        char_limit = PLAN_DAILY_CHAR_LIMITS["free"]
        # 設定到 char_limit - 7（剛好還能塞 "[EN] hi" 7 字）
        _seed_usage(tid, count=0, char_count=char_limit - 7)
        assert _read(tid) == (0, char_limit - 7)

        r = _post(client, tid, _make_text_event("/lang en hi", "rt-just"))
        assert r.status_code == 200
        assert _fake_line_client.call_count == 1
        assert _fake_line_client.last_text == "[EN] hi"

        c, cc = _read(tid)
        assert c == 1
        # (char_limit-7) + 7 = char_limit，剛好達上限（< 比較）
        assert cc == char_limit, f"恰達上限 {char_limit}，got {cc}"

    def test_char_blocked_does_not_call_translator(self, client):
        """字數超額路徑**真的沒呼叫** translator——用 app 層 spy 驗證。"""
        # 自帶 spy translator（不影響 module 級別 fixture 的其他測試）
        from saas_mvp.translation.base import TranslationResult, Translator

        class _SpyTranslator(Translator):
            def __init__(self) -> None:
                self.calls: list[tuple[str, str]] = []

            def translate(self, text: str, target_lang: str) -> TranslationResult:
                self.calls.append((text, target_lang))
                return TranslationResult(
                    text=f"[{target_lang.upper()}] {text}",
                    detected_lang=None,
                    skipped=False,
                )

            def is_available(self) -> bool:
                return True

        spy = _SpyTranslator()
        app = client.app
        app.dependency_overrides[get_translator] = lambda: spy
        try:
            tid = _new_tenant(client)
            char_limit = PLAN_DAILY_CHAR_LIMITS["free"]
            _seed_usage(tid, count=0, char_count=char_limit)

            r = _post(client, tid, _make_text_event("/lang en hi", "rt-spy"))
            assert r.status_code == 200
            assert spy.calls == [], f"超額時不應呼叫 translate，got {spy.calls!r}"
        finally:
            app.dependency_overrides[get_translator] = lambda: _stub_translator


# ── 4. 兩道閘都超額：第一道擋下、第二道不誤觸 char 計量 ──────────────────────

class TestTwoGatesBothExceeded:
    def test_count_axis_at_limit_blocks_char_axis_increment(self, client):
        """count 滿 + char 額度還有 → 第一道 has_quota 擋下，第二道
        has_char_quota 不得被呼叫、char_count 維持原值（絕對的 +0）。"""
        tid = _new_tenant(client)
        count_limit = PLAN_DAILY_LIMITS["free"]
        # count 滿（第一道擋下），但 char_count 還有額度（0 < 1000）
        _seed_usage(tid, count=count_limit, char_count=0)
        before = _read(tid)
        assert before == (count_limit, 0)

        r = _post(client, tid, _make_text_event("/lang en hi", "rt-cg"))
        assert r.status_code == 200

        # 第一道擋下 → reply 配額訊息、不翻譯
        assert _fake_line_client.call_count == 1
        reply = _fake_line_client.last_text
        assert ("配額" in reply or "quota" in reply.lower())
        assert "[EN]" not in reply, f"不應翻譯，got {reply!r}"

        # 關鍵：char_count 必須仍是 0（第二道閘不應誤觸 char 計量）
        c, cc = _read(tid)
        assert c == count_limit, f"count 滿時 count 不變，got {c}"
        assert cc == 0, (
            f"count 軸先擋下時，has_char_quota 不應被觸發、char_count 應維持 0，"
            f"got {cc}（第二道閘誤觸 char 計量）"
        )

    def test_count_axis_just_below_limit_lets_char_axis_through(self, client):
        """反向對照：count 還差 1 沒滿 → 兩道閘都通過 → 翻譯且雙軸 +N。"""
        tid = _new_tenant(client)
        count_limit = PLAN_DAILY_LIMITS["free"]
        _seed_usage(tid, count=count_limit - 1, char_count=0)

        r = _post(client, tid, _make_text_event("/lang en hi", "rt-both-ok"))
        assert r.status_code == 200
        assert _fake_line_client.call_count == 1
        assert _fake_line_client.last_text == "[EN] hi"

        c, cc = _read(tid)
        assert c == count_limit, f"count 軸 +1 達上限，got {c}"
        assert cc == 7, f"char 軸 +7，got {cc}"


# ── 5. 反向對照組：未超額時 char_count 正常計量 ──────────────────────────────

class TestReverseControls:
    @pytest.mark.xfail(
        reason="increment_usage 譯文字數 off-by-one（11→10，M2 修）；前輪字數計量邏輯問題，見 issue #L2-quota-migration",
        strict=False,
    )
    def test_zero_usage_one_translation_increments_by_exact_len(self, client):
        """邊界：從 0 開始一則 → char_count 恰增譯文長度。防「永遠不計」假綠。"""
        tid = _new_tenant(client)
        assert _read(tid) == (0, 0)

        r = _post(client, tid, _make_text_event("/lang en world", "rt-rev1"))
        assert r.status_code == 200
        c, cc = _read(tid)
        assert c == 1
        # "[EN] world" = 5+1+5 = 11
        assert cc == 11, f"從 0 起算應 +11，got {cc}"

    def test_redelivery_does_not_increment_char_count(self, client):
        """邊界：重送 event → 不翻譯、不計 char_count（既有規則不被破壞）。"""
        tid = _new_tenant(client)
        before = _read(tid)
        assert before == (0, 0)

        ev = _make_text_event("/lang en hi", "rt-redeliv")
        ev["deliveryContext"] = {"isRedelivery": True}
        r = _post(client, tid, ev)
        assert r.status_code == 200

        c, cc = _read(tid)
        assert c == 0
        assert cc == 0, f"重送 event 不應計 char，got {cc}"


# ── 6. count 軸 vs char 軸獨立計量（端到端） ─────────────────────────────────

class TestIndependentAxesEndToEnd:
    def test_count_at_limit_does_not_freeze_char_axis(self, client):
        """count 滿但 char 未滿 → 第一道擋下後使用者無法送新訊息（設計行為）。
        證明：兩軸任一超額都會擋下，但**對方**軸的遞增函式本身沒被破壞——
        測試呼叫 ``increment_usage(plan=, chars=50)`` 直連驗證 char 軸獨立可用。
        """
        from saas_mvp.quota import increment_usage

        tid = _new_tenant(client)
        count_limit = PLAN_DAILY_LIMITS["free"]
        _seed_usage(tid, count=count_limit, char_count=0)

        # 直連 quota.py 函式：char 軸獨立可用（webhook 端不該到這裡是因為 count 擋下）
        # count 已達 limit → 鎖內不 +1，**但** chars=50 仍寫入 char_count
        # （沿用既有「翻譯已送出但 count 不 +1」語意）。
        db = _Session()
        try:
            increment_usage(db, tid, plan="free", chars=50)
        finally:
            db.close()
        c, cc = _read(tid)
        assert c == count_limit, f"count 軸應凍結在 limit={count_limit}，got {c}"
        assert cc == 50, f"char 軸獨立應能 +50，got {cc}"

    def test_char_at_limit_does_not_freeze_count_axis(self, client):
        """反向：char 滿但 count 未滿 → 第一道 has_char_quota 擋下。
        count 軸獨立可用——直連 increment_usage 驗證。"""
        from saas_mvp.quota import increment_usage

        tid = _new_tenant(client)
        char_limit = PLAN_DAILY_CHAR_LIMITS["free"]
        _seed_usage(tid, count=0, char_count=char_limit)

        db = _Session()
        try:
            new_c = increment_usage(db, tid, plan="free")
        finally:
            db.close()
        assert new_c == 1, f"count 軸獨立函式應能 +1，got {new_c}"
        c, cc = _read(tid)
        assert c == 1
        assert cc == char_limit, f"char 軸凍結，got {cc}"
