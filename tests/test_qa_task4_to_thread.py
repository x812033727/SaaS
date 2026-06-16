"""QA 驗證 — webhook 背景化後的架構契約。

架構契約
--------
handler 在「驗章 / 解密 / JSON parse / destination 二次驗證」全部通過
後，把「處理 events 鏈」丟進 FastAPI ``BackgroundTasks``，自身立即回
``{"status": "ok"}`` 200，**不再同步**執行 translate / reply /
increment_usage。``BackgroundTasks`` 對 sync 函式自動以
``run_in_threadpool`` 執行（Starlette 源碼事實），等同 ``asyncio.to_thread``
效果——``line_client.reply`` 內的 ``urllib.request.urlopen`` 阻塞 I/O
不卡 event loop。

本檔以「契約面 spy」守住「handler 必丟 ``_process_events`` 到
``BackgroundTasks``、不 inline 執行 reply」這條不變量。

  * monkey-patch ``fastapi.BackgroundTasks.add_task``，斷言被以
    ``_process_events`` 與正確 events 呼叫；同時斷言
    ``response.status_code == 200``。
  * 為什麼不用 wall time：Starlette ``TestClient`` 同步模式下
    ``client.post()`` 會 block 等背景任務完成，wall time 必含 reply
    阻塞，任何門檻在 TestClient 下物理上不可能過。契約面 spy 不依賴
    threadpool dispatch 時間、不需 sleep、測試瞬間完成。

黑白對照組（保證測試判別力）：
  * positive（文字事件）：dispatch + reply call_count == 1 + 譯文正確
  * reverse（非 message 事件，如 follow）：dispatch 但 call_count == 0
    （內層 ``event_type != "message"`` guard 生效）

  兩者配對可抓出兩類 regression：非文字事件誤觸 reply、或文字事件漏 dispatch。

BackgroundTasks 結構契約（不依賴 wall time）：
  * threadpool ident：``_process_events`` 跑在與 handler request thread
    不同的 thread（用 ``_EventGateReplyClient`` 卡 reply、從 gate 內抓 ident）
  * schedule list：handler return 後 ``bg.tasks`` 含 ``_process_events`` callable
    （用 ``_invoke_handler_directly`` 繞過 TestClient 直接 inspect）
  * exception isolation：背景 reply 拋例外 → handler 仍回 200、例外不外拋
    （守住 Starlette「response 已送出則 background 不 re-raise」契約）

回歸涵蓋：
  * 既有 ``test_line_task1_quota_billing.py`` 與所有 line webhook 相關
    測試皆綠，端到端語意由它們守（reply 結果、quota 計費、redelivery
    去重、雙閘配額…）。
  * 本檔只補一個「handler 必丟背景」契約——是 line_webhook.py docstring
    「背景任務語意」段的測試落地，不是既有測試的替代。

全程離線：StubTranslator + FakeLineReplyClient，不呼叫真實 DeepL / LINE。
獨立檔，不修改任何既有測試。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import threading
import uuid
from types import SimpleNamespace

import pytest
from fastapi import BackgroundTasks
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("SAAS_RATE_LIMIT_ENABLED", "false")

# 載入所有 model metadata（與既有 webhook 測試一致）
from saas_mvp.models import tenant as _t, user as _u, note as _n, usage as _us  # noqa: F401,E402
from saas_mvp.models import api_key as _ak, api_key_usage as _aku               # noqa: F401,E402
from saas_mvp.models import plan_change_history as _pch                          # noqa: F401,E402
import saas_mvp.models.line_channel_config as _lcm                               # noqa: F401,E402
import saas_mvp.models.line_user_lang as _lul                                    # noqa: F401,E402

from saas_mvp.app import create_app                       # noqa: E402
from saas_mvp.db import Base, get_db                       # noqa: E402
from saas_mvp.line_client import FakeLineReplyClient, get_line_client  # noqa: E402
from saas_mvp.translation import StubTranslator, get_translator        # noqa: E402

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


@pytest.fixture(autouse=True)
def reset_fake_client():
    _fake_line_client.reset()
    yield


def _sign(body: bytes, secret: str = _CHANNEL_SECRET) -> str:
    mac = hmac.new(secret.encode("utf-8"), body, hashlib.sha256)
    return base64.b64encode(mac.digest()).decode("utf-8")


def _headers(body: bytes) -> dict:
    return {"X-Line-Signature": _sign(body)}


def _text_event(text: str, reply_token: str = "rt-qa4", uid: str = "Uqa4001") -> dict:
    return {
        "type": "message",
        "replyToken": reply_token,
        "source": {"type": "user", "userId": uid},
        "message": {"type": "text", "text": text},
    }


def _follow_event(uid: str = "Uct002") -> dict:
    """非 message 事件（follow）— 觸發 handler 丟背景，但 ``_process_events``
    內層 guard ``event_type != "message"`` 直接 continue，**不**觸發 reply。

    用作反向樣本：證明 BackgroundTasks dispatch ≠ 一定 reply，測試有真判別力。
    """
    return {
        "type": "follow",
        "replyToken": "rt-follow",
        "source": {"type": "user", "userId": uid},
    }


# ── 架構不變量測試輔助（gate-based） ─────────────────────────────────────────

class _EventGateReplyClient(FakeLineReplyClient):
    """Gate-based fake reply client——reply 內抓 ``threading.get_ident()``、
    signal ``called_event``、block 在 ``gate.wait()`` 等測試端釋放。

    為何用 ``threading.Event`` 而非 ``time.sleep``：
      * ``time.sleep`` 浪費 CI 時間
      * ``Event.wait()`` 提供「測試主動控制時序」能力：背景執行到 reply
        時 block、測試端可任意時刻讀取 ``self.ident`` 與釋放 gate
      * 達成 senior 方案 A：抓 background 真實 thread ident，證明
        ``_process_events`` 跑在與 handler request thread 不同的 thread

    Attributes:
        ident: ``reply()`` 被呼叫時的 thread ident（背景 threadpool）
        called_event: reply 進入後 set，測試端用來同步「已進入 reply」信號
    """

    def __init__(self, gate: threading.Event) -> None:
        super().__init__()
        self.gate = gate
        self.ident: int | None = None
        self.called_event = threading.Event()

    def reply(self, reply_token: str, text: str, *, access_token: str) -> None:
        self.ident = threading.get_ident()
        self.called_event.set()
        self.gate.wait()  # 測試端釋放 gate 才放行
        return super().reply(reply_token, text, access_token=access_token)


class _ExplodingReplyClient(FakeLineReplyClient):
    """reply 一律拋例外——驗 handler 對背景例外的隔離契約。"""

    def reply(self, reply_token: str, text: str, *, access_token: str) -> None:
        raise RuntimeError("LINE API down (test injection)")


async def _invoke_handler_directly(app, tid: int, body: bytes, bg: BackgroundTasks):
    """繞過 TestClient 直接呼叫 ``line_webhook``，傳入 explicit BackgroundTasks。

    為何需要：TestClient 的 sync mode 內部會 await background 跑完才 return，
    無法在「handler 已 return 但 background 尚未跑」的時點 inspect ``bg.tasks``。
    直呼 handler 可在 handler return 後、background 跑之前檢視排程清單。
    """
    from saas_mvp.routers.line_webhook import line_webhook

    fake_request = SimpleNamespace()
    async def get_body():
        return body
    fake_request.body = get_body
    fake_request.headers = {"X-Line-Signature": _sign(body)}

    # 從 app 的 dependency override 拿 db session（沿用既有測試配置）
    db_gen = app.dependency_overrides[get_db]()
    db = next(db_gen)
    try:
        response = await line_webhook(
            tenant_id=tid,
            request=fake_request,
            background_tasks=bg,
            db=db,
            translator=_stub_translator,
            line_client=_fake_line_client,
        )
    finally:
        try:
            next(db_gen)
        except StopIteration:
            pass

    return response


def _payload(*events) -> bytes:
    return json.dumps({"events": list(events)}).encode("utf-8")


@pytest.fixture(scope="module")
def tenant(client):
    email = f"qa4_{uuid.uuid4().hex[:8]}@example.com"
    tn = f"qa4_tenant_{uuid.uuid4().hex[:8]}"
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


class TestBackgroundDispatchContract:
    """架構不變量 — handler 必丟 ``_process_events`` 到 ``BackgroundTasks``。

    守的是「handler 不 inline 跑 reply、reply 與 handler 200 解耦」
    這條架構契約，而非「某 function 被呼叫」的實作細節。

    為何不用 wall time：Starlette ``TestClient`` 同步模式下
    ``client.post()`` 會 block 等背景任務完成，wall time 必包含 reply
    阻塞 I/O 耗時，任何門檻在 TestClient 下物理上不可能綠（見模組
    docstring「背景任務語意」段）。本測試走契約面 spy——比 wall time
    強、不依賴 threadpool dispatch 時間、不需 sleep、測試瞬間完成。
    """

    def test_handler_dispatches_process_events_to_background(
        self, client, tenant, monkeypatch
    ):
        """契約面：handler 把 ``_process_events`` 丟進 ``BackgroundTasks.add_task``；
        response 仍回 200（背景已跑完）。"""
        captured: list[dict] = []
        real_add_task = BackgroundTasks.add_task

        def spy_add_task(self, func, *args, **kwargs):
            captured.append({"name": func.__name__, "args": args, "kwargs": kwargs})
            return real_add_task(self, func, *args, **kwargs)

        monkeypatch.setattr(BackgroundTasks, "add_task", spy_add_task)

        tid = tenant
        body = _payload(_text_event("contract", reply_token="rt-ct", uid="Uct001"))
        r = client.post(f"/line/webhook/{tid}", content=body, headers=_headers(body))

        assert r.status_code == 200
        assert any(c["name"] == "_process_events" for c in captured), (
            "handler 必須把 _process_events 丟到 BackgroundTasks，"
            f"實際 add_task 呼叫 = {captured!r}"
        )
        # 端到端仍正確：背景跑完、reply 收到翻譯結果
        assert _fake_line_client.call_count == 1
        assert _fake_line_client.last_text == "[ZH-TW] contract"

    def test_non_message_event_dispatches_but_does_not_reply(
        self, client, tenant, monkeypatch
    ):
        """反向樣本：非 message 事件（follow）觸發 dispatch 但**不**觸發 reply。

        與 positive test 配對證明測試有真判別力：
          * handler 一律丟 _process_events 到 BackgroundTasks（dispatch 與 event
            類型無關，這是契約本體）
          * _process_events 內層 guard ``event_type != "message"`` 直接 continue，
            略過非文字事件，**不**觸發 translate / reply / increment

        若 regression 讓非文字事件誤觸 reply，``call_count == 1`` 會抓出；
        若 regression 讓 handler 對非文字事件不 dispatch，
        ``add_task 呼叫清單為空`` 會抓出。
        """
        captured: list[dict] = []
        real_add_task = BackgroundTasks.add_task

        def spy_add_task(self, func, *args, **kwargs):
            captured.append({"name": func.__name__, "args": args, "kwargs": kwargs})
            return real_add_task(self, func, *args, **kwargs)

        monkeypatch.setattr(BackgroundTasks, "add_task", spy_add_task)

        tid = tenant
        body = _payload(_follow_event(uid="UctFollow"))
        r = client.post(f"/line/webhook/{tid}", content=body, headers=_headers(body))

        assert r.status_code == 200
        # handler 仍丟 _process_events（dispatch 對所有合法事件無條件）
        assert any(c["name"] == "_process_events" for c in captured), (
            "handler 對非 message 事件也必須 dispatch _process_events（內層 guard 才會跳過）"
            f"實際 add_task 呼叫 = {captured!r}"
        )
        # 內層 guard 生效：reply **未**被呼叫
        assert _fake_line_client.call_count == 0, (
            f"非 message 事件不應觸發 reply，實際 call_count = "
            f"{_fake_line_client.call_count}，last_text = {_fake_line_client.last_text!r}"
        )


# ── 架構不變量：BackgroundTasks 結構契約（3 條） ──────────────────────────────

class TestBackgroundTaskContract:
    """BackgroundTasks 的三條結構契約——守「BackgroundTasks 不阻塞 handler」。

    與上一類 ``TestBackgroundDispatchContract`` 互補：
      * 上一類用 spy 證明「handler 確實呼叫 BackgroundTasks.add_task」
        （行為契約，從外部觀察）
      * 本類用 gate / 直呼 handler / 例外注入 證明「background 真的在
        與 handler 不同的 thread 跑 + 排程清單含 _process_events + 背景
        例外不污染 response」（結構契約，從內部觀察）

    為何不用 wall time：Starlette ``TestClient`` 同步模式 ``client.post``
    內部 await ``self.background()`` 才回傳 response，wall time 物理上必
    含 reply 阻塞 I/O 耗時；本類用「thread ident」「bg.tasks 結構」「例外
    隔離」三條**不依賴時序**的契約避開這個坑。
    """

    def test_process_events_runs_off_handler_thread(self, client, tenant):
        """契約 1（threadpool）：``_process_events`` 跑在與 handler request
        thread 不同的 thread——證明 BackgroundTasks 真實用 threadpool dispatch。

        機制：用 ``_EventGateReplyClient`` 卡住 reply、從 gate 內抓
        ``threading.get_ident()``（此時執行緒必在 background threadpool，
        因 reply 由 ``_process_events`` 觸發）。測試端用 ``client.post`` 跑在
        另一條 thread（避免 self-block），主 thread 同步等待後斷言 ident 不同。
        """
        gate = threading.Event()
        gated = _EventGateReplyClient(gate=gate)

        app = client.app
        prev_override = app.dependency_overrides.get(get_line_client)
        app.dependency_overrides[get_line_client] = lambda: gated

        main_tid = threading.get_ident()
        post_result: dict = {}
        post_error: dict = {}

        def post_in_thread():
            try:
                tid = tenant
                body = _payload(_text_event(
                    "gate", reply_token="rt-gate", uid="Ugate"
                ))
                r = client.post(
                    f"/line/webhook/{tid}",
                    content=body, headers=_headers(body),
                )
                post_result["status"] = r.status_code
                post_result["body"] = r.json()
            except Exception as e:
                post_error["exc"] = e

        try:
            worker = threading.Thread(target=post_in_thread)
            worker.start()

            # 等 reply 進入 background threadpool（被 gate 卡住）
            assert gated.called_event.wait(timeout=5.0), (
                "reply 從未被呼叫——background 沒跑或 reply 路徑被改"
            )

            # 此時 ``gated.ident`` 為 background thread 的 ident
            assert gated.ident is not None
            assert gated.ident != main_tid, (
                f"_process_events 應跑在 background threadpool；"
                f"main_tid={main_tid} gated.ident={gated.ident}（相同 = inline 執行）"
            )

            # 釋放 gate、收尾 post thread
            gate.set()
            worker.join(timeout=5.0)
            assert not post_error, f"client.post 內部拋例外: {post_error!r}"
            assert post_result["status"] == 200
        finally:
            if prev_override is not None:
                app.dependency_overrides[get_line_client] = prev_override
            else:
                app.dependency_overrides.pop(get_line_client, None)

    def test_handler_schedules_process_events_as_background_task(
        self, client, tenant
    ):
        """契約 2（schedule）：handler 排入 ``BackgroundTasks.tasks`` 的是
        ``_process_events`` callable——直接 inspect 結構，不依賴時序。

        機制：繞過 TestClient、用 ``asyncio.run`` 直呼 ``line_webhook`` 並
        傳入 explicit ``BackgroundTasks()`` instance。handler return 後、
        background 跑之前，inspect ``bg.tasks`` 拿排程清單。
        """
        tid = tenant
        body = _payload(_text_event(
            "inspect", reply_token="rt-inspect", uid="Uinspect"
        ))
        bg = BackgroundTasks()

        response = asyncio.run(_invoke_handler_directly(
            client.app, tid, body, bg
        ))

        assert response == {"status": "ok"}
        # 直接 inspect 排程清單
        task_funcs = [getattr(t.func, "__name__", repr(t.func)) for t in bg.tasks]
        assert "_process_events" in task_funcs, (
            f"BackgroundTasks 應排入 _process_events callable，got {task_funcs!r}"
        )

    def test_handler_returns_200_when_background_raises(
        self, client, tenant
    ):
        """契約 3（exception isolation）：背景 reply 拋例外 → handler 已送出
        200、例外不外拋、client.post 不爆。

        守住 Starlette「response 已送出則 background 例外不 re-raise」契約——
        防止 LINE 看到 5xx 觸發 redelivery storm。``_process_events`` 內部
        ``try/except Exception`` 也兜底防 log 之外的二次外拋。
        """
        exploding = _ExplodingReplyClient()
        app = client.app
        prev_override = app.dependency_overrides.get(get_line_client)
        app.dependency_overrides[get_line_client] = lambda: exploding

        try:
            tid = tenant
            body = _payload(_text_event(
                "boom", reply_token="rt-boom", uid="Uboom"
            ))
            # TestClient fixture 已設 raise_server_exceptions=True
            # 若 background 例外外拋、會變 500
            r = client.post(
                f"/line/webhook/{tid}",
                content=body, headers=_headers(body),
            )
            assert r.status_code == 200, (
                f"背景 reply 拋例外時 handler 仍應 200，got {r.status_code} "
                f"body={r.text!r}"
            )
            assert r.json() == {"status": "ok"}
        finally:
            if prev_override is not None:
                app.dependency_overrides[get_line_client] = prev_override
            else:
                app.dependency_overrides.pop(get_line_client, None)
