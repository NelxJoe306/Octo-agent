from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Protocol

from octo_agent.context_budget import ContextBudgetDecision
from octo_agent.tools import AgentTool
from octo_agent.messages import AgentMessage, AssistantMessage, ToolResultMessage
from octo_agent.provider import CancellationToken
from octo_agent.tool_history import repair_tool_history

class CheckContext(Protocol):
    async def __call__(
        self, *, model: str, system: str, messages: Sequence[AgentMessage],
        tools: Sequence[AgentTool],
    ) -> ContextBudgetDecision: ...


class PrepareContext(Protocol):
    async def __call__(
        self,
        messages: tuple[AgentMessage, ...],
        *,
        signal: CancellationToken | None,
    ) -> Sequence[AgentMessage]: ...


async def prepare_model_context(
    messages: Sequence[AgentMessage],
    prepare: PrepareContext,
    *,
    signal: CancellationToken | None = None,
) -> list[AgentMessage]:
    if signal is not None and signal.is_cancelled():
        raise asyncio.CancelledError
    snapshot = tuple(message.model_copy(deep=True) for message in messages)
    projected = await prepare(snapshot, signal=signal)
    if signal is not None and signal.is_cancelled():
        raise asyncio.CancelledError
    prepared = tuple(message.model_copy(deep=True) for message in projected)
    validate_model_context(prepared)
    return list(prepared)


def validate_model_context(prepared: Sequence[AgentMessage]) -> None:
    if repair_tool_history(tuple(prepared)).changed:
        raise ValueError("Prepared context contains incomplete or reordered tool history")
    tool_names: dict[str, str] = {}
    for message in prepared:
        if isinstance(message, AssistantMessage):
            tool_names.update((call.id, call.name) for call in message.tool_calls)
        elif isinstance(message, ToolResultMessage):
            if tool_names.get(message.tool_call_id) != message.tool_name:
                raise ValueError("Prepared context contains mismatched tool history names")
