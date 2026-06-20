"""QA 驗證 — 任務 #2：StubTranslator.translate 必須回傳 TranslationResult。

驗收標準（#2）
1. StubTranslator.translate(...) 回傳型別為 TranslationResult（非 str）。
2. 同語言情境 → skipped is True 且 text == 原文。
3. 非同語言情境 → skipped is False 且 text == 譯文（[LANG] text）。
4. TranslationResult 為 frozen（不可變）。
5. StubTranslator 仍為 Translator 抽象子類，覆寫 translate 簽章一致。

獨立新檔，不修改既有測試；不需網路、不需 API key。
"""

from __future__ import annotations

import dataclasses

import pytest

from saas_mvp.translation import StubTranslator
from saas_mvp.translation.base import TranslationResult, Translator


class TestStubTranslatorReturnsTranslationResult:
    def test_translate_returns_translation_result_instance(self):
        """回傳型別必須是 TranslationResult，不能退化成 str。"""
        s = StubTranslator()
        result = s.translate("hello", "ja")
        assert isinstance(result, TranslationResult)
        assert not isinstance(result, str)  # 防止日後有人把 dataclass 改成 str

    def test_is_translator_subclass_with_consistent_signature(self):
        """仍是 Translator 子類，translate 簽章與抽象一致。"""
        assert issubclass(StubTranslator, Translator)
        # 抽象方法存在
        assert hasattr(StubTranslator, "translate")
        # 不該是抽象（可實例化）
        assert not dataclasses.is_dataclass(StubTranslator)

    def test_translation_result_is_frozen(self):
        """TranslationResult 為 frozen，setattr 必須拋 FrozenInstanceError。"""
        s = StubTranslator(source_lang="ja")
        result = s.translate("hi", "ja")
        with pytest.raises(dataclasses.FrozenInstanceError):
            result.text = "tampered"
        with pytest.raises(dataclasses.FrozenInstanceError):
            result.skipped = not result.skipped


class TestSameLanguageSkip:
    """驗收點 2：同語言 → skipped is True 且 text == 原文。"""

    def test_exact_upper_match_skip(self):
        s = StubTranslator(source_lang="JA")
        result = s.translate("hello world", "JA")
        assert result.skipped is True
        assert result.text == "hello world"

    def test_case_insensitive_match_skip(self):
        """source_lang 與 target_lang 大小寫不一致也算同語言。"""
        s = StubTranslator(source_lang="ja")
        result = s.translate("こんにちは", "JA")
        assert result.skipped is True
        assert result.text == "こんにちは"

    def test_detected_lang_equals_source_lang_on_skip(self):
        """skip 時 detected_lang 應等於 source_lang（呼叫端可記錄）。"""
        s = StubTranslator(source_lang="en")
        result = s.translate("text", "EN")
        assert result.skipped is True
        assert result.detected_lang == "en"

    def test_unicode_and_whitespace_preserved_on_skip(self):
        """skip 時必須回傳逐字相同的原文，不做任何清理。"""
        s = StubTranslator(source_lang="ja")
        text = "  多分\n\t改行 [JA] 記号 ❤ "
        result = s.translate(text, "ja")
        assert result.skipped is True
        assert result.text == text
        assert len(result.text) == len(text)


class TestDifferentLanguageTranslate:
    """驗收點 3：非同語言 → skipped is False 且 text == [LANG] text。"""

    def test_default_stub_wraps_with_lang_tag(self):
        """未設 source_lang → 任何 target 都包裝成 [LANG] text。"""
        s = StubTranslator()
        result = s.translate("hi", "ja")
        assert result.skipped is False
        assert result.text == "[JA] hi"

    def test_different_lang_when_source_set(self):
        s = StubTranslator(source_lang="ja")
        result = s.translate("hi", "en")
        assert result.skipped is False
        assert result.text == "[EN] hi"

    def test_detected_lang_is_source_lang_when_set(self):
        """非 skip 時 detected_lang 仍為 source_lang（供下游記錄用）。"""
        s = StubTranslator(source_lang="en")
        result = s.translate("hi", "ja")
        assert result.skipped is False
        assert result.text == "[JA] hi"
        assert result.detected_lang == "en"

    def test_detected_lang_is_none_when_source_not_set(self):
        """未設 source_lang → detected_lang 為 None。"""
        s = StubTranslator()
        result = s.translate("hi", "ja")
        assert result.skipped is False
        assert result.detected_lang is None


class TestEdgeCases:
    """破壞性思考：戳邊界與隱含假設。"""

    def test_empty_text_same_lang_returns_empty(self):
        s = StubTranslator(source_lang="en")
        result = s.translate("", "en")
        assert result.skipped is True
        assert result.text == ""

    def test_empty_text_different_lang_wraps(self):
        s = StubTranslator(source_lang="en")
        result = s.translate("", "ja")
        assert result.skipped is False
        assert result.text == "[JA] "

    def test_similar_but_not_equal_lang_does_not_skip(self):
        """「近似」不等於「相同」：zh-TW vs zh-CN 必須視為不同語言。"""
        s = StubTranslator(source_lang="zh-TW")
        result = s.translate("字", "zh-CN")
        assert result.skipped is False
        assert result.text == "[ZH-CN] 字"

    def test_skip_is_per_call_not_sticky(self):
        """同一實例：skip 一次後切到他語仍正常翻譯；切回同語言再次 skip。"""
        s = StubTranslator(source_lang="ja")
        r1 = s.translate("a", "ja")
        r2 = s.translate("a", "en")
        r3 = s.translate("a", "JA")
        assert r1.skipped is True and r1.text == "a"
        assert r2.skipped is False and r2.text == "[EN] a"
        assert r3.skipped is True and r3.text == "a"

    def test_result_equality_for_same_input(self):
        """frozen dataclass 應支援 == 比較（測試斷言友善）。"""
        s = StubTranslator(source_lang="ja")
        r1 = s.translate("hi", "ja")
        r2 = s.translate("hi", "ja")
        assert r1 == r2
        assert r1 is not r2  # 不同實例物件


class TestTranslationResultDataclassShape:
    """驗證 TranslationResult 的欄位形狀符合驗收標準 #1。"""

    def test_required_fields_present(self):
        fields = {f.name for f in dataclasses.fields(TranslationResult)}
        assert fields == {"text", "detected_lang", "skipped"}

    def test_field_types(self):
        from typing import get_type_hints
        hints = get_type_hints(TranslationResult)
        assert hints["text"] is str
        assert "str" in str(hints["detected_lang"])  # str | None
        assert hints["skipped"] is bool

    def test_is_frozen_dataclass(self):
        assert dataclasses.is_dataclass(TranslationResult)
        # frozen 參數存在於 __dataclass_params__
        assert TranslationResult.__dataclass_params__.frozen is True
