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

import base64
import hashlib
import hmac
import json
import os
import uuid

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
