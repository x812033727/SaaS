"""Small synchronous facade over the async Claude Agent SDK.

The web application invokes AI from synchronous FastAPI handlers.  This module
keeps that public contract while all real Claude traffic goes through the
ClaudeSDKClient and its bundled Claude Code CLI.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import partial
from typing import Any

import anyio


def _sdk_env(api_key: str) -> dict[str, str]:
    # Passing the secret through options keeps database overrides request-local;
    # never mutate os.environ in a multi-tenant web process.
    return {"ANTHROPIC_API_KEY": api_key}


async def _text_query_async(
    *, prompt: str, system_prompt: str, api_key: str, model: str, max_turns: int = 1
) -> str:
    from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient
    from claude_agent_sdk.types import AssistantMessage, ResultMessage, TextBlock

    options = ClaudeAgentOptions(
        model=model,
        system_prompt=system_prompt,
        env=_sdk_env(api_key),
        tools=[],
        max_turns=max_turns,
        max_budget_usd=0.05,
        setting_sources=[],
    )
    chunks: list[str] = []
    async with ClaudeSDKClient(options=options) as client:
        await client.query(prompt)
        async for message in client.receive_response():
            if isinstance(message, AssistantMessage):
                chunks.extend(
                    block.text
                    for block in message.content
                    if isinstance(block, TextBlock) and block.text
                )
            elif isinstance(message, ResultMessage) and message.is_error:
                raise RuntimeError(message.result or "Claude Agent SDK request failed")
    return "".join(chunks).strip()


def text_query(
    *, prompt: str, system_prompt: str, api_key: str, model: str, max_turns: int = 1
) -> str:
    return anyio.run(partial(
        _text_query_async,
        prompt=prompt,
        system_prompt=system_prompt,
        api_key=api_key,
        model=model,
        max_turns=max_turns,
    ))


async def _booking_query_async(
    *,
    prompt: str,
    system_prompt: str,
    api_key: str,
    model: str,
    tool_dispatch: dict[str, Callable[[dict], str]],
    output_schema: dict[str, Any],
) -> dict[str, Any]:
    from claude_agent_sdk import (
        ClaudeAgentOptions,
        ClaudeSDKClient,
        create_sdk_mcp_server,
        tool,
    )
    from claude_agent_sdk.types import ResultMessage

    sdk_tools = []
    for name, fn in tool_dispatch.items():
        if name == "available_slots":
            schema = {
                "type": "object",
                "properties": {
                    "date": {"type": "string"},
                    "service_id": {"type": "integer"},
                },
                "required": ["date"],
            }
        else:
            schema = {"type": "object", "properties": {}}

        def make_handler(handler):
            async def invoke(args):
                return {"content": [{"type": "text", "text": handler(args)}]}

            return invoke

        sdk_tools.append(tool(name, f"唯讀預約查詢：{name}", schema)(make_handler(fn)))

    server = create_sdk_mcp_server(name="booking", version="1.0.0", tools=sdk_tools)
    allowed = [f"mcp__booking__{name}" for name in tool_dispatch]
    options = ClaudeAgentOptions(
        model=model,
        system_prompt=system_prompt,
        env=_sdk_env(api_key),
        tools=[],
        mcp_servers={"booking": server},
        allowed_tools=allowed,
        permission_mode="bypassPermissions",
        max_turns=3,
        max_budget_usd=0.08,
        output_format={"type": "json_schema", "schema": output_schema},
        setting_sources=[],
    )
    async with ClaudeSDKClient(options=options) as client:
        await client.query(prompt)
        async for message in client.receive_response():
            if isinstance(message, ResultMessage):
                if message.is_error:
                    raise RuntimeError(message.result or "Claude booking agent failed")
                if isinstance(message.structured_output, dict):
                    return message.structured_output
    raise RuntimeError("Claude booking agent returned no structured output")


def booking_query(**kwargs) -> dict[str, Any]:
    return anyio.run(partial(_booking_query_async, **kwargs))
