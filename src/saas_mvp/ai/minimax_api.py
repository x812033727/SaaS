"""Direct MiniMax OpenAI-compatible API adapter."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any


def _client(*, api_key: str, base_url: str):
    from openai import OpenAI

    return OpenAI(api_key=api_key, base_url=base_url, timeout=60.0, max_retries=2)


def text_query(
    *, prompt: str, system_prompt: str, api_key: str, base_url: str,
    model: str, max_turns: int = 1,
) -> str:
    del max_turns
    response = _client(api_key=api_key, base_url=base_url).chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ],
        max_completion_tokens=1024,
        extra_body={"thinking": {"type": "disabled"}, "reasoning_split": True},
    )
    return (response.choices[0].message.content or "").strip()


def _function_tool(name: str, schema: dict[str, Any], description: str) -> dict:
    return {
        "type": "function",
        "function": {"name": name, "description": description, "parameters": schema},
    }


def booking_query(
    *, prompt: str, system_prompt: str, api_key: str, base_url: str,
    model: str, tool_dispatch: dict[str, Callable[[dict], str]],
    output_schema: dict[str, Any],
) -> dict[str, Any]:
    client = _client(api_key=api_key, base_url=base_url)
    query_tools = []
    for name in tool_dispatch:
        schema = ({
            "type": "object",
            "properties": {"date": {"type": "string"}, "service_id": {"type": "integer"}},
            "required": ["date"],
        } if name == "available_slots" else {"type": "object", "properties": {}})
        query_tools.append(_function_tool(name, schema, f"唯讀預約查詢：{name}"))
    proposal = _function_tool("propose_action", output_schema, "回傳給顧客的結構化結果")
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": prompt},
    ]
    for round_no in range(3):
        final = round_no == 2
        response = client.chat.completions.create(
            model=model,
            messages=messages,
            tools=[proposal] if final else [proposal, *query_tools],
            tool_choice={"type": "function", "function": {"name": "propose_action"}} if final else "required",
            max_completion_tokens=1024,
            extra_body={"thinking": {"type": "adaptive"}, "reasoning_split": True},
        )
        message = response.choices[0].message
        messages.append(message.model_dump(exclude_none=True))
        for call in message.tool_calls or []:
            args = json.loads(call.function.arguments or "{}")
            if call.function.name == "propose_action":
                return args
            handler = tool_dispatch.get(call.function.name)
            result = handler(args) if handler else "(此工具目前不可用)"
            messages.append({"role": "tool", "tool_call_id": call.id, "content": result})
    raise RuntimeError("MiniMax booking agent returned no structured output")
