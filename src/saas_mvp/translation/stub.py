"""Deterministic offline stub translator."""

from __future__ import annotations

from saas_mvp.translation.base import Translator


class StubTranslator(Translator):
    """Offline stub that wraps text with a language tag: '[LANG] original_text'.

    Guarantees:
    - Deterministic: same (text, target_lang) always → same output.
    - No network, no API key.
    - Always ``is_available() == True``.

    可選 ``source_lang`` 模擬「同語言 skip」行為：當 ``target_lang.upper()`` 等於
    ``source_lang.upper()`` 時直接回傳原文（便於離線測試 webhook 下游 skip 流程）。
    比較採單純 ``.upper()`` 相等，不複製 DeepL 的 ZH-HANT 正規化邏輯——測試碼應使用
    可直接 ``.upper()`` 相等的語言碼（如 "JA"/"JA"）。其餘情境維持 ``[LANG] text``。

    This is the default when no real translation backend is configured,
    and the canonical implementation to use in tests.
    """

    def __init__(self, source_lang: str | None = None) -> None:
        self._source_lang = source_lang

    def translate(self, text: str, target_lang: str) -> str:
        if (
            self._source_lang is not None
            and target_lang.upper() == self._source_lang.upper()
        ):
            return text
        return f"[{target_lang.upper()}] {text}"

    def is_available(self) -> bool:
        return True
