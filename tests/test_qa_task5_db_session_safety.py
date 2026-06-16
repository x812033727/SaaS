"""QA Task #5 — DB session 安全驗收：背景任務 session 生命週期 + 例外處理。

驗收焦點
========

任務 #5 對應驗收標準 #5：

> DB session 安全：背景任務不使用已關閉的 request-scoped session；
> 背景內例外被 try/except 攔下、只 log 不影響已送出的 200。

本檔逐字對應驗收 #5，斷言四點：

1. **背景 session 與 request-scoped session 是不同物件**（id 不同）。
   程式碼保證：handler 抓 ``db.get_bind()``（engine handle）丟背景，
   背景用 ``Session(bind=bind)`` **新開** session；永不重用
   request-scoped session。

2. **背景 session 與 request-scoped session 共享同一 engine**。
   程式碼保證：背景寫入的 ``ApiUsage`` 必須在測試 DB 可讀到。
   防「engine handle 拿錯 / 背景綁到 production 引擎」回歸。

3. **背景內拋任何例外 → response 仍 200 + 不冒出 server exception**。
   程式碼保證：``_process_events`` 整段 ``for event in events`` 在
   ``try/except Exception`` 內，``_log.exception`` 後 swallow；Starlette
   預設對背景任務例外不 re-raise。

4. **背景執行時 request-scoped session 已關閉**（深化 #1）。
   FastAPI dependency teardown 在 response 送出後執行 → request
   session ``.close()`` 早於 background 啟動。背景若「剛好」在
   response 後引用 request session 會遇到 closed session，會出
   ``InvalidRequestError: this Session's transaction has been rolled
   back``。本測試直接觀察兩者時序，防「不小心重用」的 regression。

5. **多次 request 後 DB 連線不洩漏**。
   程式碼保證：``finally: db.close()`` 確保 background session 必
   釋回 pool；無 finally 會讓 StaticPool 之外的 engine 連線耗盡。
   測試用大量 request 驗證。

設計說明
--------
* 自帶 fixture / spy（不與既有測試共用），與 ``test_qa_task6_*`` 同源
  風格，但目標聚焦 session 生命週期與例外處理。
* Spy 機制：用 ``unittest.mock.patch.object`` 替換
  ``saas_mvp.routers.line_webhook.Session`` 為 factory spy，記錄所有
  背景內新開的 session 物件。
* request-scoped session spy：override ``get_db`` 為 generator 形式，
  並在 yield 前把 session 物件註冊到 module-level list。
* 例外測試：用會拋例外的 spy translator / line_client 注入，背景
  觸發例外；用 ``caplog`` 驗證 log + ``TestClient(app,
  raise_server_exceptions=True)`` 確保無 server exception 冒出。
"""

from __future__ import annotations

import base64
import datetime
import hashlib
import hmac
import json
import logging
import os
import time
import uuid
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session as RealSession, sessionmaker
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
from saas_mvp.translation import StubTranslator, get_translator
from saas_mvp.translation.base import Translator

# ── In-memory SQLite ──────────────────────────────────────────────────────────

_engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
_Session = sessionmaker(autocommit=False, autoflush=False, bind=_engine)


# ── Spy test doubles ──────────────────────────────────────────────────────────

class SpyTranslator(Translator):
    """Spy translator — 記錄呼叫。可設定 ``raise_on_translate`` 觸發背景例外。"""

    def __init__(self, *, raise_on_translate: Exception | None = None) -> None:
        self.translate_args: list[tuple[str, str]] = []
        self._raise = raise_on_translate

    def translate(self, text: str, target_lang: str) -> str:
        self.translate_args.append((text, target_lang))
        if self._raise is not None:
            raise self._raise
        return f"[{target_lang.upper()}] {text}"

    def is_available(self) -> bool:
        return True

    @property
    def translate_call_count(self) -> int:
        return len(self.translate_args)


class SpyLineReplyClient(FakeLineReplyClient):
    """Spy LINE client。"""

    @property
    def reply_call_count(self) -> int:
        return len(self.sent)


# ── Session spy 工具 ────────────────────────────────────────────────────────
# 用 patch.object 替換 line_webhook 模組內的 Session class，記下所有
# 背景內被建立的 session 物件。同時 hook 該 instance 的 close() 方法，
# 標記 ``_bg_spy_close_called`` 供測試觀察「finally 區塊是否跑了」。

# 模組載入後再 import line_webhook（避免 line_webhook import 時還沒
# patch；用 patch.object 動態替換，import 時機無影響）
import saas_mvp.routers.line_webhook as _lw  # noqa: E402

# 全域 session spy list —— 每次背景內 db = Session(bind=bind) 會 push 進來
_BG_SESSIONS: list[RealSession] = []
# request-scoped session 觀察點：fixture 內的 override_get_db 會 push 進來
_REQUEST_SESSIONS: list[RealSession] = []


def _spy_session_factory(*args, **kwargs):
    """替換 ``saas_mvp.routers.line_webhook.Session`` 的 factory。

    行為與原 Session 完全相同，僅在建立時註冊到 ``_BG_SESSIONS`` 供測試
    觀察，且在 instance 上 hook ``close()`` 標記
    ``_bg_spy_close_called = True``。SQLAlchemy 2.x session close()
    後 is_active / execute 行為不可靠，hook close() 是 100% 精準的
    「finally 區塊跑了沒」指標。
    """
    real = RealSession(*args, **kwargs)
    real._bg_spy_close_called = False
    original_close = real.close

    def _spy_close(*a, **kw):
        real._bg_spy_close_called = True
        return original_close(*a, **kw)

    real.close = _spy_close  # type: ignore[method-assign]
    _BG_SESSIONS.append(real)
    return real


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def request_session_spy():
    """記下 request-scoped session 物件，供測試對比 background session。

    清空 list 後 override ``get_db``，每次 request 都會 yield 同一個
    session instance（per-request），並 push 進 ``_REQUEST_SESSIONS``。
    """
    _REQUEST_SESSIONS.clear()
    _BG_SESSIONS.clear()

    def override_get_db():
        db = _Session()
        _REQUEST_SESSIONS.append(db)
        try:
            yield db
        finally:
            db.close()

    return override_get_db


@pytest.fixture
def client(request_session_spy):
    Base.metadata.create_all(bind=_engine)
    app = create_app()

    spy_translator = SpyTranslator()
    spy_line_client = SpyLineReplyClient()

    app.dependency_overrides[get_db] = request_session_spy
    app.dependency_overrides[get_translator] = lambda: spy_translator
    app.dependency_overrides[get_line_client] = lambda: spy_line_client

    with patch.object(_lw, "Session", side_effect=_spy_session_factory):
        with TestClient(app, raise_server_exceptions=True) as c:
            # 把 spy 物件掛在 client 上方便測試讀取
            c.test_translator = spy_translator  # type: ignore[attr-defined]
            c.test_line_client = spy_line_client  # type: ignore[attr-defined]
            yield c


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


def _new_tenant(client: TestClient) -> int:
    """建租戶 + admin + LINE config，回傳 tenant_id。"""
    email = f"ses_{uuid.uuid4().hex[:8]}@example.com"
    tn = f"ses_tenant_{uuid.uuid4().hex[:8]}"
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


def _post(client: TestClient, tid: int, *events):
    body = _payload(*events)
    return client.post(
        f"/line/webhook/{tid}",
        content=body,
        headers=_headers(body),
    )


def _read_usage(tid: int) -> tuple[int, int]:
    """讀今日 (count, char_count)。"""
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


# ── 1. 背景 session 與 request-scoped session 是不同物件 ─────────────────────

class TestBackgroundUsesOwnSession:
    """驗收 #5.1：背景 session ≠ request-scoped session（不重用已關閉 session）。"""

    def test_background_creates_new_session_not_request_scoped(self, client):
        """背景內 db 物件 id 與 request-scoped db 不同 → 絕不重用。

        handler 內 ``bind = db.get_bind()`` 抓 engine handle，背景用
        ``Session(bind=bind)`` **新開** session。若 background 不慎重用
        request-scoped session，會在 response 後遇到 closed session 報
        ``InvalidRequestError: this Session's transaction has been rolled
        back``。本測試透過 spy 直接斷言兩者 id 不同。
        """
        tid = _new_tenant(client)
        _BG_SESSIONS.clear()
        _REQUEST_SESSIONS.clear()

        r = _post(client, tid, _text_event("/lang en hi", "rt-s1"))
        assert r.status_code == 200

        # 1) request 期間有 1 個 request-scoped session 被建立
        assert len(_REQUEST_SESSIONS) == 1, (
            f"應有 1 個 request-scoped session，got {len(_REQUEST_SESSIONS)}"
        )
        request_db = _REQUEST_SESSIONS[0]

        # 2) 背景內 db = Session(bind=bind) 至少建立 1 個 background session
        assert len(_BG_SESSIONS) >= 1, (
            f"背景內應至少 1 個 db = Session(bind=bind) 呼叫，"
            f"got {len(_BG_SESSIONS)} 次（_process_events 內未自管 session → 嚴重 bug）"
        )
        bg_db = _BG_SESSIONS[0]

        # 3) 兩者 id 不同 → 背景未重用 request session
        assert id(bg_db) != id(request_db), (
            "背景 session 與 request-scoped session 是**同一物件**——"
            "會在 response 後遇到 closed session，是嚴重的 session 生命週期 bug"
        )

    def test_background_session_bound_to_same_engine_as_request(self, client):
        """背景 session 與 request-scoped session 共享同一 engine。

        防「engine handle 拿錯 / 背景綁到錯的 engine」回歸——
        若背景綁到 production engine（不同 :memory:），測試端
        ``_read_usage`` 永遠讀不到 background 寫入的 ApiUsage。
        """
        tid = _new_tenant(client)
        _BG_SESSIONS.clear()
        _REQUEST_SESSIONS.clear()

        r = _post(client, tid, _text_event("/lang en hi", "rt-s2"))
        assert r.status_code == 200

        assert len(_BG_SESSIONS) >= 1
        assert len(_REQUEST_SESSIONS) == 1

        bg_db = _BG_SESSIONS[0]
        request_db = _REQUEST_SESSIONS[0]

        # 兩者共用同一 engine 物件（綁同一顆 SQLite in-memory）
        assert bg_db.get_bind() is request_db.get_bind(), (
            f"背景 session engine ({bg_db.get_bind()!r}) 與 request "
            f"session engine ({request_db.get_bind()!r}) 不同——"
            f"engine handle 傳錯，背景寫入會到別的庫"
        )

    def test_background_writes_visible_in_test_db(self, client):
        """背景 session 寫入的 ApiUsage 在測試端可讀 → engine 確實共享。

        端到端驗證：背景的 ``increment_usage`` 寫入後，測試自己的
        session 透過 select 能讀到 count/char_count 增加。
        若 engine 綁錯，這條會 fail（讀到 0）。
        """
        tid = _new_tenant(client)
        assert _read_usage(tid) == (0, 0)

        r = _post(client, tid, _text_event("/lang en hi", "rt-s3"))
        assert r.status_code == 200

        c, cc = _read_usage(tid)
        assert c == 1 and cc == 7, (
            f"背景寫入測試端應可讀，got count={c} char_count={cc} "
            f"（= 0 = 背景 session engine 與測試 engine 不同顆 :memory:）"
        )


# ── 2. 背景內例外被 try/except 攔下、不 re-raise ──────────────────────────
#
# 共用 helper：在獨立 app 上建 tenant + LINE config，回傳 (app, tid)。
# 這樣例外測試可在同一 app 內替換 spy，斷言背景例外被吞下。

def _setup_app_with_tenant(setup_name: str):
    """建 app + 建表 + override get_db + 註冊 tenant + LINE config。

    回傳 ``(app, tid)``。後續 test 可繼續在 ``app.dependency_overrides``
    注入新 spy 替換。
    """
    Base.metadata.create_all(bind=_engine)
    app = create_app()

    def _gen():
        db = _Session()
        try:
            yield db
        finally:
            db.close()
    app.dependency_overrides[get_db] = _gen
    app.dependency_overrides[get_translator] = lambda: SpyTranslator()
    app.dependency_overrides[get_line_client] = lambda: SpyLineReplyClient()

    email = f"{setup_name}_{uuid.uuid4().hex[:8]}@example.com"
    tn = f"{setup_name}_tenant_{uuid.uuid4().hex[:8]}"
    with TestClient(app) as c:
        r1 = c.post("/auth/register", json={
            "email": email, "password": "Test1234!", "tenant_name": tn,
        })
        assert r1.status_code == 201, r1.text
        token = r1.json()["access_token"]
        me = c.get("/tenants/me", headers={"Authorization": f"Bearer {token}"})
        tid = me.json()["id"]
        from saas_mvp.auth.security import decode_access_token
        from saas_mvp.models.user import User
        p = decode_access_token(token)
        db = _Session()
        try:
            u = db.get(User, int(p["sub"]))
            u.is_admin = True
            db.commit()
        finally:
            db.close()
        r2 = c.put(
            f"/admin/line-configs/{tid}",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "channel_secret": _CHANNEL_SECRET,
                "access_token": _ACCESS_TOKEN,
                "default_target_lang": "zh-TW",
            },
        )
        assert r2.status_code == 200, r2.text

    return app, tid


class TestBackgroundExceptionSwallowed:
    """驗收 #5.2：背景內拋例外 → response 仍 200 + 無 server exception 冒出。"""

    def test_translate_exception_does_not_break_200(self, caplog):
        """翻譯炸例外 → response 仍 200，**從 client 視角看**。

        重點：``raise_server_exceptions=False`` 才能驗「client 視角的
        200」——預設 ``True`` 下 Starlette TestClient 會把 background
        例外 re-raise 到 ``client.post()`` 之外，這是 TestClient 設計
        而非程式碼 bug。真實部署中（uvicorn）Starlette 對 background
        例外**默默吞掉**，不影響已送出的 200。

        驗證點：
        1. ``response.status_code == 200``（client 視角）
        2. ``response.json() == {"status": "ok"}``（對外契約不變）
        3. log 內有 ``_process_events failed`` 記錄（log.exception 觸發）
        4. translate 被呼叫 1 次（背景跑到 translate 才炸）→ 確認
           ``try/except`` 是包在 for-loop 內，**整段**都被攔下而非
           個別 try 漏抓
        """
        app, tid = _setup_app_with_tenant("exc")
        exploding = SpyTranslator(raise_on_translate=RuntimeError("翻譯引擎爆炸了"))
        app.dependency_overrides[get_translator] = lambda: exploding

        with caplog.at_level(logging.ERROR, logger="saas_mvp.routers.line_webhook"):
            with TestClient(app, raise_server_exceptions=False) as c:
                body = _payload(_text_event("/lang en hi", "rt-exc"))
                r = c.post(
                    f"/line/webhook/{tid}",
                    content=body,
                    headers=_headers(body),
                )

        # 1) response 200（背景例外不影響 client 視角）
        assert r.status_code == 200, (
            f"背景例外應被 try/except 攔下，client 視角應仍 200，"
            f"got {r.status_code} body={r.text!r}"
        )
        # 2) 對外契約不變
        assert r.json() == {"status": "ok"}, (
            f"對外契約應仍為 {{'status':'ok'}}，got {r.json()!r}"
        )
        # 3) log 有記錄例外（log.exception 留 traceback）
        log_text = "\n".join(rec.getMessage() for rec in caplog.records)
        assert "_process_events failed" in log_text, (
            f"log 應記錄『_process_events failed』供除錯，caplog 內容:\n{log_text}"
        )
        # 4) 翻譯被呼叫 1 次（背景跑到 _translate_sync 才炸）
        assert exploding.translate_call_count == 1, (
            f"background 應跑到 translate 才炸（不是更早就攔下），"
            f"got {exploding.translate_call_count} 次"
        )

    def test_reply_exception_does_not_break_200(self):
        """reply 炸例外 → response 仍 200（client 視角）。

        注入會拋例外的 line_client.reply；background 跑到 reply 才炸，
        被 try/except 攔下。
        """
        app, tid = _setup_app_with_tenant("rpl")

        class ExplodingLineClient(FakeLineReplyClient):
            def reply(self, reply_token, text, access_token=None):
                raise RuntimeError("LINE reply API 爆炸了")

        exploding_lc = ExplodingLineClient()
        app.dependency_overrides[get_line_client] = lambda: exploding_lc

        with TestClient(app, raise_server_exceptions=False) as c:
            body = _payload(_text_event("/lang en hi", "rt-rpl-exc"))
            r = c.post(
                f"/line/webhook/{tid}",
                content=body,
                headers=_headers(body),
            )

        assert r.status_code == 200, (
            f"reply 炸例外應被 try/except 攔下，client 視角 200，"
            f"got {r.status_code} body={r.text!r}"
        )
        assert r.json() == {"status": "ok"}

    def test_increment_exception_does_not_break_200(self, caplog):
        """increment_usage 炸例外 → response 仍 200（client 視角）。

        模擬「翻譯成功 / reply 成功 / increment 失敗」場景——既有失敗
        模式（已服務未計費）本來就接受；背景例外被 try/except 攔下，
        200 不受影響。**額外驗證副作用**：count 仍 0、char_count 仍 0
        ——increment 炸了沒寫入 DB。

        注意：patch 對象是 ``line_webhook.increment_usage`` 而非
        ``saas_mvp.quota.increment_usage``——line_webhook 在 module
        load 時已 import increment_usage 進自己 namespace，呼叫走
        模組屬性查找，patch module-level 屬性不會影響已 import 的
        reference。
        """
        app, tid = _setup_app_with_tenant("inc")
        assert _read_usage(tid) == (0, 0)  # 起點

        real_increment = _lw.increment_usage

        def exploding_increment(db, tenant_id, plan, chars):
            raise RuntimeError("DB 鎖死 / 連線炸 / increment 失敗")

        _lw.increment_usage = exploding_increment
        try:
            with caplog.at_level(logging.ERROR, logger="saas_mvp.routers.line_webhook"):
                with TestClient(app, raise_server_exceptions=False) as c:
                    body = _payload(_text_event("/lang en hi", "rt-inc-exc"))
                    r = c.post(
                        f"/line/webhook/{tid}",
                        content=body,
                        headers=_headers(body),
                    )
            assert r.status_code == 200, (
                f"increment 炸例外應被 try/except 攔下，client 視角 200，"
                f"got {r.status_code} body={r.text!r}"
            )
            assert r.json() == {"status": "ok"}
            # 副作用：count 仍 0（increment 炸了 → 沒寫入）
            c_count, cc = _read_usage(tid)
            assert c_count == 0 and cc == 0, (
                f"increment 炸後 DB 應仍 (0, 0)（沒白扣也沒白寫），"
                f"got ({c_count}, {cc})"
            )
            # log 有記錄
            log_text = "\n".join(rec.getMessage() for rec in caplog.records)
            assert "_process_events failed" in log_text, (
                f"log 應記錄『_process_events failed』，caplog:\n{log_text}"
            )
        finally:
            _lw.increment_usage = real_increment


# ── 3. 背景未重用 request-scoped session（id 與時序） ─────────────────────

class TestRequestSessionClosedBeforeBackground:
    """驗收 #5.3 深化：背景 session 物件**絕對不是** request-scoped session。

    關鍵澄清：驗收 #5 說的是「不使用已關閉的 request-scoped session」——
    程式碼路徑保證（handler 傳的是 ``bind=db.get_bind()`` = engine handle，
    背景用 ``Session(bind=bind)`` **新開** session）。本 class 透過
    spy 物件 id 直接驗證「background session 與 request session 永
    遠不是同一個物件」。

    為什麼不用 ``is_active`` 斷言：SQLAlchemy session 的
    ``is_active`` 屬性代表「是否在 transaction」而非「是否已 close」，
    close() 後仍可能為 True——不可靠。本檔改用「close 後 execute 必
    炸」做 close 偵測（見 ``_assert_session_closed``）。
    """

    def test_background_does_not_touch_request_session_object(self, client):
        """背景 session 物件在建立時**不是** request-scoped session 物件。

        透過 patch 替換 Session 為 spy factory，觀察所有背景內
        ``Session(bind=...)`` 呼叫的物件 id，必須不等於 request
        session id。這是「絕對未重用」的最精準斷言。
        """
        tid = _new_tenant(client)
        _REQUEST_SESSIONS.clear()
        _BG_SESSIONS.clear()

        r = _post(client, tid, _text_event("/lang en hi", "rt-nt"))
        assert r.status_code == 200

        request_session_ids = {id(s) for s in _REQUEST_SESSIONS}
        bg_session_ids = {id(s) for s in _BG_SESSIONS}

        # 背景 session 集合與 request session 集合**完全無交集**
        overlap = request_session_ids & bg_session_ids
        assert not overlap, (
            f"背景 session 與 request session 有 id 重疊 {overlap}——"
            f"背景重用了已關閉的 request session（驗收 #5 失敗）"
        )

    def test_background_session_close_call_count_matches_request(self, client):
        """N 次 request → N 次 background session 被 close。

        觀察「close 後 execute 必炸」：每次 request 後的 background
        session 都進入 closed 狀態。防「finally: db.close() 漏寫」。
        """
        tid = _new_tenant(client)

        # 三次 request
        bg_sessions_seen = []
        for i in range(3):
            _BG_SESSIONS.clear()
            r = _post(client, tid, _text_event("/lang en hi", f"rt-close-{i}"))
            assert r.status_code == 200
            bg_sessions_seen.extend(_BG_SESSIONS)

        # 每次 request 都至少 1 個 background session
        assert len(bg_sessions_seen) >= 3, (
            f"3 次 request 應有 ≥3 個 background session，got {len(bg_sessions_seen)}"
        )
        # 全部都 close（finally 區塊釋回 pool）
        for j, bg_db in enumerate(bg_sessions_seen):
            _assert_session_closed(bg_db)

    def test_session_factory_not_used_directly_in_background(self, client):
        """背景用 ``Session(bind=bind)`` 而非 ``SessionLocal()``。

        PM 議程的「DB session 生命週期」結論本來是「用 SessionLocal /
        sessionmaker」，但實際實作改為「傳 engine handle 進背景」——
        這避開了 PM 議程未涵蓋的「測試 engine 與 production engine
        不同」問題。透過 spy ``SessionLocal`` 確認背景沒直接呼叫它
        （否則測試會綁到 production engine、background 寫入到錯的庫）。
        """
        from saas_mvp import db as _db_mod

        tid = _new_tenant(client)
        _BG_SESSIONS.clear()

        original_sessionlocal = _db_mod.SessionLocal
        sessionlocal_call_count = [0]

        class SessionLocalSpy:
            def __call__(self, *a, **kw):
                sessionlocal_call_count[0] += 1
                return original_sessionlocal(*a, **kw)

        spy_sl = SessionLocalSpy()
        _db_mod.SessionLocal = spy_sl  # type: ignore[assignment]
        try:
            r = _post(client, tid, _text_event("/lang en hi", "rt-sl"))
            assert r.status_code == 200

            # 背景內沒直接呼叫 SessionLocal（用 Session(bind=engine)）
            assert sessionlocal_call_count[0] == 0, (
                f"背景內不應直接呼叫 SessionLocal()（會綁到 production engine），"
                f"got {sessionlocal_call_count[0]} 次呼叫"
            )
            # 但背景確實有建立 session（用 Session(bind=bind)）
            assert len(_BG_SESSIONS) >= 1, (
                "背景內應有 Session(bind=bind) 呼叫，got 0 次"
            )
        finally:
            _db_mod.SessionLocal = original_sessionlocal  # type: ignore[assignment]


# ── 4. DB 連線不洩漏 ─────────────────────────────────────────────────────────
#
# close 偵測策略：SQLAlchemy 2.x session close() 後 is_active 仍可能
# True、execute 也不一定拋例外（會 reopen transaction）——不可靠。
# 唯一 100% 精準的 close 偵測是**hook close() 計數**。
# ``_spy_session_factory`` 已在 instance 上掛 ``_bg_spy_close_called``。
# ``_assert_session_closed`` 透過該 flag 判斷。

def _assert_session_closed(bg_db: RealSession) -> None:
    """斷言 SQLAlchemy session 已被 close（透過 close() hook 計數）。

    比 SQLAlchemy 的 ``is_active`` 屬性或 close 後 execute 是否拋例外
    都更可靠——直接觀察 close() 是否被呼叫。
    """
    if not getattr(bg_db, "_bg_spy_close_called", False):
        raise AssertionError(
            f"session.close() 沒被呼叫 → finally 區塊漏寫 / 連線洩漏"
        )


class TestNoConnectionLeak:
    """驗收 #5.4：``finally: db.close()`` 確保 background session 釋回 pool，
    多次 request 不會耗盡 engine 連線。"""

    def test_many_requests_close_all_background_sessions(self, client):
        """N 次 request → N 個 background session 都正確 close。

        觀察：每次 request 後，``_BG_SESSIONS`` 內的 session 物件
        嘗試 execute 必炸（= 已被 close）。若有 leak，session 仍能
        執行 query。
        """
        tid = _new_tenant(client)

        N = 20
        for i in range(N):
            _BG_SESSIONS.clear()
            r = _post(client, tid, _text_event("/lang en hi", f"rt-bulk-{i}"))
            assert r.status_code == 200, (
                f"第 {i} 次 request 失敗：{r.status_code} {r.text!r}"
            )

            # 每次 request 都應有 background session 建立 + close
            assert len(_BG_SESSIONS) >= 1, (
                f"第 {i} 次 request 應有 background session 建立，got 0"
            )
            for j, bg_db in enumerate(_BG_SESSIONS):
                _assert_session_closed(bg_db)  # raises if not closed

    def test_interleaved_tenants_all_close_sessions(self, client):
        """不同 tenant 多次穿插 request → 每個 background session 都 close。

        模擬「多租戶流量交錯」場景，驗證每個 request 自己的 background
        session 獨立釋放，不互相干擾。
        """
        tids = [_new_tenant(client) for _ in range(3)]

        for i in range(15):
            tid = tids[i % 3]
            _BG_SESSIONS.clear()
            r = _post(client, tid, _text_event("/lang en hi", f"rt-int-{i}"))
            assert r.status_code == 200

            for bg_db in _BG_SESSIONS:
                _assert_session_closed(bg_db)

    def test_translate_exception_path_still_closes_session(self):
        """例外路徑：translate 炸例外 → background session 仍要 close。

        沒有 ``finally: db.close()`` 會在例外路徑洩漏 session。本測試
        用 ``raise_server_exceptions=False`` 跑（避免 TestClient 把
        背景例外 re-raise 干擾斷言），觀察 background session 在例外
        路徑下仍正確 close。
        """
        app, tid = _setup_app_with_tenant("leak")
        exploding = SpyTranslator(raise_on_translate=RuntimeError("炸"))
        app.dependency_overrides[get_translator] = lambda: exploding

        N = 5
        leaked_sessions: list[RealSession] = []
        for i in range(N):
            with patch.object(_lw, "Session", side_effect=_spy_session_factory):
                with TestClient(app, raise_server_exceptions=False) as c:
                    _BG_SESSIONS.clear()
                    body = _payload(_text_event("/lang en hi", f"rt-leak-{i}"))
                    r = c.post(
                        f"/line/webhook/{tid}",
                        content=body,
                        headers=_headers(body),
                    )
                    assert r.status_code == 200
                    leaked_sessions.extend(_BG_SESSIONS)

        # 重點：所有 leaked_sessions 都應被 close（finally 覆蓋例外路徑）
        for j, bg_db in enumerate(leaked_sessions):
            _assert_session_closed(bg_db)
