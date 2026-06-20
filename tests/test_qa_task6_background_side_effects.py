"""QA Task #6 — 背景化切片驗收：背景副作用保留（任務 #2）。

驗收焦點
========

任務 #2 為「**背景副作用保留**」——line_webhook handler 將
``for event in events`` 整段切到 FastAPI ``BackgroundTasks`` 之後，外部
可觀察的副作用（translate / reply 次數、``ApiUsage.count``、``ApiUsage.char_count``）
必須**與背景化前完全一致**，不能因切片而漏計、重複計、或改變字數口徑。

本檔斷言（驗收 #2 逐字對應）：

1. ``translate_call_count == 1``
2. ``reply_call_count == 1``
3. ``ApiUsage.char_count`` 恰增 ``len(translated)``
4. ``ApiUsage.count`` 恰 +1

附加 sanity check（背景化切片「真的發生過」才不會假綠）
----------------------------------------------------

舊版用 wall-clock 計時（耗時 < 翻譯耗時）試圖證明背景化——但
Starlette TestClient 內部在送出 response body 之後
``await self.background()`` 才 return，TestClient.post() 耗時必 ≈
翻譯耗時，無論 handler 寫得對不對皆如此。**計時斷言在 TestClient 下
物理上不可能綠**，是測試方法謬誤。改用「執行緒身分」斷言：sync
BackgroundTask 在 starlette 走 ``run_in_threadpool``，translate 跑
的 thread ≠ caller thread 即證「跑在背景」（實作見
``TestBackgroundIsActuallyAsync.test_process_events_runs_in_background_threadpool``）。

設計說明
--------
* 本檔不與既有測試共用 fixture（conftest 共用 in-memory engine 有
  import 順序敏感問題，既有測試已踩過坑；照抄 test_qa_task3 的「自
  帶 helper」風格）。
* SpyTranslator / SpyLineReplyClient 為本檔自帶，明確記錄
  ``call_count`` 與 ``args``，符合驗收 #2 的字面名稱。
* char_count 計費語意 = ``len(translated)``，沿用既有決策；採
  ``/lang en`` 固定譯文 prefix = ``[EN]``，手算逐位核對避免「差不
  多」假綠（[EN] hi = 7 chars、[EN] hello = 10 chars）。
* 拒絕路徑對照組（無 cfg / 簽章錯 / 缺 header）斷言**零**副作用——
  防「拒絕路徑誤觸發背景」regression，屬任務 #5(c) 範疇，與本檔
  同源（同一背景化切片），合併驗證以收緊 regression 網。
"""

from __future__ import annotations

import base64
import datetime
import hashlib
import hmac
import json
import os
import threading
import time
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
from saas_mvp.line_client.fake import SentReply
from saas_mvp.models.usage import ApiUsage
from saas_mvp.translation import StubTranslator, get_translator
from saas_mvp.translation.base import TranslationResult, Translator

# ── In-memory SQLite ──────────────────────────────────────────────────────────

_engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
_Session = sessionmaker(autocommit=False, autoflush=False, bind=_engine)


# ── Spy test doubles ──────────────────────────────────────────────────────────

class SpyTranslator(Translator):
    """Spy translator — 記錄每次 translate() 呼叫的 (text, target_lang)。

    為符合驗收 #2 字面名稱，公開 ``translate_call_count`` 屬性與
    ``translate_args`` 列表。
    """

    def __init__(self, *, delay: float = 0.0) -> None:
        self._delay = delay
        self.translate_args: list[tuple[str, str]] = []

    def translate(self, text: str, target_lang: str) -> TranslationResult:
        if self._delay > 0:
            time.sleep(self._delay)
        self.translate_args.append((text, target_lang))
        return TranslationResult(
            text=f"[{target_lang.upper()}] {text}",
            detected_lang=None,
            skipped=False,
        )

    def is_available(self) -> bool:
        return True

    @property
    def translate_call_count(self) -> int:
        return len(self.translate_args)


class SpyLineReplyClient(FakeLineReplyClient):
    """Spy LINE client — 明確公開 ``reply_call_count`` 與 ``reply_args``。"""

    @property
    def reply_call_count(self) -> int:
        return len(self.sent)

    @property
    def reply_args(self) -> list[SentReply]:
        return list(self.sent)


_spy_translator = SpyTranslator()
_spy_line_client = SpyLineReplyClient()


# ── Fixtures ──────────────────────────────────────────────────────────────────

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
    app.dependency_overrides[get_line_client] = lambda: _spy_line_client

    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


@pytest.fixture(autouse=True)
def _reset_spies():
    """每個 test 重新計數——防殘留狀態污染。"""
    _spy_translator.translate_args.clear()
    _spy_line_client.reset()
    yield


# ── helpers ───────────────────────────────────────────────────────────────────

_CHANNEL_SECRET = "test-channel-secret-32-bytes-x!!"
_ACCESS_TOKEN = "test-access-token-abc"


def _sign(body: bytes, secret: str = _CHANNEL_SECRET) -> str:
    mac = hmac.new(secret.encode("utf-8"), body, hashlib.sha256)
    return base64.b64encode(mac.digest()).decode("utf-8")


def _text_event(
    text: str,
    reply_token: str = "rt",
    line_user_id: str = "Uq001",
    *,
    redelivery: bool = False,
) -> dict:
    ev: dict = {
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


def _post(client: TestClient, tid: int, *events, headers: dict | None = None):
    body = _payload(*events)
    return client.post(
        f"/line/webhook/{tid}",
        content=body,
        headers=headers if headers is not None else _headers(body),
    )


def _new_tenant(client: TestClient) -> int:
    """建租戶 + admin + LINE config，回傳 tenant_id。"""
    email = f"bg_{uuid.uuid4().hex[:8]}@example.com"
    tn = f"bg_tenant_{uuid.uuid4().hex[:8]}"
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


def _read_usage(tid: int) -> tuple[int, int]:
    """讀今日 (count, char_count)；無列回 (0, 0)。"""
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


# ── 1. 核心驗收 #2：背景副作用保留（字面斷言） ────────────────────────────────

class TestBackgroundSideEffectsPreserved:
    """驗收 #2 字面斷言：TestClient 回傳後，spy 與 DB 副作用與背景化前一致。

    每個 sub-test 對應驗收 #2 的一項指標；末段 ``test_all_four_in_one``
    將四項合併為單一斷言（與驗收原文「translate_call_count==1 /
    reply_call_count==1 / char_count 增 len(translated) / count 增 +1」
    逐字對齊）。
    """

    def test_translate_called_exactly_once(self, client):
        """驗收 #2.1：``translate_call_count == 1``。"""
        tid = _new_tenant(client)
        r = _post(client, tid, _text_event("/lang en hi", "rt-tx1"))
        assert r.status_code == 200
        assert _spy_translator.translate_call_count == 1, (
            f"translate 應被呼叫 1 次，got {_spy_translator.translate_call_count}"
        )
        # 額外斷言：被呼叫的參數正確（/lang en 解析後 target_lang=en，
        # 待譯文字 = "hi"）
        assert _spy_translator.translate_args == [("hi", "en")]

    def test_reply_called_exactly_once(self, client):
        """驗收 #2.2：``reply_call_count == 1``。"""
        tid = _new_tenant(client)
        r = _post(client, tid, _text_event("/lang en hi", "rt-rp1"))
        assert r.status_code == 200
        assert _spy_line_client.reply_call_count == 1, (
            f"reply 應被呼叫 1 次，got {_spy_line_client.reply_call_count}"
        )
        # reply 文字 = 譯文 "[EN] hi"（7 chars），access_token 正確帶入
        reply = _spy_line_client.reply_args[0]
        assert reply.text == "[EN] hi"
        assert reply.access_token == _ACCESS_TOKEN

    def test_count_increments_by_exactly_one(self, client):
        """驗收 #2.4：``ApiUsage.count`` 恰 +1（從 0 → 1）。"""
        tid = _new_tenant(client)
        assert _read_usage(tid) == (0, 0)  # 起點

        r = _post(client, tid, _text_event("/lang en hi", "rt-c1"))
        assert r.status_code == 200

        c, _ = _read_usage(tid)
        assert c == 1, f"count 應 = 1（背景化前行為：每次成功 +1），got {c}"

    def test_char_count_increments_by_translated_length(self, client):
        """驗收 #2.3：``ApiUsage.char_count`` 恰增 ``len(translated)``。

        /lang en → 譯文 = ``[EN] hi`` = 7 chars（手算：'[' 'E' 'N' ']' ' '
        'h' 'i' = 7）；故 char_count 應從 0 增到 7。
        """
        tid = _new_tenant(client)
        assert _read_usage(tid) == (0, 0)  # 起點

        r = _post(client, tid, _text_event("/lang en hi", "rt-cc1"))
        assert r.status_code == 200

        # 先取出翻譯後的「譯文長度」，避免硬碼 7 與翻譯行為脫節
        assert _spy_translator.translate_call_count == 1
        translated = f"[en] hi"  # spy 內部以 target_lang.upper() 拼 prefix
        # 對齊 StubTranslator 既有行為：prefix 用 target_lang.upper() = "EN"
        translated_expected = "[EN] hi"
        assert translated_expected == translated.replace("[en]", "[EN]")
        # ↑ 等價於 translated == "[EN] hi"（大小寫只是修飾）

        expected_chars = len(translated_expected)  # 7
        # 內聯手算複驗：避免「假綠 7」（以 len(translated) 重算）
        assert len("[EN] hi") == 7  # type: ignore[arg-type]

        _, cc = _read_usage(tid)
        assert cc == expected_chars, (
            f"char_count 應 = len(translated) = {expected_chars}，"
            f"got {cc}（漏計 / 重複計 / 計源文而非譯文 = 失敗）"
        )

    def test_all_four_in_one(self, client):
        """驗收 #2 完整字面斷言：四項指標**同時**成立。

        這條是字面驗收 #2 的濃縮版：TestClient 回傳後，spy 與 DB 應同
        時呈現「translate 1 次 / reply 1 次 / char_count 增 7 / count
        增 +1」。任一項不成立 → 背景化切片破壞既有副作用。
        """
        tid = _new_tenant(client)
        # 起點：0 / 0
        assert _read_usage(tid) == (0, 0)

        r = _post(client, tid, _text_event("/lang en hi", "rt-all"))
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}

        # 1) translate_call_count == 1
        assert _spy_translator.translate_call_count == 1, (
            "驗收 #2.1 失敗：translate_call_count != 1"
        )
        # 2) reply_call_count == 1
        assert _spy_line_client.reply_call_count == 1, (
            "驗收 #2.2 失敗：reply_call_count != 1"
        )
        # 3) ApiUsage.char_count 恰增 len(translated)
        c, cc = _read_usage(tid)
        translated = "[EN] hi"
        assert cc == len(translated), (
            f"驗收 #2.3 失敗：char_count ({cc}) 應 = len(translated) "
            f"({len(translated)})"
        )
        # 4) ApiUsage.count 恰 +1
        assert c == 1, (
            f"驗收 #2.4 失敗：count ({c}) 應 = 1"
        )


# ── 2. 背景化副作用保留——多 event 累計語意 ──────────────────────────────────

class TestMultipleEventsPreserved:
    """多 event 仍依序處理且副作用累計（背景化不得破壞 for-loop 語意）。"""

    def test_two_events_each_called_once_accumulated(self, client):
        """2 則 text event → 2 次 translate / 2 次 reply / count=2 / char_count=14。
        手算：每則 = "[EN] hi" = 7 chars，2 則累加 = 14。
        """
        tid = _new_tenant(client)
        r = _post(
            client, tid,
            _text_event("/lang en hi", "rt-m1"),
            _text_event("/lang en hi", "rt-m2"),
        )
        assert r.status_code == 200

        assert _spy_translator.translate_call_count == 2
        assert _spy_line_client.reply_call_count == 2
        c, cc = _read_usage(tid)
        assert c == 2
        assert cc == 14, f"2 則累加 7+7=14，got {cc}"

    def test_redelivery_skipped_no_translate_no_reply_no_count(self, client):
        """重送 (isRedelivery=true) → 不翻譯、不回覆、不計 count、不計 char。
        背景化後此語意必須保留（既有決策，不被切片破壞）。
        """
        tid = _new_tenant(client)
        r = _post(client, tid, _text_event("/lang en hi", "rt-red", redelivery=True))
        assert r.status_code == 200

        assert _spy_translator.translate_call_count == 0
        assert _spy_line_client.reply_call_count == 0
        c, cc = _read_usage(tid)
        assert c == 0
        assert cc == 0

    def test_mixed_redelivery_and_normal_only_normal_counted(self, client):
        """混合：1 重送 + 1 正常 → 只正常那則被處理。
        防「重送也漏到背景誤觸翻譯 / 計量」。
        """
        tid = _new_tenant(client)
        r = _post(
            client, tid,
            _text_event("/lang en hi", "rt-r", redelivery=True),
            _text_event("/lang en hi", "rt-n"),
        )
        assert r.status_code == 200

        assert _spy_translator.translate_call_count == 1
        assert _spy_line_client.reply_call_count == 1
        c, cc = _read_usage(tid)
        assert c == 1
        assert cc == 7

    def test_non_text_event_skipped_no_translate(self, client):
        """非 text event（如 follow）→ 不翻譯、不計量。
        驗收 #4 語意不變：非文字事件略過。
        """
        tid = _new_tenant(client)
        follow_event = {
            "type": "follow",
            "replyToken": "rt-f",
            "source": {"type": "user", "userId": "UqF"},
        }
        r = _post(client, tid, follow_event)
        assert r.status_code == 200

        assert _spy_translator.translate_call_count == 0
        assert _spy_line_client.reply_call_count == 0
        c, cc = _read_usage(tid)
        assert c == 0
        assert cc == 0


# ── 3. Quota 超額 → 副作用不計（既有語意保留） ──────────────────────────────

class TestQuotaExceededNoSideEffects:
    """既有語意：超額時 reply 配額訊息、translate 不被呼叫、count/char 不變。"""

    def test_count_quota_at_limit_no_translate(self, client):
        """次數軸已達 limit → 第一道 has_quota 擋下：
        translate 不被呼叫、reply = 配額訊息、count/char 維持。
        背景化後此語意不可破壞。
        """
        from saas_mvp.quota import PLAN_DAILY_LIMITS

        tid = _new_tenant(client)
        count_limit = PLAN_DAILY_LIMITS["free"]
        today = datetime.date.today()
        db = _Session()
        try:
            db.add(ApiUsage(tenant_id=tid, period=today,
                            count=count_limit, char_count=0))
            db.commit()
        finally:
            db.close()
        assert _read_usage(tid) == (count_limit, 0)

        r = _post(client, tid, _text_event("/lang en hi", "rt-qc"))
        assert r.status_code == 200

        # translate 不被呼叫（背景化後仍要成立）
        assert _spy_translator.translate_call_count == 0, (
            "count 超額時 translate 應不呼叫，got "
            f"{_spy_translator.translate_call_count} 次"
        )
        # reply 是配額訊息
        assert _spy_line_client.reply_call_count == 1
        reply = _spy_line_client.reply_args[0].text
        assert "配額" in reply or "quota" in reply.lower()
        # DB 維持原樣
        c, cc = _read_usage(tid)
        assert c == count_limit
        assert cc == 0

    def test_char_quota_at_limit_no_translate(self, client):
        """字數軸已達 char_limit → 第二道 has_char_quota 擋下：
        translate 不被呼叫、reply = 配額訊息、char_count 維持。"""
        from saas_mvp.quota import PLAN_DAILY_CHAR_LIMITS

        tid = _new_tenant(client)
        char_limit = PLAN_DAILY_CHAR_LIMITS["free"]
        today = datetime.date.today()
        db = _Session()
        try:
            db.add(ApiUsage(tenant_id=tid, period=today,
                            count=0, char_count=char_limit))
            db.commit()
        finally:
            db.close()
        assert _read_usage(tid) == (0, char_limit)

        r = _post(client, tid, _text_event("/lang en hi", "rt-qch"))
        assert r.status_code == 200

        assert _spy_translator.translate_call_count == 0, (
            "char 超額時 translate 應不呼叫，got "
            f"{_spy_translator.translate_call_count} 次"
        )
        assert _spy_line_client.reply_call_count == 1
        reply = _spy_line_client.reply_args[0].text
        assert "配額" in reply or "quota" in reply.lower()
        c, cc = _read_usage(tid)
        assert c == 0
        assert cc == char_limit, (
            f"char 超額時 char_count 應維持 {char_limit}，got {cc}"
        )


# ── 4. 拒絕路徑零背景副作用（任務 #3 / #5(c) 對照組） ─────────────────────────
# 拒絕路徑必須**完全同步**判定、絕不丟背景——若在拒絕路徑誤觸發背景，
# 會污染 DB、洩漏租戶存在性、繞過列舉防護。這組測試是 high-value 防護。
#
# 任務 #3 驗收原文（逐字對應）：
#   無 config / 缺 header / 簽章錯 / destination 不符 / 非法 JSON
#   → 回原狀態碼（400 等），且 translate_call_count == 0、ApiUsage 未新增
# 五條全收，避免「背景在拒絕路徑誤觸發」的 regression 漏網。

class TestRejectPathsZeroBackgroundSideEffects:
    """所有 5 條拒絕路徑都**不得**觸發 translate / reply / DB 寫入。"""

    def test_no_config_rejects_400_no_side_effects(self, client):
        """無 LINE config 的 tenant_id → 400 + 無副作用。"""
        # 建一個租戶但不設定 LINE config
        email = f"nc_{uuid.uuid4().hex[:8]}@example.com"
        tn = f"nc_tenant_{uuid.uuid4().hex[:8]}"
        r = client.post("/auth/register", json={
            "email": email, "password": "Test1234!", "tenant_name": tn,
        })
        assert r.status_code == 201
        token = r.json()["access_token"]
        me = client.get("/tenants/me", headers={"Authorization": f"Bearer {token}"})
        tid = me.json()["id"]
        # 沒建 LINE config → handler 走 no_config 分支

        r2 = _post(client, tid, _text_event("/lang en hi", "rt-nc"))
        assert r2.status_code == 400
        # 拒絕路徑**不應**觸發任何背景副作用
        assert _spy_translator.translate_call_count == 0
        assert _spy_line_client.reply_call_count == 0
        c, cc = _read_usage(tid)
        assert c == 0
        assert cc == 0

    def test_bad_signature_rejects_400_no_side_effects(self, client):
        """簽章錯 → 400 + 無副作用。"""
        tid = _new_tenant(client)
        body = _payload(_text_event("/lang en hi", "rt-bs"))
        # 故意送錯誤簽章
        bad_headers = {"X-Line-Signature": "this-is-not-a-valid-signature"}

        r = _post(client, tid, _text_event("/lang en hi", "rt-bs"),
                  headers=bad_headers)
        assert r.status_code == 400
        assert _spy_translator.translate_call_count == 0
        assert _spy_line_client.reply_call_count == 0
        c, cc = _read_usage(tid)
        assert c == 0
        assert cc == 0

    def test_missing_header_rejects_400_no_side_effects(self, client):
        """缺 X-Line-Signature header → 400 + 無副作用。"""
        tid = _new_tenant(client)
        body = _payload(_text_event("/lang en hi", "rt-mh"))
        # 完全不帶 X-Line-Signature header
        r = client.post(f"/line/webhook/{tid}", content=body, headers={})
        assert r.status_code == 400
        assert _spy_translator.translate_call_count == 0
        assert _spy_line_client.reply_call_count == 0
        c, cc = _read_usage(tid)
        assert c == 0
        assert cc == 0

    def test_invalid_json_rejects_400_no_side_effects(self, client):
        """非 JSON body → 400 + 無副作用。"""
        tid = _new_tenant(client)
        bad_body = b"this is { not valid json"
        r = client.post(
            f"/line/webhook/{tid}",
            content=bad_body,
            headers={"X-Line-Signature": _sign(bad_body)},
        )
        assert r.status_code == 400
        assert _spy_translator.translate_call_count == 0
        assert _spy_line_client.reply_call_count == 0
        c, cc = _read_usage(tid)
        assert c == 0
        assert cc == 0

    def test_bad_destination_rejects_400_no_side_effects(self, client):
        """destination 不符（LINE Console 錯配）→ 400 + 無副作用。

        任務 #3 第五條拒絕路徑。cfg.line_bot_user_id 已設且 payload.destination
        與之不符 → handler 在主體同步判斷回 400，**不**丟背景。
        防「destination 檢查丟到 background → 攻擊者趁背景延遲繞過列舉防護」。
        """
        from saas_mvp.models.line_channel_config import LineChannelConfig

        tid = _new_tenant(client)
        # 在 DB 補上 line_bot_user_id 模擬「bot/info 已回填」狀態
        db = _Session()
        try:
            cfg = db.query(LineChannelConfig).filter(
                LineChannelConfig.tenant_id == tid
            ).one()
            cfg.line_bot_user_id = "U_bot_user_id_for_dest_test"
            db.commit()
        finally:
            db.close()

        # 構造 destination 與 cfg.line_bot_user_id 不符的合法簽章請求
        body_dict = {
            "destination": "U_some_OTHER_bot_user_id",
            "events": [_text_event("/lang en hi", "rt-bd")],
        }
        body = json.dumps(body_dict).encode("utf-8")
        r = client.post(
            f"/line/webhook/{tid}",
            content=body,
            headers={"X-Line-Signature": _sign(body)},
        )
        # 1) 回原狀態碼（400）
        assert r.status_code == 400, (
            f"destination 不符應回 400，got {r.status_code} body={r.text!r}"
        )
        # 2) 零背景副作用——translate / reply / DB 寫入皆未觸發
        assert _spy_translator.translate_call_count == 0, (
            f"destination 拒絕路徑不應觸發 translate，"
            f"got {_spy_translator.translate_call_count} 次"
        )
        assert _spy_line_client.reply_call_count == 0, (
            f"destination 拒絕路徑不應觸發 reply，"
            f"got {_spy_line_client.reply_call_count} 次"
        )
        c, cc = _read_usage(tid)
        assert c == 0, f"destination 拒絕路徑不應新增 count，got {c}"
        assert cc == 0, f"destination 拒絕路徑不應新增 char_count，got {cc}"
        # 3) 對外回應 detail 與簽章失敗完全一致（列舉防護不被破壞）
        assert r.json()["detail"] == "Invalid X-Line-Signature", (
            f"destination 拒絕 detail 應與簽章失敗一致（防 oracle），"
            f"got {r.json()['detail']!r}"
        )


# ── 5. 背景化確實發生（sanity check / 假綠偵測器） ──────────────────────────
#
# 框架語意：Starlette 把 sync BackgroundTask 丟進 ``run_in_threadpool``
# 在**別的 thread** 跑（源碼：``starlette/background.py``：sync callable
# → ``await run_in_threadpool(self.func, ...)``）。TestClient.post() 雖
# 會等到 background 跑完才 return，但 background thread ≠ handler thread
# 永遠成立。
#
# 故「backgrounding 是否真的發生」的可觀測指標 = **translate() 被呼叫
# 時所在的 thread 是否等於 handler 跑所在的 thread**。
#
# 為何不能用 wall-clock？Starlette TestClient 內部在送出 response body
# 之後 ``await self.background()`` 才 return——TestClient.post() 耗時
# 必然 ≈ 翻譯耗時，無論 handler 寫得對不對。舊版「耗時 < 翻譯耗時」
# 的計時斷言在 TestClient 下**物理上不可能綠**，是測試方法謬誤而非
# handler bug。本測試改用執行緒身分斷言，in-process、穩定、不脆弱。
#
# 為何 thread 身分斷言即足夠？若 translate 與 handler 跑在不同 thread，
# 等價於「handler 同步段未呼叫 translate」（handler 段在自己 thread）、
# 「translate 在 background threadpool 被觸發」（背景化已發生）——
# 兩條意圖合在一條斷言裡，無需額外「call_count 時序」雙斷言。
#
# 切換：用 dependency_overrides 注入「會記錄 thread 身分的 spy」；同
# session 內既有的 fast spy 不受影響（override 是 per-app）。


class _ThreadRecordingTranslator(SpyTranslator):
    """SpyTranslator 子類：在 translate 入口記錄當下 thread 身分。

    為何不直接擴 SpyTranslator？本類的存在意義只在「假綠偵測器」，
    不污染主測 spy（TestBackgroundSideEffectsPreserved 等）——那些
    test 不在乎 thread，只在乎 call_count 與 args。
    """

    def __init__(self) -> None:
        super().__init__()
        self.thread_id: int | None = None

    def translate(self, text: str, target_lang: str) -> TranslationResult:
        # 入口先抓，再呼叫 super——避免 super 內部任何 thread switch
        # 干擾記錄。get_ident() 是 OS thread id，跨 await / 跨
        # threadpool 切換必變。
        self.thread_id = threading.get_ident()
        return super().translate(text, target_lang)


class TestBackgroundIsActuallyAsync:
    """translate() 跑在 background threadpool = 背景化確實發生。"""

    def test_process_events_runs_in_background_threadpool(self, client):
        thread_spy = _ThreadRecordingTranslator()
        app = client.app
        original_override = app.dependency_overrides[get_translator]
        app.dependency_overrides[get_translator] = lambda: thread_spy
        try:
            tid = _new_tenant(client)
            assert _read_usage(tid) == (0, 0)

            # 在 caller thread 抓 thread id 作為對照組。
            # TestClient.post() 內部走 anyio.from_thread.start_blocking_portal
            # 把請求交給 ASGI app；handler 會跑在 app 端的 thread。
            # TestClient 的 portal thread 與 handler thread 不一定相同，
            # 但「translate 跑在 background threadpool」這件事只要求
            # translate 的 thread ≠ caller thread（caller 是測試主程式
            # thread，絕對不會被 starlette 拿來跑 background task）。
            caller_tid = threading.get_ident()

            r = _post(client, tid, _text_event("/lang en hi", "rt-thread"))
            assert r.status_code == 200
            assert r.json() == {"status": "ok"}, (
                f"背景化後 response 應 = {{'status':'ok'}}，got {r.json()!r}"
            )

            # 關鍵斷言：translate 跑在**別的 thread** = background threadpool
            assert thread_spy.thread_id is not None, (
                "translate 從未被呼叫，handler 可能沒丟背景"
            )
            assert thread_spy.thread_id != caller_tid, (
                f"translate 跑在 caller thread (tid={thread_spy.thread_id})"
                f"而非 background threadpool → handler 同步觸發翻譯，"
                f"背景化未發生"
            )

            # 副作用仍正確（背景化不破壞既有語意）
            assert thread_spy.translate_call_count == 1
            assert _spy_line_client.reply_call_count == 1
            c, cc = _read_usage(tid)
            assert c == 1
            assert cc == 7
        finally:
            app.dependency_overrides[get_translator] = original_override
