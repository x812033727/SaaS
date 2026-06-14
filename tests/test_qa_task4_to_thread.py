"""QA 驗證 — 任務 #4：webhook 阻塞翻譯呼叫經 asyncio.to_thread 包裝。

驗收標準（任務 #4）：
  webhook 翻譯呼叫經 ``asyncio.to_thread`` 包裝且測試仍綠。

本檔以「行為斷言」直接證明落地，而非僅靠既有測試間接通過：
  1. 對 webhook 發一則合法文字訊息時，handler 確實呼叫 asyncio.to_thread，
     且其第一個位置參數正是注入的 translator.translate（不是其他阻塞呼叫）。
  2. 經 to_thread 包裝後，端到端翻譯結果仍正確（StubTranslator → [LANG] text）。
  3. 反向樣本：非文字事件不會觸發 to_thread 翻譯呼叫。

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
from unittest import mock

import pytest
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
import saas_mvp.routers.line_webhook as webhook_mod        # noqa: E402

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


def _image_event() -> dict:
    return {"type": "message", "replyToken": "rt-img", "message": {"type": "image"}}


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


class TestToThreadWrapping:
    def test_translate_called_via_to_thread(self, client, tenant):
        """核心驗收：翻譯呼叫確實透過 asyncio.to_thread，且首參為 translator.translate。"""
        tid = tenant
        body = _payload(_text_event("hello", reply_token="rt-spy"))

        real_to_thread = webhook_mod.asyncio.to_thread
        captured = {}

        async def spy_to_thread(func, *args, **kwargs):
            captured["func"] = func
            captured["args"] = args
            return await real_to_thread(func, *args, **kwargs)

        with mock.patch.object(
            webhook_mod.asyncio, "to_thread", side_effect=spy_to_thread
        ) as spy:
            r = client.post(f"/line/webhook/{tid}", content=body, headers=_headers(body))

        assert r.status_code == 200
        # to_thread 必須被呼叫
        assert spy.called, "翻譯呼叫未經 asyncio.to_thread 包裝"
        # 首參必須是注入的 translator.translate（綁定方法）
        assert captured["func"] == _stub_translator.translate, (
            f"to_thread 包裝的不是 translator.translate，而是 {captured.get('func')!r}"
        )
        # 翻譯參數正確傳遞（text, target_lang）
        assert captured["args"] == ("hello", "zh-TW")

    def test_result_correct_through_to_thread(self, client, tenant):
        """經 to_thread 包裝後端到端結果仍正確。"""
        tid = tenant
        body = _payload(_text_event("world", reply_token="rt-e2e"))
        r = client.post(f"/line/webhook/{tid}", content=body, headers=_headers(body))
        assert r.status_code == 200
        assert _fake_line_client.call_count == 1
        assert _fake_line_client.last_text == "[ZH-TW] world"

    def test_non_text_event_does_not_call_to_thread(self, client, tenant):
        """反向樣本：非文字事件不觸發翻譯，to_thread 不應被呼叫。"""
        tid = tenant
        body = _payload(_image_event())
        with mock.patch.object(
            webhook_mod.asyncio, "to_thread", wraps=webhook_mod.asyncio.to_thread
        ) as spy:
            r = client.post(f"/line/webhook/{tid}", content=body, headers=_headers(body))
        assert r.status_code == 200
        assert _fake_line_client.call_count == 0
        assert not spy.called, "非文字事件不應觸發 to_thread 翻譯呼叫"


class TestToThreadDoesNotBlockEventLoop:
    """阻塞翻譯被移到 thread pool，不卡 event loop。

    用一個會「等待 main thread 設定 flag」的 translator：若翻譯在 event loop
    內同步執行（未經 to_thread），main thread 無機會設 flag → 永遠死等。
    能完成即證明翻譯跑在獨立 thread。
    """

    def test_blocking_translate_runs_off_event_loop(self, client, tenant):
        import threading

        released = threading.Event()
        ran_in_other_thread = {}

        class _GatedTranslator(StubTranslator):
            def translate(self, text: str, target_lang: str) -> str:
                # 記錄執行緒，並等待主執行緒釋放（驗證確實在另一條 thread）
                ran_in_other_thread["tid"] = threading.get_ident()
                released.wait(timeout=5)
                return f"[{target_lang.upper()}] {text}"

        tid = tenant
        app = client.app
        app.dependency_overrides[get_translator] = lambda: _GatedTranslator()
        main_tid = threading.get_ident()
        try:
            # 在背景送請求；主執行緒稍後釋放 gate
            import concurrent.futures

            def _do_post():
                body = _payload(_text_event("gate", reply_token="rt-gate", uid="Ugate001"))
                return client.post(f"/line/webhook/{tid}", content=body, headers=_headers(body))

            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                fut = ex.submit(_do_post)
                # 釋放 gate，讓翻譯完成
                released.set()
                r = fut.result(timeout=10)
        finally:
            app.dependency_overrides[get_translator] = lambda: _stub_translator

        assert r.status_code == 200
        # 翻譯執行緒不等於主測試執行緒（證明跑在 thread pool）
        assert ran_in_other_thread["tid"] != main_tid
