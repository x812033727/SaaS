"""Real Anthropic Claude backend for the AI customer-service assistant.

The ``anthropic`` package is imported **lazily** (inside ``__init__`` / ``answer``)
so this module imports cleanly even when the package is not installed — the stub
path keeps tests working offline. Any SDK/network error is wrapped in
:class:`AIError`.
"""

from __future__ import annotations

from saas_mvp.ai.base import AIError, AIResult, AIAssistant

# 系統提示模板：把店家 FAQ / 資訊（context）注入為 Claude 的 system prompt，
# 讓回答貼合該店家知識，且僅依據已知資訊作答。
_SYSTEM_PROMPT = (
    "你是店家的 AI 客服助理，請用親切的繁體中文簡潔回答顧客問題。"
    "優先根據以下店家資訊作答；若資訊不足，請禮貌建議顧客聯繫店家。\n\n"
    "店家資訊：\n{context}"
)


class AnthropicAssistant(AIAssistant):
    """AI assistant backed by Anthropic's Claude (Messages API).

    Configured from settings (``SAAS_ANTHROPIC_API_KEY`` / ``SAAS_AI_MODEL``).
    The ``anthropic`` package is imported lazily; if absent, instantiation still
    succeeds but ``answer()`` raises :class:`AIError` on use.
    """

    def __init__(self) -> None:
        from saas_mvp.config import settings  # lazy — avoid circular import

        self._api_key = settings.anthropic_api_key
        self._model = settings.ai_model

    def is_available(self) -> bool:
        return bool(self._api_key)

    def answer(self, question: str, context: str = "") -> AIResult:
        try:
            import anthropic  # lazy import — module loads even without the package
        except ImportError as exc:  # pragma: no cover - exercised only without pkg
            raise AIError(
                "anthropic package is not installed"
            ) from exc

        system_prompt = _SYSTEM_PROMPT.format(context=context or "（無）")
        try:
            client = anthropic.Anthropic(api_key=self._api_key)
            resp = client.messages.create(
                model=self._model,
                max_tokens=1024,
                system=system_prompt,
                messages=[{"role": "user", "content": question}],
            )
            # Concatenate text from all text-type content blocks.
            text = "".join(
                block.text
                for block in resp.content
                if getattr(block, "type", None) == "text"
            )
        except AIError:
            raise
        except Exception as exc:  # noqa: BLE001 - wrap any SDK/network error
            raise AIError(f"Anthropic request failed: {exc}") from exc

        return AIResult(answer=text, source="anthropic")
