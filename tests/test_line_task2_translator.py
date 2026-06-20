"""Tests for Task #2: Translator interface, StubTranslator, HTTP backend, /lang parsing.

All tests are fully offline — no network calls, no real API keys required.
"""

import pytest

from saas_mvp.translation import (
    DeepLTranslator,
    StubTranslator,
    Translator,
    TranslationError,
    TranslationResult,
    get_translator,
    parse_lang_command,
)


# ── Translator ABC subclass check ────────────────────────────────────────────

def test_stub_is_translator_subclass():
    assert isinstance(StubTranslator(), Translator)


def test_deepl_is_translator_subclass():
    assert isinstance(DeepLTranslator("fake_key"), Translator)


# ── StubTranslator ───────────────────────────────────────────────────────────

class TestStubTranslator:
    def test_basic_translation(self):
        t = StubTranslator()
        result = t.translate("hello", "ja")
        assert isinstance(result, TranslationResult)
        assert result.text == "[JA] hello"
        assert result.detected_lang is None
        assert result.skipped is False

    def test_lang_uppercased_in_output(self):
        t = StubTranslator()
        result = t.translate("world", "en")
        assert result.text == "[EN] world"
        assert result.skipped is False

    def test_input_lang_already_upper(self):
        t = StubTranslator()
        assert t.translate("test", "ZH-TW").text == "[ZH-TW] test"

    def test_deterministic_same_inputs(self):
        t = StubTranslator()
        r1 = t.translate("hello", "ja")
        r2 = t.translate("hello", "ja")
        assert r1 == r2

    def test_deterministic_different_instances(self):
        assert StubTranslator().translate("hi", "en") == StubTranslator().translate("hi", "en")

    def test_translate_empty_string(self):
        t = StubTranslator()
        result = t.translate("", "ja")
        assert result.text == "[JA] "
        assert result.skipped is False

    def test_is_available_always_true(self):
        assert StubTranslator().is_available() is True

    def test_unicode_text(self):
        t = StubTranslator()
        result = t.translate("こんにちは", "en")
        assert result.text == "[EN] こんにちは"
        assert result.skipped is False

    def test_same_language_returns_original_and_skipped(self):
        t = StubTranslator(source_lang="ja")
        result = t.translate("こんにちは", "JA")
        assert result.text == "こんにちは"
        assert result.detected_lang == "ja"
        assert result.skipped is True


# ── DeepLTranslator (offline tests only) ─────────────────────────────────────

class TestDeepLTranslator:
    def test_available_with_nonempty_key(self):
        t = DeepLTranslator(api_key="some_key")
        assert t.is_available() is True

    def test_unavailable_with_empty_key(self):
        t = DeepLTranslator(api_key="")
        assert t.is_available() is False

    def test_raises_translation_error_on_connection_refused(self):
        """Backend raises TranslationError (not a raw exception) on network failure."""
        t = DeepLTranslator(api_key="test_key", api_url="http://127.0.0.1:1", timeout=1)
        with pytest.raises(TranslationError):
            t.translate("hello", "ja")

    def test_raises_translation_error_on_bad_host(self):
        """Unreachable hostname → TranslationError."""
        t = DeepLTranslator(
            api_key="test_key",
            api_url="http://no-such-host-xyz.invalid/v2/translate",
            timeout=1,
        )
        with pytest.raises(TranslationError):
            t.translate("hello", "ja")

    def test_custom_api_url_stored(self):
        url = "https://api.deepl.com/v2/translate"
        t = DeepLTranslator(api_key="key", api_url=url)
        assert t._api_url == url


# ── get_translator() factory ─────────────────────────────────────────────────

class TestGetTranslator:
    def test_returns_stub_when_no_api_key(self, monkeypatch):
        from saas_mvp import config as _cfg
        monkeypatch.setattr(_cfg.settings, "deepl_api_key", "")
        t = get_translator()
        assert isinstance(t, StubTranslator)

    def test_returns_deepl_when_api_key_set(self, monkeypatch):
        from saas_mvp import config as _cfg
        monkeypatch.setattr(_cfg.settings, "deepl_api_key", "dk-test-xxx")
        monkeypatch.setattr(_cfg.settings, "deepl_api_url", "https://api-free.deepl.com/v2/translate")
        t = get_translator()
        assert isinstance(t, DeepLTranslator)

    def test_graceful_degradation_stub_always_available(self, monkeypatch):
        """Without API key the returned translator is always ready."""
        from saas_mvp import config as _cfg
        monkeypatch.setattr(_cfg.settings, "deepl_api_key", "")
        t = get_translator()
        assert t.is_available() is True

    def test_stub_translate_works_offline(self, monkeypatch):
        from saas_mvp import config as _cfg
        monkeypatch.setattr(_cfg.settings, "deepl_api_key", "")
        t = get_translator()
        result = t.translate("test", "ja")
        assert result.text == "[JA] test"
        assert result.skipped is False


# ── parse_lang_command() ─────────────────────────────────────────────────────

class TestParseLangCommand:
    @pytest.mark.parametrize("text,expected_lang,expected_rest", [
        ("/lang ja",              "ja",    ""),
        ("/lang JA",              "ja",    ""),          # lang code lowercased
        ("/lang en hello world",  "en",    "hello world"),
        ("/lang zh-tw 你好",      "zh-tw", "你好"),
        ("/lang EN  spaced  ",    "en",    "spaced  "),  # trailing whitespace preserved
        ("/lang ko",              "ko",    ""),
    ])
    def test_valid_lang_command(self, text, expected_lang, expected_rest):
        lang, rest = parse_lang_command(text)
        assert lang == expected_lang
        assert rest == expected_rest

    @pytest.mark.parametrize("text", [
        "hello world",
        "/Lang ja",     # case-sensitive — capital L is not a command
        "/LANG ja",
        "",
        "/language ja",
        "/lang",        # no space → not the prefix
    ])
    def test_not_a_command_returns_none_and_original(self, text):
        lang, rest = parse_lang_command(text)
        assert lang is None
        assert rest == text

    def test_lang_only_with_trailing_space(self):
        """/lang<space> with nothing after — treated as not a valid command."""
        lang, rest = parse_lang_command("/lang ")
        assert lang is None
        assert rest == "/lang "

    def test_lang_with_only_whitespace_after(self):
        """/lang followed by spaces only — no code."""
        lang, rest = parse_lang_command("/lang   ")
        assert lang is None
        assert rest == "/lang   "

    def test_does_not_strip_trailing_text_whitespace(self):
        """Trailing whitespace in the translated text is preserved."""
        lang, rest = parse_lang_command("/lang ja   text with spaces   ")
        assert lang == "ja"
        assert rest == "text with spaces   "

    def test_lang_code_lowercased(self):
        lang, _ = parse_lang_command("/lang ZH-TW message")
        assert lang == "zh-tw"
