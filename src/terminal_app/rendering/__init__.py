from __future__ import annotations

from terminal_app.extensions.api import CustomMessageMarkup
from terminal_app.rendering.base import EventRenderer, PrintOutputMode
from terminal_app.rendering.json import JsonEventRenderer
from terminal_app.rendering.plain import FinalTextRenderer
from terminal_app.rendering.transcript import TranscriptRenderer


def create_event_renderer(
    mode: PrintOutputMode,
    *,
    custom_message_renderer: CustomMessageMarkup | None = None,
) -> EventRenderer:

    if mode is PrintOutputMode.text:
        return FinalTextRenderer()
    if mode is PrintOutputMode.json:
        return JsonEventRenderer()
    return TranscriptRenderer(custom_message_renderer=custom_message_renderer)


__all__ = [
    "EventRenderer",
    "FinalTextRenderer",
    "JsonEventRenderer",
    "PrintOutputMode",
    "TranscriptRenderer",
    "create_event_renderer",
]
