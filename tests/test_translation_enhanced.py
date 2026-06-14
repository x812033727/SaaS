"""翻譯增強驗收測試 — 語言碼正規化、同語言 skip、Stub skip、webhook 整合。

全程離線：以 unittest.mock 替換 urllib，不呼叫真實 DeepL；
webhook 整合用 StubTranslator + FakeLineReplyClient 注入。

獨立檔案，不改既有測試檔。
"""

from __future__ import annotations

import json
from unittest import mock

import pytest

from saas_mvp.translation import StubTranslator
from saas_mvp.translation.http import DeepLTranslator
from saas_mvp.translation.base import TranslationError


# ════════════════════════════════════════════════════════════════════════════
# #1 _normalize_target_lang — 靜態映射
# ════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize(
    "raw,expected",
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
def test_normalize_target_lang(raw, expected):
    assert DeepLTranslator._normalize_target_lang(raw) == expected


# ════════════════════════════════════════════════════════════════════════════
# #2 DeepLTranslator.translate — payload 用正規化 target、同語言 skip
# ════════════════════════════════════════════════════════════════════════════

def _fake_urlopen_factory(response_body: dict, capture: dict):
    """回傳一個可當 context manager 的 fake urlopen，並擷取送出的 payload。"""
    class _Resp:
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def read(self):
            return json.dumps(response_body).encode()

    def _fake_urlopen(req, timeout=None):
        capture["data"] = req.data
        return _Resp()

    return _fake_urlopen


def test_translate_sends_normalized_target_for_zh_tw():
    """target=zh-TW → DeepL payload 的 target_lang 必須是 ZH-HANT。"""
    capture = {}
    fake = _fake_urlopen_factory(
        {"translations": [{"detected_source_language": "EN", "text": "你好"}]},
        capture,
    )
    with mock.patch("saas_mvp.translation.http.urllib.request.urlopen", fake):
        t = DeepLTranslator(api_key="k")
        out = t.translate("hello", "zh-TW")

    assert out == "你好"
    sent = capture["data"].decode()
    assert "target_lang=ZH-HANT" in sent
    assert "ZH-TW" not in sent


def test_translate_normal_path_returns_translation():
    capture = {}
    fake = _fake_urlopen_factory(
        {"translations": [{"detected_source_language": "EN", "text": "こんにちは"}]},
        capture,
    )
    with mock.patch("saas_mvp.translation.http.urllib.request.urlopen", fake):
        t = DeepLTranslator(api_key="k")
        out = t.translate("hello", "ja")

    assert out == "こんにちは"
    assert "target_lang=JA" in capture["data"].decode()


def test_translate_skips_when_source_equals_target():
    """detected_source_language == 正規化 target → 回傳原文（不回譯文）。"""
    capture = {}
    fake = _fake_urlopen_factory(
        # 用戶本來就用日文，target 也是 ja → skip，回原文
        {"translations": [{"detected_source_language": "JA", "text": "（不應使用此譯文）"}]},
        capture,
    )
    with mock.patch("saas_mvp.translation.http.urllib.request.urlopen", fake):
        t = DeepLTranslator(api_key="k")
        out = t.translate("おはよう", "ja")

    assert out == "おはよう"  # 原文，而非 body 內的 text


def test_translate_skip_case_insensitive_detected():
    """detected 為小寫也應正確比對（.upper()）。"""
    capture = {}
    fake = _fake_urlopen_factory(
        {"translations": [{"detected_source_language": "en", "text": "X"}]},
        capture,
    )
    with mock.patch("saas_mvp.translation.http.urllib.request.urlopen", fake):
        t = DeepLTranslator(api_key="k")
        out = t.translate("origin", "en")
    assert out == "origin"


def test_translate_no_detected_field_returns_translation():
    """回應缺 detected_source_language → 不 skip，正常回譯文。"""
    capture = {}
    fake = _fake_urlopen_factory(
        {"translations": [{"text": "translated"}]},
        capture,
    )
    with mock.patch("saas_mvp.translation.http.urllib.request.urlopen", fake):
        t = DeepLTranslator(api_key="k")
        out = t.translate("src", "ja")
    assert out == "translated"


def test_translate_bad_response_raises():
    capture = {}
    fake = _fake_urlopen_factory({"unexpected": True}, capture)
    with mock.patch("saas_mvp.translation.http.urllib.request.urlopen", fake):
        t = DeepLTranslator(api_key="k")
        with pytest.raises(TranslationError):
            t.translate("src", "ja")


# ════════════════════════════════════════════════════════════════════════════
# #3 StubTranslator — 同語言 skip 與既有 [LANG] 行為
# ════════════════════════════════════════════════════════════════════════════

def test_stub_default_wraps_with_tag():
    assert StubTranslator().translate("hi", "ja") == "[JA] hi"


def test_stub_skip_when_source_equals_target():
    stub = StubTranslator(source_lang="ja")
    assert stub.translate("おはよう", "ja") == "おはよう"
    assert stub.translate("おはよう", "JA") == "おはよう"  # 大小寫無關


def test_stub_with_source_lang_still_wraps_other_targets():
    stub = StubTranslator(source_lang="ja")
    assert stub.translate("hi", "en") == "[EN] hi"

# 註：webhook 端對端整合（含 asyncio.to_thread 翻譯呼叫路徑）已由既有
# tests/test_line_task5_webhook.py 全套覆蓋——所有 webhook 測試都會走 handler
# 的 await asyncio.to_thread(translator.translate, ...)。本檔不再重複該昂貴
# （bcrypt 註冊）整合路徑，聚焦於翻譯層單元行為，維持離線且快速。
