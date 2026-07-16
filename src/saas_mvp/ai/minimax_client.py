"""Direct MiniMax API backend for the AI customer-service assistant."""

from __future__ import annotations

from saas_mvp.ai.base import AIError, AIResult, AIAssistant

# 系統提示模板：把店家 FAQ / 資訊（context）注入為 MiniMax 的 system prompt，
# 讓回答貼合該店家知識，且僅依據已知資訊作答。
_SYSTEM_PROMPT = (
    "你是店家的 AI 客服助理，請用親切的繁體中文簡潔回答顧客問題。"
    "優先根據以下店家資訊作答；若資訊不足，請禮貌建議顧客聯繫店家。\n\n"
    "店家資訊：\n{context}"
)


class MiniMaxAssistant(AIAssistant):
    """AI assistant backed by the direct MiniMax API.

    Configured from settings (``SAAS_MINIMAX_API_KEY`` / ``SAAS_AI_MODEL``).
    The ``openai`` package is imported lazily; if absent, instantiation still
    succeeds but ``answer()`` raises :class:`AIError` on use.
    """

    def __init__(
        self, *, api_key: str | None = None, base_url: str | None = None,
        model: str | None = None, runner=None
    ) -> None:
        from saas_mvp.config import settings  # lazy — avoid circular import

        self._api_key = api_key if api_key is not None else settings.minimax_api_key
        self._base_url = base_url if base_url is not None else settings.minimax_base_url
        self._model = model if model is not None else settings.ai_model
        self._runner = runner

    def is_available(self) -> bool:
        return bool(self._api_key)

    def answer(self, question: str, context: str = "") -> AIResult:
        system_prompt = _SYSTEM_PROMPT.format(context=context or "（無）")
        try:
            from saas_mvp.ai.minimax_api import text_query

            runner = self._runner or text_query
            text = runner(
                prompt=question[:4000],
                system_prompt=system_prompt,
                api_key=self._api_key,
                base_url=self._base_url,
                model=self._model,
                max_turns=1,
            )
        except AIError:
            raise
        except Exception as exc:  # noqa: BLE001 - wrap any SDK/network error
            raise AIError(f"MiniMax API request failed: {exc}") from exc

        return AIResult(answer=text, source="minimax-api")
