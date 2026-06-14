"""Deterministic offline stub translator."""

from saas_mvp.translation.base import Translator


class StubTranslator(Translator):
    """Offline stub that wraps text with a language tag: '[LANG] original_text'.

    Guarantees:
    - Deterministic: same (text, target_lang) always → same output.
    - No network, no API key.
    - Always ``is_available() == True``.

    This is the default when no real translation backend is configured,
    and the canonical implementation to use in tests.
    """

    def translate(self, text: str, target_lang: str) -> str:
        return f"[{target_lang.upper()}] {text}"

    def is_available(self) -> bool:
        return True
