"""Deterministic offline stub AI assistant.

This is the default when no Anthropic API key is configured, and the canonical
implementation to use in tests (never calls the real API).
"""

from __future__ import annotations

from saas_mvp.ai.base import AIResult, AIAssistant


class StubAIAssistant(AIAssistant):
    """Offline deterministic assistant.

    Guarantees:
    - Deterministic: same (question, context) always → same output.
    - No network, no API key.
    - Always ``is_available() == True``.

    Behaviour:
    - If *context* is non-empty (e.g. matched FAQ text), echo it back as the
      answer — so the LINE auto-responder surfaces the店家 FAQ verbatim.
    - Otherwise echo the question with a canned acknowledgement.
    """

    def answer(self, question: str, context: str = "") -> AIResult:
        if context and context.strip():
            return AIResult(answer=context.strip(), source="stub")
        return AIResult(
            answer=f"您好，關於「{question}」我們會盡快為您說明。",
            source="stub",
        )

    def is_available(self) -> bool:
        return True
