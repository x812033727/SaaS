"""翻譯增強驗收測試（獨立檔，全離線，不呼叫真實 DeepL）。

涵蓋本輪 Task #1~#5：
- #1 DeepLTranslator._normalize_target_lang()：ZH-TW→ZH-HANT、ZH-CN→ZH-HANS，其餘 upper()
- #2 DeepLTranslator.translate()：送出 target_lang 為正規化值；
      detected_source_language == 正規化 target 時返回原文（skip）
- #3 StubTranslator 同語言 skip 行為，且不破壞既有 [LANG] 包裝
- #4/#5 webhook 整合：以 Stub(skip) + Fake client 注入，驗證 skip 時回覆原文

所有 DeepL HTTP 呼叫均以 unittest.mock.patch 替換 urllib，絕不打真實 API。
不修改任何既有測試檔。
"""

from __future__ import annotations

import io
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

from saas_mvp.translation import DeepLTranslator, StubTranslator


# ════════════════════════════════════════════════════════════════════════════
# Task #1 — _normalize_target_lang()
# ════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize(
    "raw, expected",
    [
        ("zh-TW", "ZH-HANT"),
        ("ZH-TW", "ZH-HANT"),
        ("zh-CN", "ZH-HANS"),
        ("ZH-CN", "ZH-HANS"),
        ("ja", "JA"),
        ("JA", "JA"),
        ("en", "EN"),
        ("ko", "KO"),
        ("de", "DE"),
    ],
)
def test_normalize_target_lang(raw, expected):
    assert DeepLTranslator._normalize_target_lang(raw) == expected


def test_normalize_is_staticmethod():
    # 可不建 instance 直接呼叫
    assert DeepLTranslator._normalize_target_lang("zh-TW") == "ZH-HANT"


# ════════════════════════════════════════════════════════════════════════════
# Task #2 — DeepLTranslator.translate()（mock urllib）
# ════════════════════════════════════════════════════════════════════════════

def _fake_response(payload: dict):
    """模擬 urlopen 回傳的 context-manager 物件。"""
    raw = json.dumps(payload).encode("utf-8")

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            self.close()
            return False

    return _Resp(raw)


def _patched_urlopen(payload: dict):
    """patch urllib.request.urlopen 並回傳 (patcher, captured) 供斷言送出的 data。"""
    captured = {}

    def _fake(req, timeout=None):
        captured["data"] = req.data
        captured["url"] = req.full_url
        return _fake_response(payload)

    return mock.patch("urllib.request.urlopen", side_effect=_fake), captured


def test_translate_sends_normalized_target_lang_for_zh_tw():
    """target=zh-TW → 實際送出的 target_lang 為 ZH-HANT。"""
    resp = {"translations": [{"detected_source_language": "EN", "text": "你好"}]}
    patcher, captured = _patched_urlopen(resp)
    with patcher:
        t = DeepLTranslator(api_key="dummy-key")
        out = t.translate("hello", "zh-TW")

    assert out == "你好"
    body = captured["data"].decode()
    assert "target_lang=ZH-HANT" in body
    assert "target_lang=ZH-TW" not in body


def test_translate_sends_normalized_target_lang_for_zh_cn():
    resp = {"translations": [{"detected_source_language": "EN", "text": "你好"}]}
    patcher, captured = _patched_urlopen(resp)
    with patcher:
        t = DeepLTranslator(api_key="dummy-key")
        t.translate("hello", "zh-CN")
    assert "target_lang=ZH-HANS" in captured["data"].decode()


def test_translate_keeps_upper_for_other_langs():
    resp = {"translations": [{"detected_source_language": "EN", "text": "こんにちは"}]}
    patcher, captured = _patched_urlopen(resp)
    with patcher:
        t = DeepLTranslator(api_key="dummy-key")
        out = t.translate("hello", "ja")
    assert out == "こんにちは"
    assert "target_lang=JA" in captured["data"].decode()


def test_translate_normal_path_returns_translated_text():
    """來源語言 != target → 回傳 DeepL 譯文。"""
    resp = {"translations": [{"detected_source_language": "EN", "text": "翻訳済み"}]}
    patcher, _ = _patched_urlopen(resp)
    with patcher:
        t = DeepLTranslator(api_key="dummy-key")
        assert t.translate("source", "ja") == "翻訳済み"


def test_translate_skips_when_detected_equals_target():
    """detected_source_language == 正規化 target → 返回原文，不回傳譯文。"""
    resp = {"translations": [{"detected_source_language": "JA", "text": "DEEPL-WRAPPED"}]}
    patcher, _ = _patched_urlopen(resp)
    with patcher:
        t = DeepLTranslator(api_key="dummy-key")
        out = t.translate("これは日本語", "ja")
    assert out == "これは日本語"  # 原文，而非 DeepL 的 "DEEPL-WRAPPED"


def test_translate_skip_uses_normalized_target_for_comparison():
    """zh-TW 正規化為 ZH-HANT；DeepL 偵測回 ZH-HANT 時亦觸發 skip。"""
    resp = {"translations": [{"detected_source_language": "ZH-HANT", "text": "x"}]}
    patcher, _ = _patched_urlopen(resp)
    with patcher:
        t = DeepLTranslator(api_key="dummy-key")
        out = t.translate("原文繁中", "zh-TW")
    assert out == "原文繁中"


def test_translate_no_skip_when_detected_differs():
    resp = {"translations": [{"detected_source_language": "EN", "text": "譯文"}]}
    patcher, _ = _patched_urlopen(resp)
    with patcher:
        t = DeepLTranslator(api_key="dummy-key")
        assert t.translate("hello", "ja") == "譯文"


def test_translate_handles_missing_detected_field():
    """回應缺 detected_source_language → 不 skip，正常回譯文。"""
    resp = {"translations": [{"text": "譯文無偵測欄"}]}
    patcher, _ = _patched_urlopen(resp)
    with patcher:
        t = DeepLTranslator(api_key="dummy-key")
        assert t.translate("hello", "ja") == "譯文無偵測欄"


# ════════════════════════════════════════════════════════════════════════════
# Task #3 — StubTranslator 同語言 skip
# ════════════════════════════════════════════════════════════════════════════

def test_stub_default_wraps_with_lang_tag():
    """無 source_lang → 維持既有 [LANG] text 行為。"""
    assert StubTranslator().translate("hello", "ja") == "[JA] hello"


def test_stub_skip_returns_original_when_same_lang():
    stub = StubTranslator(source_lang="ja")
    assert stub.translate("これは日本語", "ja") == "これは日本語"
    assert stub.translate("これは日本語", "JA") == "これは日本語"


def test_stub_skip_case_insensitive():
    stub = StubTranslator(source_lang="JA")
    assert stub.translate("text", "ja") == "text"


def test_stub_skip_does_not_affect_other_langs():
    """設了 source_lang=ja，但 target=en 時仍正常包裝。"""
    stub = StubTranslator(source_lang="ja")
    assert stub.translate("hello", "en") == "[EN] hello"


def test_stub_source_lang_none_never_skips():
    stub = StubTranslator(source_lang=None)
    assert stub.translate("x", "ja") == "[JA] x"


# ════════════════════════════════════════════════════════════════════════════
# Task #4/#5 — webhook 整合（Stub skip + Fake client，全離線）
# ════════════════════════════════════════════════════════════════════════════

from saas_mvp.models import tenant as _t, user as _u, note as _n, usage as _us  # noqa: E402,F401
from saas_mvp.models import api_key as _ak, api_key_usage as _aku                # noqa: E402,F401
from saas_mvp.models import plan_change_history as _pch                          # noqa: E402,F401
import saas_mvp.models.line_channel_config as _lcm                              # noqa: E402,F401
import saas_mvp.models.line_user_lang as _lul                                    # noqa: E402,F401

from saas_mvp.app import create_app                                              # noqa: E402
from saas_mvp.db import Base, get_db                                             # noqa: E402
from saas_mvp.line_client import FakeLineReplyClient, get_line_client           # noqa: E402
from saas_mvp.translation import get_translator                                 # noqa: E402

import base64                                                                    # noqa: E402
import hashlib                                                                   # noqa: E402
import hmac                                                                      # noqa: E402

_engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
_Session = sessionmaker(autocommit=False, autoflush=False, bind=_engine)

_CHANNEL_SECRET = "enh-channel-secret-32-bytes-xyz!"
_ACCESS_TOKEN = "enh-access-token"

# Stub 設為同語言 ja → skip；fake client 捕捉回覆
_skip_translator = StubTranslator(source_lang="ja")
_fake_client = FakeLineReplyClient()


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
    app.dependency_overrides[get_translator] = lambda: _skip_translator
    app.dependency_overrides[get_line_client] = lambda: _fake_client

    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


def _sign(body: bytes, secret: str = _CHANNEL_SECRET) -> str:
    mac = hmac.new(secret.encode("utf-8"), body, hashlib.sha256)
    return base64.b64encode(mac.digest()).decode("utf-8")


def _register(client: TestClient, default_lang: str = "ja") -> int:
    email = f"enh_{uuid.uuid4().hex[:8]}@example.com"
    tn = f"enh_tenant_{uuid.uuid4().hex[:8]}"
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
            "default_target_lang": default_lang,
        },
    )
    assert r2.status_code == 200, r2.text
    return tid


def _post_text(client: TestClient, tid: int, text: str, lang: str = "ja"):
    event = {
        "type": "message",
        "replyToken": f"rt-{uuid.uuid4().hex[:6]}",
        "source": {"type": "user", "userId": f"U{uuid.uuid4().hex[:8]}"},
        "message": {"type": "text", "text": text},
    }
    body = json.dumps({"events": [event]}).encode("utf-8")
    return client.post(
        f"/line/webhook/{tid}",
        content=body,
        headers={"X-Line-Signature": _sign(body)},
    )


def test_webhook_skip_replies_original_text(client):
    """同語言 skip：default_target_lang=ja，stub source=ja → 回覆原文（非 [JA] 包裝）。"""
    _fake_client.reset()
    tid = _register(client, default_lang="ja")
    r = _post_text(client, tid, "これは日本語のテキスト")
    assert r.status_code == 200, r.text
    assert _fake_client.last_text == "これは日本語のテキスト"
    assert not _fake_client.last_text.startswith("[")


def test_webhook_non_skip_wraps_text(client):
    """target=en（非同語言）→ stub 正常包裝 [EN]。"""
    _fake_client.reset()
    tid = _register(client, default_lang="en")
    r = _post_text(client, tid, "hello world")
    assert r.status_code == 200, r.text
    assert _fake_client.last_text == "[EN] hello world"


def test_webhook_skip_path_still_counts_quota(client):
    """skip 仍是一次成功翻譯回覆 → quota +1（不因 skip 漏算）。"""
    from saas_mvp.quota import get_quota_status
    _fake_client.reset()
    tid = _register(client, default_lang="ja")
    before = get_quota_status(_Session(), tid, "free")["used"]
    _post_text(client, tid, "日本語1")
    after = get_quota_status(_Session(), tid, "free")["used"]
    assert after == before + 1
