"""AI 預約 agent（A2 建單 + D1 意圖擴充 + D2 tool loop + D3 歷史）。

設計鐵則:**AI 只理解/填槽**(intent + service_id / date / party_size /
reservation_id),mutation 永遠走既有 postback 確認 → 服務層確定性路徑
(含擁有者驗證);LLM 幻覺不可能直接改資料。

* ``AnthropicAgent``(D2):最多 3 輪 tool-use loop — 4 個唯讀查詢 tool
  (services/dates/slots/my_reservations)+ 終結 tool ``propose_action``;
  第 3 輪強制 propose_action 保證收斂。內部 loop 不加計額度(每則用戶
  訊息仍只扣 1),max_tokens=512/輪、tool result 截 800 字,成本可預算。
* ``StubAgent``:關鍵字/正則規則,離線決定性,測試與未設 API key 時用。
* 歷史(D3):converse 接受 history=[(role, text)],只有 AnthropicAgent 用。
"""

from __future__ import annotations

import dataclasses
import re

from saas_mvp.ai.base import AIError

VALID_INTENTS = ("book", "reschedule", "cancel", "query", "other")

_MAX_TOOL_ROUNDS = 3
_TOOL_RESULT_MAX_CHARS = 800


@dataclasses.dataclass
class AgentTurn:
    """單輪理解結果。reply_text 僅在無槽位更新時作為回覆素材。"""

    reply_text: str = ""
    intent: str = "book"
    service_id: int | None = None
    date: str | None = None          # 'YYYY-MM-DD'
    party_size: int | None = None
    reservation_id: int | None = None


@dataclasses.dataclass
class ToolBelt:
    """唯讀查詢工具(D2)— 由 ai_conversation 以 closure 綁 db/tenant/user。"""

    list_services: object = None       # () -> str
    available_dates: object = None     # () -> str
    available_slots: object = None     # (date, service_id|None) -> str
    my_reservations: object = None     # () -> str


class AIAgent:
    """介面:converse(text, slots, context, *, tools=None, history=None)。"""

    def converse(
        self, text: str, slots: dict, context: str, *,
        tools: ToolBelt | None = None,
        history: list | None = None,
    ) -> AgentTurn:
        raise NotImplementedError

    def is_available(self) -> bool:
        return True


_PROPOSE_TOOL = {
    "name": "propose_action",
    "description": (
        "輸出對顧客訊息的最終理解:意圖與槽位。沒把握的欄位一律留空。"
        "這是終結工具 — 呼叫後對話輪結束。"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "reply": {
                "type": "string",
                "description": "給顧客的親切繁中回覆(一兩句;若還缺資訊就追問)",
            },
            "intent": {
                "type": "string",
                "enum": list(VALID_INTENTS),
                "description": "book=想預約 reschedule=想改期 cancel=想取消 "
                               "query=查自己的預約 other=其他",
            },
            "service_id": {
                "type": "integer",
                "description": "顧客指名的服務 id(必須出自店家服務清單)",
            },
            "date": {
                "type": "string",
                "description": "顧客想要的日期 YYYY-MM-DD(必須出自可預約日期)",
            },
            "party_size": {"type": "integer", "description": "人數 1-6"},
            "reservation_id": {
                "type": "integer",
                "description": "改期/取消目標的預約編號(必須出自顧客現有預約清單)",
            },
        },
        "required": ["reply", "intent"],
    },
}

_QUERY_TOOLS = [
    {
        "name": "list_services",
        "description": "查店家上架服務清單(id/名稱/時長/價格)。",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "available_dates",
        "description": "查近期可預約日期清單。",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "available_slots",
        "description": "查某日期的可預約時段。",
        "input_schema": {
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "YYYY-MM-DD"},
                "service_id": {"type": "integer"},
            },
            "required": ["date"],
        },
    },
    {
        "name": "my_reservations",
        "description": "查這位顧客現有的預約(編號/時間)。",
        "input_schema": {"type": "object", "properties": {}},
    },
]

_AGENT_SYSTEM = (
    "你是店家的 LINE 預約助理。理解顧客訊息的意圖(預約/改期/取消/查詢),"
    "需要資料時先用查詢工具,最後**必須**呼叫 propose_action 輸出結構化結果。"
    "已知槽位:{slots}。規則:service_id 必須出自服務清單、date 必須可預約、"
    "reservation_id 必須出自顧客現有預約;沒把握留空,不要編造;"
    "reply 用親切繁中,缺什麼就追問什麼。\n\n店家資訊:\n{context}"
)


def _run_tool(tools, name: str, args: dict) -> str:
    """執行唯讀工具;無 belt/未綁 → 回固定訊息。結果截長。"""
    fn = getattr(tools, name, None) if tools else None
    if fn is None:
        return "(此工具目前不可用)"
    try:
        if name == "available_slots":
            out = fn(args.get("date") or "", args.get("service_id"))
        else:
            out = fn()
        return str(out)[:_TOOL_RESULT_MAX_CHARS]
    except Exception as exc:  # noqa: BLE001 — 工具失敗回文字,不炸 loop
        return f"(查詢失敗:{type(exc).__name__})"


def _turn_from(data: dict) -> AgentTurn:
    intent = data.get("intent")
    return AgentTurn(
        reply_text=str(data.get("reply") or ""),
        intent=intent if intent in VALID_INTENTS else "other",
        service_id=_opt_int(data.get("service_id")),
        date=_valid_date(data.get("date")),
        party_size=_opt_int(data.get("party_size")),
        reservation_id=_opt_int(data.get("reservation_id")),
    )


class AnthropicAgent(AIAgent):
    """Claude tool-use loop(D2):≤3 輪,末輪強制 propose_action 收斂。"""

    def __init__(self, *, client_factory=None) -> None:
        from saas_mvp.config import settings

        self._api_key = settings.anthropic_api_key
        self._model = settings.ai_model
        self._client_factory = client_factory  # 測試注入 fake client

    def is_available(self) -> bool:
        return bool(self._api_key)

    def _client(self):
        if self._client_factory is not None:
            return self._client_factory()
        import anthropic

        return anthropic.Anthropic(api_key=self._api_key)

    def converse(
        self, text: str, slots: dict, context: str, *,
        tools=None,
        history: list | None = None,
    ) -> AgentTurn:
        # D3:歷史(role 交錯合法化 — 連續同角色合併)。
        messages: list[dict] = []
        for role, content in (history or [])[-8:]:
            role = "user" if role == "user" else "assistant"
            snippet = str(content)[:200]
            if messages and messages[-1]["role"] == role:
                messages[-1]["content"] += "\n" + snippet
            else:
                messages.append({"role": role, "content": snippet})
        if messages and messages[0]["role"] == "assistant":
            messages.insert(0, {"role": "user", "content": "(先前對話)"})
        if messages and messages[-1]["role"] == "user":
            messages[-1]["content"] += "\n" + text[:1000]
        else:
            messages.append({"role": "user", "content": text[:1000]})

        system = _AGENT_SYSTEM.format(slots=slots or "（無）", context=context)
        try:
            client = self._client()
            for round_no in range(_MAX_TOOL_ROUNDS):
                final_round = round_no == _MAX_TOOL_ROUNDS - 1
                resp = client.messages.create(
                    model=self._model,
                    max_tokens=512,
                    system=system,
                    tools=[_PROPOSE_TOOL] if final_round
                    else [_PROPOSE_TOOL, *_QUERY_TOOLS],
                    tool_choice=(
                        {"type": "tool", "name": "propose_action"}
                        if final_round else {"type": "any"}
                    ),
                    messages=messages,
                )
                tool_uses = [
                    b for b in resp.content
                    if getattr(b, "type", None) == "tool_use"
                ]
                if not tool_uses:
                    texts = "".join(
                        b.text for b in resp.content
                        if getattr(b, "type", None) == "text"
                    )
                    return AgentTurn(reply_text=texts or "", intent="other")

                proposal = next(
                    (b for b in tool_uses if b.name == "propose_action"), None
                )
                if proposal is not None:
                    return _turn_from(proposal.input or {})

                messages.append({"role": "assistant", "content": resp.content})
                results = [
                    {
                        "type": "tool_result",
                        "tool_use_id": b.id,
                        "content": _run_tool(tools, b.name, b.input or {}),
                    }
                    for b in tool_uses
                ]
                messages.append({"role": "user", "content": results})
            return AgentTurn(intent="other")  # pragma: no cover — 末輪強制收斂
        except AIError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise AIError(f"Anthropic agent request failed: {exc}") from exc


class StubAgent(AIAgent):
    """離線規則 agent(全意圖):服務名關鍵字、YYYY-MM-DD、「N 位/人」、
    取消/改期/查詢關鍵字 + #編號。context 服務行形如「・id=3 剪髮 …」。"""

    def converse(
        self, text: str, slots: dict, context: str, *,
        tools=None,
        history: list | None = None,
    ) -> AgentTurn:
        turn = AgentTurn(reply_text="好的，請問還有什麼需求？")

        if re.search(r"取消", text):
            turn.intent = "cancel"
        elif re.search(r"改期|改時間|換時間", text):
            turn.intent = "reschedule"
        elif re.search(r"查詢|我的預約|查一下", text):
            turn.intent = "query"

        if turn.intent in ("cancel", "reschedule"):
            rid = re.search(r"#?(\d+)", text)
            if rid:
                turn.reservation_id = int(rid.group(1))

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
    """依 settings 選實作:有 SAAS_ANTHROPIC_API_KEY 走 Claude,否則 Stub。"""
    from saas_mvp.config import settings

    if settings.anthropic_api_key:
        return AnthropicAgent()
    return StubAgent()
