"""AIAssistant abstract base class, result type, and shared exceptions."""

from abc import ABC, abstractmethod
from dataclasses import dataclass


class AIError(Exception):
    """Raised when an AI backend fails (network error, SDK error, etc.)."""


@dataclass(frozen=True, slots=True)
class AIResult:
    """AI answer plus which backend produced it ('stub' | 'claude-agent-sdk')."""

    answer: str
    source: str


class AIAssistant(ABC):
    """Abstract AI customer-service assistant interface.

    All backends (stub, Anthropic, …) must implement this. Callers depend only
    on this interface, not on any concrete class.
    """

    #: How many matched FAQ entries the caller should feed into ``context``.
    #: A real LLM backend can synthesise across several entries, so the default
    #: is generous; the offline stub只會原文呈現 context，多筆會變成「問一個列一堆」，
    #: 因此 stub 覆寫為 1（只回最相關那筆）。
    context_max_entries: int = 6

    @abstractmethod
    def answer(self, question: str, context: str = "") -> AIResult:
        """Answer *question*, optionally grounded in *context* (FAQ + shop info).

        Args:
            question: The customer's question text.
            context: Optional supporting context (matched FAQ entries, shop
                facts) used to ground the answer.

        Returns:
            :class:`AIResult` with the answer text and the backend ``source``.

        Raises:
            AIError: if the backend is unavailable or returns an error.
        """

    @abstractmethod
    def is_available(self) -> bool:
        """Return True if this assistant has a working backend configured."""
