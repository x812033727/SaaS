"""翻譯增強功能測試（全離線，不呼叫真實 DeepL）。

覆蓋：
* DeepLTranslator._normalize_target_lang() 靜態映射
* DeepLTranslator.translate() 以 mock urllib 模擬回應（正常翻譯 + 同語言 skip）
* StubTranslator 同語言 skip 行為（保留既有 [LANG] 包裝）

不修改任何既有測試檔。
"""

from __future__ import annotations

import json
from unittest import mock

import pytest

from saas_mvp.translation import DeepLTranslator, StubTranslator
from saas_mvp.translation.base import TranslationError


# ── _normalize_target_lang 靜態映射 ────────────────────────────────────────────
class TestNormalizeTargetLang:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("zh-TW", "ZH-HANT"),
            ("ZH-TW", "ZH-HANT"),
            ("zh-CN", "ZH-HANS"),
            ("ZH-CN", "ZH-HANS"),
            ("ja", "JA"),
            ("JA", "JA"),
            ("en", "EN"),
            ("ko", "KO"),
        ],
    )
    def test_mapping(self, raw, expected):
        assert DeepLTranslator._normalize_target_lang(raw) == expected


def _fake_urlopen(body_dict):
    """產生一個可作為 urlopen context manager 的 mock，回傳指定 JSON body。"""
    resp = mock.MagicMock()
    resp.read.return_value = json.dumps(body_dict).encode()
    cm = mock.MagicMock()
    cm.__enter__.return_value = resp
    cm.__exit__.return_value = False
    return cm


# ── DeepLTranslator.translate() — 正常 + skip ──────────────────────────────────
class TestDeepLTranslate:
    def test_normal_translation(self):
        t = DeepLTranslator(api_key="k")
        body = {"translations": [{"detected_source_language": "EN", "text": "こんにちは"}]}
        with mock.patch("urllib.request.urlopen", return_value=_fake_urlopen(body)):
            out = t.translate("hello", "ja")
        assert out == "こんにちは"

    def test_zh_tw_sends_normalized_target(self):
        """target=zh-TW 時，DeepL payload 的 target_lang 必為 ZH-HANT。"""
        t = DeepLTranslator(api_key="k")
        body = {"translations": [{"detected_source_language": "EN", "text": "你好"}]}
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["data"] = req.data.decode()
            return _fake_urlopen(body)

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            t.translate("hello", "zh-TW")
        assert "target_lang=ZH-HANT" in captured["data"]
        assert "target_lang=ZH-TW" not in captured["data"]

    def test_same_language_skip_returns_original(self):
        """detected_source_language == 正規化後 target → 回原文，不用 API 翻譯結果。"""
        t = DeepLTranslator(api_key="k")
        body = {"translations": [{"detected_source_language": "JA", "text": "DIFFERENT"}]}
        with mock.patch("urllib.request.urlopen", return_value=_fake_urlopen(body)):
            out = t.translate("元の文", "ja")
        assert out == "元の文"

    def test_skip_uses_normalized_comparison(self):
        """zh-CN 正規化為 ZH-HANS，detected=ZH-HANS 時 skip。"""
        t = DeepLTranslator(api_key="k")
        body = {"translations": [{"detected_source_language": "ZH-HANS", "text": "X"}]}
        with mock.patch("urllib.request.urlopen", return_value=_fake_urlopen(body)):
            out = t.translate("你好", "zh-CN")
        assert out == "你好"

    def test_different_language_no_skip(self):
        t = DeepLTranslator(api_key="k")
        body = {"translations": [{"detected_source_language": "EN", "text": "translated"}]}
        with mock.patch("urllib.request.urlopen", return_value=_fake_urlopen(body)):
            out = t.translate("hello", "ja")
        assert out == "translated"

    def test_missing_detected_field_no_skip(self):
        """回應缺 detected_source_language 時不 skip，回翻譯結果。"""
        t = DeepLTranslator(api_key="k")
        body = {"translations": [{"text": "translated"}]}
        with mock.patch("urllib.request.urlopen", return_value=_fake_urlopen(body)):
            out = t.translate("hello", "ja")
        assert out == "translated"

    def test_bad_response_structure_raises(self):
        t = DeepLTranslator(api_key="k")
        body = {"unexpected": True}
        with mock.patch("urllib.request.urlopen", return_value=_fake_urlopen(body)):
            with pytest.raises(TranslationError):
                t.translate("hello", "ja")


# ── StubTranslator skip 行為 ──────────────────────────────────────────────────
class TestStubTranslatorSkip:
    def test_default_keeps_wrapping_behavior(self):
        s = StubTranslator()
        assert s.translate("hello", "ja") == "[JA] hello"

    def test_same_language_returns_original(self):
        s = StubTranslator(source_lang="ja")
        assert s.translate("元の文", "JA") == "元の文"

    def test_same_language_case_insensitive(self):
        s = StubTranslator(source_lang="JA")
        assert s.translate("x", "ja") == "x"

    def test_different_language_still_wraps(self):
        s = StubTranslator(source_lang="ja")
        assert s.translate("hello", "en") == "[EN] hello"

    def test_always_available(self):
        assert StubTranslator(source_lang="ja").is_available() is True
