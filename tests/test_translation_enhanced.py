"""任務 #1–#5 驗收測試 — 翻譯增強（語言碼正規化、同語言 skip、Stub skip、webhook 整合）

驗收標準
--------
- target=zh-TW → DeepL 實送 target_lang=ZH-HANT；zh-CN→ZH-HANS；ja/en/ko 維持 upper()。
- DeepL 回應 detected_source_language == 正規化後 target → translate() 回原文，不重複包裝。
- StubTranslator(source_lang=...) 同語言回原文，其餘維持 [LANG] text。
- webhook 翻譯呼叫經 asyncio.to_thread 包裝後流程仍綠（離線 Stub/Fake 注入）。

全部離線：以 unittest.mock.patch 替換 urllib，不呼叫真實 DeepL；webhook 用 Stub/Fake。
本檔為獨立新檔，不修改任何既有測試檔。
"""

from __future__ import annotations

import io
import json
import os
import urllib.error
from contextlib import contextmanager
from unittest import mock

import pytest

os.environ.setdefault("SAAS_RATE_LIMIT_ENABLED", "false")

from saas_mvp.translation import DeepLTranslator, StubTranslator
from saas_mvp.translation.base import TranslationError


# ── 工具：建立假的 urlopen context manager ──────────────────────────────────────


@contextmanager
def _fake_urlopen(body_dict: dict):
    """產生可被 `with urllib.request.urlopen(...) as resp:` 使用的假回應。"""
    payload = json.dumps(body_dict).encode("utf-8")

    class _Resp:
        def read(self):
            return payload

    yield _Resp()


def _capture_sent_target(captured: dict):
    """回傳一個假的 urlopen，會把送出的 target_lang 記錄到 captured。"""

    def _fake(req, timeout=None):
        raw = req.data.decode("utf-8")
        # urlencode 後的 querystring：找 target_lang=...
        import urllib.parse

        parsed = urllib.parse.parse_qs(raw)
        captured["target_lang"] = parsed.get("target_lang", [None])[0]
        captured["text"] = parsed.get("text", [None])[0]
        return _fake_urlopen(
            {"translations": [{"detected_source_language": "EN", "text": "translated"}]}
        )

    return _fake


# ══════════════════════════════════════════════════════════════════════════════
# 任務 #1 — _normalize_target_lang 靜態映射
# ══════════════════════════════════════════════════════════════════════════════


class TestNormalizeTargetLang:
    def test_zh_tw_to_zh_hant(self):
        assert DeepLTranslator._normalize_target_lang("zh-TW") == "ZH-HANT"

    def test_zh_cn_to_zh_hans(self):
        assert DeepLTranslator._normalize_target_lang("zh-CN") == "ZH-HANS"

    def test_already_upper_zh_tw(self):
        assert DeepLTranslator._normalize_target_lang("ZH-TW") == "ZH-HANT"

    @pytest.mark.parametrize("lang", ["ja", "en", "ko", "de", "fr"])
    def test_other_langs_just_upper(self, lang):
        assert DeepLTranslator._normalize_target_lang(lang) == lang.upper()

    def test_is_staticmethod(self):
        # 可不建立實例直接呼叫
        assert DeepLTranslator._normalize_target_lang("JA") == "JA"


# ══════════════════════════════════════════════════════════════════════════════
# 任務 #1 — translate() 實際送出的 target_lang 已正規化
# ══════════════════════════════════════════════════════════════════════════════


class TestTranslateSendsNormalizedTarget:
    def test_zh_tw_sends_zh_hant(self):
        captured: dict = {}
        t = DeepLTranslator(api_key="k")
        with mock.patch("urllib.request.urlopen", _capture_sent_target(captured)):
            t.translate("hello", "zh-TW")
        assert captured["target_lang"] == "ZH-HANT"

    def test_zh_cn_sends_zh_hans(self):
        captured: dict = {}
        t = DeepLTranslator(api_key="k")
        with mock.patch("urllib.request.urlopen", _capture_sent_target(captured)):
            t.translate("hello", "zh-CN")
        assert captured["target_lang"] == "ZH-HANS"

    def test_ja_sends_ja(self):
        captured: dict = {}
        t = DeepLTranslator(api_key="k")
        with mock.patch("urllib.request.urlopen", _capture_sent_target(captured)):
            t.translate("hello", "ja")
        assert captured["target_lang"] == "JA"


# ══════════════════════════════════════════════════════════════════════════════
# 任務 #2 — 同語言 skip（DeepL 回應 detected == normalized target）
# ══════════════════════════════════════════════════════════════════════════════


class TestDeepLSameLanguageSkip:
    def test_skip_returns_original_text(self):
        """detected_source_language == 正規化後 target → 回原文，不回 API 的 text。"""
        t = DeepLTranslator(api_key="k")

        def _fake(req, timeout=None):
            return _fake_urlopen(
                {"translations": [{"detected_source_language": "JA", "text": "別的內容"}]}
            )

        with mock.patch("urllib.request.urlopen", _fake):
            result = t.translate("これはペンです", "ja")
        assert result.text == "これはペンです"  # 原文，非 API 回傳的 "別的內容"
        assert result.detected_lang == "JA"
        assert result.skipped is True

    def test_normal_path_returns_api_text(self):
        """detected != target → 回 API 譯文。"""
        t = DeepLTranslator(api_key="k")

        def _fake(req, timeout=None):
            return _fake_urlopen(
                {"translations": [{"detected_source_language": "EN", "text": "翻訳結果"}]}
            )

        with mock.patch("urllib.request.urlopen", _fake):
            result = t.translate("hello", "ja")
        assert result.text == "翻訳結果"
        assert result.detected_lang == "EN"
        assert result.skipped is False

    def test_skip_case_insensitive(self):
        """detected 大小寫不影響比較（.upper()）。"""
        t = DeepLTranslator(api_key="k")

        def _fake(req, timeout=None):
            return _fake_urlopen(
                {"translations": [{"detected_source_language": "ja", "text": "x"}]}
            )

        with mock.patch("urllib.request.urlopen", _fake):
            result = t.translate("原文text", "JA")
        assert result.text == "原文text"
        assert result.detected_lang == "ja"
        assert result.skipped is True

    def test_zh_detect_skips_zh_hant(self):
        """DeepL 對中文回 ZH，target=zh-TW（正規化 ZH-HANT）→ 視為同語言 skip，回原文。"""
        t = DeepLTranslator(api_key="k")

        def _fake(req, timeout=None):
            return _fake_urlopen(
                {"translations": [{"detected_source_language": "ZH", "text": "別的譯文"}]}
            )

        with mock.patch("urllib.request.urlopen", _fake):
            result = t.translate("中文原文", "zh-TW")
        assert result.text == "中文原文"  # skip → 回原文
        assert result.detected_lang == "ZH"
        assert result.skipped is True

    def test_zh_detect_skips_zh_hans(self):
        """DeepL 回 ZH，target=zh-CN（正規化 ZH-HANS）→ 同語言 skip，回原文。"""
        t = DeepLTranslator(api_key="k")

        def _fake(req, timeout=None):
            return _fake_urlopen(
                {"translations": [{"detected_source_language": "ZH", "text": "x"}]}
            )

        with mock.patch("urllib.request.urlopen", _fake):
            result = t.translate("简体原文", "zh-CN")
        assert result.text == "简体原文"
        assert result.detected_lang == "ZH"
        assert result.skipped is True

    def test_zh_detect_does_not_skip_non_zh_target(self):
        """DeepL 回 ZH 但 target=ja → 非同語言，回 API 譯文。"""
        t = DeepLTranslator(api_key="k")

        def _fake(req, timeout=None):
            return _fake_urlopen(
                {"translations": [{"detected_source_language": "ZH", "text": "翻訳"}]}
            )

        with mock.patch("urllib.request.urlopen", _fake):
            result = t.translate("中文原文", "ja")
        assert result.text == "翻訳"  # 未 skip
        assert result.detected_lang == "ZH"
        assert result.skipped is False

    def test_missing_detected_field_returns_api_text(self):
        """缺 detected_source_language → 視為不同語言，回 API 譯文。"""
        t = DeepLTranslator(api_key="k")

        def _fake(req, timeout=None):
            return _fake_urlopen({"translations": [{"text": "fallback"}]})

        with mock.patch("urllib.request.urlopen", _fake):
            result = t.translate("hello", "ja")
        assert result.text == "fallback"
        assert result.detected_lang is None
        assert result.skipped is False

    def test_bad_response_structure_raises(self):
        t = DeepLTranslator(api_key="k")

        def _fake(req, timeout=None):
            return _fake_urlopen({"unexpected": True})

        with mock.patch("urllib.request.urlopen", _fake):
            with pytest.raises(TranslationError):
                t.translate("hello", "ja")

    def test_translation_not_dict_wrapped_as_translation_error(self):
        """translations[0] 非 dict（格式異常）→ AttributeError 須包成 TranslationError。"""
        t = DeepLTranslator(api_key="k")

        def _fake(req, timeout=None):
            return _fake_urlopen({"translations": [12345]})

        with mock.patch("urllib.request.urlopen", _fake):
            with pytest.raises(TranslationError):
                t.translate("hello", "ja")

    def test_http_error_raises(self):
        t = DeepLTranslator(api_key="k")

        def _fake(req, timeout=None):
            raise urllib.error.HTTPError(
                url="x", code=400, msg="Bad Request", hdrs=None, fp=io.BytesIO(b"")
            )

        with mock.patch("urllib.request.urlopen", _fake):
            with pytest.raises(TranslationError):
                t.translate("hello", "ja")


# ══════════════════════════════════════════════════════════════════════════════
# 任務 #3 — StubTranslator 同語言 skip
# ══════════════════════════════════════════════════════════════════════════════


class TestStubTranslatorSkip:
    def test_default_wraps_with_tag(self):
        s = StubTranslator()
        result = s.translate("hi", "ja")
        assert result.text == "[JA] hi"
        assert result.detected_lang is None
        assert result.skipped is False

    def test_same_lang_returns_original(self):
        s = StubTranslator(source_lang="ja")
        result = s.translate("これ", "ja")
        assert result.text == "これ"
        assert result.detected_lang == "ja"
        assert result.skipped is True

    def test_same_lang_case_insensitive(self):
        s = StubTranslator(source_lang="JA")
        result = s.translate("これ", "ja")
        assert result.text == "これ"
        assert result.detected_lang == "JA"
        assert result.skipped is True

    def test_different_lang_still_wraps(self):
        s = StubTranslator(source_lang="ja")
        result = s.translate("hi", "en")
        assert result.text == "[EN] hi"
        assert result.detected_lang == "ja"
        assert result.skipped is False

    def test_none_source_never_skips(self):
        s = StubTranslator(source_lang=None)
        result = s.translate("hi", "ja")
        assert result.text == "[JA] hi"
        assert result.detected_lang is None
        assert result.skipped is False

    def test_is_available(self):
        assert StubTranslator().is_available() is True
        assert StubTranslator(source_lang="ja").is_available() is True


# ══════════════════════════════════════════════════════════════════════════════
# 任務 #5 — webhook 整合（asyncio.to_thread 包裝後流程仍綠，全離線）
# ══════════════════════════════════════════════════════════════════════════════


class TestWebhookTranslateViaThread:
    """以離線 Stub/Fake 注入，確認 webhook 翻譯呼叫經 to_thread 後流程仍正常。"""

    @staticmethod
    def _setup():
        import base64
        import hashlib
        import hmac
        import uuid

        from fastapi.testclient import TestClient
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from sqlalchemy.pool import StaticPool

        from saas_mvp.models import tenant as _t, user as _u, note as _n, usage as _us  # noqa: F401
        from saas_mvp.models import api_key as _ak, api_key_usage as _aku  # noqa: F401
        from saas_mvp.models import plan_change_history as _pch  # noqa: F401
        import saas_mvp.models.line_channel_config as _lcm  # noqa: F401
        import saas_mvp.models.line_user_lang as _lul  # noqa: F401

        from saas_mvp.app import create_app
        from saas_mvp.db import Base, get_db
        from saas_mvp.line_client import FakeLineReplyClient, get_line_client
        from saas_mvp.translation import StubTranslator, get_translator

        engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
        Base.metadata.create_all(bind=engine)

        translator = StubTranslator()
        fake_client = FakeLineReplyClient()

        app = create_app()

        def override_db():
            db = Session()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db] = override_db
        app.dependency_overrides[get_translator] = lambda: translator
        app.dependency_overrides[get_line_client] = lambda: fake_client

        client = TestClient(app, raise_server_exceptions=True)

        # 註冊租戶 + 設為 admin + 建 LINE config
        secret = "test-channel-secret-32-bytes-x!!"
        email = f"thr_{uuid.uuid4().hex[:8]}@example.com"
        tn = f"thr_tenant_{uuid.uuid4().hex[:8]}"
        r = client.post(
            "/auth/register",
            json={"email": email, "password": "Test1234!", "tenant_name": tn},
        )
        assert r.status_code == 201, r.text
        token = r.json()["access_token"]
        me = client.get("/tenants/me", headers={"Authorization": f"Bearer {token}"})
        tid = me.json()["id"]

        from saas_mvp.auth.security import decode_access_token
        from saas_mvp.models.user import User

        sub = int(decode_access_token(token)["sub"])
        db = Session()
        try:
            db.get(User, sub).is_admin = True
            db.commit()
        finally:
            db.close()

        r2 = client.put(
            f"/admin/line-configs/{tid}",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "channel_secret": secret,
                "access_token": "tok",
                "default_target_lang": "ja",
            },
        )
        assert r2.status_code == 200, r2.text

        def sign(body: bytes) -> str:
            mac = hmac.new(secret.encode(), body, hashlib.sha256)
            return base64.b64encode(mac.digest()).decode()

        return client, tid, sign, fake_client

    def test_text_message_translated_and_replied(self):
        client, tid, sign, fake_client = self._setup()
        body = json.dumps(
            {
                "events": [
                    {
                        "type": "message",
                        "replyToken": "rt-001",
                        "source": {"type": "user", "userId": "Uthread1"},
                        "message": {"type": "text", "text": "hello"},
                    }
                ]
            }
        ).encode("utf-8")

        r = client.post(
            f"/line/webhook/{tid}",
            content=body,
            headers={"X-Line-Signature": sign(body)},
        )
        assert r.status_code == 200, r.text
        # to_thread 包裝後仍正確回覆譯文（default_target_lang=ja → [JA] hello）
        replies = [s.text for s in fake_client.sent]
        assert "[JA] hello" in replies
