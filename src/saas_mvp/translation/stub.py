"""Deterministic offline stub translator."""

from saas_mvp.translation.base import Translator


class StubTranslator(Translator):
    """Offline stub that wraps text with a language tag: '[LANG] original_text'.

    Guarantees:
    - Deterministic: same (text, target_lang) always → same output.
    - No network, no API key.
    - Always ``is_available() == True``.

    同語言 skip：建構子可選 ``source_lang``。若提供，且
    ``target_lang.upper() == source_lang.upper()``，translate() 回傳原文，
    用於離線測試 webhook 下游的 skip 流程。

    侷限：此處用單純 ``.upper()`` 相等比較，**不**套用 DeepL 的
    ``_normalize_target_lang`` 正規化；測試須使用能直接 .upper() 相等的語言碼
    （如 "JA"/"JA"、"ZH-HANT"/"ZH-HANT"），不要用 "ZH-TW" 對 "ZH-HANT"。

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
