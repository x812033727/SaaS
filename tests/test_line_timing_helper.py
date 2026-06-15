"""等量時間 helper 收斂測試 — 確認三條拒絕路徑走同一 helper、對外回應等價、log 區分。

本檔不重做既有的「status code + body 統一」測試（已於 test_line_task5_webhook.py 的
test_configured_vs_unconfigured_indistinguishable_all_probes 與
test_mismatch_detail_identical_to_signature_failure 鎖死），本檔只補三個缺口：
1. 三條 HMAC 拒絕路徑（cfg None / 缺 header / 簽章錯）皆呼叫
   `_constant_time_verify`（用 mock spy 驗證「走同一 helper」契約）。
2. 四條拒絕路徑（cfg None / 缺 header / 簽章錯 / destination 不符）的對外回應
   `status_code` 與 response body 逐字節完全相等。
3. 伺服器端 log 對每條拒絕路徑記錄可區分的 `reason=...`，且 log 內容不夾帶對外
   detail 字串。

全部離線：StubTranslator + FakeLineReplyClient，不需真實 LINE/翻譯金鑰。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import uuid
from unittest.mock import patch

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

# 載入所有 model metadata（確保 create_all 建出完整 schema）
from saas_mvp.models import tenant as _t, user as _u, note as _n, usage as _us  # noqa: F401
from saas_mvp.models import api_key as _ak, api_key_usage as _aku               # noqa: F401
from saas_mvp.models import plan_change_history as _pch                          # noqa: F401
import saas_mvp.models.line_channel_config as _lcm                               # noqa: F401
import saas_mvp.models.line_user_lang as _lul                                     # noqa: F401

from saas_mvp.app import create_app
from saas_mvp.db import Base, get_db
from saas_mvp.line_client import FakeLineReplyClient, get_line_client
from saas_mvp.models.line_channel_config import LineChannelConfig
from saas_mvp.routers.line_webhook import _DUMMY_SECRET, _constant_time_verify
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
    body: dict = {
        "events": [
            {
                "type": "message",
                "replyToken": "rt-helper",
                "source": {"type": "user", "userId": "Uhelper001"},
                "message": {"type": "text", "text": text},
            }
        ]
    }
    if destination is not None:
        body["destination"] = destination
    return json.dumps(body).encode("utf-8")


def _register_with_config(client: TestClient, set_bot_id: bool = False) -> int:
    """註冊租戶並建立 LINE config；set_bot_id=True 時額外補 line_bot_user_id。"""
    email = f"tim_{uuid.uuid4().hex[:8]}@example.com"
    tn = f"tim_tenant_{uuid.uuid4().hex[:8]}"
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

    if set_bot_id:
        db = _Session()
        try:
            cfg = db.query(LineChannelConfig).filter(
                LineChannelConfig.tenant_id == tid
            ).one()
            cfg.line_bot_user_id = _BOT_USER_ID
            db.commit()
        finally:
            db.close()
    return tid


# ── fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def configured_tid(client):
    """有 config、line_bot_user_id 為 None 的租戶（舊 config 行為）。"""
    return _register_with_config(client, set_bot_id=False)


@pytest.fixture(scope="module")
def configured_with_bot_tid(client):
    """有 config + line_bot_user_id 的租戶（destination 二次驗證生效）。"""
    return _register_with_config(client, set_bot_id=True)


# ── 1. 三條 HMAC 拒絕路徑走同一 helper ───────────────────────────────────────


class TestAllRejectionPathsGoThroughHelper:
    """等量時間 helper 收斂契約：三條 HMAC 拒絕路徑（cfg None / 缺 header /
    簽章錯）皆呼叫 `_constant_time_verify`。

    destination 不符發生在 helper「成功之後」（簽章合法 → helper 回 True → 才進
    destination 二次驗證），不算 HMAC 拒絕；對應斷言另列。
    """

    def test_no_config_path_runs_helper_with_dummy_secret(self, client):
        """cfg 缺失路徑：即使找不到 config，也跑完整 HMAC 鏈（不再 short-circuit），
        且 secret 為 `_DUMMY_SECRET` 確保結果必 False。
        """
        body = _payload(None)
        with patch(
            "saas_mvp.routers.line_webhook._constant_time_verify",
            wraps=_constant_time_verify,
        ) as spy:
            r = client.post(
                f"/line/webhook/99999",  # 不存在 → cfg 為 None
                content=body,
                headers={"X-Line-Signature": "AAAA"},
            )
        assert r.status_code == 400
        assert spy.call_count == 1, "cfg 缺失路徑未走 helper"
        # helper 收到的 secret 應為 _DUMMY_SECRET
        assert spy.call_args.args[2] == _DUMMY_SECRET, (
            f"cfg 缺失路徑 secret 應為 dummy，卻是 {spy.call_args.args[2]!r}"
        )

    def test_missing_header_path_runs_helper_with_empty_sig(self, client, configured_tid):
        """缺 header 路徑：cfg 存在但不帶 X-Line-Signature，仍跑完整 HMAC 鏈；
        傳入空字串讓 compare_digest 自然回 False。
        """
        body = _payload(None)
        with patch(
            "saas_mvp.routers.line_webhook._constant_time_verify",
            wraps=_constant_time_verify,
        ) as spy:
            r = client.post(
                f"/line/webhook/{configured_tid}",
                content=body,
                # 完全不帶 X-Line-Signature
            )
        assert r.status_code == 400
        assert spy.call_count == 1, "缺 header 路徑未走 helper（仍 short-circuit）"
        # signature 為空字串、secret 為真實 channel_secret
        assert spy.call_args.args[1] == ""
        assert spy.call_args.args[2] == _CHANNEL_SECRET.encode("utf-8")

    def test_bad_signature_path_runs_helper(self, client, configured_tid):
        """簽章錯路徑：cfg 存在、header 有但內容錯，走 helper 回 False。"""
        body = _payload(None)
        with patch(
            "saas_mvp.routers.line_webhook._constant_time_verify",
            wraps=_constant_time_verify,
        ) as spy:
            r = client.post(
                f"/line/webhook/{configured_tid}",
                content=body,
                headers={"X-Line-Signature": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="},
            )
        assert r.status_code == 400
        assert spy.call_count == 1
        assert spy.call_args.args[1] == "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
        assert spy.call_args.args[2] == _CHANNEL_SECRET.encode("utf-8")

    def test_destination_mismatch_runs_helper_only_once(self, client, configured_with_bot_tid):
        """destination 不符：helper 先被合法簽章路徑呼叫一次（回 True），
        不會因 destination 錯而「再呼叫一次」做第二次 HMAC。
        """
        body = _payload(_OTHER_BOT_USER_ID)
        with patch(
            "saas_mvp.routers.line_webhook._constant_time_verify",
            wraps=_constant_time_verify,
        ) as spy:
            r = client.post(
                f"/line/webhook/{configured_with_bot_tid}",
                content=body,
                headers={"X-Line-Signature": _sign(body)},  # 合法簽章
            )
        assert r.status_code == 400
        # helper 只被呼叫一次（合法簽章 → True → 進入 destination 比對 → 失敗 raise）
        assert spy.call_count == 1
        # 確認傳入的 secret 是真實 channel_secret
        assert spy.call_args.args[2] == _CHANNEL_SECRET.encode("utf-8")

    def test_positive_control_runs_helper_exactly_once(self, client, configured_tid):
        """正向對照：合法簽章 + 有效 config → helper 呼叫一次 → 200。
        防測試本身假綠：若 helper 沒被呼叫或被呼叫多次，下游會爆。
        """
        body = _payload(None)
        with patch(
            "saas_mvp.routers.line_webhook._constant_time_verify",
            wraps=_constant_time_verify,
        ) as spy:
            r = client.post(
                f"/line/webhook/{configured_tid}",
                content=body,
                headers={"X-Line-Signature": _sign(body)},
            )
        assert r.status_code == 200
        assert spy.call_count == 1
        assert spy.call_args.args[2] == _CHANNEL_SECRET.encode("utf-8")


# ── 2. 四條拒絕路徑對外回應逐字節等價 ────────────────────────────────────────


class TestAllFourPathsByteIdentical:
    """四條拒絕路徑（cfg None / 缺 header / 簽章錯 / destination 不符）的 status_code
    與 response body 逐字節完全相等，外部無法藉任何 HTTP 內容區分。
    """

    def test_all_four_rejection_responses_byte_identical(self, client, configured_with_bot_tid):
        configured_tid = configured_with_bot_tid
        body_dest_wrong = _payload(_OTHER_BOT_USER_ID)  # 給 destination 不符探針
        body_baseline = _payload(_BOT_USER_ID)          # 給其他三個探針

        BAD_SIG = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="

        # 探針 A：destination 不符（合法簽章、合法 cfg、bot_id 已設）
        r_dest = client.post(
            f"/line/webhook/{configured_tid}",
            content=body_dest_wrong,
            headers={"X-Line-Signature": _sign(body_dest_wrong)},
        )
        # 探針 B：簽章錯（合法 cfg、destination 相符）
        r_sig = client.post(
            f"/line/webhook/{configured_tid}",
            content=body_baseline,
            headers={"X-Line-Signature": BAD_SIG},
        )
        # 探針 C：缺 header（合法 cfg、destination 相符）
        r_miss = client.post(
            f"/line/webhook/{configured_tid}",
            content=body_baseline,
        )
        # 探針 D：cfg 缺失（不存在 tenant）
        r_nocfg = client.post(
            f"/line/webhook/99998",
            content=body_baseline,
            headers={"X-Line-Signature": _sign(body_baseline)},
        )

        responses = [r_dest, r_sig, r_miss, r_nocfg]

        # status code 全部 400
        status_codes = [r.status_code for r in responses]
        assert status_codes == [400, 400, 400, 400], f"status code 不一致：{status_codes}"

        # response body 逐字節等價
        bodies = [r.text for r in responses]
        assert bodies.count(bodies[0]) == 4, (
            f"response body 逐字節不相等，存在列舉旁路：{bodies!r}"
        )

        # detail 唯一性（保險起見再單獨驗一次）
        details = [r.json()["detail"] for r in responses]
        assert len(set(details)) == 1, f"detail 不唯一：{details!r}"


# ── 3. log reason 區分（伺服器端可觀察、對外不可見）────────────────────────────


class TestLogReasonDistinguished:
    """四條拒絕路徑的伺服器端 log 各帶可區分的 `reason=...`，供監控使用。"""

    def _fire(self, client, configured_tid, configured_with_bot_tid, scenario):
        body_baseline = _payload(_BOT_USER_ID)
        BAD_SIG = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="

        if scenario == "no_config":
            return client.post(
                f"/line/webhook/99997",
                content=body_baseline,
                headers={"X-Line-Signature": _sign(body_baseline)},
            )
        if scenario == "missing_header":
            return client.post(
                f"/line/webhook/{configured_tid}",
                content=body_baseline,
            )
        if scenario == "bad_signature":
            return client.post(
                f"/line/webhook/{configured_tid}",
                content=body_baseline,
                headers={"X-Line-Signature": BAD_SIG},
            )
        if scenario == "bad_destination":
            body_dest_wrong = _payload(_OTHER_BOT_USER_ID)
            return client.post(
                f"/line/webhook/{configured_with_bot_tid}",
                content=body_dest_wrong,
                headers={"X-Line-Signature": _sign(body_dest_wrong)},
            )
        raise ValueError(scenario)

    def test_no_config_logs_reason(self, client, configured_tid, configured_with_bot_tid, caplog):
        with caplog.at_level(logging.WARNING, logger="saas_mvp.routers.line_webhook"):
            r = self._fire(client, configured_tid, configured_with_bot_tid, "no_config")
        assert r.status_code == 400
        assert any("reason=no_config" in rec.message for rec in caplog.records), (
            f"未找到 no_config log：{[r.message for r in caplog.records]!r}"
        )

    def test_missing_header_logs_reason(self, client, configured_tid, configured_with_bot_tid, caplog):
        with caplog.at_level(logging.WARNING, logger="saas_mvp.routers.line_webhook"):
            r = self._fire(client, configured_tid, configured_with_bot_tid, "missing_header")
        assert r.status_code == 400
        assert any("reason=missing_header" in rec.message for rec in caplog.records), (
            f"未找到 missing_header log：{[r.message for r in caplog.records]!r}"
        )

    def test_bad_signature_logs_reason(self, client, configured_tid, configured_with_bot_tid, caplog):
        with caplog.at_level(logging.WARNING, logger="saas_mvp.routers.line_webhook"):
            r = self._fire(client, configured_tid, configured_with_bot_tid, "bad_signature")
        assert r.status_code == 400
        assert any("reason=bad_signature" in rec.message for rec in caplog.records), (
            f"未找到 bad_signature log：{[r.message for r in caplog.records]!r}"
        )

    def test_bad_destination_logs_reason(self, client, configured_tid, configured_with_bot_tid, caplog):
        with caplog.at_level(logging.WARNING, logger="saas_mvp.routers.line_webhook"):
            r = self._fire(client, configured_tid, configured_with_bot_tid, "bad_destination")
        assert r.status_code == 400
        assert any("reason=bad_destination" in rec.message for rec in caplog.records), (
            f"未找到 bad_destination log：{[r.message for r in caplog.records]!r}"
        )

    def test_log_does_not_leak_response_detail(self, client, configured_tid, configured_with_bot_tid, caplog):
        """log 訊息不應夾帶對外 detail 字串（'Invalid X-Line-Signature'）——
        若 log 端重複洩漏，SIEM 側就能間接放大列舉風險。
        """
        with caplog.at_level(logging.WARNING, logger="saas_mvp.routers.line_webhook"):
            self._fire(client, configured_tid, configured_with_bot_tid, "missing_header")
            self._fire(client, configured_tid, configured_with_bot_tid, "bad_signature")
            self._fire(client, configured_tid, configured_with_bot_tid, "no_config")
            self._fire(client, configured_tid, configured_with_bot_tid, "bad_destination")
        for rec in caplog.records:
            assert "Invalid X-Line-Signature" not in rec.message, (
                f"log 不應含對外 detail 字串：{rec.message!r}"
            )

    def test_log_level_is_warning(self, client, configured_tid, configured_with_bot_tid, caplog):
        """log 級別為 WARNING——SIEM 才能用於告警；INFO 不夠醒目。"""
        with caplog.at_level(logging.WARNING, logger="saas_mvp.routers.line_webhook"):
            self._fire(client, configured_tid, configured_with_bot_tid, "bad_signature")
        assert any(rec.levelno == logging.WARNING for rec in caplog.records), (
            f"未記錄 WARNING 級 log：{[(rec.levelname, rec.message) for rec in caplog.records]!r}"
        )
