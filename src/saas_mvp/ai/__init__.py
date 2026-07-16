"""AI customer-service package (PHASE 4-1, MiniMax M3).

Mirrors the ``translation`` package shape: an ABC (:class:`AIAssistant`), a
deterministic offline :class:`StubAIAssistant`, a lazy-import real backend
(:class:`MiniMaxAssistant`), and a :func:`get_assistant` factory.

Public API::

    from saas_mvp.ai import (
        AIAssistant,         # abstract base class
        AIError,             # raised when the backend fails
        AIResult,            # answer + source
        StubAIAssistant,     # deterministic offline stub
        MiniMaxAssistant,  # real MiniMax backend (lazy openai import)
        get_assistant,       # factory: returns configured backend or stub
    )
"""

from saas_mvp.ai.minimax_client import MiniMaxAssistant
from saas_mvp.ai.base import AIAssistant, AIError, AIResult
from saas_mvp.ai.stub import StubAIAssistant


def get_assistant(db=None) -> AIAssistant:
    """Return an ``AIAssistant`` instance based on current settings.

    Selection logic (first match wins):

    1. 後台資料庫或 ``SAAS_MINIMAX_API_KEY`` 有設定 → MiniMax-backed SDK
    2. 否則 → :class:`StubAIAssistant`（離線、安全退化）

    Mirrors :func:`saas_mvp.translation.get_translator`. Callers never need to
    know which concrete backend was returned; both satisfy :class:`AIAssistant`.
    """
    from saas_mvp.config import settings  # lazy — avoid circular import at load
    from saas_mvp.services.platform_ai_config import effective_ai_config

    config = effective_ai_config(db, settings)
    if config is not None:
        return MiniMaxAssistant(
            api_key=config.api_key, base_url=config.base_url, model=config.model
        )
    return StubAIAssistant()


__all__ = [
    "AIAssistant",
    "AIError",
    "AIResult",
    "StubAIAssistant",
    "MiniMaxAssistant",
    "get_assistant",
]
