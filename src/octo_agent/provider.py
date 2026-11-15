from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol

from octo_agent.messages import AgentMessage
from octo_agent.provider_events import AssistantMessageEvent
from octo_agent.tools import AgentTool


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
