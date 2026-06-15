"""Task #5/#6 補強測試 — 等量時間驗簽 helper、log 區分、四路徑逐字節等價。

對應任務 #4 驗收標準
--------------------
* 缺-header 與 cfg-None 與「簽章錯」三條路徑皆走同一驗簽 helper
  `_constant_time_verify`（不再 short-circuit 跳過 HMAC），呼叫次數與引數對稱。
* 伺服器端 log 對每條拒絕路徑記錄可區分的 `reason=...`，對外 detail 仍為單一字串。
* 既有 detail 唯一性斷言保留並擴及缺-header 路徑——跨四條拒絕路徑
  （cfg 不存在、缺 header、簽章錯、destination 不符）的 status code + body
  逐字節完全相等。
* 每個拒絕案例都配一個正向 200 對照組（同租戶合法簽章 → 200），
  避免測試本身假綠（若 helper 永遠 True 既能被 200 對照抓到）。

全部離線：StubTranslator + FakeLineReplyClient，不需真實 LINE/翻譯金鑰。
本檔為獨立測試檔，沿用既有 InMemorySQLite + 模組級 fixture 隔離風格。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import uuid
from unittest import mock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("SAAS_RATE_LIMIT_ENABLED", "false")

from saas_mvp.models import tenant as _t, user as _u, note as _n, usage as _us  # noqa: F401
from saas_mvp.models import api_key as _ak, api_key_usage as _aku               # noqa: F401
from saas_mvp.models import plan_change_history as _pch                          # noqa: F401
import saas_mvp.models.line_channel_config as _lcm                               # noqa: F401
import saas_mvp.models.line_user_lang as _lul                                     # noqa: F401

from saas_mvp.app import create_app
from saas_mvp.db import Base, get_db
from saas_mvp.line_client import FakeLineReplyClient, get_line_client
from saas_mvp.models.line_channel_config import LineChannelConfig
from saas_mvp.routers import line_webhook
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
_BOT_USER_ID = "U" + "b" * 32
_OTHER_BOT_USER_ID = "U" + "c" * 32
BAD_SIG = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="


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


def _payload_with_destination(destination: str | None, text: str = "hello") -> bytes:
    body: dict = {
        "destination": destination,
        "events": [
            {
                "type": "message",
                "replyToken": "rt-tl",
                "source": {"type": "user", "userId": "Utluser001"},
                "message": {"type": "text", "text": text},
            }
        ],
    }
    return json.dumps(body).encode("utf-8")


def _payload_simple(text: str = "hello") -> bytes:
    body = {
        "events": [
            {
                "type": "message",
                "replyToken": "rt-tl",
                "source": {"type": "user", "userId": "Utluser001"},
                "message": {"type": "text", "text": text},
            }
        ],
    }
    return json.dumps(body).encode("utf-8")


def _headers(body: bytes) -> dict:
    return {"X-Line-Signature": _sign(body)}


def _register(client: TestClient) -> int:
    email = f"tl_{uuid.uuid4().hex[:8]}@example.com"
    tn = f"tl_tenant_{uuid.uuid4().hex[:8]}"
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


@pytest.fixture(scope="module")
def tid_configured(client):
    return _register(client)


@pytest.fixture(scope="module")
def tid_with_bot_id(client):
    tid = _register(client)
    _set_bot_user_id(tid, _BOT_USER_ID)
    return tid


@pytest.fixture(scope="module")
def unconfigured_tid(client):
    email = f"tl_nc_{uuid.uuid4().hex[:8]}@example.com"
    tn = f"tl_nc_tenant_{uuid.uuid4().hex[:8]}"
    r = client.post("/auth/register", json={
        "email": email, "password": "Test1234!", "tenant_name": tn,
    })
    assert r.status_code == 201
    token = r.json()["access_token"]
    me = client.get("/tenants/me", headers={"Authorization": f"Bearer {token}"})
    return me.json()["id"]


# ── Spy helper ───────────────────────────────────────────────────────────────


class _HelperSpy:
    """包裝 _constant_time_verify，記錄呼叫歷史但仍執行真實 HMAC 計算。

    用 side_effect 包裝而非 wraps，確保 call_args 乾淨抓到我們包裝層的 args，
    同時保留原函式的真實行為（讓後續 status code / detail 契約仍由真實邏輯產生）。
    """

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self._real = line_webhook._constant_time_verify

    def __call__(self, body: bytes, signature: str, secret: bytes) -> bool:
        self.calls.append({
            "body_len": len(body),
            "signature": signature,
            "secret_is_dummy": secret == line_webhook._DUMMY_SECRET,
        })
        return self._real(body, signature, secret)


@pytest.fixture
def helper_spy():
    spy = _HelperSpy()
    patcher = mock.patch(
        "saas_mvp.routers.line_webhook._constant_time_verify",
        side_effect=spy,
    )
    patcher.start()
    yield spy
    patcher.stop()


# ═════════════════════════════════════════════════════════════════════════════
# Test 1: 四路徑逐字節等價 + detail 集合唯一性
# ═════════════════════════════════════════════════════════════════════════════


class TestAllRejectionPathsBytewiseIdentical:
    """四條拒絕路徑對外回應逐字節完全相等——status code 1 種、detail 1 種、Content-Length 1 種。"""

    def test_status_code_uniform(
        self, client, tid_configured, unconfigured_tid, tid_with_bot_id
    ):
        body_simple = _payload_simple("x")
        body_with_dest = _payload_with_destination(_OTHER_BOT_USER_ID, "x")
        body_match_dest = _payload_with_destination(_BOT_USER_ID, "x")

        cases = [
            ("cfg_none+bad_sig",  f"/line/webhook/{unconfigured_tid}",  body_simple,       {"X-Line-Signature": BAD_SIG}),
            ("cfg_none+no_sig",   f"/line/webhook/{unconfigured_tid}",  body_simple,       {}),
            ("cfg_ok+no_sig",     f"/line/webhook/{tid_configured}",    body_simple,       {}),
            ("cfg_ok+bad_sig",    f"/line/webhook/{tid_configured}",    body_simple,       {"X-Line-Signature": BAD_SIG}),
            ("dest_mismatch",     f"/line/webhook/{tid_with_bot_id}",   body_with_dest,    _headers(body_with_dest)),
            ("dest_mismatch_no_sig", f"/line/webhook/{tid_with_bot_id}", body_match_dest,  {}),
        ]

        codes = set()
        for name, url, body, headers in cases:
            r = client.post(url, content=body, headers=headers)
            codes.add(r.status_code)
            assert r.status_code == 400, f"{name} 應回 400，實得 {r.status_code}"
        assert len(codes) == 1, f"拒絕路徑 status_code 不唯一：{codes}"

    def test_detail_bytewise_identical(
        self, client, tid_configured, unconfigured_tid, tid_with_bot_id
    ):
        body_simple = _payload_simple("x")
        body_with_dest = _payload_with_destination(_OTHER_BOT_USER_ID, "x")

        cases = [
            ("cfg_none+bad_sig",  f"/line/webhook/{unconfigured_tid}",  body_simple,       {"X-Line-Signature": BAD_SIG}),
            ("cfg_none+no_sig",   f"/line/webhook/{unconfigured_tid}",  body_simple,       {}),
            ("cfg_ok+no_sig",     f"/line/webhook/{tid_configured}",    body_simple,       {}),
            ("cfg_ok+bad_sig",    f"/line/webhook/{tid_configured}",    body_simple,       {"X-Line-Signature": BAD_SIG}),
            ("dest_mismatch",     f"/line/webhook/{tid_with_bot_id}",   body_with_dest,    _headers(body_with_dest)),
        ]

        details: set[str] = set()
        bodies: set[bytes] = set()
        for name, url, body, headers in cases:
            r = client.post(url, content=body, headers=headers)
            details.add(r.json()["detail"])
            bodies.add(r.content)

        # 單一字串
        assert len(details) == 1, f"detail 不唯一，存在列舉旁路：{details}"
        # 整個 body bytes 也唯一
        assert len(bodies) == 1, f"response body bytes 不唯一：{bodies!r}"
        # 字串內容必須是共用 detail
        assert details == {"Invalid X-Line-Signature"}


# ═════════════════════════════════════════════════════════════════════════════
# Test 2: 三條 HMAC 路徑走同一 helper（mock spy 斷言）
# ═════════════════════════════════════════════════════════════════════════════


class TestConstantTimeHelperCalled:
    """驗證 cfg-None / 缺-header / 簽章錯 三條路徑皆走同一 helper。"""

    def test_cfg_none_with_bad_header_calls_helper(
        self, client, unconfigured_tid, helper_spy
    ):
        body = _payload_simple("hello")
        r = client.post(
            f"/line/webhook/{unconfigured_tid}",
            content=body,
            headers={"X-Line-Signature": BAD_SIG},
        )
        assert r.status_code == 400
        assert len(helper_spy.calls) == 1
        call = helper_spy.calls[0]
        assert call["secret_is_dummy"] is True
        assert call["body_len"] == len(body)
        assert call["signature"] == BAD_SIG

    def test_cfg_none_without_header_calls_helper(
        self, client, unconfigured_tid, helper_spy
    ):
        """缺 header 不可 short-circuit，必須把空字串餵進 helper。"""
        body = _payload_simple("hello")
        r = client.post(f"/line/webhook/{unconfigured_tid}", content=body)
        assert r.status_code == 400
        assert len(helper_spy.calls) == 1
        call = helper_spy.calls[0]
        assert call["secret_is_dummy"] is True
        # 缺 header → signature 為空字串（不可 None、不可拋錯）
        assert call["signature"] == ""
        assert call["body_len"] == len(body)

    def test_cfg_ok_without_header_calls_helper(
        self, client, tid_configured, helper_spy
    ):
        """有 cfg 但缺 header 不可 short-circuit。"""
        body = _payload_simple("hello")
        r = client.post(f"/line/webhook/{tid_configured}", content=body)
        assert r.status_code == 400
        assert len(helper_spy.calls) == 1
        call = helper_spy.calls[0]
        assert call["secret_is_dummy"] is False
        assert call["signature"] == ""
        assert call["body_len"] == len(body)

    def test_cfg_ok_with_bad_header_calls_helper(
        self, client, tid_configured, helper_spy
    ):
        body = _payload_simple("hello")
        r = client.post(
            f"/line/webhook/{tid_configured}",
            content=body,
            headers={"X-Line-Signature": BAD_SIG},
        )
        assert r.status_code == 400
        assert len(helper_spy.calls) == 1
        call = helper_spy.calls[0]
        assert call["secret_is_dummy"] is False
        assert call["signature"] == BAD_SIG
        assert call["body_len"] == len(body)

    def test_destination_mismatch_does_not_re_run_helper(
        self, client, tid_with_bot_id, helper_spy
    ):
        """destination 不符發生在驗簽通過後，不可重新跑 helper。"""
        body = _payload_with_destination(_OTHER_BOT_USER_ID, "x")
        r = client.post(
            f"/line/webhook/{tid_with_bot_id}",
            content=body,
            headers=_headers(body),
        )
        assert r.status_code == 400
        # 驗簽成功 → helper 呼叫 1 次；destination 不符不應再呼叫第二次
        assert len(helper_spy.calls) == 1

    def test_successful_path_calls_helper_exactly_once(
        self, client, tid_configured, helper_spy
    ):
        """正向對照：合法簽章 → helper 呼叫 1 次 → 200。"""
        body = _payload_simple("hello")
        r = client.post(
            f"/line/webhook/{tid_configured}",
            content=body,
            headers=_headers(body),
        )
        assert r.status_code == 200
        assert len(helper_spy.calls) == 1
        assert helper_spy.calls[0]["secret_is_dummy"] is False


# ═════════════════════════════════════════════════════════════════════════════
# Test 3: 伺服器端 log 區分 reason
# ═════════════════════════════════════════════════════════════════════════════


class TestLogReasonDistinguished:
    """四條拒絕路徑各 log 一個 reason=...，且 log 不含 destination 實際值。"""

    def test_cfg_none_logs_no_config(self, client, unconfigured_tid, caplog):
        with caplog.at_level(logging.WARNING, logger="saas_mvp.routers.line_webhook"):
            body = _payload_simple("x")
            r = client.post(
                f"/line/webhook/{unconfigured_tid}",
                content=body,
                headers={"X-Line-Signature": _sign(body)},
            )
        assert r.status_code == 400
        assert any(
            "reason=no_config" in rec.getMessage()
            for rec in caplog.records
        ), f"預期 log 含 reason=no_config，實得 {[r.getMessage() for r in caplog.records]}"

    def test_missing_header_logs_missing_header(self, client, tid_configured, caplog):
        with caplog.at_level(logging.WARNING, logger="saas_mvp.routers.line_webhook"):
            body = _payload_simple("x")
            r = client.post(f"/line/webhook/{tid_configured}", content=body)
        assert r.status_code == 400
        assert any(
            "reason=missing_header" in rec.getMessage()
            for rec in caplog.records
        ), f"預期 log 含 reason=missing_header，實得 {[r.getMessage() for r in caplog.records]}"

    def test_bad_signature_logs_bad_signature(self, client, tid_configured, caplog):
        with caplog.at_level(logging.WARNING, logger="saas_mvp.routers.line_webhook"):
            body = _payload_simple("x")
            r = client.post(
                f"/line/webhook/{tid_configured}",
                content=body,
                headers={"X-Line-Signature": BAD_SIG},
            )
        assert r.status_code == 400
        assert any(
            "reason=bad_signature" in rec.getMessage()
            for rec in caplog.records
        ), f"預期 log 含 reason=bad_signature，實得 {[r.getMessage() for r in caplog.records]}"

    def test_destination_mismatch_logs_bad_destination(
        self, client, tid_with_bot_id, caplog
    ):
        with caplog.at_level(logging.WARNING, logger="saas_mvp.routers.line_webhook"):
            body = _payload_with_destination(_OTHER_BOT_USER_ID, "x")
            r = client.post(
                f"/line/webhook/{tid_with_bot_id}",
                content=body,
                headers=_headers(body),
            )
        assert r.status_code == 400
        assert any(
            "reason=bad_destination" in rec.getMessage()
            for rec in caplog.records
        ), f"預期 log 含 reason=bad_destination，實得 {[r.getMessage() for r in caplog.records]}"

    def test_log_does_not_leak_destination_value(
        self, client, tid_with_bot_id, caplog
    ):
        """log 不含 destination 實際值——即使在 log 側也不應洩漏。"""
        with caplog.at_level(logging.WARNING, logger="saas_mvp.routers.line_webhook"):
            body = _payload_with_destination(_OTHER_BOT_USER_ID, "x")
            r = client.post(
                f"/line/webhook/{tid_with_bot_id}",
                content=body,
                headers=_headers(body),
            )
        assert r.status_code == 400
        all_log_text = "\n".join(rec.getMessage() for rec in caplog.records)
        assert _OTHER_BOT_USER_ID not in all_log_text, (
            f"log 不應含 destination 值（避免側通道）：{all_log_text!r}"
        )

    def test_reasons_distinct_across_paths(
        self,
        client,
        tid_configured,
        unconfigured_tid,
        tid_with_bot_id,
        caplog,
    ):
        """跨四條路徑的 log reason 字串集合唯一。"""
        with caplog.at_level(logging.WARNING, logger="saas_mvp.routers.line_webhook"):
            body = _payload_simple("x")
            # 1. cfg none
            client.post(
                f"/line/webhook/{unconfigured_tid}",
                content=body,
                headers={"X-Line-Signature": _sign(body)},
            )
            # 2. missing header
            client.post(f"/line/webhook/{tid_configured}", content=body)
            # 3. bad signature
            client.post(
                f"/line/webhook/{tid_configured}",
                content=body,
                headers={"X-Line-Signature": BAD_SIG},
            )
            # 4. destination mismatch
            body_dest = _payload_with_destination(_OTHER_BOT_USER_ID, "x")
            client.post(
                f"/line/webhook/{tid_with_bot_id}",
                content=body_dest,
                headers=_headers(body_dest),
            )

        reasons: set[str] = set()
        for rec in caplog.records:
            msg = rec.getMessage()
            for token in msg.split():
                if token.startswith("reason="):
                    reasons.add(token)
        assert reasons == {
            "reason=no_config",
            "reason=missing_header",
            "reason=bad_signature",
            "reason=bad_destination",
        }, f"log reason 集合不對：{reasons}"


# ═════════════════════════════════════════════════════════════════════════════
# Test 4: 正反向 200 對照組（防測試假綠）
# ═════════════════════════════════════════════════════════════════════════════


class TestPositiveControlForEachRejection:
    """每個拒絕路徑都附一個正向 200 對照組——同租戶送合法簽章應 200。"""

    def test_positive_control_against_missing_header(self, client, tid_configured):
        body = _payload_simple("hello")
        # 反向：缺 header → 400
        r_bad = client.post(f"/line/webhook/{tid_configured}", content=body)
        assert r_bad.status_code == 400
        # 正向：合法簽章 → 200
        r_ok = client.post(
            f"/line/webhook/{tid_configured}",
            content=body,
            headers=_headers(body),
        )
        assert r_ok.status_code == 200
        assert r_ok.json() == {"status": "ok"}

    def test_positive_control_against_bad_signature(self, client, tid_configured):
        body = _payload_simple("hello")
        # 反向：錯簽章 → 400
        r_bad = client.post(
            f"/line/webhook/{tid_configured}",
            content=body,
            headers={"X-Line-Signature": BAD_SIG},
        )
        assert r_bad.status_code == 400
        # 正向：合法簽章 → 200
        r_ok = client.post(
            f"/line/webhook/{tid_configured}",
            content=body,
            headers=_headers(body),
        )
        assert r_ok.status_code == 200
        assert r_ok.json() == {"status": "ok"}

    def test_positive_control_against_destination_mismatch(
        self, client, tid_with_bot_id
    ):
        body_wrong = _payload_with_destination(_OTHER_BOT_USER_ID, "hello")
        # 反向：destination 錯 → 400
        r_bad = client.post(
            f"/line/webhook/{tid_with_bot_id}",
            content=body_wrong,
            headers=_headers(body_wrong),
        )
        assert r_bad.status_code == 400
        # 正向：同租戶、destination 相符 → 200
        body_right = _payload_with_destination(_BOT_USER_ID, "hello")
        r_ok = client.post(
            f"/line/webhook/{tid_with_bot_id}",
            content=body_right,
            headers=_headers(body_right),
        )
        assert r_ok.status_code == 200
        assert r_ok.json() == {"status": "ok"}

    def test_positive_control_against_cfg_none(
        self, client, tid_configured, unconfigured_tid
    ):
        """cfg None 的正向對照：同 payload、合法簽章、換已設定 cfg 的租戶 → 200。"""
        body = _payload_simple("hello")
        # 反向：未設定 cfg → 400
        r_bad = client.post(
            f"/line/webhook/{unconfigured_tid}",
            content=body,
            headers=_headers(body),
        )
        assert r_bad.status_code == 400
        # 正向：同 payload、合法簽章、換已設定 cfg 的租戶 → 200
        r_ok = client.post(
            f"/line/webhook/{tid_configured}",
            content=body,
            headers=_headers(body),
        )
        assert r_ok.status_code == 200
        assert r_ok.json() == {"status": "ok"}


# ═════════════════════════════════════════════════════════════════════════════
# Test 5: 契約保留——既有 status code + body 統一斷言仍綠
# ═════════════════════════════════════════════════════════════════════════════


class TestExistingContractsStillHold:
    """本輪 refactor 後，既有契約必須全綠。"""

    def test_invalid_signature_detail_constant(self):
        assert line_webhook._INVALID_SIGNATURE_DETAIL == "Invalid X-Line-Signature"

    def test_dummy_secret_is_32_bytes(self):
        assert len(line_webhook._DUMMY_SECRET) == 32

    def test_helper_is_module_level_function(self):
        assert callable(line_webhook._constant_time_verify)
        assert line_webhook._constant_time_verify.__module__ == "saas_mvp.routers.line_webhook"
