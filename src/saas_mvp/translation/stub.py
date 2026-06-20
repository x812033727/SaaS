"""Deterministic offline stub translator."""

from __future__ import annotations

from saas_mvp.translation.base import TranslationResult, Translator


class StubTranslator(Translator):
    """Offline stub that wraps text with a language tag: '[LANG] original_text'.

    Guarantees:
    - Deterministic: same (text, target_lang) always → same output.
    - No network, no API key.
    - Always ``is_available() == True``.

    同語言 skip（測試用）
    --------------------
    建構子可選 ``source_lang``。若提供，且 ``target_lang.upper() ==
    source_lang.upper()`` 時 ``translate()`` 直接回傳原文（不包 ``[LANG]`` tag），
    用於離線測試 webhook 下游的同語言 skip 流程。

    侷限：比較採單純 ``.upper()`` 相等，**不**複製 DeepL 的
    ``_normalize_target_lang`` / ZH-HANT 正規化邏輯；測試須使用能直接
    ``.upper()`` 相等的語言碼（如 "JA"/"JA"、"ZH-HANT"/"ZH-HANT"），
    不要用 "ZH-TW" 對 "ZH-HANT"。其餘情境維持 ``[LANG] text``。

    This is the default when no real translation backend is configured,
    and the canonical implementation to use in tests.

    Args:
        source_lang: 可選的固定來源語言。若提供，當 ``target_lang.upper() ==
            source_lang.upper()`` 時直接返回原文（同語言 skip），便於離線測試
            webhook 下游的 skip 行為。比較採單純 upper() 相等，不做 DeepL 正規化
            （此為 Stub 侷限：測試請用能 upper() 相等的語言碼，如 "JA"/"JA"）。
    """

    def __init__(self, source_lang: str | None = None) -> None:
        self._source_lang = source_lang

    def translate(self, text: str, target_lang: str) -> TranslationResult:
        if (
            self._source_lang is not None
            and target_lang.upper() == self._source_lang.upper()
        ):
            return TranslationResult(
                text=text,
                detected_lang=self._source_lang,
                skipped=True,
            )
        return TranslationResult(
            text=f"[{target_lang.upper()}] {text}",
            detected_lang=self._source_lang,
            skipped=False,
        )

    def is_available(self) -> bool:
        return True
