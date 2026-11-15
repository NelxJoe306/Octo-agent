import typer

from agent_core.events import MessageEndEvent
from agent_core.messages import AssistantMessage
from terminal_app.events import AutoRetryEndEvent, CodingSessionEvent


class JsonEventRenderer:
    def __init__(self) -> None:
        self._failed = False

    def render(self, event: CodingSessionEvent) -> None:
        if isinstance(event, AutoRetryEndEvent) and event.success:
            self._failed = False
        if (
            isinstance(event, MessageEndEvent)
            and isinstance(event.message, AssistantMessage)
            and event.message.stop_reason == "error"
        ):
            self._failed = True
        typer.echo(event.model_dump_json(by_alias=True, exclude_none=True))

    def finish(self) -> bool:
        return not self._failed
