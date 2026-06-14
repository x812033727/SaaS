"""QA 補強 — 任務 #3：StubTranslator 同語言 skip 的邊界與反向樣本。

驗收標準（#3）
- 同語言情境（source_lang.upper() == target_lang.upper()）→ 回原文。
- 其餘情境 → 維持既有 [LANG] text 包裝。

獨立新檔，不修改既有測試檔；全程離線、不呼叫真實 DeepL。
"""

from __future__ import annotations

import pytest

from saas_mvp.translation import StubTranslator


class TestStubSkipBoundaries:
    def test_returns_exact_original_object(self):
        """skip 時回傳的就是原字串內容（含特殊字元/空白），不做任何改寫。"""
        s = StubTranslator(source_lang="ja")
        text = "  これは\tテスト [JA] 記号 ❤ "
        assert s.translate(text, "ja") == text

    def test_empty_text_same_lang_returns_empty(self):
        s = StubTranslator(source_lang="en")
        assert s.translate("", "en") == ""

    def test_empty_text_different_lang_wraps(self):
        s = StubTranslator(source_lang="en")
        assert s.translate("", "ja") == "[JA] "

    @pytest.mark.parametrize(
        "src,tgt,text,expected",
        [
            ("ja", "JA", "x", "x"),          # 大小寫不同但同語言 → 原文
            ("ZH-HANT", "zh-hant", "繁", "繁"),  # 含連字號、混合大小寫 → 原文
            ("en", "EN", "hi", "hi"),
        ],
    )
    def test_same_lang_case_insensitive_variants(self, src, tgt, text, expected):
        assert StubTranslator(source_lang=src).translate(text, tgt) == expected

    @pytest.mark.parametrize(
        "src,tgt,text,expected",
        [
            ("ja", "en", "hi", "[EN] hi"),       # 反向黑樣本：不同語言必包裝
            ("ja", "ko", "hi", "[KO] hi"),
            ("zh-TW", "zh-CN", "字", "[ZH-CN] 字"),  # 近似但非相等 → 包裝
        ],
    )
    def test_different_lang_always_wraps(self, src, tgt, text, expected):
        """反向樣本：證明 skip 具真實判別力，非全部回原文。"""
        assert StubTranslator(source_lang=src).translate(text, tgt) == expected

    def test_none_source_lang_no_skip_even_if_target_looks_same(self):
        """未設 source_lang → 任何 target 都包裝，永不 skip。"""
        s = StubTranslator()
        assert s.translate("hi", "ja") == "[JA] hi"
        assert s.translate("hi", "en") == "[EN] hi"

    def test_skip_does_not_persist_across_calls(self):
        """同一實例：同語言回原文、切到他語仍正確包裝（無狀態殘留）。"""
        s = StubTranslator(source_lang="ja")
        assert s.translate("a", "ja") == "a"        # skip
        assert s.translate("a", "en") == "[EN] a"   # 包裝
        assert s.translate("a", "JA") == "a"        # 再次 skip

    def test_is_available_always_true(self):
        assert StubTranslator(source_lang="ja").is_available() is True
