"""AI customer-service package (PHASE 4-1, Anthropic Claude).

Mirrors the ``translation`` package shape: an ABC (:class:`AIAssistant`), a
deterministic offline :class:`StubAIAssistant`, a lazy-import real backend
(:class:`AnthropicAssistant`), and a :func:`get_assistant` factory.

Public API::

    from saas_mvp.ai import (
        AIAssistant,         # abstract base class
        AIError,             # raised when the backend fails
        AIResult,            # answer + source
        StubAIAssistant,     # deterministic offline stub
        AnthropicAssistant,  # real Claude backend (lazy anthropic import)
        get_assistant,       # factory: returns configured backend or stub
    )
"""

from saas_mvp.ai.anthropic_client import AnthropicAssistant
from saas_mvp.ai.base import AIAssistant, AIError, AIResult
from saas_mvp.ai.stub import StubAIAssistant


def get_assistant() -> AIAssistant:
    """Return an ``AIAssistant`` instance based on current settings.

    Selection logic (first match wins):

    1. ``SAAS_ANTHROPIC_API_KEY`` is set → :class:`AnthropicAssistant` (real Claude)
    2. Otherwise                          → :class:`StubAIAssistant` (offline, safe)

    Mirrors :func:`saas_mvp.translation.get_translator`. Callers never need to
    know which concrete backend was returned; both satisfy :class:`AIAssistant`.
    """
    from saas_mvp.config import settings  # lazy — avoid circular import at load

    if settings.anthropic_api_key:
        return AnthropicAssistant()
    return StubAIAssistant()


__all__ = [
    "AIAssistant",
    "AIError",
    "AIResult",
    "StubAIAssistant",
    "AnthropicAssistant",
    "get_assistant",
]
