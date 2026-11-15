from __future__ import annotations

from collections.abc import AsyncIterator, Iterable

from agent_core.messages import AgentMessage
from agent_core.tools import AgentTool
from model_adapters.model_limits import DEFAULT_REQUEST_OUTPUT_TOKENS, RequestTokenBudget
from model_adapters.events import AssistantMessageEvent
from model_adapters.provider import CancellationToken


class FakeProvider:


    def __init__(self, streams: Iterable[Iterable[AssistantMessageEvent]]) -> None:
        self._request_output_tokens = DEFAULT_REQUEST_OUTPUT_TOKENS
        self._streams = [list(stream) for stream in streams]
        self.calls: list[tuple[str, str, list[AgentMessage], list[AgentTool]]] = []
        self.session_ids: list[str | None] = []

    def request_token_budget(self) -> RequestTokenBudget:
        return RequestTokenBudget(self._request_output_tokens)

    def configure_request_budget(
        self, *, context_window: int, safety_margin: int,
        output_override: int | None = None, model_max_output_tokens: int | None = None,
    ) -> RequestTokenBudget:
        self._request_output_tokens = DEFAULT_REQUEST_OUTPUT_TOKENS if output_override is None else output_override
        return self.request_token_budget()

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
