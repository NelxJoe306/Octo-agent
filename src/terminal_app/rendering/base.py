from __future__ import annotations

from enum import StrEnum
from typing import Protocol

from terminal_app.events import CodingSessionEvent


class PrintOutputMode(StrEnum):


    text = "text"
    json = "json"
    transcript = "transcript"
    rpc = "rpc"


class EventRenderer(Protocol):


    def render(self, event: CodingSessionEvent) -> None:
        pass

    def finish(self) -> bool:
        pass
