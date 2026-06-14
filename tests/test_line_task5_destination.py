"""Task #5 驗收測試 — Webhook destination 二次驗證

對應整體計畫 #3 的行為邊界（驗簽後的 destination/channel 對應驗證）：

驗收標準
--------
3. cfg.line_bot_user_id 已設且 payload.destination 不符 → 400，且 detail 與
   簽章失敗「完全一致」（共用 _INVALID_SIGNATURE_DETAIL，不洩漏租戶存在性）。
4. cfg.line_bot_user_id 為 None（舊 config）→ 跳過二次驗證，destination 任意值仍 200。
5. 二次驗證在 HMAC 驗簽「之後」執行——簽章錯的請求不會走到 destination 比對。

補充（正向）：line_bot_user_id 已設且 destination 相符 → 正常翻譯回覆、200。

全部離線：StubTranslator + FakeLineReplyClient，不需真實 LINE/翻譯金鑰。
本檔為獨立測試檔，不改既有測試。
"""

from __future__ import annotations

import base64
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
os.environ.setdefault(
    "SAAS_LINE_CHANNEL_ENCRYPT_KEY",
    "ZGV2LWxpbmUtc2VjcmV0LWtleS0zMmJ5dGVzLWxvbmc=",
)

# 載入所有 model metadata
from saas_mvp.models import tenant as _t, user as _u, note as _n, usage as _us  # noqa: F401
from saas_mvp.models import api_key as _ak, api_key_usage as _aku               # noqa: F401
from saas_mvp.models import plan_change_history as _pch                          # noqa: F401
import saas_mvp.models.line_channel_config as _lcm                               # noqa: F401
import saas_mvp.models.line_user_lang as _lul                                     # noqa: F401

from saas_mvp.app import create_app
from saas_mvp.db import Base, get_db
from saas_mvp.line_client import FakeLineReplyClient, get_line_client
from saas_mvp.models.line_channel_config import LineChannelConfig
from saas_mvp.translation import StubTranslator, get_translator

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
_BOT_USER_ID = "U" + "b" * 32          # 此租戶 bot 的 userId
_OTHER_BOT_USER_ID = "U" + "c" * 32    # 另一個 bot（模擬錯配來源）


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


# ── helpers ───────────────────────────────────────────────────────────────────


def _sign(body: bytes, secret: str = _CHANNEL_SECRET) -> str:
    mac = hmac.new(secret.encode("utf-8"), body, hashlib.sha256)
    return base64.b64encode(mac.digest()).decode("utf-8")


def _payload(destination: str | None, text: str = "hello") -> bytes:
    """組裝含 destination 的 webhook payload。destination=None 時不放此欄位。"""
    body: dict = {
        "events": [
            {
                "type": "message",
                "replyToken": "rt-dest",
                "source": {"type": "user", "userId": "Udesttest"},
                "message": {"type": "text", "text": text},
            }
        ]
    }
    if destination is not None:
        body["destination"] = destination
    return json.dumps(body).encode("utf-8")


def _headers(body: bytes) -> dict:
    return {"X-Line-Signature": _sign(body)}


def _register_with_config(client: TestClient) -> int:
    """註冊租戶並建立 LINE config，回傳 tenant_id（line_bot_user_id 尚未設定）。"""
    email = f"dest_{uuid.uuid4().hex[:8]}@example.com"
    tn = f"dest_tenant_{uuid.uuid4().hex[:8]}"
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


def _set_bot_user_id(tid: int, user_id: str | None) -> None:
    """直接在 DB 設定 line_bot_user_id（模擬 bot/info 已回填）。"""
    db = _Session()
    try:
        cfg = (
            db.query(LineChannelConfig)
            .filter(LineChannelConfig.tenant_id == tid)
            .one()
        )
        cfg.line_bot_user_id = user_id
        db.commit()
    finally:
        db.close()


# ── fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def tid_with_bot_id(client):
    """已設定 line_bot_user_id 的租戶。"""
    tid = _register_with_config(client)
    _set_bot_user_id(tid, _BOT_USER_ID)
    return tid


@pytest.fixture(scope="module")
def tid_legacy(client):
    """line_bot_user_id 為 None 的舊 config 租戶。"""
    return _register_with_config(client)


# ── 測試：destination 二次驗證 ────────────────────────────────────────────────


class TestDestinationMatched:
    def test_destination_match_translates_200(self, client, tid_with_bot_id):
        """line_bot_user_id 已設且 destination 相符 → 正常翻譯回覆、200。"""
        body = _payload(_BOT_USER_ID, text="hello")
        r = client.post(f"/line/webhook/{tid_with_bot_id}", content=body, headers=_headers(body))
        assert r.status_code == 200
        assert _fake_line_client.call_count == 1
        assert _fake_line_client.last_text == "[ZH-TW] hello"


class TestDestinationMismatch:
    def test_destination_mismatch_returns_400(self, client, tid_with_bot_id):
        """line_bot_user_id 已設、destination 不符（LINE Console 錯配）→ 400，不翻譯。"""
        body = _payload(_OTHER_BOT_USER_ID, text="wrong dest")
        r = client.post(f"/line/webhook/{tid_with_bot_id}", content=body, headers=_headers(body))
        assert r.status_code == 400
        assert _fake_line_client.call_count == 0

    def test_destination_missing_returns_400_when_bot_id_set(self, client, tid_with_bot_id):
        """line_bot_user_id 已設但 payload 完全沒有 destination → 視為不符 → 400。"""
        body = _payload(None, text="no dest")
        r = client.post(f"/line/webhook/{tid_with_bot_id}", content=body, headers=_headers(body))
        assert r.status_code == 400
        assert _fake_line_client.call_count == 0

    def test_mismatch_detail_identical_to_signature_failure(self, client, tid_with_bot_id):
        """列舉防護核心斷言：destination 不符的 detail 與簽章失敗『完全一致』。

        若兩者 detail 不同，攻擊者即可藉回應區分「已設定 bot id 但 destination 錯」
        與「簽章錯」，形成 oracle。共用 _INVALID_SIGNATURE_DETAIL 鎖死此旁路。
        """
        # 探針 A：destination 不符（簽章正確）
        body_dest = _payload(_OTHER_BOT_USER_ID, text="x")
        r_dest = client.post(
            f"/line/webhook/{tid_with_bot_id}", content=body_dest, headers=_headers(body_dest)
        )

        # 探針 B：簽章錯誤
        body_sig = _payload(_BOT_USER_ID, text="x")
        r_sig = client.post(
            f"/line/webhook/{tid_with_bot_id}",
            content=body_sig,
            headers={"X-Line-Signature": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="},
        )

        assert r_dest.status_code == r_sig.status_code == 400
        assert r_dest.json()["detail"] == r_sig.json()["detail"], "destination 不符可被回應區分，存在列舉旁路"


class TestDestinationLegacyConfig:
    def test_none_bot_id_skips_check_200(self, client, tid_legacy):
        """舊 config（line_bot_user_id=None）→ 略過二次驗證，destination 任意值仍 200。"""
        body = _payload(_OTHER_BOT_USER_ID, text="legacy")
        r = client.post(f"/line/webhook/{tid_legacy}", content=body, headers=_headers(body))
        assert r.status_code == 200
        assert _fake_line_client.last_text == "[ZH-TW] legacy"

    def test_none_bot_id_no_destination_field_200(self, client, tid_legacy):
        """舊 config + payload 無 destination 欄位 → 行為與現況一致，200。"""
        body = _payload(None, text="legacy nodest")
        r = client.post(f"/line/webhook/{tid_legacy}", content=body, headers=_headers(body))
        assert r.status_code == 200
        assert _fake_line_client.last_text == "[ZH-TW] legacy nodest"


class TestDestinationCheckAfterSignature:
    def test_bad_signature_rejected_before_destination_check(self, client, tid_with_bot_id):
        """信任順序：簽章錯的請求（即使 destination 相符）→ 400，不因 destination 正確放行。

        驗證二次驗證在 HMAC 之後——攻擊者無法用正確 destination 繞過簽章。
        """
        body = _payload(_BOT_USER_ID, text="x")  # destination 正確
        r = client.post(
            f"/line/webhook/{tid_with_bot_id}",
            content=body,
            headers={"X-Line-Signature": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="},
        )
        assert r.status_code == 400
        assert _fake_line_client.call_count == 0
