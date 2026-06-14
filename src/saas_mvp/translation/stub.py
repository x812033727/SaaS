"""Deterministic offline stub translator."""

from __future__ import annotations

from saas_mvp.translation.base import Translator


class StubTranslator(Translator):
    """Offline stub that wraps text with a language tag: '[LANG] original_text'.

    Guarantees:
    - Deterministic: same (text, target_lang) always → same output.
    - No network, no API key.
    - Always ``is_available() == True``.

    同語言 skip（測試用）
    --------------------
    若以 ``source_lang`` 建構，且 ``target_lang.upper() == source_lang.upper()``，
    ``translate()`` 直接返回原文（不包 ``[LANG]`` tag），用以離線驗證 webhook
    下游的同語言 skip 流程。比較採單純 ``.upper()`` 相等，**不**引入 DeepL 專屬的
    ``_normalize_target_lang``——測試碼須使用能 ``.upper()`` 相等的語言碼
    （如 ``"JA"/"JA"`` 或 ``"ZH-HANT"/"ZH-HANT"``），不用 ``ZH-TW`` 對 ``ZH-HANT``。

    This is the default when no real translation backend is configured,
    and the canonical implementation to use in tests.

    Args:
        source_lang: 若指定，當 target 與其同語言時返回原文（skip）。
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
