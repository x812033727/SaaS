"""AI 預約 agent（A2.2）— 自然語言 → 槽位抽取。

設計鐵則：**AI 只填槽**（service_id / date / party_size），寫入永遠走既有
postback 確認 → book_slot 確定性路徑；LLM 幻覺不可能直接產生訂單。

* ``AnthropicAgent``：單次 Messages API 呼叫 + 強制 tool（propose_booking）
  結構化輸出；抽取值由伺服器端驗證（服務存在/日期可約）後才採納。
  刻意不做多輪 tool-use loop：一次抽取 + 確定性推進已覆蓋主要場景，
  成本可控（每輪恰一次 LLM 呼叫）。
* ``StubAgent``：關鍵字/正則規則，離線決定性，測試與未設 API key 時用。
"""

from __future__ import annotations

import dataclasses
import re

from saas_mvp.ai.base import AIError


@dataclasses.dataclass
class AgentTurn:
    """單輪抽取結果。reply_text 僅在無槽位更新時作為回覆素材。"""

    reply_text: str = ""
    service_id: int | None = None
    date: str | None = None          # 'YYYY-MM-DD'
    party_size: int | None = None


class AIAgent:
    """介面：converse(text, slots, context) -> AgentTurn。"""

    def converse(self, text: str, slots: dict, context: str) -> AgentTurn:
        raise NotImplementedError

    def is_available(self) -> bool:
        return True


_TOOL = {
    "name": "propose_booking",
    "description": "從顧客訊息抽取預約意圖與槽位；沒把握的欄位一律留空。",
    "input_schema": {
        "type": "object",
        "properties": {
            "reply": {
                "type": "string",
                "description": "給顧客的親切繁中回覆（一兩句；若還缺資訊就追問）",
            },
            "service_id": {
                "type": "integer",
                "description": "顧客指名的服務 id（必須出自店家服務清單）",
            },
            "date": {
                "type": "string",
                "description": "顧客想要的日期 YYYY-MM-DD（必須出自可預約日期清單）",
            },
            "party_size": {"type": "integer", "description": "人數 1-6"},
        },
        "required": ["reply"],
    },
}

_AGENT_SYSTEM = (
    "你是店家的 LINE 預約助理。根據顧客訊息與下方店家資訊，呼叫 propose_booking "
    "抽取預約槽位：service_id（服務清單內的 id）、date（可預約日期清單內的 "
    "YYYY-MM-DD）、party_size（1-6）。已知槽位：{slots}。"
    "沒把握的欄位留空，不要編造；reply 用親切繁中，缺什麼就追問什麼。\n\n"
    "店家資訊：\n{context}"
)


class AnthropicAgent(AIAgent):
    """Claude 槽位抽取（單呼叫 + 強制 tool_choice）。"""

    def __init__(self) -> None:
        from saas_mvp.config import settings

        self._api_key = settings.anthropic_api_key
        self._model = settings.ai_model

    def is_available(self) -> bool:
        return bool(self._api_key)

    def converse(self, text: str, slots: dict, context: str) -> AgentTurn:
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover
            raise AIError("anthropic package is not installed") from exc

        try:
            client = anthropic.Anthropic(api_key=self._api_key)
            resp = client.messages.create(
                model=self._model,
                max_tokens=512,
                system=_AGENT_SYSTEM.format(slots=slots or "（無）", context=context),
                tools=[_TOOL],
                tool_choice={"type": "tool", "name": "propose_booking"},
                messages=[{"role": "user", "content": text[:1000]}],
            )
            data: dict = {}
            for block in resp.content:
                if getattr(block, "type", None) == "tool_use":
                    data = block.input or {}
                    break
        except AIError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise AIError(f"Anthropic agent request failed: {exc}") from exc

        return AgentTurn(
            reply_text=str(data.get("reply") or ""),
            service_id=_opt_int(data.get("service_id")),
            date=_valid_date(data.get("date")),
            party_size=_opt_int(data.get("party_size")),
        )


class StubAgent(AIAgent):
    """離線規則 agent：服務名關鍵字、YYYY-MM-DD、「N 位/人」。測試/無 key 用。

    context 中的服務清單行形如「・id=3 剪髮 60分鐘 NT$800」— 以此比對關鍵字。
    """

    def converse(self, text: str, slots: dict, context: str) -> AgentTurn:
        turn = AgentTurn(reply_text="好的，請問還有什麼需求？")

        for m in re.finditer(r"・id=(\d+) (\S+)", context):
            sid, name = int(m.group(1)), m.group(2)
            if name and name in text:
                turn.service_id = sid
                break

        d = re.search(r"\d{4}-\d{2}-\d{2}", text)
        if d:
            turn.date = d.group(0)

        p = re.search(r"(\d)\s*[位人]", text)
        if p:
            turn.party_size = int(p.group(1))

        return turn


def _opt_int(v) -> int | None:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _valid_date(v) -> str | None:
    if isinstance(v, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
        return v
    return None


def get_agent() -> AIAgent:
    """依 settings 選實作：有 SAAS_ANTHROPIC_API_KEY 走 Claude，否則 Stub。"""
    from saas_mvp.config import settings

    if settings.anthropic_api_key:
        return AnthropicAgent()
    return StubAgent()
