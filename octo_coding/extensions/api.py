from __future__ import annotations

import sys
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol, cast
from uuid import uuid4

from octo_agent.messages import AgentMessage, ToolResultMessage
from octo_agent.tools import AgentTool, AgentToolResult
from octo_agent.types import JSONValue

if TYPE_CHECKING:
    from textual import events
    from textual.widget import Widget

    from octo_coding.extensions.providers import DynamicProvider
    from octo_coding.extensions.runtime import ExtensionRuntime
    from octo_coding.local_backends import LocalBackend
    from octo_coding.paths import OctoPaths
    from octo_coding.tui.config import TuiTheme

AGENT_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "agent_start",
        "agent_end",
        "agent_settled",
        "turn_start",
        "turn_end",
        "queue_update",
        "message_start",
        "message_update",
        "message_end",
        "tool_execution_start",
        "tool_execution_update",
        "tool_execution_end",
        "compaction_start",
        "compaction_end",
        "entry_appended",
        "session_info_changed",
        "thinking_level_changed",
        "auto_retry_start",
        "auto_retry_end",
    }
)
AGENT_EVENT_WILDCARD = "agent_event"

LIFECYCLE_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "session_start",
        "session_shutdown",
        "input",
        "tool_call",
        "tool_result",
        "project_trust",
    }
)

SessionLifecycleReason = Literal["startup", "reload", "new", "resume", "branch", "quit"]
DeliverAs = Literal["steer", "follow_up"]
NotifyLevel = Literal["info", "warning", "error"]


@dataclass(frozen=True, slots=True)
class CustomMessageView:


    custom_type: str
    content: str
    details: Mapping[str, JSONValue] | None = None


@dataclass(frozen=True, slots=True)
class MessageRenderOptions:


    expanded: bool = False


MessageRenderer = Callable[[CustomMessageView, MessageRenderOptions], str]


CustomMessageMarkup = Callable[[str, str, "Mapping[str, JSONValue] | None", bool], "str | None"]


ToolCallMarkup = Callable[[str, "Mapping[str, JSONValue]"], "str | None"]


ToolResultMarkup = Callable[[str, AgentToolResult, bool], "str | None"]


Placement = Literal["above_prompt", "below_prompt"]


SlotWidgetFactory = Callable[["TuiTheme"], "Widget"]


SlotWidgetContent = Sequence[str] | SlotWidgetFactory


SidebarWidgetFactory = Callable[["TuiTheme"], "Widget"]
SidebarContent = Sequence[str] | SidebarWidgetFactory

MainViewFactory = Callable[["MainViewHandle", "TuiTheme"], "Widget"]


KeyInterceptor = Callable[["events.Key", str], bool]


_DEFAULT_THEME: TuiTheme | None = None


def _default_theme() -> TuiTheme:

    global _DEFAULT_THEME
    if _DEFAULT_THEME is None:
        from octo_coding.tui.config import OCTO_DARK_THEME

        _DEFAULT_THEME = OCTO_DARK_THEME
    return _DEFAULT_THEME


class MainViewHandle(Protocol):


    def close(self, result: object | None = None) -> None:

        ...

    async def wait(self) -> object | None:

        ...

    @property
    def is_open(self) -> bool: ...


class ComponentBridge(Protocol):


    @property
    def supports_components(self) -> bool:

        ...

    @property
    def theme(self) -> TuiTheme:

        ...

    def get_prompt_text(self) -> str:

        ...

    def request_render(self) -> None:

        ...

    def set_slot_widget(
        self,
        key: str,
        content: SlotWidgetContent | None,
        *,
        placement: Placement = "above_prompt",
    ) -> None:

        ...

    def open_main_view(self, factory: MainViewFactory) -> MainViewHandle:

        ...

    def register_key_interceptor(self, handler: KeyInterceptor) -> Callable[[], None]:

        ...


class ExtensionSidebar:


    def __init__(
        self,
        runtime: ExtensionRuntime,
        extension_name: str,
        generation: ExtensionGeneration,
    ) -> None:
        self._runtime = runtime
        self._extension_name = extension_name
        self._generation = generation

    @property
    def supported(self) -> bool:

        self._generation.assert_active()
        return bool(getattr(self._runtime.ui, "supports_sidebar", False))

    def set_section(self, key: str, *, title: str, content: SidebarContent) -> None:

        self._generation.assert_active()
        normalized_key = key.strip()
        normalized_title = title.strip()
        if not normalized_key:
            raise ExtensionError("sidebar section key must not be empty")
        if not normalized_title:
            raise ExtensionError("sidebar section title must not be empty")
        if not self.supported:
            return
        setter = getattr(self._runtime.ui, "set_sidebar_section", None)
        if setter is not None:
            setter(
                self._extension_name,
                normalized_key,
                title=normalized_title,
                content=content,
            )

    def remove_section(self, key: str) -> None:

        self._generation.assert_active()
        normalized_key = key.strip()
        if not normalized_key:
            raise ExtensionError("sidebar section key must not be empty")
        if not self.supported:
            return
        remover = getattr(self._runtime.ui, "remove_sidebar_section", None)
        if remover is not None:
            remover(self._extension_name, normalized_key)


class ExtensionError(RuntimeError):
    pass


_STALE_MESSAGE = (
    "extension instance is stale after reload: state captured before /reload"
    " (a saved `octo` API object, context, or ui handle) must not be reused;"
    " the reloaded extension received a fresh API in its new setup()"
)


class ExtensionGeneration:


    __slots__ = ("_id", "_stale_message")

    def __init__(self) -> None:
        self._id = uuid4().hex
        self._stale_message: str | None = None

    @property
    def id(self) -> str:

        return self._id

    @property
    def active(self) -> bool:

        return self._stale_message is None

    def invalidate(self, message: str | None = None) -> None:

        if self._stale_message is None:
            self._stale_message = message or _STALE_MESSAGE

    def assert_active(self) -> None:

        if self._stale_message is not None:
            raise ExtensionError(self._stale_message)


@dataclass(frozen=True, slots=True)
class TurnStartEvent:


    turn_index: int
    timestamp: int
    type: Literal["turn_start"] = field(default="turn_start", init=False)


@dataclass(frozen=True, slots=True)
class TurnEndEvent:


    turn_index: int
    message: AgentMessage
    tool_results: list[ToolResultMessage]
    type: Literal["turn_end"] = field(default="turn_end", init=False)


@dataclass(frozen=True, slots=True)
class SessionStartEvent:


    reason: SessionLifecycleReason


@dataclass(frozen=True, slots=True)
class SessionShutdownEvent:


    reason: SessionLifecycleReason


@dataclass(frozen=True, slots=True)
class InputEvent:


    text: str
    source: Literal["interactive", "extension"] = "interactive"
    streaming_behavior: Literal["steer", "follow_up"] | None = None


@dataclass(frozen=True, slots=True)
class InputHookResult:


    action: Literal["continue", "transform", "handled"] = "continue"
    text: str | None = None
    message: str | None = None


@dataclass(frozen=True, slots=True)
class ToolCallHookEvent:


    tool_name: str
    arguments: Mapping[str, JSONValue]


@dataclass(frozen=True, slots=True)
class ToolCallHookResult:


    block: bool = False
    reason: str | None = None
    arguments: Mapping[str, JSONValue] | None = None


@dataclass(frozen=True, slots=True)
class ToolResultHookEvent:


    tool_name: str
    arguments: Mapping[str, JSONValue]
    result: AgentToolResult


@dataclass(frozen=True, slots=True)
class ToolResultHookResult:


    content: str | None = None
    details: dict[str, JSONValue] | None = None


ExtensionHandler = Callable[[object, "ExtensionContext"], object | Awaitable[object]]


ExtensionCommandHandler = Callable[["str", "ExtensionCommandContext"], "str | None"]


@dataclass(frozen=True, slots=True)
class ExtensionRuntimeDiagnostic:


    extension: str
    event: str
    message: str


class UiBridge(Protocol):


    @property
    def has_ui(self) -> bool:

        ...

    def notify(self, message: str, level: NotifyLevel = "info") -> None:

        ...

    async def select(
        self,
        title: str,
        options: Sequence[str],
        *,
        timeout: float | None = None,
    ) -> str | None:

        ...

    async def confirm(
        self,
        title: str,
        message: str,
        *,
        timeout: float | None = None,
    ) -> bool:

        ...

    async def input(
        self,
        title: str,
        placeholder: str = "",
        *,
        timeout: float | None = None,
    ) -> str | None:

        ...


    @property
    def supports_components(self) -> bool:

        ...

    @property
    def theme(self) -> TuiTheme:

        ...

    def get_prompt_text(self) -> str:

        ...

    def request_render(self) -> None:

        ...

    def set_slot_widget(
        self,
        key: str,
        content: SlotWidgetContent | None,
        *,
        placement: Placement = "above_prompt",
    ) -> None:

        ...

    def open_main_view(self, factory: MainViewFactory) -> MainViewHandle:

        ...

    def register_key_interceptor(self, handler: KeyInterceptor) -> Callable[[], None]:

        ...

    @property
    def supports_sidebar(self) -> bool:

        ...

    def set_sidebar_section(
        self,
        extension_name: str,
        key: str,
        *,
        title: str,
        content: SidebarContent,
    ) -> None:

        ...

    def remove_sidebar_section(self, extension_name: str, key: str) -> None:

        ...

    def clear_components(self) -> None:

        ...


class _DeadMainViewHandle:


    def close(self, result: object | None = None) -> None:
        pass

    async def wait(self) -> object | None:

        return None

    @property
    def is_open(self) -> bool:

        return False


class NullUiBridge:


    @property
    def has_ui(self) -> bool:

        return False

    def notify(self, message: str, level: NotifyLevel = "info") -> None:
        pass

    async def select(
        self,
        title: str,
        options: Sequence[str],
        *,
        timeout: float | None = None,
    ) -> str | None:

        return None

    async def confirm(
        self,
        title: str,
        message: str,
        *,
        timeout: float | None = None,
    ) -> bool:

        return False

    async def input(
        self,
        title: str,
        placeholder: str = "",
        *,
        timeout: float | None = None,
    ) -> str | None:

        return None


    @property
    def supports_components(self) -> bool:

        return False

    @property
    def theme(self) -> TuiTheme:

        return _default_theme()

    def get_prompt_text(self) -> str:

        return ""

    def request_render(self) -> None:
        pass

    def set_slot_widget(
        self,
        key: str,
        content: SlotWidgetContent | None,
        *,
        placement: Placement = "above_prompt",
    ) -> None:
        pass

    def open_main_view(self, factory: MainViewFactory) -> MainViewHandle:

        return _DeadMainViewHandle()

    def register_key_interceptor(self, handler: KeyInterceptor) -> Callable[[], None]:

        return lambda: None

    @property
    def supports_sidebar(self) -> bool:

        return False

    def set_sidebar_section(
        self,
        extension_name: str,
        key: str,
        *,
        title: str,
        content: SidebarContent,
    ) -> None:
        pass

    def remove_sidebar_section(self, extension_name: str, key: str) -> None:
        pass

    def clear_components(self) -> None:
        pass


class StderrUiBridge(NullUiBridge):


    def notify(self, message: str, level: NotifyLevel = "info") -> None:

        print(f"[extension:{level}] {message}", file=sys.stderr)


@dataclass(frozen=True, slots=True)
class ExtensionCommandContext:


    name: str
    args: str
    api: ExtensionAPI


class ExtensionUi:


    def __init__(
        self,
        runtime: ExtensionRuntime,
        generation: ExtensionGeneration | None = None,
        *,
        extension_name: str = "",
    ) -> None:
        self._runtime = runtime
        self._generation = generation if generation is not None else ExtensionGeneration()
        self._sidebar = ExtensionSidebar(runtime, extension_name, self._generation)

    @property
    def has_ui(self) -> bool:

        self._generation.assert_active()
        return self._runtime.ui.has_ui

    async def select(
        self,
        title: str,
        options: Sequence[str],
        *,
        timeout: float | None = None,
    ) -> str | None:

        self._generation.assert_active()
        return await self._runtime.ui.select(title, options, timeout=timeout)

    async def confirm(
        self,
        title: str,
        message: str,
        *,
        timeout: float | None = None,
    ) -> bool:

        self._generation.assert_active()
        return await self._runtime.ui.confirm(title, message, timeout=timeout)

    async def input(
        self,
        title: str,
        placeholder: str = "",
        *,
        timeout: float | None = None,
    ) -> str | None:

        self._generation.assert_active()
        return await self._runtime.ui.input(title, placeholder, timeout=timeout)

    @property
    def sidebar(self) -> ExtensionSidebar:

        self._generation.assert_active()
        return self._sidebar

    @property
    def components(self) -> ComponentBridge:

        self._generation.assert_active()
        return cast("ComponentBridge", self._runtime.ui)

    def notify(self, message: str, level: NotifyLevel = "info") -> None:

        self._generation.assert_active()
        self._runtime.ui.notify(message, level)


class ExtensionContext:


    def __init__(
        self,
        runtime: ExtensionRuntime,
        generation: ExtensionGeneration | None = None,
        *,
        extension_name: str = "",
    ) -> None:
        self._runtime = runtime
        self._generation = generation if generation is not None else ExtensionGeneration()
        self._ui = ExtensionUi(runtime, self._generation, extension_name=extension_name)

    @property
    def cwd(self) -> Path:

        self._generation.assert_active()
        return self._runtime.session_view.cwd

    @property
    def paths(self) -> OctoPaths:

        self._generation.assert_active()
        return self._runtime.paths

    @property
    def model(self) -> str:

        self._generation.assert_active()
        return self._runtime.session_view.model

    @property
    def provider_name(self) -> str:

        self._generation.assert_active()
        return self._runtime.session_view.provider_name

    @property
    def inference_provider(self) -> str | None:

        self._generation.assert_active()
        return self._runtime.session_view.inference_provider

    @property
    def inference_provider_mode(self) -> str:

        self._generation.assert_active()
        return self._runtime.session_view.inference_provider_mode

    @property
    def session_id(self) -> str | None:

        self._generation.assert_active()
        return self._runtime.session_view.session_id

    @property
    def session_name(self) -> str | None:

        self._generation.assert_active()
        return self._runtime.session_view.session_name

    @property
    def thinking_level(self) -> str:

        self._generation.assert_active()
        return self._runtime.session_view.thinking_level

    @property
    def system_prompt(self) -> str:

        self._generation.assert_active()
        return self._runtime.session_view.system_prompt

    @property
    def is_running(self) -> bool:

        self._generation.assert_active()
        return self._runtime.session_view.is_running

    @property
    def transcript(self) -> tuple[AgentMessage, ...]:

        self._generation.assert_active()
        messages = self._runtime.session_view.messages
        return tuple(message.model_copy(deep=True) for message in messages)

    @property
    def has_ui(self) -> bool:

        self._generation.assert_active()
        return self._runtime.ui.has_ui

    @property
    def ui(self) -> ExtensionUi:

        self._generation.assert_active()
        return self._ui


class ExtensionAPI:


    def __init__(
        self,
        runtime: ExtensionRuntime,
        extension_name: str,
        generation: ExtensionGeneration | None = None,
        *,
        source_id: str | None = None,
    ) -> None:
        self._runtime = runtime
        self._extension_name = extension_name
        self._source_id = source_id or f"extension-name:{extension_name}"
        self._generation = generation if generation is not None else ExtensionGeneration()
        self._context = ExtensionContext(
            runtime,
            self._generation,
            extension_name=extension_name,
        )

    @property
    def name(self) -> str:

        self._generation.assert_active()
        return self._extension_name

    @property
    def context(self) -> ExtensionContext:

        self._generation.assert_active()
        return self._context

    def set_inference_provider(self, route: str | None) -> str:

        self._generation.assert_active()
        return self._runtime.session_view.set_inference_provider(route)

    def register_tool(self, tool: AgentTool) -> None:

        self._generation.assert_active()
        self._runtime.register_tool(self._source_id, self._extension_name, tool)

    def register_provider(self, provider: DynamicProvider) -> None:

        self._generation.assert_active()
        self._runtime.register_provider(self._source_id, provider)

    def update_provider(self, provider: DynamicProvider) -> bool:

        self._generation.assert_active()
        return self._runtime.update_provider(self._source_id, provider)

    def register_local_backend(self, backend: LocalBackend) -> None:

        self._generation.assert_active()
        self._runtime.register_local_backend(self._source_id, backend)

    def register_command(
        self,
        name: str,
        handler: ExtensionCommandHandler,
        *,
        description: str = "",
        usage: str | None = None,
        aliases: tuple[str, ...] = (),
    ) -> None:

        self._generation.assert_active()
        self._runtime.register_command(
            self._source_id,
            self._extension_name,
            name,
            handler,
            description=description,
            usage=usage,
            aliases=aliases,
        )

    def add_prompt_guideline(self, guideline: str) -> None:

        self._generation.assert_active()
        self._runtime.register_prompt_guideline(self._source_id, self._extension_name, guideline)

    def add_prompt_section(self, title: str | None, body: str) -> None:

        self._generation.assert_active()
        self._runtime.register_prompt_section(
            self._source_id,
            self._extension_name,
            title,
            body,
        )

    def on(
        self,
        event: str,
        handler: ExtensionHandler | None = None,
    ) -> Callable[[ExtensionHandler], ExtensionHandler] | ExtensionHandler:

        self._generation.assert_active()
        if handler is not None:
            self._runtime.subscribe(self._source_id, event, handler)
            return handler

        def decorator(decorated: ExtensionHandler) -> ExtensionHandler:
            self._generation.assert_active()
            self._runtime.subscribe(self._source_id, event, decorated)
            return decorated

        return decorator

    def send_user_message(
        self,
        content: str,
        *,
        deliver_as: DeliverAs = "follow_up",
    ) -> None:

        self._generation.assert_active()
        self._runtime.send_user_message(content, deliver_as=deliver_as)

    def register_message_renderer(
        self,
        custom_type: str,
        renderer: MessageRenderer,
    ) -> None:

        self._generation.assert_active()
        self._runtime.register_message_renderer(
            self._source_id, self._extension_name, custom_type, renderer
        )

    def send_custom_message(
        self,
        content: str,
        *,
        custom_type: str,
        details: dict[str, JSONValue] | None = None,
        deliver_as: DeliverAs = "follow_up",
        trigger_turn: bool = True,
    ) -> None:

        self._generation.assert_active()
        self._runtime.send_custom_message(
            content,
            custom_type=custom_type,
            details=details,
            deliver_as=deliver_as,
            trigger_turn=trigger_turn,
        )

    async def append_entry(self, namespace: str, data: dict[str, JSONValue]) -> None:

        self._generation.assert_active()
        await self._runtime.append_custom_entry(namespace, data)

    async def set_label(self, entry_id: str, label: str | None) -> None:

        self._generation.assert_active()
        await self._runtime.set_label(entry_id, label)

    def notify(self, message: str, level: NotifyLevel = "info") -> None:

        self._generation.assert_active()
        self._runtime.ui.notify(message, level)


@dataclass(slots=True)
class RegisteredExtension:


    name: str
    source_id: str
    path: Path | None
    api: ExtensionAPI
    source: Literal["built-in", "user", "explicit", "project"] = "explicit"
    hidden: bool = False
    handlers: dict[str, list[ExtensionHandler]] = field(default_factory=dict)
