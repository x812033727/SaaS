"""Translation package.

Public API::

    from saas_mvp.translation import (
        Translator,          # abstract base class
        TranslationResult,   # output object returned by translate()
        TranslationError,    # raised when translation fails
        StubTranslator,      # deterministic offline stub
        DeepLTranslator,     # real HTTP backend (DeepL-compatible)
        get_translator,      # factory: returns configured backend or stub
        parse_lang_command,  # parse /lang <code> [text] from LINE messages
    )
"""

from saas_mvp.translation.base import TranslationResult, Translator, TranslationError
from saas_mvp.translation.commands import parse_lang_command
from saas_mvp.translation.http import DeepLTranslator
from saas_mvp.translation.stub import StubTranslator


def get_translator() -> Translator:
    """Return a ``Translator`` instance based on current settings.

    Selection logic (first match wins):

    1. ``SAAS_DEEPL_API_KEY`` is set  → :class:`DeepLTranslator` (real HTTP backend)
    2. Otherwise                       → :class:`StubTranslator` (offline, always safe)

    Callers never need to know which concrete backend was returned;
    both satisfy the same :class:`Translator` interface.
    """
    from saas_mvp.config import settings  # lazy import — avoids circular import at module load

    if settings.deepl_api_key:
        return DeepLTranslator(
            api_key=settings.deepl_api_key,
            api_url=settings.deepl_api_url,
        )
    return StubTranslator()


__all__ = [
    "Translator",
    "TranslationResult",
    "TranslationError",
    "StubTranslator",
    "DeepLTranslator",
    "get_translator",
    "parse_lang_command",
]
