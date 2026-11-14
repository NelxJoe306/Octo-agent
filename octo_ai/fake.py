from __future__ import annotations

from collections.abc import AsyncIterator, Iterable

from octo_agent.messages import AgentMessage
from octo_agent.tools import AgentTool
from octo_ai.model_limits import DEFAULT_REQUEST_OUTPUT_TOKENS, RequestTokenBudget
from octo_ai.events import AssistantMessageEvent
from octo_ai.provider import CancellationToken


class FakeProvider:


    def __init__(self, streams: Iterable[Iterable[AssistantMessageEvent]]) -> None:
        self._streams = [list(stream) for stream in streams]
        self.calls: list[tuple[str, str, list[AgentMessage], list[AgentTool]]] = []
        self.session_ids: list[str | None] = []

    def request_token_budget(self) -> RequestTokenBudget:
        return RequestTokenBudget(DEFAULT_REQUEST_OUTPUT_TOKENS)

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
        self.calls.append((model, system, list(messages), list(tools)))
        self.session_ids.append(session_id)
        stream = self._streams.pop(0) if self._streams else []

        async def iterator() -> AsyncIterator[AssistantMessageEvent]:
            for event in stream:
                if signal is not None and signal.is_cancelled():
                    return
                yield event

        return iterator()
