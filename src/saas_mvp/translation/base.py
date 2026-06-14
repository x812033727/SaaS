"""Translator abstract base class and shared exceptions."""

from abc import ABC, abstractmethod


class TranslationError(Exception):
    """Raised when a translation backend fails (network error, API error, etc.)."""


class Translator(ABC):
    """Abstract translator interface.

    All backends (stub, DeepL, …) must implement this.
    Callers depend only on this interface, not on any concrete class.
    """

    @abstractmethod
    def translate(self, text: str, target_lang: str) -> str:
        """Translate *text* to *target_lang* (e.g. 'ja', 'en', 'zh-TW').

        Args:
            text: Source text to translate.
            target_lang: BCP-47 language tag or API-specific code (e.g. 'JA', 'ZH-TW').

        Returns:
            Translated string.

        Raises:
            TranslationError: if the backend is unavailable or returns an error.
        """

    @abstractmethod
    def is_available(self) -> bool:
        """Return True if this translator has a working backend configured."""
