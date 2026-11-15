from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol

from agent_core.messages import AgentMessage
from agent_core.provider_events import AssistantMessageEvent
from agent_core.tools import AgentTool


class CancellationToken(Protocol):
    def is_cancelled(self) -> bool:

        ...


class ModelProvider(Protocol):


    def stream_response(
        self,
        *,
        model: str,
        system: str,
        messages: list[AgentMessage],
        tools: list[AgentTool],
        signal: CancellationToken | None = None,
        session_id: str | None = None,
    ) -> AsyncIterator[AssistantMessageEvent]:

        ...
