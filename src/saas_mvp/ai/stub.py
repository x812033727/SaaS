"""Deterministic offline stub AI assistant.

This is the default when no MiniMax API key is configured, and the canonical
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

    # 沒有真 LLM 可綜合，stub 只把 context 原文回給顧客，因此只取最相關 1 筆 FAQ，
    # 避免「問一個問題卻列出一整排 FAQ」。
    context_max_entries: int = 1

    def answer(self, question: str, context: str = "") -> AIResult:
        if context and context.strip():
            return AIResult(answer=context.strip(), source="stub")
        return AIResult(
            answer=f"您好，關於「{question}」我們會盡快為您說明。",
            source="stub",
        )

    def is_available(self) -> bool:
        return True
