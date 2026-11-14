from __future__ import annotations

import asyncio
import errno
import os
import stat
import tempfile
import traceback
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum, auto
from inspect import isawaitable
from io import StringIO
from pathlib import Path
from typing import Any, BinaryIO, ClassVar, Literal, Protocol, TypeVar, cast

from rich.console import Console, Group
from rich.style import Style
from rich.text import Text
from textual import constants as textual_constants
from textual import events, on
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingsMap
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.events import Key, Resize
from textual.screen import ModalScreen
from textual.strip import Strip
from textual.timer import Timer
from textual.widget import Widget
from textual.widgets import (
    Button,
    Input,
    Label,
    ListItem,
    ListView,
    OptionList,
    Static,
    TextArea,
)
from textual.worker import Worker

from octo_agent.events import (
    ContextBudgetEvent,
    ContextRecoveryStartEvent,
    ContextRecoveryEndEvent,
    AgentEndEvent,
    AgentStartEvent,
    MessageEndEvent,
    MessageStartEvent,
    MessageUpdateEvent,
    ToolExecutionEndEvent,
    ToolExecutionStartEvent,
    ToolExecutionUpdateEvent,
)
from octo_agent.messages import (
    AgentMessage,
    AssistantMessage,
    CustomMessage,
    TextContent,
    ThinkingContent,
    UserMessage,
)
from octo_agent.provider import CancellationToken
from octo_agent.provider_events import (
    AssistantErrorEvent,
    AssistantMessageEvent,
    TextDeltaEvent,
    ThinkingDeltaEvent,
    ThinkingEndEvent,
)
from octo_agent.tools import AgentTool
from octo_agent.types import JSONValue
from octo_coding.catalog_loader import save_user_catalog_entries
from octo_coding.commands import (
    LOGIN_PROVIDER_ALIASES,
    CommandRegistry,
    create_default_command_registry,
    format_reload_summary,
)
from octo_coding.credentials import FileCredentialStore, OAuthCredential
from octo_coding.events import (
    AgentSettledEvent,
    AutoRetryStartEvent,
    CodingSessionEvent,
    CompactionEndEvent,
    CompactionStartEvent,
    QueueUpdateEvent,
)
from octo_coding.extensions.api import (
    KeyInterceptor,
    MainViewFactory,
    MainViewHandle,
    Placement,
    SidebarContent,
    SlotWidgetContent,
    SlotWidgetFactory,
)
from octo_coding.oauth import login_openai_codex
from octo_coding.oauth_registry import get_oauth_provider, oauth_provider_ids
from octo_coding.oauth_types import (
    OAuthAuthInfo,
    OAuthDeviceCodeInfo,
    OAuthLoginCallbacks,
    OAuthPrompt,
    OAuthSelectPrompt,
)
from octo_coding.project_trust import ProjectTrustRequest, TrustChoice, TrustOverride
from octo_coding.prompt_templates import PromptTemplate
from octo_coding.provider_catalog import (
    BUILTIN_PROVIDER_CATALOG,
    ProviderCatalogEntry,
    builtin_provider_entry,
)
from octo_coding.provider_config import (
    DEFAULT_MODEL,
    DEFAULT_PROVIDER_NAME,
    OpenAICompatibleProviderConfig,
    ProviderConfig,
    ProviderConfigError,
    ProviderSelection,
    load_provider_settings,
    provider_config_from_catalog_entry,
    provider_has_usable_credentials,
    resolve_provider_selection,
    resolve_startup_thinking_level,
    save_provider_settings,
    upsert_openai_compatible_provider,
    upsert_saved_provider,
)
from octo_coding.provider_runtime import ClosableModelProvider, create_model_provider
from octo_coding.resources import ResourceDiagnostic, OctoResourcePaths
from octo_coding.session import (
    CodingSession,
    CodingSessionConfig,
    ModelChoice,
    is_context_overflow_error,
    parse_terminal_command,
)
from octo_coding.topic_web import TopicWeb
from octo_coding.session_manager import CodingSessionRecord, SessionManager
from octo_coding.session_preparation import prepare_coding_session
from octo_coding.shell_config import load_shell_settings
from octo_coding.skills import Skill
from octo_coding.system_prompt import SystemPromptInspection
from octo_coding.thinking import ThinkingLevel
from octo_coding.tui.adapter import TuiEventAdapter
from octo_coding.tui.autocomplete import (
    CompletionItem,
    CompletionKind,
    CompletionOption,
    CompletionState,
    build_completion_state,
)
from octo_coding.tui.config import (
    OCTO_DARK_THEME,
    TuiKeybindings,
    TuiSettings,
    TuiTheme,
    TuiThemeName,
    load_tui_settings,
    save_tui_settings,
)
from octo_coding.tui.file_drop import normalize_dropped_paths
from octo_coding.tui.local_backends import (
    LocalBackendPickerScreen,
    LocalBackendScreen,
    LocalChoiceConfirmScreen,
    LocalConfirmScreen,
    LocalSearchResultsScreen,
)
from octo_coding.tui.project_trust import ProjectTrustScreen, prompt_project_trust
from octo_coding.tui.state import TuiState, format_terminal_command_result_block
from octo_coding.tui.terminal_notification import TerminalNotificationController
from octo_coding.tui.terminal_title import TerminalTitleController
from octo_coding.tui.themes import (
    available_tui_theme_names,
    load_custom_tui_themes,
    set_custom_tui_themes,
    textual_theme_for_tui_theme,
    theme_css_variables,
)
from octo_coding.tui.widgets import (
    CompactSessionInfo,
    SessionSidebar,
    SidebarFileItem,
    TranscriptView,
    _custom_markup_to_text,
    _sidebar_separator,
    render_completion_suggestions,
)

_textual_theme_for_octo_theme = textual_theme_for_tui_theme
_theme_css_variables = theme_css_variables

type BindingEntry = Binding | tuple[str, str] | tuple[str, str, str]
SIDEBAR_MIN_WIDTH = 96
SIDEBAR_MIN_HEIGHT = 38
ACTIVITY_TICK_SECONDS = 0.15
ACTIVITY_COLOR_FADE_STEPS = 24
ACTIVITY_INDICATOR_HEIGHT = 3
COMPLETION_MAX_VISIBLE_LINES = 16
COMPLETION_INITIAL_TERMINAL_FRACTION = 3
COMPLETION_MIN_TRANSCRIPT_LINES = 4
COMPLETION_WIDGET_CHROME_LINES = 3
NO_STORED_CREDENTIALS_MESSAGE = (
    "No stored credentials to remove. /logout only removes credentials saved by /login; "
    "environment variables and providers.json config are unchanged."
)


def _configure_herdr_textual_mouse() -> None:

    if os.environ.get("HERDR_ENV") != "1" or "TEXTUAL_SMOOTH_SCROLL" in os.environ:
        return
    os.environ["TEXTUAL_SMOOTH_SCROLL"] = "0"


    textual_constants.SMOOTH_SCROLL = False


class LoginRequiredProvider:


    def __init__(self, message: str) -> None:
        self.message = message

    async def aclose(self) -> None:
        pass

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

        del system, messages, tools, signal, session_id

        async def iterator() -> AsyncIterator[AssistantMessageEvent]:
            error = AssistantMessage(
                model=model,
                stop_reason="error",
                error_message=self.message,
            )
            yield AssistantErrorEvent(reason="error", error=error)

        return iterator()


_DialogResult = TypeVar("_DialogResult")


@dataclass(frozen=True, slots=True)
class _SidebarContribution:


    title: str
    content: SidebarContent


class _TuiExtensionUiBridge:


    _SEVERITIES: ClassVar[dict[str, Literal["information", "warning", "error"]]] = {
        "info": "information",
        "warning": "warning",
        "error": "error",
    }

    def __init__(self, app: OctoTuiApp) -> None:
        self._app = app

    @property
    def has_ui(self) -> bool:

        return True

    def notify(self, message: str, level: str = "info") -> None:

        self._app._notify(message, severity=self._SEVERITIES.get(level, "information"))

    async def select(
        self,
        title: str,
        options: Sequence[str],
        *,
        timeout: float | None = None,
    ) -> str | None:

        theme = self._app.tui_settings.resolved_theme
        screen: ModalScreen[str | None] = ExtensionSelectScreen(title, options, theme=theme)
        return await self._run_dialog(screen, default=None, timeout=timeout)

    async def confirm(
        self,
        title: str,
        message: str,
        *,
        timeout: float | None = None,
    ) -> bool:

        theme = self._app.tui_settings.resolved_theme
        screen: ModalScreen[bool] = ExtensionConfirmScreen(title, message, theme=theme)
        return await self._run_dialog(screen, default=False, timeout=timeout)

    async def input(
        self,
        title: str,
        placeholder: str = "",
        *,
        timeout: float | None = None,
    ) -> str | None:

        theme = self._app.tui_settings.resolved_theme
        screen: ModalScreen[str | None] = ExtensionInputScreen(title, placeholder, theme=theme)
        return await self._run_dialog(screen, default=None, timeout=timeout)


    @property
    def supports_components(self) -> bool:

        return True

    @property
    def theme(self) -> TuiTheme:

        return self._app.tui_settings.resolved_theme

    def get_prompt_text(self) -> str:

        return self._app._current_prompt_text()

    def request_render(self) -> None:

        self._app._refresh_extension_components()

    def set_slot_widget(
        self,
        key: str,
        content: SlotWidgetContent | None,
        *,
        placement: Placement = "above_prompt",
    ) -> None:

        self._app._set_extension_slot_widget(key, content, placement)

    def open_main_view(self, factory: MainViewFactory) -> MainViewHandle:

        return self._app._open_extension_main_view(factory)

    def register_key_interceptor(self, handler: KeyInterceptor) -> Callable[[], None]:

        return self._app._register_extension_key_interceptor(handler)

    @property
    def supports_sidebar(self) -> bool:

        return self._app.tui_settings.sidebar_position != "off"

    def set_sidebar_section(
        self,
        extension_name: str,
        key: str,
        *,
        title: str,
        content: SidebarContent,
    ) -> None:

        self._app._set_extension_sidebar_section(
            extension_name,
            key,
            title=title,
            content=content,
        )

    def remove_sidebar_section(self, extension_name: str, key: str) -> None:

        self._app._remove_extension_sidebar_section(extension_name, key)

    def clear_components(self) -> None:

        self._app._clear_extension_components()

    async def _run_dialog(
        self,
        screen: ModalScreen[_DialogResult],
        *,
        default: _DialogResult,
        timeout: float | None,
    ) -> _DialogResult:

        loop = asyncio.get_running_loop()
        future: asyncio.Future[_DialogResult] = loop.create_future()

        def _resolve(result: _DialogResult | None) -> None:


            if not future.done():
                future.set_result(default if result is None else result)

        self._app.push_screen(screen, _resolve)
        if timeout is None:
            return await future
        try:
            return await asyncio.wait_for(future, timeout)
        except TimeoutError:


            if screen.is_current:
                with suppress(Exception):
                    screen.dismiss(default)
            return default


class _MainViewHandle:


    def __init__(self, app: OctoTuiApp, result: asyncio.Future[object | None]) -> None:
        self._app = app
        self._open = True
        self.widget: Widget | None = None


        self._result = result

    def close(self, result: object | None = None) -> None:

        if not self._open:
            return
        self._open = False
        self._resolve(result)
        self._app._close_extension_main_view(self)

    def _resolve(self, result: object | None) -> None:

        if not self._result.done():
            self._result.set_result(result)

    async def wait(self) -> object | None:

        return await self._result

    @property
    def is_open(self) -> bool:

        return self._open


class _DeadMainViewHandle:


    def close(self, result: object | None = None) -> None:
        pass

    async def wait(self) -> object | None:

        return None

    @property
    def is_open(self) -> bool:

        return False


class CompletionActionTarget(Protocol):


    def action_accept_completion(self) -> None: ...

    def action_cancel(self) -> None: ...

    def action_completion_next(self) -> None: ...

    def action_completion_previous(self) -> None: ...

    def action_open_command_palette(self) -> None: ...

    def action_open_session_picker(self) -> None: ...

    def action_cycle_thinking(self) -> None: ...

    def action_cycle_model(self) -> None: ...

    def action_cycle_model_reverse(self) -> None: ...

    def action_toggle_tool_results(self) -> None: ...

    def action_toggle_thinking(self) -> None: ...

    def action_edit_queued_message(self) -> bool: ...

    async def action_submit_prompt(self) -> None: ...

    async def action_submit_follow_up(self) -> None: ...


class SessionCompletionRecord(Protocol):


    id: str
    title: str | None
    model: str
    cwd: Path
    updated_at: float


PASTE_DISPLAY_THRESHOLD = 2_000


class PromptInput(TextArea):


    BINDINGS: ClassVar[list[BindingEntry]] = []
    shell_mode_style: str = ""

    def __init__(
        self,
        *,
        tui_keybindings: TuiKeybindings | None = None,
        **kwargs: Any,
    ) -> None:
        kwargs.setdefault("highlight_cursor_line", False)
        super().__init__(**kwargs)
        self.tui_keybindings = tui_keybindings or TuiKeybindings()
        self._base_bindings = self._bindings.copy()
        self._footer_mode: Literal["normal", "completion", "file_completion", "running"] = "normal"
        self._pending_pastes: list[tuple[str, str]] = []
        self._paste_placeholder_counter = 0
        self._apply_prompt_bindings()

    def set_footer_mode(
        self,
        mode: Literal["normal", "completion", "file_completion", "running"],
    ) -> None:

        if mode == self._footer_mode:
            return
        self._footer_mode = mode
        self._apply_prompt_bindings()
        self.refresh_bindings()

    def _apply_prompt_bindings(self) -> None:
        self._bindings = BindingsMap.merge(
            [
                self._base_bindings,
                BindingsMap(_prompt_bindings(self.tui_keybindings, mode=self._footer_mode)),
            ]
        )

    @property
    def value(self) -> str:

        return self.text

    @value.setter
    def value(self, text: str) -> None:
        self.text = text

    @property
    def cursor_position(self) -> int:

        row, column = self.cursor_location
        lines = self.text.split("\n")
        return sum(len(line) + 1 for line in lines[:row]) + column

    @cursor_position.setter
    def cursor_position(self, offset: int) -> None:
        text = self.text
        bounded = max(0, min(offset, len(text)))
        before = text[:bounded]
        self.move_cursor((before.count("\n"), len(before.rsplit("\n", 1)[-1])))

    def action_accept_completion(self) -> None:

        self._completion_target().action_accept_completion()

    def action_completion_next(self) -> None:

        if self._has_completion_options():
            self._completion_target().action_completion_next()
        else:
            self.action_cursor_down()

    def action_completion_previous(self) -> None:

        if self._has_completion_options():
            self._completion_target().action_completion_previous()
        elif self._completion_target().action_edit_queued_message():
            return
        else:
            self.action_cursor_up()

    def action_cancel(self) -> None:

        self._completion_target().action_cancel()

    def action_open_command_palette(self) -> None:

        self._completion_target().action_open_command_palette()

    def action_open_session_picker(self) -> None:

        self._completion_target().action_open_session_picker()

    def action_cycle_thinking(self) -> None:

        self._completion_target().action_cycle_thinking()

    def action_cycle_model(self) -> None:

        self._completion_target().action_cycle_model()

    def action_cycle_model_reverse(self) -> None:

        self._completion_target().action_cycle_model_reverse()

    def action_toggle_tool_results(self) -> None:

        self._completion_target().action_toggle_tool_results()

    def action_toggle_thinking(self) -> None:

        self._completion_target().action_toggle_thinking()

    def action_clear_prompt(self) -> None:

        if self.selected_text:
            return
        if self.text:
            self.text = ""
            self.move_cursor((0, 0))
            self._clear_pending_paste()

    def render_line(self, y: int) -> Strip:

        if self.content_size.width <= 0:
            return Strip.blank(0, self.visual_style.rich_style)
        return super().render_line(y)

    def get_line(self, line_index: int) -> Text:

        line = super().get_line(line_index)
        if not self.shell_mode_style:
            return line
        span = _terminal_command_prefix_span(self.text)
        if span is None:
            return line
        start, _ = span
        line.stylize(self.shell_mode_style, start if line_index == 0 else 0)
        return line

    async def action_submit_follow_up(self) -> None:

        await self._completion_target().action_submit_follow_up()

    async def action_submit_prompt(self) -> None:

        await self._completion_target().action_submit_prompt()

    def action_insert_newline(self) -> None:

        self.insert("\n")

    async def action_quit(self) -> None:

        await self.app.action_quit()

    def action_scroll_down(self) -> None:

        self.action_completion_next()

    def action_scroll_up(self) -> None:

        self.action_completion_previous()

    def on_paste(self, event: events.Paste) -> None:

        if self.handle_pasted_text(event.text):
            event.stop()
            event.prevent_default()

    def handle_pasted_text(self, text: str) -> bool:

        dropped_paths = normalize_dropped_paths(text)
        if dropped_paths is not None:
            self._insert_dropped_paths(dropped_paths)
            return True
        if len(text) <= PASTE_DISPLAY_THRESHOLD:
            return False
        self._show_large_paste_placeholder(text)
        return True

    def insert_pasted_text(self, text: str) -> None:

        if not self.handle_pasted_text(text):
            self.insert(text)

    def _insert_dropped_paths(self, insertion: str) -> None:

        position = self.cursor_position
        before = self.text[:position]
        after = self.text[position:]
        if before and not before[-1].isspace():
            insertion = f" {insertion}"
        if not after or not after[0].isspace():
            insertion = f"{insertion} "
        self.insert(insertion)

    def _show_large_paste_placeholder(self, content: str) -> None:

        self._paste_placeholder_counter += 1
        placeholder = self._large_paste_placeholder(content, self._paste_placeholder_counter)
        self._pending_pastes.append((placeholder, content))
        self.insert(placeholder)

    def _large_paste_placeholder(self, content: str, paste_number: int) -> str:

        char_count = len(content)
        line_count = content.count("\n") + 1
        kb = char_count / 1024
        parts: list[str] = [f"{char_count:,} characters"]
        if line_count > 1:
            parts.append(f"{line_count} lines")
        if kb >= 1:
            parts.append(f"{kb:.1f} KB")
        return f"[Pasted content #{paste_number}: {', '.join(parts)}]"

    def _clear_pending_paste(self) -> None:

        self._pending_pastes.clear()

    def sync_pending_paste(self) -> None:

        self._pending_pastes = [
            (placeholder, content)
            for placeholder, content in self._pending_pastes
            if placeholder in self.text
        ]

    def text_for_submission(self) -> str:

        self.sync_pending_paste()
        text = self.text
        for placeholder, content in self._pending_pastes:
            text = text.replace(placeholder, content, 1)
        return text

    async def on_key(self, event: Key) -> None:

        keybindings = self.tui_keybindings
        if event.key == keybindings.queue_follow_up:
            event.stop()
            event.prevent_default()
            await self._completion_target().action_submit_follow_up()
        elif event.key == "enter":
            event.stop()
            event.prevent_default()
            await self._completion_target().action_submit_prompt()
        elif event.key == keybindings.insert_newline:
            event.stop()
            event.prevent_default()
            self.insert("\n")
        elif event.key == keybindings.accept_completion:
            event.stop()
            self._completion_target().action_accept_completion()
        elif event.key == keybindings.cancel:
            event.stop()
            self._completion_target().action_cancel()
        elif event.key == keybindings.command_palette:
            event.stop()
            self._completion_target().action_open_command_palette()
        elif event.key == keybindings.session_picker:
            event.stop()
            self._completion_target().action_open_session_picker()
        elif _is_thinking_cycle_key(event.key, keybindings.thinking_cycle):
            event.stop()
            self._completion_target().action_cycle_thinking()
        elif event.key == keybindings.model_cycle:
            event.stop()
            self._completion_target().action_cycle_model()
        elif event.key == keybindings.model_cycle_reverse:
            event.stop()
            self._completion_target().action_cycle_model_reverse()
        elif event.key == keybindings.toggle_tool_results:
            event.stop()
            self._completion_target().action_toggle_tool_results()
        elif event.key == keybindings.toggle_thinking:
            event.stop()
            self._completion_target().action_toggle_thinking()
        elif event.key == keybindings.copy_message:
            if self.selected_text:
                return
            event.stop()
            event.prevent_default()
            if self.text:
                self.text = ""
                self.move_cursor((0, 0))
        elif event.key == keybindings.completion_next:
            event.stop()
            if self._has_completion_options():
                self._completion_target().action_completion_next()
            else:
                self.action_cursor_down()
        elif event.key == keybindings.completion_previous:
            event.stop()
            self.action_completion_previous()
        elif event.key == keybindings.quit:
            event.stop()
            await self.action_quit()

    def _has_completion_options(self) -> bool:
        completion_state = getattr(self.app, "_completion_state", None)
        return bool(getattr(completion_state, "items", ()))

    def _completion_target(self) -> CompletionActionTarget:
        return cast(CompletionActionTarget, self.app)


class ExtensionSelectScreen(ModalScreen[str | None]):


    BINDINGS: ClassVar[list[BindingEntry]] = [
        Binding("escape", "cancel", "Cancel"),
        Binding("up", "cursor_up", "Up", show=False),
        Binding("down", "cursor_down", "Down", show=False),
        Binding("enter", "select_cursor", "Select", show=False),
    ]

    def __init__(
        self,
        title: str,
        options: Sequence[str],
        *,
        theme: TuiTheme,
    ) -> None:
        super().__init__()
        self.title_text = title
        self.options = tuple(options)
        self.theme = theme

    def compose(self) -> ComposeResult:

        with Vertical(id="extension-select"):
            yield Static(self.title_text, id="extension-select-title", markup=False)
            yield ListView(
                *[ListItem(Label(option, markup=False)) for option in self.options],
                id="extension-select-list",
            )
            yield Static("Enter selects - Escape cancels", id="extension-select-help")

    def on_mount(self) -> None:

        option_list = self.query_one("#extension-select-list", ListView)
        option_list.index = 0
        option_list.focus()

    def on_key(self, event: Key) -> None:

        if event.key == "up":
            event.stop()
            self.action_cursor_up()
        elif event.key == "down":
            event.stop()
            self.action_cursor_down()
        elif event.key == "enter":
            event.stop()
            self.action_select_cursor()

    def on_list_view_selected(self, event: ListView.Selected) -> None:

        self.dismiss(self.options[event.index])

    def action_cursor_up(self) -> None:

        self.query_one("#extension-select-list", ListView).action_cursor_up()

    def action_cursor_down(self) -> None:

        self.query_one("#extension-select-list", ListView).action_cursor_down()

    def action_select_cursor(self) -> None:

        self.query_one("#extension-select-list", ListView).action_select_cursor()

    def action_cancel(self) -> None:

        self.dismiss(None)


class ExtensionConfirmScreen(ModalScreen[bool]):


    BINDINGS: ClassVar[list[BindingEntry]] = [
        Binding("escape", "cancel", "Cancel"),
        Binding("up", "cursor_up", "Up", show=False),
        Binding("down", "cursor_down", "Down", show=False),
        Binding("enter", "select_cursor", "Select", show=False),
    ]

    def __init__(self, title: str, message: str, *, theme: TuiTheme) -> None:
        super().__init__()
        self.title_text = title
        self.message = message
        self.theme = theme

    def compose(self) -> ComposeResult:

        with Vertical(id="extension-confirm"):
            yield Static(self.title_text, id="extension-confirm-title", markup=False)
            yield Static(self.message, id="extension-confirm-message", markup=False)
            yield ListView(
                ListItem(Label("Yes", markup=False)),
                ListItem(Label("No", markup=False)),
                id="extension-confirm-list",
            )
            yield Static("Enter selects - Escape cancels", id="extension-confirm-help")

    def on_mount(self) -> None:

        choice_list = self.query_one("#extension-confirm-list", ListView)
        choice_list.index = 0
        choice_list.focus()

    def on_key(self, event: Key) -> None:

        if event.key == "up":
            event.stop()
            self.action_cursor_up()
        elif event.key == "down":
            event.stop()
            self.action_cursor_down()
        elif event.key == "enter":
            event.stop()
            self.action_select_cursor()

    def on_list_view_selected(self, event: ListView.Selected) -> None:

        self.dismiss(event.index == 0)

    def action_cursor_up(self) -> None:

        self.query_one("#extension-confirm-list", ListView).action_cursor_up()

    def action_cursor_down(self) -> None:

        self.query_one("#extension-confirm-list", ListView).action_cursor_down()

    def action_select_cursor(self) -> None:

        self.query_one("#extension-confirm-list", ListView).action_select_cursor()

    def action_cancel(self) -> None:

        self.dismiss(False)


class ExtensionInputScreen(ModalScreen[str | None]):


    BINDINGS: ClassVar[list[BindingEntry]] = [Binding("escape", "cancel", "Cancel")]

    def __init__(
        self,
        title: str,
        placeholder: str = "",
        *,
        theme: TuiTheme,
        value: str = "",
    ) -> None:
        super().__init__()
        self.title_text = title
        self.placeholder = placeholder
        self.theme = theme
        self.value = value

    def compose(self) -> ComposeResult:

        with Vertical(id="extension-input"):
            yield Static(self.title_text, id="extension-input-title", markup=False)
            yield Input(
                value=self.value,
                placeholder=self.placeholder,
                id="extension-input-field",
            )
            yield Static("Enter submits - Escape cancels", id="extension-input-help")

    def on_mount(self) -> None:

        self.query_one("#extension-input-field", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:

        if event.input.id != "extension-input-field":
            return
        event.stop()
        self.dismiss(event.value)

    def action_cancel(self) -> None:

        self.dismiss(None)


class ToolsReferenceSearchInput(Input):


    BINDINGS: ClassVar[list[BindingEntry]] = [
        Binding("escape", "cancel", "Cancel", show=False, priority=True),
        Binding("up", "cursor_up", "Up", show=False, priority=True),
        Binding("down", "cursor_down", "Down", show=False, priority=True),
        Binding("enter", "open_selected", "Open", show=False, priority=True),
    ]

    def _reference(self) -> ToolsReferenceScreen:
        return cast(ToolsReferenceScreen, self.screen)

    def on_key(self, event: Key) -> None:

        if event.key == "up":
            event.stop()
            event.prevent_default()
            self._reference().action_cursor_up()
        elif event.key == "down":
            event.stop()
            event.prevent_default()
            self._reference().action_cursor_down()
        elif event.key == "escape":
            event.stop()
            event.prevent_default()
            self._reference().action_cancel()
        elif event.key == "enter":
            event.stop()
            event.prevent_default()
            self._reference().action_open_selected()

    def action_open_selected(self) -> None:
        self._reference().action_open_selected()


class ToolsReferenceScreen(ModalScreen[None]):


    BINDINGS: ClassVar[list[BindingEntry]] = [
        Binding("escape", "cancel", "Close"),
        Binding("up", "cursor_up", "Up", show=False),
        Binding("down", "cursor_down", "Down", show=False),
        Binding("enter", "open_selected", "Open", show=False),
    ]

    def __init__(
        self,
        tools: Sequence[AgentTool],
        *,
        extension_sources: Mapping[str, str],
        theme: TuiTheme,
    ) -> None:
        super().__init__()
        self.extension_sources = dict(extension_sources)
        self.tools = self._order_tools(tools)
        self.visible_tools = self.tools
        self.theme = theme

    def compose(self) -> ComposeResult:

        with Vertical(id="tools-reference"):
            yield Static("Available tools", id="tools-reference-title")
            yield ToolsReferenceSearchInput(placeholder="Search tools", id="tools-reference-search")
            yield Static(
                self._table_row("Tool", "Origin", "Description"),
                id="tools-reference-header",
            )
            yield ListView(id="tools-reference-list")
            yield Static("Enter opens description - Escape closes", id="tools-reference-help")

    def on_mount(self) -> None:

        self._refresh_tools("")
        self.query_one("#tools-reference-search", Input).focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "tools-reference-search":
            event.stop()
            self._refresh_tools(event.value)

    def on_list_view_selected(self, event: ListView.Selected) -> None:

        event.stop()
        self._open_tool(event.index)

    def action_cursor_up(self) -> None:
        self.query_one("#tools-reference-list", ListView).action_cursor_up()

    def action_cursor_down(self) -> None:
        self.query_one("#tools-reference-list", ListView).action_cursor_down()

    def action_open_selected(self) -> None:
        tool_list = self.query_one("#tools-reference-list", ListView)
        if tool_list.index is not None:
            self._open_tool(tool_list.index)

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _open_tool(self, index: int) -> None:
        if index >= len(self.visible_tools):
            return
        tool = self.visible_tools[index]
        self.app.push_screen(
            CommandOutputScreen(
                f"{tool.name} — {self._source_label(tool)}",
                tool.description or "No description",
                theme=self.theme,
            )
        )

    def _refresh_tools(self, query: str) -> None:
        needle = query.casefold().strip()
        self.visible_tools = tuple(
            tool
            for tool in self.tools
            if not needle
            or needle in tool.name.casefold()
            or needle in tool.label.casefold()
            or needle in tool.description.casefold()
            or needle in self._source_label(tool).casefold()
        )
        tool_list = self.query_one("#tools-reference-list", ListView)
        tool_list.clear()
        if not self.visible_tools:
            message = "No tools available." if not self.tools else "No tools match your search."
            tool_list.append(ListItem(Label(message, markup=False), disabled=True))
            return
        tool_list.extend(
            [
                ListItem(
                    Label(
                        self._table_row(
                            tool.name,
                            self._source_label(tool),
                            f"{len(tool.description)} chars",
                        ),
                        markup=False,
                    )
                )
                for tool in self.visible_tools
            ]
        )
        tool_list.index = 0

    def _order_tools(self, tools: Sequence[AgentTool]) -> tuple[AgentTool, ...]:
        tools_by_name = {tool.name: tool for tool in tools}
        builtins = sorted(
            (tool for tool in tools if tool.name not in self.extension_sources),
            key=lambda tool: tool.name.casefold(),
        )
        extension_tools: list[AgentTool] = []
        seen_extensions: set[str] = set()
        for extension in self.extension_sources.values():
            if extension in seen_extensions:
                continue
            seen_extensions.add(extension)
            extension_tools.extend(
                tools_by_name[tool_name]
                for tool_name, source in self.extension_sources.items()
                if source == extension and tool_name in tools_by_name
            )
        return tuple([*builtins, *extension_tools])

    def _table_row(self, name: str, source: str, description: str) -> str:
        name_width = max((len(tool.name) for tool in self.tools), default=len("Tool"))
        source_width = max(
            (len(self._source_label(tool)) for tool in self.tools),
            default=len("Origin"),
        )
        return (
            f"{name:<{max(name_width, len('Tool'))}}  "
            f"{source:<{max(source_width, len('Origin'))}}  {description}"
        )

    def _source_label(self, tool: AgentTool) -> str:
        extension = self.extension_sources.get(tool.name)
        return extension if extension is not None else "Built in"


class SessionPickerSearchInput(Input):


    BINDINGS: ClassVar[list[BindingEntry]] = [
        Binding("escape", "cancel", "Cancel", show=False, priority=True),
        Binding("up", "cursor_up", "Up", show=False, priority=True),
        Binding("down", "cursor_down", "Down", show=False, priority=True),
    ]

    def _picker(self) -> SessionPickerScreen:
        return cast(SessionPickerScreen, self.screen)

    def on_key(self, event: Key) -> None:

        if event.key == "up":
            event.stop()
            event.prevent_default()
            self.action_cursor_up()
        elif event.key == "down":
            event.stop()
            event.prevent_default()
            self.action_cursor_down()
        elif event.key in {"left", "right"} and isinstance(self.screen, SessionPickerScreen):
            event.stop()
            event.prevent_default()
            if event.key == "left":
                self.screen.action_focus_projects()
            else:
                self.screen.action_focus_sessions()
        elif event.key == "escape":
            event.stop()
            event.prevent_default()
            self.action_cancel()

    def action_cursor_up(self) -> None:

        self._picker().action_cursor_up()

    def action_cursor_down(self) -> None:

        self._picker().action_cursor_down()

    def action_cancel(self) -> None:

        self._picker().action_cancel()


@dataclass(frozen=True, slots=True)
class PromptTemplatePickerResult:


    action: Literal["insert", "edit"]
    template: PromptTemplate


class PromptTemplatePickerScreen(ModalScreen[PromptTemplatePickerResult | None]):


    BINDINGS: ClassVar[list[BindingEntry]] = [
        Binding("escape", "cancel", "Cancel"),
        Binding("up", "cursor_up", "Up", show=False),
        Binding("down", "cursor_down", "Down", show=False),
        Binding("enter", "select_cursor", "Select", show=False),
        Binding("ctrl+e", "edit_cursor", "Edit", show=False, priority=True),
    ]

    def __init__(self, templates: Sequence[PromptTemplate]) -> None:
        super().__init__()
        self.templates = tuple(sorted(templates, key=lambda item: item.name.lower()))
        self.visible_templates = self.templates

    def compose(self) -> ComposeResult:
        with Vertical(id="prompt-template-picker"):
            yield Static("Prompt templates", id="prompt-template-picker-title")
            yield SessionPickerSearchInput(
                placeholder="Search prompt templates", id="prompt-template-picker-search"
            )
            yield ListView(id="prompt-template-picker-list")
            yield Static("", id="prompt-template-picker-help")

    def on_mount(self) -> None:
        self.query_one("#prompt-template-picker-search", Input).focus()
        self._refresh_list()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "prompt-template-picker-search":
            return
        event.stop()
        query = event.value.casefold()
        self.visible_templates = tuple(
            template
            for template in self.templates
            if query in template.name.casefold() or query in (template.description or "").casefold()
        )
        self._refresh_list()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "prompt-template-picker-search":
            event.stop()
            self.action_select_cursor()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        event.stop()
        self.action_select_cursor()

    def action_cursor_up(self) -> None:
        self.query_one("#prompt-template-picker-list", ListView).action_cursor_up()

    def action_cursor_down(self) -> None:
        self.query_one("#prompt-template-picker-list", ListView).action_cursor_down()

    def action_select_cursor(self) -> None:
        template = self._selected_template()
        if template is not None:
            self.dismiss(PromptTemplatePickerResult(action="insert", template=template))

    def action_edit_cursor(self) -> None:

        template = self._selected_template()
        if template is not None:
            self.dismiss(PromptTemplatePickerResult(action="edit", template=template))

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _selected_template(self) -> PromptTemplate | None:
        picker_list = self.query_one("#prompt-template-picker-list", ListView)
        index = picker_list.index
        if index is None or index >= len(self.visible_templates):
            return None
        return self.visible_templates[index]

    def _refresh_list(self) -> None:
        picker_list = self.query_one("#prompt-template-picker-list", ListView)
        picker_list.clear()
        picker_list.extend(
            ListItem(
                Label(
                    f"/{template.name} — {template.description or 'No description'}",
                    markup=False,
                )
            )
            for template in self.visible_templates
        )
        picker_list.index = 0 if self.visible_templates else None
        if self.visible_templates:
            help_text = "Enter inserts - Ctrl+E edits - Escape closes"
        elif self.templates:
            help_text = "No matching prompt templates - Escape closes"
        else:
            help_text = "No prompt templates loaded - Escape closes"
        self.query_one("#prompt-template-picker-help", Static).update(help_text)


class PromptTemplateEditorScreen(ModalScreen[str | None]):


    BINDINGS: ClassVar[list[BindingEntry]] = [
        Binding("escape", "cancel", "Cancel"),
        Binding("ctrl+s", "save", "Save", show=False, priority=True),
    ]

    def __init__(self, template: PromptTemplate, source: str) -> None:
        super().__init__()
        self.template = template
        self.source = source

    def compose(self) -> ComposeResult:
        with Vertical(id="prompt-template-editor"):
            yield Static(f"Edit /{self.template.name}", id="prompt-template-editor-title")
            yield Static(str(self.template.path), id="prompt-template-editor-path")
            yield TextArea(self.source, id="prompt-template-editor-input")
            yield Static(
                "Ctrl+S saves - Escape returns without saving",
                id="prompt-template-editor-help",
            )

    def on_mount(self) -> None:
        self.query_one("#prompt-template-editor-input", TextArea).focus()

    def action_save(self) -> None:
        source = self.query_one("#prompt-template-editor-input", TextArea).text
        self.dismiss(source)

    def action_cancel(self) -> None:
        self.dismiss(None)


def _write_staged_utf8(handle: BinaryIO, source: str) -> None:

    remaining = memoryview(source.encode("utf-8"))
    while remaining:
        written = handle.write(remaining)
        if written is None or written <= 0:
            raise OSError("staged write did not make progress")
        remaining = remaining[written:]


@dataclass(frozen=True, slots=True)
class _SidebarFileSnapshot:


    target: Path
    content: bytes


def _read_sidebar_file(path: Path) -> tuple[str, _SidebarFileSnapshot]:

    target = path.resolve(strict=True)
    content = target.read_bytes()
    return content.decode("utf-8"), _SidebarFileSnapshot(target=target, content=content)


def _atomic_write_sidebar_file(
    path: Path,
    source: str,
    expected: _SidebarFileSnapshot,
) -> _SidebarFileSnapshot:

    target = path.resolve(strict=True)
    replacement = source.encode("utf-8")
    if target != expected.target:
        raise OSError(f"File target changed on disk; reopen before saving: {path}")

    authorization = os.open(
        target,
        os.O_WRONLY | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        target_stat = os.fstat(authorization)


        if target_stat.st_mode & 0o222 == 0:
            raise PermissionError(errno.EACCES, os.strerror(errno.EACCES), target)
        target_mode = stat.S_IMODE(target_stat.st_mode)
    finally:
        os.close(authorization)

    descriptor = -1
    temporary: Path | None = None
    try:
        descriptor, raw_temporary = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
        )
        temporary = Path(raw_temporary)
        os.chmod(temporary, target_mode)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            _write_staged_utf8(handle, source)
            handle.flush()
            os.fsync(handle.fileno())
        current_target = path.resolve(strict=True)
        if current_target != expected.target or current_target.read_bytes() != expected.content:
            raise OSError(f"File changed on disk; reopen before saving: {path}")
        os.replace(temporary, target)
        temporary = None
        return _SidebarFileSnapshot(target=target, content=replacement)
    finally:
        if descriptor >= 0:
            with suppress(OSError):
                os.close(descriptor)
        if temporary is not None:
            with suppress(OSError):
                temporary.unlink()


class SidebarFileEditor(Vertical):


    BINDINGS: ClassVar[list[BindingEntry]] = [
        Binding("escape", "close", "Close", show=False, priority=True),
        Binding("ctrl+s", "save", "Save", show=False, priority=True),
    ]

    def __init__(
        self,
        *,
        handle: MainViewHandle,
        path: Path,
        label: str,
        kind: str,
        source: str,
        snapshot: _SidebarFileSnapshot,
    ) -> None:
        super().__init__(id="sidebar-file-editor")
        self.handle = handle
        self.path = path
        self.label = label
        self.kind = kind
        self.source = source
        self._saved_source = source
        self._snapshot = snapshot
        self._saving = False

    def compose(self) -> ComposeResult:
        yield Static(f"Edit {self.kind}: {self.label}", id="sidebar-file-editor-title")
        yield Static(str(self.path), id="sidebar-file-editor-path")
        yield TextArea(self.source, id="sidebar-file-editor-input")
        yield Static(
            "Ctrl+S saves - Escape closes",
            id="sidebar-file-editor-help",
        )
        yield Static("", id="sidebar-file-editor-status")

    def on_mount(self) -> None:
        self.query_one("#sidebar-file-editor-input", TextArea).focus()

    @property
    def is_dirty(self) -> bool:

        try:
            source = self.query_one("#sidebar-file-editor-input", TextArea).text
        except NoMatches:
            return False
        return source != self._saved_source

    def action_save(self) -> None:

        if self._saving:
            return
        self._saving = True
        self.query_one("#sidebar-file-editor-status", Static).update("Saving…")
        self.app.run_worker(self._save(), exclusive=False)

    async def _save(self) -> None:
        source = self.query_one("#sidebar-file-editor-input", TextArea).text
        try:
            snapshot = await asyncio.to_thread(
                _atomic_write_sidebar_file,
                self.path,
                source,
                self._snapshot,
            )
        except Exception as exc:
            message = f"Could not save {self.path}: {exc}"
            self.query_one("#sidebar-file-editor-status", Static).update(message)
            cast(OctoTuiApp, self.app)._notify(message, severity="error")
        else:
            self._snapshot = snapshot
            self._saved_source = source
            message = f"Saved {self.path}"
            self.query_one("#sidebar-file-editor-status", Static).update(message)
            cast(OctoTuiApp, self.app)._notify(message)
        finally:
            self._saving = False

    def action_close(self) -> None:

        self.handle.close()


class SessionPickerScreen(ModalScreen[str | None]):


    BINDINGS: ClassVar[list[BindingEntry]] = [
        Binding("escape", "cancel", "Cancel"),
        Binding("up", "cursor_up", "Up", show=False),
        Binding("down", "cursor_down", "Down", show=False),
        Binding("left", "focus_projects", "Projects", show=False),
        Binding("right", "focus_sessions", "Sessions", show=False),
        Binding("enter", "select_cursor", "Select", show=False),
    ]

    CSS = """
    #session-picker {
        width: 110;
        max-width: 94%;
        height: auto;
        max-height: 85%;
    }

    #session-picker-columns {
        height: auto;
        border: tall $octo-border;
        background: $octo-transcript-background;
    }

    .session-picker-column {
        height: auto;
        background: $octo-transcript-background;
    }

    #session-picker-project-column {
        width: 34;
        border-right: tall $octo-border;
    }

    #session-picker-session-column {
        width: 1fr;
    }

    .session-picker-column-title {
        height: 1;
        padding: 0 1;
        color: $octo-muted-text;
        text-style: bold;
    }

    .-active-column > .session-picker-column-title {
        color: $octo-accent;
    }

    #session-picker-project-list,
    #session-picker-list {
        height: auto;
        max-height: 16;
        border: none;
        background: $octo-transcript-background;
    }

    #session-picker-list {
        padding: 0 1;
    }
    """

    def __init__(
        self,
        records: Sequence[SessionCompletionRecord],
        *,
        local_cwd: Path,
        theme: TuiTheme,
        loading_other_projects: bool = False,
        current_project_loaded: bool = True,
    ) -> None:
        super().__init__()
        self.records = tuple(records)
        self.local_cwd = local_cwd.resolve()
        self.theme = theme
        self.search_value = ""
        self.active_column: Literal["projects", "sessions"] = "sessions"
        self.records_by_project = self._group_records_by_project()
        self.project_cwds = tuple(self.records_by_project)
        self.selected_project_index = 0
        self.visible_records: tuple[SessionCompletionRecord, ...] = ()
        self.loading_other_projects = loading_other_projects
        self.current_project_loaded = current_project_loaded

    def compose(self) -> ComposeResult:

        with Vertical(id="session-picker"):
            yield Static("Sessions", id="session-picker-title")
            yield SessionPickerSearchInput(
                placeholder="Search sessions in selected project",
                id="session-picker-search",
            )
            with Horizontal(id="session-picker-columns"):
                with Vertical(
                    id="session-picker-project-column",
                    classes="session-picker-column",
                ):
                    yield Static("Projects", classes="session-picker-column-title")
                    yield OptionList(id="session-picker-project-list", markup=False, compact=True)
                with Vertical(
                    id="session-picker-session-column",
                    classes="session-picker-column -active-column",
                ):
                    yield Static(
                        "",
                        id="session-picker-session-title",
                        classes="session-picker-column-title",
                    )
                    yield OptionList(id="session-picker-list", markup=False, compact=True)
            yield Static("", id="session-picker-help")

    def on_mount(self) -> None:

        self.query_one("#session-picker-search", Input).focus()
        self._refresh_project_list()
        self._refresh_session_list()
        self._update_help()

    def on_input_changed(self, event: Input.Changed) -> None:

        if event.input.id != "session-picker-search":
            return
        event.stop()
        self.search_value = event.value
        self._refresh_session_list()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "session-picker-search":
            return
        event.stop()
        self.action_select_cursor()

    def on_key(self, event: Key) -> None:

        actions = {
            "up": self.action_cursor_up,
            "down": self.action_cursor_down,
            "left": self.action_focus_projects,
            "right": self.action_focus_sessions,
            "enter": self.action_select_cursor,
        }
        action = actions.get(event.key)
        if action is not None:
            event.stop()
            action()

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:

        if event.option_list.id != "session-picker-project-list":
            return
        index = event.option_index
        if index == self.selected_project_index:
            return
        self.selected_project_index = index
        self._refresh_session_list()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        if event.option_list.id == "session-picker-project-list":
            self.selected_project_index = event.option_index
            self._refresh_session_list()
            self.action_focus_sessions()
            return
        self._select_visible_record()

    def action_cursor_up(self) -> None:
        self._active_list().action_cursor_up()

    def action_cursor_down(self) -> None:
        self._active_list().action_cursor_down()

    def action_focus_projects(self) -> None:
        self._set_active_column("projects")

    def action_focus_sessions(self) -> None:
        self._set_active_column("sessions")

    def action_select_cursor(self) -> None:
        if self.active_column == "projects":
            self.action_focus_sessions()
        else:
            self._select_visible_record()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def update_records(
        self,
        records: Sequence[SessionCompletionRecord],
        *,
        loading_other_projects: bool = False,
    ) -> None:

        selected_cwd = self.project_cwds[self.selected_project_index]
        session_list = self.query_one("#session-picker-list", OptionList)
        selected_session_id = None
        if session_list.highlighted is not None and session_list.highlighted < len(
            self.visible_records
        ):
            selected_session_id = self.visible_records[session_list.highlighted].id

        self.records = tuple(records)
        self.records_by_project = self._group_records_by_project()
        self.project_cwds = tuple(self.records_by_project)
        try:
            self.selected_project_index = self.project_cwds.index(selected_cwd)
        except ValueError:
            self.selected_project_index = 0
        self.loading_other_projects = loading_other_projects
        self.current_project_loaded = True
        self._refresh_project_list()
        self._refresh_session_list()

        if selected_session_id is not None:
            for index, record in enumerate(self.visible_records):
                if record.id == selected_session_id:
                    session_list.highlighted = index
                    break

    def finish_loading(self) -> None:

        self.loading_other_projects = False
        self._update_help()

    def _active_list(self) -> OptionList:
        selector = (
            "#session-picker-project-list"
            if self.active_column == "projects"
            else "#session-picker-list"
        )
        return self.query_one(selector, OptionList)

    def _set_active_column(self, column: Literal["projects", "sessions"]) -> None:
        self.active_column = column
        projects = self.query_one("#session-picker-project-column", Vertical)
        sessions = self.query_one("#session-picker-session-column", Vertical)
        projects.set_class(column == "projects", "-active-column")
        sessions.set_class(column == "sessions", "-active-column")
        self._update_help()

    def _select_visible_record(self) -> None:
        index = self.query_one("#session-picker-list", OptionList).highlighted
        if index is not None and index < len(self.visible_records):
            self.dismiss(self.visible_records[index].id)

    def _group_records_by_project(
        self,
    ) -> dict[Path, tuple[SessionCompletionRecord, ...]]:

        grouped: dict[Path, list[SessionCompletionRecord]] = {self.local_cwd: []}
        for record in self.records:
            grouped.setdefault(Path(record.cwd).resolve(), []).append(record)
        return {cwd: tuple(records) for cwd, records in grouped.items()}

    def _refresh_project_list(self) -> None:
        project_list = self.query_one("#session-picker-project-list", OptionList)
        items: list[str] = []
        for cwd in self.project_cwds:
            marker = "● " if cwd == self.local_cwd else "  "
            folder_name = cwd.name or str(cwd)
            items.append(f"{marker}{folder_name}")
        project_list.set_options(items)
        project_list.highlighted = self.selected_project_index

    def _refresh_session_list(self) -> None:
        selected_cwd = self.project_cwds[self.selected_project_index]
        self.query_one("#session-picker-session-title", Static).update(
            f"Recent sessions — {selected_cwd}"
        )
        project_records = self.records_by_project[selected_cwd]
        self.visible_records = _filter_session_records(project_records, self.search_value)
        session_list = self.query_one("#session-picker-list", OptionList)
        session_list.set_options(_session_picker_label(record) for record in self.visible_records)
        session_list.highlighted = 0 if self.visible_records else None
        self._update_help()

    def _update_help(self) -> None:
        if self.loading_other_projects and not self.current_project_loaded:
            text = "Loading sessions… - Escape closes"
        elif self.loading_other_projects:
            text = "Loading other projects… - Current sessions are ready - Escape closes"
        elif not self.visible_records and self.active_column == "sessions":
            text = "No matching sessions - Left selects a project - Escape closes"
        elif self.active_column == "projects":
            text = "Up/Down selects project - Right opens sessions - Escape closes"
        else:
            text = "Left selects project - Up/Down navigates - Enter resumes - Escape closes"
        self.query_one("#session-picker-help", Static).update(text)


class SkillPickerSearchInput(Input):


    BINDINGS: ClassVar[list[BindingEntry]] = [
        Binding("escape", "cancel", "Cancel", show=False, priority=True),
        Binding("up", "cursor_up", "Up", show=False, priority=True),
        Binding("down", "cursor_down", "Down", show=False, priority=True),
    ]

    def _picker(self) -> SkillPickerScreen:
        return cast(SkillPickerScreen, self.screen)

    def on_key(self, event: Key) -> None:

        if event.key == "up":
            event.stop()
            event.prevent_default()
            self.action_cursor_up()
        elif event.key == "down":
            event.stop()
            event.prevent_default()
            self.action_cursor_down()
        elif event.key == "escape":
            event.stop()
            event.prevent_default()
            self.action_cancel()
        elif event.key == "f1":
            event.stop()
            event.prevent_default()
            self.action_show_description()
        elif event.key == "ctrl+enter":
            event.stop()
            event.prevent_default()
            self.action_show_in_transcript()

    def action_cursor_up(self) -> None:
        self._picker().action_cursor_up()

    def action_cursor_down(self) -> None:
        self._picker().action_cursor_down()

    def action_cancel(self) -> None:
        self._picker().action_cancel()

    def action_show_description(self) -> None:
        self._picker().action_show_description()

    def action_show_in_transcript(self) -> None:
        self._picker().action_show_in_transcript()


@dataclass(frozen=True, slots=True)
class SkillPickerResult:


    skill: Skill
    action: Literal["insert", "transcript"]


class SkillPickerScreen(ModalScreen[SkillPickerResult | None]):


    BINDINGS: ClassVar[list[BindingEntry]] = [
        Binding("escape", "cancel", "Cancel", priority=True),
        Binding("up", "cursor_up", "Up", show=False, priority=True),
        Binding("down", "cursor_down", "Down", show=False, priority=True),
        Binding("enter", "select_cursor", "Insert", show=False, priority=True),
        Binding("f1", "show_description", "Description", show=False, priority=True),
        Binding("ctrl+enter", "show_in_transcript", "Transcript", show=False, priority=True),
    ]

    def __init__(self, skills: Sequence[Skill], *, theme: TuiTheme) -> None:
        super().__init__()
        self.skills = tuple(sorted(skills, key=lambda skill: skill.name.casefold()))
        self.visible_skills = self.skills
        self.theme = theme

    def compose(self) -> ComposeResult:
        with Vertical(id="skill-picker"):
            yield Static("Skills", id="skill-picker-title")
            yield SkillPickerSearchInput(placeholder="Search skills", id="skill-picker-search")
            yield ListView(id="skill-picker-list")
            yield Static("", id="skill-picker-help")

    def on_mount(self) -> None:
        self.query_one("#skill-picker-search", Input).focus()
        self._refresh_skill_list("")

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "skill-picker-search":
            event.stop()
            self._refresh_skill_list(event.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "skill-picker-search":
            event.stop()
            self._select_visible_skill()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        event.stop()
        self._select_visible_skill()

    def action_cursor_up(self) -> None:
        skill_list = self.query_one("#skill-picker-list", ListView)
        if skill_list.index is not None:
            skill_list.index = max(0, skill_list.index - 1)

    def action_cursor_down(self) -> None:
        skill_list = self.query_one("#skill-picker-list", ListView)
        if skill_list.index is not None:
            skill_list.index = min(len(self.visible_skills) - 1, skill_list.index + 1)

    def action_select_cursor(self) -> None:
        skill = self._selected_skill()
        if skill is not None:
            self.dismiss(SkillPickerResult(skill, "insert"))

    def action_show_description(self) -> None:
        skill = self._selected_skill()
        if skill is not None:
            self.app.push_screen(
                CommandOutputScreen(
                    f"Skill description: {skill.name}",
                    skill.description or "No description",
                    theme=self.theme,
                )
            )

    def action_show_in_transcript(self) -> None:
        skill = self._selected_skill()
        if skill is not None:
            self.dismiss(SkillPickerResult(skill, "transcript"))

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _selected_skill(self) -> Skill | None:
        index = self.query_one("#skill-picker-list", ListView).index
        if index is None or not self.visible_skills:
            return None
        return self.visible_skills[index]

    def _select_visible_skill(self) -> None:
        self.action_select_cursor()

    def _refresh_skill_list(self, search: str) -> None:
        query = search.casefold().strip()
        self.visible_skills = tuple(
            skill
            for skill in self.skills
            if not query
            or query in skill.name.casefold()
            or query in (skill.description or "").casefold()
        )
        skill_list = self.query_one("#skill-picker-list", ListView)
        skill_list.clear()
        skill_list.extend(
            ListItem(
                Horizontal(
                    Label(skill.name, classes="skill-picker-name", markup=False),
                    Label(
                        skill.description or "No description",
                        classes="skill-picker-description",
                        markup=False,
                    ),
                    classes="skill-picker-row",
                )
            )
            for skill in self.visible_skills
        )
        skill_list.index = 0 if self.visible_skills else None
        if not self.skills:
            help_text = "No skills loaded - Escape closes"
        elif not self.visible_skills:
            help_text = "No matching skills - Escape closes"
        else:
            help_text = "Enter inserts - F1 describes - Ctrl+Enter shows full skill"
        self.query_one("#skill-picker-help", Static).update(help_text)


class CommandOutputScroll(VerticalScroll):


    BINDINGS: ClassVar[list[BindingEntry]] = [
        Binding("up", "scroll_up", "Scroll up", show=False, priority=True),
        Binding("down", "scroll_down", "Scroll down", show=False, priority=True),
    ]

    def action_scroll_up(self) -> None:

        self.scroll_y = max(0, self.scroll_y - 1)

    def action_scroll_down(self) -> None:

        self.scroll_y = min(self.max_scroll_y, self.scroll_y + 1)


class CommandOutputScreen(ModalScreen[None]):


    auto_copy_selection: bool = False

    BINDINGS: ClassVar[list[BindingEntry]] = [
        Binding("escape", "close", "Close"),
        Binding("enter", "close", "Close"),
        Binding("up", "scroll_up", "Scroll up", show=False, priority=True),
        Binding("down", "scroll_down", "Scroll down", show=False, priority=True),
    ]

    def __init__(
        self,
        title: str,
        message: str,
        *,
        theme: TuiTheme,
        auto_copy_selection: bool = False,
    ) -> None:
        super().__init__()
        self.title_text = title
        self.message = message
        self.theme = theme
        self.auto_copy_selection = auto_copy_selection

    def compose(self) -> ComposeResult:

        with Vertical(id="command-output"):
            yield Static(self.title_text, id="command-output-title")
            with CommandOutputScroll(id="command-output-scroll"):
                yield Static(self.message, id="command-output-body", markup=False)
            yield Static(self._help_text(), id="command-output-help")

    def on_mount(self) -> None:

        self.query_one("#command-output-scroll", VerticalScroll).focus()

    def on_key(self, event: Key) -> None:

        if event.key == "up":
            event.stop()
            self.action_scroll_up()
        elif event.key == "down":
            event.stop()
            self.action_scroll_down()

    def action_close(self) -> None:

        self.dismiss(None)

    def _help_text(self) -> str:
        if self.auto_copy_selection:
            return "Select text to copy - Enter or Escape closes"
        return "Enter or Escape closes"

    def action_scroll_up(self) -> None:

        self.query_one("#command-output-scroll", CommandOutputScroll).action_scroll_up()

    def action_scroll_down(self) -> None:

        self.query_one("#command-output-scroll", CommandOutputScroll).action_scroll_down()


class LoginProviderSearchInput(Input):


    BINDINGS: ClassVar[list[BindingEntry]] = [
        Binding("escape", "cancel", "Cancel", show=False, priority=True),
        Binding("up", "cursor_up", "Up", show=False, priority=True),
        Binding("down", "cursor_down", "Down", show=False, priority=True),
    ]

    def _picker(self) -> LoginProviderPickerScreen:
        return cast(LoginProviderPickerScreen, self.screen)

    def on_key(self, event: Key) -> None:

        if event.key == "up":
            event.stop()
            event.prevent_default()
            self.action_cursor_up()
        elif event.key == "down":
            event.stop()
            event.prevent_default()
            self.action_cursor_down()
        elif event.key == "escape":
            event.stop()
            event.prevent_default()
            self.action_cancel()

    def action_cursor_up(self) -> None:

        self._picker().action_cursor_up()

    def action_cursor_down(self) -> None:

        self._picker().action_cursor_down()

    def action_cancel(self) -> None:

        self._picker().action_cancel()


class _LoginFlowAction(Enum):


    BACK = auto()


class LoginProviderPickerScreen(ModalScreen[str | _LoginFlowAction | None]):


    BINDINGS: ClassVar[list[BindingEntry]] = [
        Binding("escape", "cancel", "Cancel"),
        Binding("ctrl+d", "close", "Close", priority=True),
        Binding("up", "cursor_up", "Up", show=False),
        Binding("down", "cursor_down", "Down", show=False),
        Binding("enter", "select_cursor", "Select", show=False),
    ]

    def __init__(
        self,
        providers: Sequence[ProviderCatalogEntry],
        *,
        theme: TuiTheme,
        title: str = "Login",
        back_on_cancel: bool = False,
    ) -> None:
        super().__init__()
        self.providers = tuple(providers)
        self.back_on_cancel = back_on_cancel
        self.visible_providers = self.providers
        self.theme = theme
        self.title_text = title

    def compose(self) -> ComposeResult:

        with Vertical(id="login-provider-picker"):
            yield Static(self.title_text, id="login-provider-title")
            yield LoginProviderSearchInput(
                placeholder="Search providers",
                id="login-provider-search",
            )
            yield ListView(
                *[
                    ListItem(Label(_login_provider_label(provider), markup=False))
                    for provider in self.providers
                ],
                id="login-provider-list",
            )
            yield Static("Enter selects - Escape closes", id="login-provider-help")

    async def on_mount(self) -> None:

        self.query_one("#login-provider-search", Input).focus()
        await self._refresh_provider_list()

    async def on_input_changed(self, event: Input.Changed) -> None:

        if event.input.id != "login-provider-search":
            return
        event.stop()
        self.visible_providers = _filter_login_providers(self.providers, event.value)
        await self._refresh_provider_list()

    def on_input_submitted(self, event: Input.Submitted) -> None:

        if event.input.id != "login-provider-search":
            return
        event.stop()
        self._select_visible_provider()

    def on_key(self, event: Key) -> None:

        if event.key == "up":
            event.stop()
            self.action_cursor_up()
        elif event.key == "down":
            event.stop()
            self.action_cursor_down()
        elif event.key == "enter":
            event.stop()
            self.action_select_cursor()

    def on_list_view_selected(self, event: ListView.Selected) -> None:

        event.stop()
        self._select_visible_provider()

    def action_cursor_up(self) -> None:

        self.query_one("#login-provider-list", ListView).action_cursor_up()

    def action_cursor_down(self) -> None:

        self.query_one("#login-provider-list", ListView).action_cursor_down()

    def action_select_cursor(self) -> None:

        self._select_visible_provider()

    def action_cancel(self) -> None:

        self.dismiss(_LoginFlowAction.BACK if self.back_on_cancel else None)

    def action_close(self) -> None:

        self.dismiss(None)

    def _select_visible_provider(self) -> None:
        if not self.visible_providers:
            return
        provider_list = self.query_one("#login-provider-list", ListView)


        index = provider_list.index
        self.dismiss(self.visible_providers[0 if index is None else index].name)

    async def _refresh_provider_list(self) -> None:
        provider_list = self.query_one("#login-provider-list", ListView)


        await provider_list.clear()
        await provider_list.extend(
            [
                ListItem(Label(_login_provider_label(provider), markup=False))
                for provider in self.visible_providers
            ]
        )
        provider_list.index = 0 if self.visible_providers else None
        help_text = (
            "Enter selects - Escape closes"
            if self.visible_providers
            else "No matching providers - Escape closes"
        )
        self.query_one("#login-provider-help", Static).update(help_text)


@dataclass(frozen=True, slots=True)
class CustomProviderLoginResult:


    provider_name: str
    display_name: str
    base_url: str
    api_key_env: str
    models: tuple[str, ...]
    default_model: str
    api_key: str


class LoginMethodPickerScreen(ModalScreen[str | None]):


    BINDINGS: ClassVar[list[BindingEntry]] = [
        Binding("escape", "cancel", "Cancel", priority=True),
        Binding("ctrl+d", "cancel", "Close", priority=True),
        Binding("up", "cursor_up", "Up", show=False, priority=True),
        Binding("down", "cursor_down", "Down", show=False, priority=True),
        Binding("enter", "select_cursor", "Select", show=False, priority=True),
    ]

    def __init__(self, *, theme: TuiTheme) -> None:
        super().__init__()
        self.theme = theme

    def compose(self) -> ComposeResult:

        with Vertical(id="login-method-picker"):
            yield Static("Login", id="login-method-title")
            yield Static("Choose how to authenticate.", id="login-method-intro")
            yield LoginMethodListView(
                ListItem(
                    Label("Subscription — OAuth account", markup=False),
                    id="login-method-subscription",
                ),
                ListItem(
                    Label("API key — built-in provider", markup=False),
                    id="login-method-api-key",
                ),
                ListItem(
                    Label("Custom provider — OpenAI-compatible", markup=False),
                    id="login-method-custom",
                ),
                id="login-method-list",
            )
            yield Static("Enter selects - Escape/Ctrl+D closes", id="login-method-help")

    def on_mount(self) -> None:

        method_list = self.query_one("#login-method-list", ListView)
        method_list.index = 0
        method_list.focus()

    def on_key(self, event: Key) -> None:

        if event.key == "up":
            event.stop()
            self.action_cursor_up()
        elif event.key == "down":
            event.stop()
            self.action_cursor_down()
        elif event.key == "enter":
            event.stop()
            self.action_select_cursor()

    def on_button_pressed(self, event: Button.Pressed) -> None:

        if event.button.id == "login-method-subscription":
            self.dismiss("subscription")
        elif event.button.id == "login-method-api-key":
            self.dismiss("api-key")
        elif event.button.id == "login-method-custom":
            self.dismiss("custom")

    def on_list_view_selected(self, event: ListView.Selected) -> None:

        if event.item.id == "login-method-subscription":
            self.dismiss("subscription")
        elif event.item.id == "login-method-api-key":
            self.dismiss("api-key")
        elif event.item.id == "login-method-custom":
            self.dismiss("custom")

    def action_cancel(self) -> None:

        self.dismiss(None)

    def action_cursor_up(self) -> None:

        self._move_method_cursor(offset=-1)

    def action_cursor_down(self) -> None:

        self._move_method_cursor(offset=1)

    def action_select_cursor(self) -> None:

        self.query_one("#login-method-list", ListView).action_select_cursor()

    def _move_method_cursor(self, *, offset: int) -> None:
        method_list = self.query_one("#login-method-list", ListView)
        item_count = len(method_list.children)
        if item_count == 0:
            method_list.index = None
            return
        current_index = method_list.index if method_list.index is not None else 0
        method_list.index = (current_index + offset) % item_count


class LoginMethodListView(ListView):


    def action_cursor_up(self) -> None:

        self._move_cursor(offset=-1)

    def action_cursor_down(self) -> None:

        self._move_cursor(offset=1)

    def _move_cursor(self, *, offset: int) -> None:
        item_count = len(self.children)
        if item_count == 0:
            self.index = None
            return
        current_index = self.index if self.index is not None else 0
        self.index = (current_index + offset) % item_count


class ThemePickerScreen(ModalScreen[TuiThemeName | None]):


    BINDINGS: ClassVar[list[BindingEntry]] = [
        Binding("escape", "cancel", "Cancel", priority=True),
        Binding("up", "cursor_up", "Up", show=False, priority=True),
        Binding("down", "cursor_down", "Down", show=False, priority=True),
        Binding("enter", "select_cursor", "Select", show=False, priority=True),
    ]

    def __init__(
        self,
        *,
        current_theme: TuiThemeName,
        theme: TuiTheme,
        theme_names: tuple[TuiThemeName, ...],
    ) -> None:
        super().__init__()
        self.current_theme = current_theme
        self.theme = theme
        self.theme_names = theme_names

    def compose(self) -> ComposeResult:

        with Vertical(id="theme-picker"):
            yield Static("Theme", id="theme-picker-title")
            yield ListView(
                *[
                    ListItem(
                        Label(
                            _theme_picker_label(theme_name, current_theme=self.current_theme),
                            markup=False,
                        )
                    )
                    for theme_name in self.theme_names
                ],
                id="theme-picker-list",
            )
            yield Static("Enter selects - Escape closes", id="theme-picker-help")

    def on_mount(self) -> None:

        theme_list = self.query_one("#theme-picker-list", ListView)
        try:
            theme_list.index = self.theme_names.index(self.current_theme)
        except ValueError:
            theme_list.index = 0
        theme_list.focus()

    def on_key(self, event: Key) -> None:

        if event.key == "up":
            event.stop()
            self.action_cursor_up()
        elif event.key == "down":
            event.stop()
            self.action_cursor_down()
        elif event.key == "enter":
            event.stop()
            self.action_select_cursor()

    def on_list_view_selected(self, event: ListView.Selected) -> None:

        self.dismiss(self.theme_names[event.index])

    def action_cursor_up(self) -> None:

        self.query_one("#theme-picker-list", ListView).action_cursor_up()

    def action_cursor_down(self) -> None:

        self.query_one("#theme-picker-list", ListView).action_cursor_down()

    def action_select_cursor(self) -> None:

        self.query_one("#theme-picker-list", ListView).action_select_cursor()

    def action_cancel(self) -> None:

        self.dismiss(None)


class ModelPickerSearchInput(Input):


    BINDINGS: ClassVar[list[BindingEntry]] = [
        Binding("escape", "cancel", "Cancel", show=False, priority=True),
        Binding("tab", "toggle_mode", "Mode", show=False, priority=True),
        Binding("ctrl+i", "toggle_mode", "Mode", show=False, priority=True),
        Binding("up", "cursor_up", "Up", show=False, priority=True),
        Binding("down", "cursor_down", "Down", show=False, priority=True),
    ]

    def _picker(self) -> ModelPickerScreen:
        return cast(ModelPickerScreen, self.screen)

    def on_key(self, event: Key) -> None:

        if event.key == "up":
            event.stop()
            event.prevent_default()
            self.action_cursor_up()
        elif event.key == "down":
            event.stop()
            event.prevent_default()
            self.action_cursor_down()
        elif event.key in {"tab", "ctrl+i"}:
            event.stop()
            event.prevent_default()
            self.action_toggle_mode()
        elif event.key == "escape":
            event.stop()
            event.prevent_default()
            self.action_cancel()

    def action_cursor_up(self) -> None:

        self._picker().action_cursor_up()

    def action_cursor_down(self) -> None:

        self._picker().action_cursor_down()

    def action_toggle_mode(self) -> None:

        self._picker().action_toggle_mode()

    def action_cancel(self) -> None:

        self._picker().action_cancel()


class ModelPickerScreen(ModalScreen[ModelChoice | None]):


    BINDINGS: ClassVar[list[BindingEntry]] = [
        Binding("escape", "cancel", "Cancel"),
        Binding("tab", "toggle_mode", "Mode", show=False, priority=True),
        Binding("ctrl+i", "toggle_mode", "Mode", show=False, priority=True),
        Binding("up", "cursor_up", "Up", show=False),
        Binding("down", "cursor_down", "Down", show=False),
        Binding("enter", "accept_model", "Select", show=False),
    ]

    def __init__(
        self,
        choices: Sequence[ModelChoice],
        *,
        scoped_choices: Sequence[ModelChoice],
        current_model: str,
        provider_name: str,
        theme: TuiTheme,
        on_toggle_scoped: Callable[[ModelChoice], Sequence[ModelChoice]] | None = None,
        picker_kind: Literal["model", "scoped"] = "model",
    ) -> None:
        super().__init__()
        available = tuple(dict.fromkeys(choices))
        self.scoped_choices = tuple(dict.fromkeys(scoped_choices))
        self.unavailable_choices = frozenset(self.scoped_choices) - frozenset(available)
        self.choices = tuple(dict.fromkeys((*available, *self.scoped_choices)))
        self.visible_choices = self.choices
        self.current_model = current_model
        self.provider_name = provider_name
        self.theme = theme
        self.on_toggle_scoped = on_toggle_scoped
        self.picker_kind = picker_kind
        self.mode: Literal["all", "scoped"] = "all"
        self.search_value = ""

    def compose(self) -> ComposeResult:

        with Vertical(id="model-picker"):
            title = (
                f"Model: {self.provider_name}" if self.picker_kind == "model" else "Scoped models"
            )
            yield Static(title, id="model-picker-title")
            yield Static("", id="model-picker-tabs")
            yield ModelPickerSearchInput(placeholder="Search models", id="model-picker-search")
            yield ListView(
                *[
                    ListItem(
                        Label(
                            _model_picker_label(
                                choice,
                                current_model=self.current_model,
                                current_provider=self.provider_name,
                                scoped=choice in self.scoped_choices,
                                unavailable=choice in self.unavailable_choices,
                            ),
                            markup=False,
                        )
                    )
                    for choice in self.choices
                ],
                id="model-picker-list",
            )
            yield Static("", id="model-picker-help")

    def on_mount(self) -> None:

        search = self.query_one("#model-picker-search", Input)
        search.focus()
        self._refresh_model_list()

    def on_input_changed(self, event: Input.Changed) -> None:

        if event.input.id != "model-picker-search":
            return
        event.stop()
        self.search_value = event.value
        self._refresh_model_list()

    def on_input_submitted(self, event: Input.Submitted) -> None:

        if event.input.id != "model-picker-search":
            return
        event.stop()
        self._select_visible_choice()

    def _reset_model_list_index(self) -> None:

        model_list = self.query_one("#model-picker-list", ListView)
        if not self.visible_choices:
            model_list.index = None
            return
        try:
            model_list.index = self.visible_choices.index(
                ModelChoice(provider_name=self.provider_name, model=self.current_model)
            )
        except ValueError:
            model_list.index = 0

    def on_key(self, event: Key) -> None:

        if event.key == "up":
            event.stop()
            self.action_cursor_up()
        elif event.key == "down":
            event.stop()
            self.action_cursor_down()
        elif event.key == "enter":
            event.stop()
            self.action_accept_model()
        elif event.key in {"tab", "ctrl+i"}:
            event.stop()
            self.action_toggle_mode()

    def on_list_view_selected(self, event: ListView.Selected) -> None:

        event.stop()
        self._select_visible_choice()

    def action_cursor_up(self) -> None:

        self.query_one("#model-picker-list", ListView).action_cursor_up()

    def action_cursor_down(self) -> None:

        self.query_one("#model-picker-list", ListView).action_cursor_down()

    def action_accept_model(self) -> None:

        self._select_visible_choice()

    def action_toggle_mode(self) -> None:

        self.mode = "scoped" if self.mode == "all" else "all"
        self._refresh_model_list()

    def action_toggle_scoped(self) -> None:

        if self.on_toggle_scoped is None or not self.visible_choices:
            return
        model_list = self.query_one("#model-picker-list", ListView)
        index = model_list.index
        if index is None:
            return
        choice = self.visible_choices[index]
        self.scoped_choices = tuple(dict.fromkeys(self.on_toggle_scoped(choice)))
        self._refresh_model_list()

    def action_cancel(self) -> None:

        self.dismiss(None)

    def update_choices(
        self,
        choices: Sequence[ModelChoice],
        scoped_choices: Sequence[ModelChoice],
    ) -> None:

        available = tuple(dict.fromkeys(choices))
        self.scoped_choices = tuple(dict.fromkeys(scoped_choices))
        self.unavailable_choices = frozenset(self.scoped_choices) - frozenset(available)
        self.choices = tuple(dict.fromkeys((*available, *self.scoped_choices)))
        self._refresh_model_list()

    def _select_visible_choice(self) -> None:
        if not self.visible_choices:
            return
        model_list = self.query_one("#model-picker-list", ListView)
        index = model_list.index
        if index is None:
            return
        choice = self.visible_choices[index]
        if self.picker_kind == "scoped":
            self.action_toggle_scoped()
            return
        if choice in self.unavailable_choices:
            return
        self.dismiss(choice)

    def _refresh_model_list(self) -> None:
        base_choices = self.scoped_choices if self.mode == "scoped" else self.choices
        self.visible_choices = _filter_model_choices(base_choices, self.search_value)
        model_list = self.query_one("#model-picker-list", ListView)
        model_list.clear()
        model_list.extend(
            [
                ListItem(
                    Label(
                        _model_picker_label(
                            choice,
                            current_model=self.current_model,
                            current_provider=self.provider_name,
                            scoped=choice in self.scoped_choices,
                            unavailable=choice in self.unavailable_choices,
                        ),
                        markup=False,
                    )
                )
                for choice in self.visible_choices
            ]
        )
        self._reset_model_list_index()
        scope_count = len(self.scoped_choices)
        tabs = self.query_one("#model-picker-tabs", Static)
        if self.picker_kind == "scoped":
            if self.mode == "all":
                tabs.update("Tabs: ● All models  ○ Scoped models")
                help_text = (
                    "all models: no matching models - Tab switches to scoped models"
                    if not self.visible_choices
                    else (
                        "All models - Enter toggles scoped model - Tab switches tabs - "
                        f"{scope_count} scoped - active model is unchanged"
                    )
                )
            else:
                tabs.update("Tabs: ○ All models  ● Scoped models")
                help_text = (
                    "scoped models: no scoped models - Tab switches to all models"
                    if not self.visible_choices
                    else "Scoped models - Enter removes scoped model - Tab switches tabs"
                )
        elif self.mode == "all":
            tabs.update("Tabs: ● All models  ○ Scoped models")
            help_text = (
                "all models: no matching models - Tab switches to scoped models"
                if not self.visible_choices
                else (
                    "All models - Enter selects active model - Tab switches tabs - "
                    f"{scope_count} scoped"
                )
            )
        else:
            tabs.update("Tabs: ○ All models  ● Scoped models")
            help_text = (
                "scoped models: no matching models - Tab switches to all models"
                if not self.visible_choices
                else "Scoped models - Enter selects active model - Tab switches tabs"
            )
        self.query_one("#model-picker-help", Static).update(help_text)


class CustomProviderLoginScreen(ModalScreen[CustomProviderLoginResult | _LoginFlowAction | None]):


    BINDINGS: ClassVar[list[BindingEntry]] = [
        Binding("escape", "back", "Back"),
        Binding("ctrl+d", "close", "Close", priority=True),
    ]

    _INPUT_ORDER: ClassVar[tuple[str, ...]] = (
        "custom-provider-name",
        "custom-provider-display-name",
        "custom-provider-base-url",
        "custom-provider-api-key-env",
        "custom-provider-models",
        "custom-provider-default-model",
        "custom-provider-api-key",
    )

    def __init__(self, *, theme: TuiTheme) -> None:
        super().__init__()
        self.theme = theme

    def compose(self) -> ComposeResult:

        with Vertical(id="login-screen"):
            yield Static("Add custom provider", id="login-title")
            yield Static(
                "Short provider name is used in commands/config.",
                id="custom-provider-help",
            )
            yield Input(placeholder="Provider name/id, e.g. nebius", id="custom-provider-name")
            yield Input(
                placeholder="Display name shown in UI, e.g. Nebius AI Studio",
                id="custom-provider-display-name",
            )
            yield Input(
                placeholder="OpenAI-compatible base URL, e.g. https://api.studio.nebius.ai/v1",
                id="custom-provider-base-url",
            )
            yield Input(
                placeholder="API key environment variable fallback, e.g. NEBIUS_API_KEY",
                id="custom-provider-api-key-env",
            )
            yield Input(
                placeholder="Model ids, comma-separated, e.g. model-a, model-b",
                id="custom-provider-models",
            )
            yield Input(
                placeholder="Default model id, must be listed above",
                id="custom-provider-default-model",
            )
            yield Input(
                placeholder="Paste API key to save for this provider",
                password=True,
                id="custom-provider-api-key",
            )
            yield Static(
                "Enter advances/saves - Escape goes back - Ctrl+D closes",
                id="login-footer",
            )

    def on_mount(self) -> None:

        self.query_one("#custom-provider-name", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:

        input_id = event.input.id
        if input_id not in self._INPUT_ORDER:
            return
        event.stop()
        if input_id != self._INPUT_ORDER[-1]:
            self._focus_next(input_id)
            return
        result = self._collect_result()
        if result is not None:
            self.dismiss(result)

    def _focus_next(self, input_id: str) -> None:
        index = self._INPUT_ORDER.index(input_id)
        self.query_one(f"#{self._INPUT_ORDER[index + 1]}", Input).focus()

    def _collect_result(self) -> CustomProviderLoginResult | None:
        provider_name = self._field("custom-provider-name", "Provider name")
        if provider_name is None:
            return None
        base_url = self._field("custom-provider-base-url", "Base URL")
        if base_url is None:
            return None
        api_key_env = self._field("custom-provider-api-key-env", "API key environment variable")
        if api_key_env is None:
            return None
        models_text = self._field("custom-provider-models", "Model ids")
        if models_text is None:
            return None
        models = tuple(
            dict.fromkeys(item.strip() for item in models_text.split(",") if item.strip())
        )
        if not models:
            self.query_one("#custom-provider-help", Static).update(
                "At least one model id is required."
            )
            self.query_one("#custom-provider-models", Input).focus()
            return None
        default_model = self._field("custom-provider-default-model", "Default model")
        if default_model is None:
            return None
        if default_model not in models:
            self.query_one("#custom-provider-help", Static).update(
                "Default model must be included in the model list."
            )
            self.query_one("#custom-provider-default-model", Input).focus()
            return None
        api_key = self._field("custom-provider-api-key", "API key")
        if api_key is None:
            return None
        display_name = self.query_one("#custom-provider-display-name", Input).value.strip()
        return CustomProviderLoginResult(
            provider_name=provider_name,
            display_name=display_name or provider_name,
            base_url=base_url,
            api_key_env=api_key_env,
            models=models,
            default_model=default_model,
            api_key=api_key,
        )

    def _field(self, input_id: str, label: str) -> str | None:
        value = self.query_one(f"#{input_id}", Input).value.strip()
        if value:
            return value
        self.query_one("#custom-provider-help", Static).update(f"{label} is required.")
        self.query_one(f"#{input_id}", Input).focus()
        return None

    def action_back(self) -> None:

        self.dismiss(_LoginFlowAction.BACK)

    def action_close(self) -> None:

        self.dismiss(None)


class LoginScreen(ModalScreen[str | _LoginFlowAction | None]):


    BINDINGS: ClassVar[list[BindingEntry]] = [
        Binding("escape", "back", "Back"),
        Binding("ctrl+d", "close", "Close", priority=True),
    ]

    def __init__(self, provider: ProviderCatalogEntry, *, theme: TuiTheme) -> None:
        super().__init__()
        self.provider = provider
        self.theme = theme

    def compose(self) -> ComposeResult:

        with Vertical(id="login-screen"):
            yield Static(f"Login: {self.provider.display_name}", id="login-title")
            yield Static("Paste this provider's API key.", id="login-help")
            yield Input(placeholder="Paste API key", password=True, id="login-api-key")
            yield Static("Enter saves - Escape goes back - Ctrl+D closes", id="login-footer")

    def on_mount(self) -> None:

        self.query_one("#login-api-key", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:

        if event.input.id != "login-api-key":
            return
        event.stop()
        self.dismiss(event.value.strip() or None)

    def action_back(self) -> None:

        self.dismiss(_LoginFlowAction.BACK)

    def action_close(self) -> None:

        self.dismiss(None)


class OAuthLoginScreen(ModalScreen[OAuthCredential | _LoginFlowAction | None]):


    BINDINGS: ClassVar[list[BindingEntry]] = [
        Binding("escape", "back", "Back"),
        Binding("ctrl+d", "close", "Close", priority=True),
    ]

    def __init__(
        self,
        provider: ProviderCatalogEntry,
        *,
        theme: TuiTheme,
        login: Callable[[OAuthLoginCallbacks], Awaitable[OAuthCredential]] | None = None,
    ) -> None:
        super().__init__()
        self.provider = provider
        self.theme = theme
        self._login = login
        self._manual_code_future: asyncio.Future[str] | None = None
        self._manual_code_value: str | None = None
        self._prompt_allows_empty = False

    def compose(self) -> ComposeResult:

        with Vertical(id="login-screen"):
            yield Static(f"Login: {self.provider.display_name}", id="login-title")
            yield Static("Follow the provider instructions to complete login.", id="login-help")
            yield Static("", id="login-oauth-url")
            yield Input(
                placeholder="Paste redirect URL or authorization code",
                id="login-oauth-code",
            )
            yield Static("Enter submits - Escape goes back - Ctrl+D closes", id="login-footer")

    def on_mount(self) -> None:

        self.query_one("#login-oauth-code", Input).focus()
        self.run_worker(self._run_login(), exclusive=True)

    async def _run_login(self) -> None:
        try:
            oauth_provider = get_oauth_provider(self.provider.name)
            login = self._login or (oauth_provider.login if oauth_provider is not None else None)
            if login is None:
                raise RuntimeError(f"No OAuth implementation for {self.provider.name}")
            credential = await login(
                OAuthLoginCallbacks(
                    on_auth=self._show_auth,
                    on_device_code=self._show_device_code,
                    on_prompt=self._prompt_for_code,
                    on_select=self._select_option,
                    on_progress=self._show_progress,
                    on_manual_code_input=self._manual_code_input,
                )
            )
        except Exception as exc:
            self.query_one("#login-help", Static).update(f"OAuth failed: {exc}")
            return
        self.dismiss(credential)

    def _show_auth(self, info: OAuthAuthInfo) -> None:
        self._show_url(info.url)


        with suppress(Exception):
            self.app.copy_to_clipboard(info.url)
        self.notify("Authorization URL copied to clipboard.")
        if info.instructions:
            self.query_one("#login-help", Static).update(info.instructions)

    def _show_url(self, url: str) -> None:

        self.query_one("#login-oauth-url", Static).update(Text(url, style=Style(link=url)))

    def _show_device_code(self, info: OAuthDeviceCodeInfo) -> None:


        self._show_url(info.verification_uri)
        self.query_one("#login-help", Static).update(
            f"Open the URL and enter code: {info.user_code}"
        )

    def _show_progress(self, message: str) -> None:
        self.query_one("#login-help", Static).update(message)

    async def _prompt_for_code(self, prompt: OAuthPrompt) -> str:
        self.query_one("#login-help", Static).update(prompt.message)
        self._prompt_allows_empty = prompt.allow_empty
        try:
            return await self._manual_code_input()
        finally:
            self._prompt_allows_empty = False

    async def _select_option(self, prompt: OAuthSelectPrompt) -> str | None:
        self.query_one("#login-help", Static).update(prompt.message)
        return prompt.options[0].id if prompt.options else None

    async def _manual_code_input(self) -> str:
        if self._manual_code_value is not None:
            return self._manual_code_value
        loop = asyncio.get_running_loop()
        self._manual_code_future = loop.create_future()
        try:
            return await self._manual_code_future
        finally:
            self._manual_code_future = None

    def on_input_submitted(self, event: Input.Submitted) -> None:

        if event.input.id != "login-oauth-code":
            return
        event.stop()
        value = event.value.strip()
        if not value and not self._prompt_allows_empty:
            return
        self._manual_code_value = value
        if self._manual_code_future is not None and not self._manual_code_future.done():
            self._manual_code_future.set_result(value)

    def action_back(self) -> None:

        self._cancel_manual_code_input()
        self.dismiss(_LoginFlowAction.BACK)

    def action_close(self) -> None:

        self._cancel_manual_code_input()
        self.dismiss(None)

    def _cancel_manual_code_input(self) -> None:
        if self._manual_code_future is not None and not self._manual_code_future.done():
            self._manual_code_future.cancel()


RESERVED_EXTENSION_INTERCEPTOR_KEYS: frozenset[str] = frozenset({"ctrl+c", "ctrl+d"})


class OctoTuiApp(App[None]):


    TITLE = "Octo"
    CSS = """
    Screen {
        layout: vertical;
        background: $octo-screen-background;
        color: $octo-screen-text;
    }

    Toast {
        background: $octo-chrome-background;
        color: $octo-chrome-text;
    }

    Toast .toast--title {
        color: $octo-accent;
    }

    #workspace {
        height: 1fr;
    }

    #sidebar {
        width: 40;
        min-width: 36;
        height: 1fr;
        padding: 1 1 1 2;
        background: $octo-prompt-background;
        border: none;
    }

    #sidebar-scroll {
        height: 1fr;
        scrollbar-size-vertical: 1;
    }

    #sidebar-content {
        height: auto;
    }

    #sidebar .sidebar-separator {
        height: auto;
    }

    #sidebar .sidebar-resource-section {
        width: 1fr;
        height: auto;
        padding: 0;
        background: transparent;
        border: none;
    }

    #sidebar .sidebar-resource-section:focus-within {
        background-tint: transparent;
    }

    #sidebar .sidebar-resource-section CollapsibleTitle {
        width: 1fr;
        padding: 0 0 0 1;
        color: $octo-prompt-text;
        text-style: none;
        background: transparent;
    }

    #sidebar .sidebar-resource-section CollapsibleTitle:hover,
    #sidebar .sidebar-resource-section CollapsibleTitle:focus {
        color: $octo-prompt-text;
        text-style: none;
        background: transparent;
    }

    #sidebar .sidebar-resource-section Contents {
        padding: 1 0 0 1;
    }

    #sidebar .sidebar-section-title {
        height: 1;
        padding: 0 0 0 1;
        color: $octo-prompt-text;
        text-style: bold;
    }

    #sidebar .sidebar-file-list {
        width: 1fr;
        height: auto;
    }

    #sidebar .sidebar-resource-origin,
    #sidebar .sidebar-file-empty,
    #sidebar .sidebar-file-overflow {
        width: 1fr;
        height: auto;
        color: $octo-muted-text;
    }

    #sidebar .sidebar-file-item {
        width: 1fr;
        height: auto;
        color: $octo-muted-text;
        background: transparent;
    }

    #sidebar .sidebar-file-item:hover,
    #sidebar .sidebar-file-item:focus {
        color: $octo-highlight-text;
        background: $octo-highlight-background;
        text-style: underline;
    }

    #sidebar-extension-sections,
    #sidebar .extension-sidebar-section,
    #sidebar .extension-sidebar-body {
        width: 1fr;
        height: auto;
    }

    #sidebar .extension-sidebar-title {
        height: auto;
        padding: 0 0 0 1;
    }

    #sidebar .extension-sidebar-body {
        padding: 1 0 0 1;
    }

    #sidebar-brand {
        height: auto;
        color: $octo-prompt-text;
    }

    OctoTuiApp.-hide-sidebar #sidebar {
        display: none;
    }

    OctoTuiApp.-hide-sidebar #main-pane {
        padding-left: 1;
    }

    OctoTuiApp.-sidebar-right #sidebar {
        dock: right;
    }

    #main-pane {
        width: 1fr;
        padding: 1 1 0 1;
    }

    #transcript {
        height: 1fr;
        border: none;
        background: $octo-transcript-background;
        padding: 0 0 0 2;
        overflow-x: auto;
        scrollbar-size-vertical: 0;
        scrollbar-size-horizontal: 1;
    }

    #main-slot {
        display: none;
        height: 1fr;
        border: none;
        background: $octo-transcript-background;
        padding: 0 0 0 2;
        overflow-x: auto;
        scrollbar-size-vertical: 0;
        scrollbar-size-horizontal: 1;
    }

    #sidebar-file-editor {
        width: 1fr;
        height: 1fr;
        padding: 0 1;
    }

    #sidebar-file-editor-title {
        height: 1;
        color: $octo-accent;
        text-style: bold;
    }

    #sidebar-file-editor-path,
    #sidebar-file-editor-help,
    #sidebar-file-editor-status {
        height: 1;
        color: $octo-muted-text;
    }

    #sidebar-file-editor-path {
        margin-bottom: 1;
    }

    #sidebar-file-editor-input {
        height: 1fr;
        background: $octo-prompt-background;
        color: $octo-prompt-text;
        border: tall $octo-prompt-border;
    }

    #above-prompt-slot {
        height: auto;
        max-height: 8;
        margin: 0 1 0 1;
        padding: 0;
        background: $octo-screen-background;
    }

    #below-prompt-slot {
        height: auto;
        max-height: 8;
        margin: 0 1 0 1;
        padding: 0;
        background: $octo-screen-background;
    }

    #queued-messages {
        height: auto;
        max-height: 8;
        margin: 0 1 1 1;
        padding: 0 1;
        background: $octo-screen-background;
        color: $octo-muted-text;
    }

    #prompt-row {
        height: auto;
        margin: 0 1 1 1;
    }

    #prompt-prefix {
        width: 2;
        height: 3;
        padding: 0 0 0 0;
        margin: 0;
        content-align: center middle;
        color: $octo-accent;
        text-style: bold;
    }

    #prompt {
        width: 1fr;
        height: auto;
        background: $octo-prompt-background;
        color: $octo-prompt-text;
        border: none;
        border-left: tall transparent;
        margin: 0;
        padding: 1 1;
        max-height: 8;
    }

    #prompt:focus {
        border-left: tall $octo-prompt-border;
    }

    #prompt.-shell-mode {
        border-left: tall $octo-tool-running;
    }

    #compact-session-info {
        height: auto;
        max-height: 3;
        margin: 0 1 1 1;
        padding: 0 1;
        color: $octo-muted-text;
    }

    #autocomplete {
        height: auto;
        max-height: 18;
        margin: 0 1 1 1;
        padding: 0 1;
        background: $octo-autocomplete-background;
        color: $octo-screen-text;
        border: tall $octo-border;
        overflow-y: auto;
    }

    SessionPickerScreen,
    PromptTemplatePickerScreen,
    PromptTemplateEditorScreen,
    SkillPickerScreen,
    ToolsReferenceScreen,
    CommandOutputScreen {
        align: center middle;
    }

    #session-picker,
    #prompt-template-picker,
    #prompt-template-editor,
    #skill-picker,
    #tools-reference {
        width: 76;
        max-width: 90%;
        height: auto;
        max-height: 70%;
        padding: 1 2;
        background: $octo-chrome-background;
        border: tall $octo-border;
    }

    #session-picker-title,
    #prompt-template-picker-title,
    #prompt-template-editor-title,
    #skill-picker-title,
    #tools-reference-title {
        height: 1;
        color: $octo-chrome-text;
        text-style: bold;
        margin-bottom: 1;
    }

    #session-picker-search,
    #prompt-template-picker-search,
    #skill-picker-search,
    #tools-reference-search {
        height: 3;
        margin-bottom: 1;
        background: $octo-prompt-background;
        color: $octo-prompt-text;
        border: tall $octo-prompt-border;
    }

    #tools-reference-header {
        height: 1;
        color: $octo-muted-text;
        text-style: bold;
    }

    #prompt-template-editor {
        height: 80%;
    }

    #prompt-template-editor-path {
        height: 1;
        margin-bottom: 1;
        color: $octo-muted-text;
    }

    #prompt-template-editor-input {
        height: 1fr;
        background: $octo-prompt-background;
        color: $octo-prompt-text;
        border: tall $octo-prompt-border;
    }

    #session-picker-list,
    #prompt-template-picker-list,
    #skill-picker-list,
    #tools-reference-list {
        height: auto;
        max-height: 16;
        background: $octo-transcript-background;
        border: tall $octo-border;
    }

    ListView,
    OptionList {
        scrollbar-background: $octo-transcript-background;
        scrollbar-color: $octo-border;
        scrollbar-color-hover: $octo-highlight-background;
        scrollbar-background-hover: $octo-transcript-background;
        scrollbar-color-active: $octo-accent;
        scrollbar-background-active: $octo-transcript-background;
        scrollbar-size-vertical: 2;
    }

    ListView > ListItem.-highlight {
        background: $octo-highlight-background;
        color: $octo-highlight-text;
    }

    ListView > ListItem.-highlight Label {
        background: $octo-highlight-background;
        color: $octo-highlight-text;
    }

    OptionList > .option-list--option-highlighted {
        background: $octo-highlight-background;
        color: $octo-highlight-text;
    }

    #skill-picker-list .skill-picker-row {
        height: 1;
    }

    #skill-picker-list .skill-picker-name {
        width: 35%;
        text-style: bold;
    }

    #skill-picker-list .skill-picker-description {
        width: 65%;
        color: $octo-muted-text;
    }

    #skill-picker-list ListItem.-highlight .skill-picker-description {
        color: $octo-highlight-text;
    }

    #session-picker-help,
    #prompt-template-picker-help,
    #prompt-template-editor-help,
    #skill-picker-help,
    #tools-reference-help {
        height: 1;
        margin-top: 1;
        color: $octo-muted-text;
    }

    ExtensionSelectScreen,
    ExtensionConfirmScreen,
    ExtensionInputScreen,
    LocalBackendPickerScreen,
    LocalBackendScreen,
    LocalChoiceConfirmScreen,
    LocalConfigureScreen,
    LocalConfirmScreen,
    LocalModelActionScreen,
    LocalSearchResultsScreen,
    ProjectTrustScreen {
        align: center middle;
    }

    ProjectTrustScreen {
        background: $octo-screen-background 60%;
    }

    #extension-select,
    #extension-confirm,
    #extension-input {
        width: 76;
        max-width: 90%;
        height: auto;
        max-height: 70%;
        padding: 1 2;
        background: $octo-chrome-background;
        border: tall $octo-border;
    }

    #extension-select-title,
    #extension-confirm-title,
    #extension-input-title {
        height: auto;
        color: $octo-chrome-text;
        text-style: bold;
        margin-bottom: 1;
    }

    #extension-confirm-message {
        height: auto;
        color: $octo-chrome-text;
        margin-bottom: 1;
    }

    #extension-select-list,
    #extension-confirm-list {
        height: auto;
        max-height: 16;
        background: $octo-transcript-background;
        border: tall $octo-border;
    }

    #extension-input-field {
        background: $octo-transcript-background;
        border: tall $octo-border;
    }

    #extension-select-help,
    #extension-confirm-help,
    #extension-input-help {
        height: 1;
        margin-top: 1;
        color: $octo-muted-text;
    }

    #local-backend-picker,
    #local-backend-screen,
    #local-configure-screen,
    #local-confirm-screen,
    #local-model-action-screen,
    #local-search-results-screen {
        width: 82;
        max-width: 92%;
        height: auto;
        max-height: 82%;
        padding: 1 2;
        background: $octo-chrome-background;
        border: tall $octo-border;
    }

    #local-backend-picker-title,
    #local-backend-title,
    #local-configure-title,
    #local-confirm-title,
    #local-model-action-title,
    #local-search-results-title {
        height: auto;
        color: $octo-chrome-text;
        text-style: bold;
        margin-bottom: 1;
    }

    #local-backend-picker-help,
    #local-backend-help,
    #local-configure-screen Label,
    #local-confirm-message,
    #local-search-results-help {
        color: $octo-muted-text;
    }

    #local-backend-list,
    #local-backend-status,
    #local-backend-progress,
    #local-model-list,
    #local-action-menu,
    #local-confirm-list,
    #local-choice-list,
    #local-search-results-list,
    #local-configure-screen Input,
    #local-configure-screen Select,
    #local-model-action-input {
        background: $octo-transcript-background;
        border: tall $octo-border;
        margin-top: 1;
    }

    #local-backend-list,
    #local-model-list,
    #local-action-menu,
    #local-confirm-list,
    #local-choice-list,
    #local-search-results-list {
        height: auto;
        max-height: 16;
    }

    #local-model-list,
    #local-action-menu {
        max-height: 10;
    }

    #local-model-list:focus,
    #local-action-menu:focus {
        border: tall $octo-accent;
    }

    #local-model-list.local-section-inactive > ListItem.-highlight,
    #local-action-menu.local-section-inactive > ListItem.-highlight,
    #local-model-list.local-section-inactive > ListItem.-highlight Label,
    #local-action-menu.local-section-inactive > ListItem.-highlight Label {
        background: $octo-transcript-background;
        color: $octo-chrome-text;
    }

    #local-model-section-title,
    #local-action-section-title {
        height: 1;
        margin-top: 1;
        color: $octo-chrome-text;
        text-style: bold;
    }

    #local-backend-progress-bar {
        width: 100%;
        margin-top: 1;
    }

    #local-backend-progress-bar Bar {
        width: 1fr;
    }

    #local-backend-progress-bar Bar > .bar--bar,
    #local-backend-progress-bar Bar > .bar--complete,
    #local-backend-progress-bar Bar > .bar--indeterminate {
        color: $octo-accent;
        background: $octo-border;
    }

    #local-backend-picker-footer,
    #local-backend-footer,
    #local-configure-footer,
    #local-confirm-footer,
    #local-model-action-footer,
    #local-search-results-footer {
        height: 1;
        margin-top: 1;
        color: $octo-muted-text;
    }

    #local-backend-progress {
        min-height: 1;
        color: $octo-muted-text;
    }

    #project-trust-dialog {
        width: 76;
        max-width: 92%;
        height: auto;
        max-height: 90%;
        padding: 1 2;
        background: $octo-chrome-background;
        color: $octo-chrome-text;
        border: tall $octo-border;
    }

    #project-trust-title {
        color: $octo-chrome-text;
    }

    #project-trust-path-label,
    #project-trust-summary-label,
    #project-trust-boundary,
    #project-trust-help {
        color: $octo-muted-text;
    }

    #project-trust-list {
        background: $octo-transcript-background;
        color: $octo-screen-text;
        border: tall $octo-border;
    }

    #project-trust-list ListItem.-highlight,
    #project-trust-list ListItem.-highlight Label {
        background: $octo-highlight-background;
        color: $octo-highlight-text;
    }

    #command-output {
        width: 76;
        max-width: 90%;
        height: auto;
        max-height: 70%;
        padding: 1 2;
        background: $octo-chrome-background;
        color: $octo-chrome-text;
        border: tall $octo-border;
    }

    #command-output-title {
        height: 1;
        color: $octo-chrome-text;
        text-style: bold;
        margin-bottom: 1;
    }

    #command-output-scroll {
        height: auto;
        max-height: 18;
        background: $octo-transcript-background;
        border: tall $octo-border;
    }

    #command-output-body {
        color: $octo-screen-text;
        padding: 1;
    }

    #command-output-help {
        height: 1;
        margin-top: 1;
        color: $octo-muted-text;
    }

    LoginMethodPickerScreen,
    LoginProviderPickerScreen,
    ThemePickerScreen,
    ModelPickerScreen {
        align: center middle;
    }

    #login-method-picker,
    #login-provider-picker,
    #theme-picker,
    #model-picker {
        width: 76;
        max-width: 90%;
        height: auto;
        max-height: 70%;
        padding: 1 2;
        background: $octo-chrome-background;
        color: $octo-chrome-text;
        border: tall $octo-border;
    }

    #login-method-title,
    #login-provider-title,
    #theme-picker-title,
    #model-picker-title {
        height: 1;
        color: $octo-chrome-text;
        text-style: bold;
        margin-bottom: 1;
    }

    #model-picker-tabs {
        height: 1;
        color: $octo-muted-text;
        margin-bottom: 1;
    }

    #login-method-list,
    #login-provider-list,
    #theme-picker-list,
    #model-picker-list {
        height: auto;
        max-height: 12;
        background: $octo-transcript-background;
        color: $octo-screen-text;
        border: tall $octo-border;
    }

    #login-method-list ListItem Label,
    #login-provider-list ListItem Label,
    #theme-picker-list ListItem Label,
    #model-picker-list ListItem Label {
        color: $octo-screen-text;
    }

    #login-method-list ListItem.-highlight Label,
    #login-provider-list ListItem.-highlight Label,
    #theme-picker-list ListItem.-highlight Label,
    #model-picker-list ListItem.-highlight Label {
        background: $octo-highlight-background;
        color: $octo-highlight-text;
    }

    #login-method-intro {
        height: 1;
        color: $octo-muted-text;
        margin-bottom: 1;
    }

    #login-method-list {
        height: auto;
        max-height: 10;
    }

    #login-provider-search,
    #model-picker-search {
        height: 3;
        margin-bottom: 1;
        background: $octo-prompt-background;
        color: $octo-prompt-text;
        border: tall $octo-prompt-border;
    }

    #login-method-help,
    #login-provider-help,
    #theme-picker-help,
    #model-picker-help {
        height: 1;
        margin-top: 1;
        color: $octo-muted-text;
    }

    CustomProviderLoginScreen,
    LoginScreen,
    OAuthLoginScreen {
        align: center middle;
    }

    #login-screen {
        width: 72;
        max-width: 92%;
        height: auto;
        max-height: 100%;
        overflow-y: auto;
        padding: 1 2;
        background: $octo-chrome-background;
        border: tall $octo-border;
    }

    #login-title {
        height: 1;
        color: $octo-chrome-text;
        text-style: bold;
        margin-bottom: 1;
    }

    #login-help,
    #custom-provider-help {
        height: 1;
        color: $octo-muted-text;
        margin-bottom: 1;
    }

    #login-api-key,
    #login-oauth-code,
    #custom-provider-name,
    #custom-provider-display-name,
    #custom-provider-base-url,
    #custom-provider-api-key-env,
    #custom-provider-models,
    #custom-provider-default-model,
    #custom-provider-api-key {
        background: $octo-prompt-background;
        color: $octo-prompt-text;
        border: tall $octo-prompt-border;
        margin-bottom: 1;
    }

    #login-oauth-url {
        min-height: 1;
        height: auto;
        color: $octo-chrome-text;
        margin-bottom: 1;
    }

    #login-footer {
        height: 1;
        color: $octo-muted-text;
    }
    """
    BINDINGS: ClassVar[list[BindingEntry]] = []

    def __init__(
        self,
        session: CodingSession,
        *,
        tui_settings: TuiSettings | None = None,
        startup_message: str | None = None,
        startup_notice: str | None = None,
        startup_alerts: Sequence[str] = (),
        startup_notices: Sequence[str] = (),
        initial_prompt: str | None = None,
    ) -> None:
        self.tui_settings = tui_settings or TuiSettings()
        self.startup_message = startup_message
        legacy_notices = (startup_notice,) if startup_notice else ()
        self.startup_notices = tuple((*startup_notices, *legacy_notices))
        self.initial_prompt = initial_prompt


        self._sidebar_visibility_override: bool | None = None
        super().__init__()
        self._register_octo_textual_themes()


        self._applying_settings_theme = True
        self.theme = self.tui_settings.resolved_theme.name
        self._applying_settings_theme = False
        self._bindings = BindingsMap(_app_bindings(self.tui_settings.keybindings))
        self.session = session
        self.state = TuiState(skills=session.skills)
        for alert in startup_alerts:
            self.state.add_item("status", alert, highlight="alert")
        for notice in self.startup_notices:
            self.state.add_item("status", notice)
        if self.tui_settings.theme != self.tui_settings.resolved_theme.name:
            self.state.add_item(
                "status",
                f"Theme '{self.tui_settings.theme}' was not found; "
                f"using {self.tui_settings.resolved_theme.name}.",
            )
        self._prompt_history: tuple[str, ...] = ()
        self._load_session_messages_from_session()
        self.adapter = TuiEventAdapter(self.state)


        self._extension_slot_widgets: dict[str, Widget] = {}
        self._extension_slot_mounted: dict[str, Widget] = {}
        self._extension_slot_slot_ids: dict[str, str] = {}
        self._topic_web: TopicWeb | None = None
        self._topic_unsubscribe = None
        self._extension_slot_locks: dict[str, asyncio.Lock] = {}
        self._extension_key_interceptors: list[KeyInterceptor] = []
        self._extension_sidebar_contributions: dict[tuple[str, str], _SidebarContribution] = {}
        self._extension_sidebar_widgets: dict[tuple[str, str], Widget] = {}
        self._extension_sidebar_mounted: dict[tuple[str, str], Widget] = {}
        self._extension_sidebar_lock = asyncio.Lock()
        self._extension_sidebar_theme: TuiTheme | None = None
        self._extension_main_view: _MainViewHandle | None = None
        self._extension_main_view_mounted: Widget | None = None
        self._extension_main_view_lock = asyncio.Lock()
        self._extension_swap_tasks: set[asyncio.Task[None]] = set()
        self._extension_component_failures_reported: set[str] = set()
        self._connect_extension_runtime(session)
        self._prompt_worker: Worker[None] | None = None
        self._compaction_worker: Worker[None] | None = None
        self._compacting = False
        self._compaction_run_id = 0
        self._prompt_run_id = 0
        self._optimistic_user_messages: list[tuple[int, str]] = []
        self._completion_state = CompletionState()
        self._completion_visible_line_budget: int | None = None
        self._activity_frame = 0
        self._activity_timer: Timer | None = None
        self._last_tool_timer_refresh_at = 0.0
        self._last_activity_indicator_key: tuple[object, ...] | None = None
        self._last_queue_render_key: tuple[object, ...] | None = None
        self._terminal_title = TerminalTitleController()
        self._terminal_notification = TerminalNotificationController(
            self.tui_settings.turn_notification
        )
        self._app_has_focus = True
        self._active_notification_keys: set[tuple[str, str]] = set()
        self._supports_pyperclip: bool | None = None
        self._sync_session_title()

    async def prompt_project_trust(self, request: ProjectTrustRequest) -> TrustChoice | None:

        return await self.push_screen_wait(ProjectTrustScreen(request))

    def _sync_session_title(self) -> None:

        self._sync_terminal_title()

    def _is_working(self) -> bool:

        return self.state.running or self._compacting

    def _sync_terminal_title(self) -> None:

        self._terminal_title.update(
            getattr(self.session, "session_title", None),
            running=self._is_working(),
            frame=self._activity_frame,
        )

    def _sync_text_selection_state(self) -> None:

        type(self).ALLOW_SELECT = not self.state.running
        if self.state.running and self.screen_stack:
            with suppress(Exception):
                self.screen.clear_selection()

    def copy_to_clipboard(self, text: str) -> None:

        if self._supports_pyperclip is None:
            try:
                import pyperclip
            except ImportError:
                self._supports_pyperclip = False
            else:
                self._supports_pyperclip = True
        if self._supports_pyperclip:
            import pyperclip

            with suppress(Exception):
                pyperclip.copy(text)
        super().copy_to_clipboard(text)

    def _register_octo_textual_themes(self) -> None:

        self._registered_themes.clear()
        for theme_name in available_tui_theme_names():
            self.register_theme(_textual_theme_for_octo_theme(theme_name))

    def _reload_session_themes(self) -> None:

        theme_dirs = getattr(self.session, "theme_dirs", None)
        if theme_dirs is None:
            trust_resolution = getattr(self.session, "project_trust_resolution", None)
            trusted = trust_resolution is None or trust_resolution.trusted
            theme_dirs = OctoResourcePaths(
                cwd=self.session.cwd,
                project_resources_enabled=trusted,
            ).themes_dirs
        try:
            custom_themes, diagnostics = load_custom_tui_themes(theme_dirs)
        except (OSError, RuntimeError) as exc:


            custom_themes = {}
            diagnostics = []
            self._notify(f"Could not reload custom themes: {exc}", severity="error")

        set_custom_tui_themes(custom_themes)
        self._register_octo_textual_themes()
        resolved_theme = self.tui_settings.resolved_theme.name
        self._applying_settings_theme = True
        try:
            if self.theme == resolved_theme:

                self._watch_theme(resolved_theme)
            else:
                self.theme = resolved_theme
        finally:
            self._applying_settings_theme = False
        for diagnostic in diagnostics:
            severity: Literal["information", "warning", "error"] = (
                "error"
                if diagnostic.severity == "error"
                else "warning"
                if diagnostic.severity == "warning"
                else "information"
            )
            self._notify(diagnostic.format(), severity=severity)

    def _watch_theme(self, theme_name: str) -> None:

        super()._watch_theme(theme_name)
        if theme_name not in available_tui_theme_names():
            return
        if getattr(self, "_applying_settings_theme", False):
            return
        octo_theme: TuiThemeName = theme_name
        if self.tui_settings.theme == octo_theme:
            return
        self._replace_tui_settings(theme=octo_theme)
        save_tui_settings(self.tui_settings)

    def get_theme_variable_defaults(self) -> dict[str, str]:

        variables = super().get_theme_variable_defaults()
        return {**variables, **_theme_css_variables(self.tui_settings.resolved_theme)}

    def compose(self) -> ComposeResult:

        with Horizontal(id="workspace"):
            yield SessionSidebar(id="sidebar")
            with Vertical(id="main-pane"):
                yield TranscriptView(
                    id="transcript",
                    min_width=1,
                    wrap=True,
                    highlight=True,
                    markup=False,
                )


                yield Container(id="main-slot")
                yield Container(id="above-prompt-slot")
                yield Static("", id="topic-indicator", markup=False)
                yield Static("", id="queued-messages")
                with Horizontal(id="prompt-row"):
                    yield Static("Octo", id="prompt-prefix")
                    yield PromptInput(
                        placeholder=(
                            "Ask Octo…  Enter submits, "
                            f"{_key_hint(self.tui_settings.keybindings.insert_newline)} "
                            "inserts a newline"
                        ),
                        id="prompt",
                        tui_keybindings=self.tui_settings.keybindings,
                    )
                yield CompactSessionInfo(id="compact-session-info")
                yield Static("", id="autocomplete")
                yield Container(id="below-prompt-slot")

    async def on_mount(self) -> None:

        subscribe = getattr(self.session, "subscribe_topics", None)
        if callable(subscribe):
            self._topic_unsubscribe = subscribe(lambda: self.call_later(self._refresh_chrome_if_mounted))
        prompt = self.query_one(PromptInput)
        prompt.shell_mode_style = self.tui_settings.resolved_theme.role_styles["tool"].border
        self._sync_prompt_shell_mode(prompt.text)
        prompt.focus()
        self._update_responsive_layout(self.size.width, self.size.height)
        self._apply_sidebar_position()
        self._refresh()
        self._sync_text_selection_state()
        self._refresh_completions()
        if self.startup_message:
            self._notify(self.startup_message, severity="warning")


        await self.session.emit_pending_session_start()
        if self.initial_prompt and self.initial_prompt.strip():
            await self._submit_prompt(self.initial_prompt.strip())

    async def on_event(self, event: events.Event) -> None:

        if (
            isinstance(event, events.Key)
            and not event.is_forwarded
            and event.key not in RESERVED_EXTENSION_INTERCEPTOR_KEYS
            and self._extension_key_interceptors
            and len(self.screen_stack) <= 1
            and self._run_extension_key_interceptors(event, self._current_prompt_text())
        ):
            event.stop()
            event.prevent_default()
            return
        await super().on_event(event)

    def on_unmount(self) -> None:
        if self._topic_web is not None:
            self._topic_web.close()
        if self._topic_unsubscribe is not None:
            self._topic_unsubscribe()

        if self._activity_timer is not None:
            self._activity_timer.stop()
            self._activity_timer = None
        self._terminal_title.restore()
        self._clear_extension_components()

    def on_app_blur(self) -> None:

        self._app_has_focus = False

    def on_app_focus(self) -> None:

        self._app_has_focus = True

    def on_paste(self, event: events.Paste) -> None:

        if self.focused is not None:
            return
        try:
            prompt = self.screen.query_one("#prompt", PromptInput)
        except NoMatches:

            return
        event.stop()
        prompt.insert_pasted_text(event.text)

    def on_resize(self, event: Resize) -> None:

        self._completion_visible_line_budget = None
        self._update_responsive_layout(event.size.width, event.size.height)

    @on(SidebarFileItem.OpenRequested)
    def on_sidebar_file_open_requested(self, event: SidebarFileItem.OpenRequested) -> None:

        event.stop()
        item = event.item
        current = self._extension_main_view
        if (
            current is not None
            and isinstance(current.widget, SidebarFileEditor)
            and current.widget.is_dirty
        ):
            message = "Save or close the current file before opening another."
            self._notify(message, severity="warning")
            current.widget.query_one("#sidebar-file-editor-status", Static).update(message)
            current.widget.query_one("#sidebar-file-editor-input", TextArea).focus()
            return
        try:
            source, snapshot = _read_sidebar_file(item.path)
        except (OSError, UnicodeDecodeError) as exc:
            self._notify(f"Could not read {item.path}: {exc}", severity="error")
            return
        self._open_extension_main_view(
            lambda handle, theme: SidebarFileEditor(
                handle=handle,
                path=item.path,
                label=item.file_label,
                kind=item.kind,
                source=source,
                snapshot=snapshot,
            )
        )

    def on_click(self, event: events.Click) -> None:

        if event.button != 1:
            return
        if self._extension_main_view is not None:


            return
        with suppress(NoMatches):
            self.screen.query_one("#prompt", PromptInput).focus()

    @on(events.TextSelected)
    async def on_text_selected(self) -> None:

        active_screen = self.screen
        if not (
            self.tui_settings.auto_copy_selection
            or getattr(active_screen, "auto_copy_selection", False)
        ):
            return
        selection = active_screen.get_selected_text()
        if selection:
            self.copy_to_clipboard(selection)
            self._notify("Copied selection to clipboard.")

    def on_text_area_changed(self, event: TextArea.Changed) -> None:

        if event.text_area.id != "prompt":
            return
        prompt = self.query_one("#prompt", PromptInput)
        prompt.sync_pending_paste()

        text = prompt.text
        self._sync_prompt_shell_mode(text)
        self._completion_state = self._build_completion_state(text, cursor=prompt.cursor_position)
        self._refresh_completions()

    def on_text_area_selection_changed(self, event: TextArea.SelectionChanged) -> None:

        if event.text_area.id != "prompt":
            return

        self.call_later(self._close_completions_if_caret_left_token)

    def _close_completions_if_caret_left_token(self) -> None:
        if not self._completion_state.items:
            return
        item = self._completion_state.items[0]
        cursor = self.query_one("#prompt", PromptInput).cursor_position
        if item.start < cursor <= item.end:
            return
        self._completion_state = CompletionState()
        self._refresh_completions()

    async def action_submit_prompt(self) -> None:

        selected = self._completion_state.selected
        if selected is not None and selected.kind is not CompletionKind.FILE_REFERENCE:
            prompt = self.query_one("#prompt", PromptInput)
            text_before_completion = prompt.text
            self.action_accept_completion()
            if prompt.text != text_before_completion:
                return
        await self._submit_prompt_from_editor(streaming_behavior="steer")

    async def action_submit_follow_up(self) -> None:

        await self._submit_prompt_from_editor(streaming_behavior="follow_up")

    async def _submit_prompt_from_editor(
        self,
        *,
        streaming_behavior: Literal["steer", "follow_up"],
    ) -> None:
        prompt = self.query_one("#prompt", PromptInput)
        raw_text = prompt.text_for_submission()

        text = raw_text.strip()
        if not text:
            prompt.text = ""
            prompt._clear_pending_paste()
            self._completion_state = CompletionState()
            self._refresh_completions()
            return

        if self._is_compaction_active():
            if text.startswith("/compact"):
                self._notify("A compaction is already running.", severity="warning")
            else:
                prompt.text = raw_text
                prompt.move_cursor(_text_end_location(raw_text))
                self._notify(
                    "Compaction is still running. You can keep editing, but wait to submit.",
                    severity="warning",
                )
            return

        prompt.text = ""
        prompt._clear_pending_paste()
        self._completion_state = CompletionState()
        self._refresh_completions()

        terminal_command = parse_terminal_command(text)
        if terminal_command is not None:
            self.run_worker(
                self._run_terminal_command(
                    terminal_command.command,
                    add_to_context=terminal_command.add_to_context,
                ),
                group="terminal-command",
                exclusive=True,
            )
            return

        command = self.session.handle_command(text)
        if command.handled:
            if command.topic_arguments is not None:
                try:
                    command = replace(command, message=await self.session.topic_command(command.topic_arguments))
                except ValueError as exc:
                    command = replace(command, message=str(exc))
            if command.map_requested:
                try:
                    if self._topic_web is None:
                        self._topic_web = TopicWeb(self.session)
                    import webbrowser
                    opened = await asyncio.to_thread(webbrowser.open, self._topic_web.url)
                    command = replace(command, message=("话题树已连接当前终端：" if opened else "请在浏览器打开话题树：") + self._topic_web.url)
                except (ValueError, OSError) as exc:
                    command = replace(command, message=str(exc))
            if command.clear_requested:
                self.state.clear()
            if command.reload_requested:
                try:
                    summary = await self.session.reload()
                except ValueError as exc:
                    command = replace(command, message=f"Could not reload: {exc}")
                else:
                    self._reload_session_themes()
                    command = replace(command, message=format_reload_summary(summary))
            if command.new_session_requested:
                await self._new_session()
            if command.compact_summary is not None:
                if self._is_compaction_active():
                    self._notify("A compaction is already running.", severity="warning")
                elif self._is_agent_or_queue_active():
                    prompt.text = raw_text
                    prompt.move_cursor(_text_end_location(raw_text))
                    self._notify(
                        "Wait for the current agent turn and queued messages to finish "
                        "before compacting.",
                        severity="warning",
                    )
                    return
                else:
                    self._compaction_worker = self.run_worker(
                        self._run_compaction(command.compact_summary),
                        exclusive=False,
                    )
            if command.export_requested:
                try:
                    exported_path = await self.session.export(
                        command.export_destination,
                        format=command.export_format,
                    )
                    self._append_command_message(
                        text,
                        f"Exported session to {exported_path}",
                    )
                except Exception as exc:
                    self._notify(f"Could not export session: {exc}", severity="error")
            if command.resume_session_id is not None:
                await self._resume_session(command.resume_session_id)
            if command.resume_picker_requested:
                self.action_open_session_picker()
            if command.prompts_picker_requested:
                self._open_prompt_template_picker()
            if command.login_picker_requested:
                self._open_login_picker()
            if command.custom_provider_login_requested:
                self._open_custom_provider_login()
            if command.local_requested:
                self._open_local_backend_picker()
            if command.sidebar_toggle_requested:
                self._toggle_sidebar_visibility()
            if command.login_provider is not None:
                self._open_login(command.login_provider, method=command.login_method)
            if command.logout_picker_requested:
                self._open_logout_picker()
            if command.logout_provider is not None:
                self._logout(command.logout_provider)
            if command.model_selection_model is not None:
                self.run_worker(
                    self._switch_model(
                        ModelChoice(
                            provider_name=command.model_selection_provider
                            or self.session.provider_name,
                            model=command.model_selection_model,
                        )
                    ),
                    exclusive=False,
                )
            if command.model_picker_requested:
                self._open_model_picker()
            if command.tools_picker_requested:
                self._open_tools_reference()
            if command.scoped_models_picker_requested:
                self._open_scoped_models_picker()
            if command.skills_picker_requested:
                self._open_skills_picker()
            if command.theme_picker_requested:
                self._open_theme_picker()
            if command.session_name is not None:
                try:
                    await self.session.set_session_name(command.session_name)
                except ValueError as exc:
                    self._notify(f"Could not rename session: {exc}", severity="error")
                    self._refresh()
                    return
                self._sync_session_title()
            if command.thinking_level is not None:
                await self._set_thinking_level(command.thinking_level)
            if command.theme is not None:
                self._set_tui_theme(command.theme)
            self.state.set_skills(self.session.skills)
            if command.message:
                if command.topic_arguments is not None or command.map_requested:
                    self._append_command_message(text, command.message)
                elif _command_message_uses_notification(text, command.message):
                    self._notify(command.message)
                elif _command_message_uses_transcript(text):
                    self._append_command_message(
                        text,
                        command.message,
                        system_prompt_inspection=command.system_prompt_inspection,
                    )
                else:
                    self._show_command_message(text, command.message)
            self._refresh()
            if command.exit_requested:
                self.exit()
            return

        if self.state.running:
            self._remember_prompt(text)
            await self._queue_prompt(text, streaming_behavior=streaming_behavior)
            return

        self._remember_prompt(text)
        await self._submit_prompt(text)

    def _remember_prompt(self, text: str) -> None:

        if not text.strip():
            return
        self._prompt_history = (*self._prompt_history, text)

    def _load_session_messages_from_session(self) -> None:

        self.state.load_messages(self.session.messages)
        self._prompt_history = tuple(
            message.text
            for message in self.session.messages
            if isinstance(message, UserMessage) and message.text.strip()
        )

    def _is_compaction_active(self) -> bool:

        worker = self._compaction_worker
        if worker is not None and not worker.is_finished and not worker.is_cancelled:
            return True
        return self._compacting

    def _is_agent_or_queue_active(self) -> bool:

        self._sync_queue_state()
        worker = self._prompt_worker
        is_worker_active = worker is not None and not worker.is_finished and not worker.is_cancelled
        is_session_running = bool(getattr(self.session, "is_running", False))
        return (
            self.state.running
            or is_session_running
            or is_worker_active
            or self.state.queued_message_count > 0
        )

    async def _run_compaction(self, summary: str) -> None:

        self._compaction_run_id += 1
        run_id = self._compaction_run_id
        self._compacting = True
        try:
            self.state.clear()
            self.state.add_item("status", "Compacting session…")
            self._refresh()
            compact_message = await self.session.compact(summary)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            self._notify(f"Error: {exc}", severity="error")
            return
        finally:


            if self._compaction_run_id == run_id:
                self._compacting = False
                self._compaction_worker = None
                self._refresh_chrome_if_mounted()
        self.state.clear()
        self.state.set_skills(self.session.skills)
        self._load_session_messages_from_session()
        self._notify(compact_message)
        self._refresh()
        if not self._app_has_focus:
            self._terminal_notification.notify_turn_finished()

    async def _submit_prompt(
        self,
        text: str,
        *,
        source: Literal["interactive", "extension"] = "interactive",
        custom_type: str | None = None,
        details: dict[str, JSONValue] | None = None,
    ) -> None:

        self._prompt_run_id += 1
        run_id = self._prompt_run_id


        if custom_type is None and _should_optimistically_render_prompt(text):
            self._optimistic_user_messages.append((run_id, text))
            await self._append_optimistic_user_message(text)
        self._prompt_worker = self.run_worker(
            self._run_prompt(text, run_id, source=source, custom_type=custom_type, details=details,
                             topic_id=getattr(self.session, "current_topic_id", "main")),
            exclusive=True,
        )

    async def _append_optimistic_user_message(
        self,
        text: str,
        *,
        custom_type: str | None = None,
        details: dict[str, JSONValue] | None = None,
    ) -> None:

        start_index = len(self.state.items)
        self.state.add_user_message(text, custom_type=custom_type, details=details)
        self._follow_transcript_output()
        if not self.screen_stack:
            self._refresh()
            return
        theme = self.tui_settings.resolved_theme
        try:
            transcript = self.query_one("#transcript", TranscriptView)
        except NoMatches:
            self._refresh()
            return
        for item in self.state.items[start_index:]:
            await transcript.append_item(
                item,
                theme=theme,
                show_tool_results=self.state.show_tool_results,
                scroll_end=True,
                custom_markup=self.state.resolve_custom_markup(
                    item, expanded=self.state.show_tool_results
                ),
            )
        self._refresh_chrome(theme=theme)

    def _consume_optimistic_user_event(self, event: CodingSessionEvent, *, run_id: int) -> bool:

        if not isinstance(event, MessageEndEvent) or not isinstance(event.message, UserMessage):
            return False
        for index, (pending_run_id, pending_text) in enumerate(self._optimistic_user_messages):
            if pending_run_id == run_id and pending_text == event.message.content:
                del self._optimistic_user_messages[index]
                return True
        return False

    def _replace_transformed_optimistic_user_message(
        self, event: CodingSessionEvent, *, run_id: int
    ) -> bool:

        if not isinstance(event, MessageEndEvent) or not isinstance(event.message, UserMessage):
            return False
        for index, (pending_run_id, pending_text) in enumerate(self._optimistic_user_messages):
            if pending_run_id != run_id:
                continue
            del self._optimistic_user_messages[index]
            for item in reversed(self.state.items):
                if item.role == "user" and item.text == pending_text:
                    item.text = event.message.text
                    break
            self._refresh()
            self._sync_session_title()
            return True
        return False

    def _clear_optimistic_user_messages(self, *, run_id: int) -> None:

        self._optimistic_user_messages = [
            pending for pending in self._optimistic_user_messages if pending[0] != run_id
        ]

    async def _append_confirmed_user_message(self, message: AgentMessage) -> None:

        if isinstance(message, UserMessage):
            await self._append_optimistic_user_message(message.text)
            return
        if isinstance(message, CustomMessage):
            if message.display:
                await self._append_optimistic_user_message(
                    message.text,
                    custom_type=message.custom_type,
                    details=message.details if isinstance(message.details, dict) else None,
                )
            return
        self._refresh()

    def _connect_extension_runtime(self, session: CodingSession) -> None:

        runtime = getattr(session, "extension_runtime", None)
        if runtime is None:
            return


        self._clear_extension_components()
        runtime.set_ui_bridge(_TuiExtensionUiBridge(self))
        runtime.set_turn_requested_callback(self._on_extension_turn_requested)

        self.state.custom_renderer = runtime.render_custom_message

        self.state.tool_call_renderer = runtime.render_tool_call

        self.state.tool_result_renderer = runtime.render_tool_result

    def _on_extension_turn_requested(
        self,
        content: str,
        custom_type: str | None = None,
        details: dict[str, JSONValue] | None = None,
    ) -> None:

        self.call_later(self._deliver_extension_message, content, custom_type, details)

    async def _deliver_extension_message(
        self,
        content: str,
        custom_type: str | None = None,
        details: dict[str, JSONValue] | None = None,
    ) -> None:
        if self.session.is_running or self._prompt_worker is not None:

            queue_follow_up = getattr(self.session, "queue_follow_up_message", None)
            if callable(queue_follow_up):
                queue_follow_up(content, custom_type=custom_type, details=details)
            return
        await self._submit_prompt(
            content, source="extension", custom_type=custom_type, details=details
        )


    def _current_prompt_text(self) -> str:

        try:
            return self.query_one("#prompt", PromptInput).text
        except NoMatches:
            return ""

    def _register_extension_key_interceptor(self, handler: KeyInterceptor) -> Callable[[], None]:

        self._extension_key_interceptors.append(handler)

        def unsubscribe() -> None:
            with suppress(ValueError):
                self._extension_key_interceptors.remove(handler)

        return unsubscribe

    def _run_extension_key_interceptors(self, event: Key, text: str) -> bool:

        for interceptor in tuple(self._extension_key_interceptors):
            try:
                if interceptor(event, text):
                    return True
            except Exception as exc:


                self._record_extension_component_failure(
                    f"key_interceptor:{id(interceptor)}", exc, notify=True
                )
        return False

    def _schedule_extension_swap(self, coro: Coroutine[object, object, None]) -> None:

        try:
            task = asyncio.ensure_future(coro)
        except RuntimeError:
            coro.close()
            return
        self._extension_swap_tasks.add(task)
        task.add_done_callback(self._extension_swap_tasks.discard)

    @staticmethod
    def _string_slot_widget(lines: Sequence[str]) -> Static:

        content = "\n".join(lines)
        return Static(_custom_markup_to_text(content))

    def _set_extension_slot_widget(
        self,
        key: str,
        content: SlotWidgetContent | None,
        placement: Placement,
    ) -> None:

        factory: SlotWidgetFactory | None
        if content is None:
            factory = None
        elif callable(content):


            factory = content
        else:


            lines = [content] if isinstance(content, str) else list(content)
            factory = lambda _theme: self._string_slot_widget(lines)
        new_widget: Widget | None = None
        if factory is not None:
            try:
                new_widget = factory(self.tui_settings.resolved_theme)
            except Exception as exc:
                self._record_extension_component_failure(f"slot:{key}", exc, notify=True)
                return
        slot_id = "above-prompt-slot" if placement == "above_prompt" else "below-prompt-slot"
        if new_widget is None:
            self._extension_slot_widgets.pop(key, None)
            self._extension_slot_slot_ids.pop(key, None)
        else:
            self._extension_slot_widgets[key] = new_widget
            self._extension_slot_slot_ids[key] = slot_id
        self._schedule_extension_swap(self._reconcile_slot(key))

    async def _reconcile_slot(self, key: str) -> None:

        lock = self._extension_slot_locks.setdefault(key, asyncio.Lock())
        async with lock:
            target = self._extension_slot_widgets.get(key)
            mounted = self._extension_slot_mounted.get(key)
            if mounted is not None and mounted is not target:
                with suppress(Exception):
                    await mounted.remove()
                if self._extension_slot_mounted.get(key) is mounted:
                    self._extension_slot_mounted.pop(key, None)

            target = self._extension_slot_widgets.get(key)
            if target is None or self._extension_slot_mounted.get(key) is target:
                return
            slot_id = self._extension_slot_slot_ids.get(key, "below-prompt-slot")
            try:
                self.query_one(f"#{slot_id}", Container).mount(target)
            except Exception as exc:
                if self._extension_slot_widgets.get(key) is target:
                    self._extension_slot_widgets.pop(key, None)
                    self._extension_slot_slot_ids.pop(key, None)
                self._record_extension_component_failure(f"slot:{key}", exc, notify=True)
                return
            self._extension_slot_mounted[key] = target

    def _build_extension_sidebar_widget(
        self,
        owner: tuple[str, str],
        contribution: _SidebarContribution,
        *,
        theme: TuiTheme,
    ) -> Widget | None:

        content = contribution.content
        try:
            if callable(content):
                body = content(theme)
            else:
                lines = [content] if isinstance(content, str) else list(content)
                body = self._string_slot_widget(lines)
            if not isinstance(body, Widget):
                raise TypeError("sidebar factory must return a Textual Widget")
            header = Text(contribution.title, style=f"bold {theme.prompt_text}")
            return Vertical(
                Static(_sidebar_separator(theme=theme), classes="sidebar-separator"),
                Static(header, classes="extension-sidebar-title"),
                Container(body, classes="extension-sidebar-body"),
                classes="extension-sidebar-section",
            )
        except Exception as exc:
            extension_name, key = owner
            self._record_extension_component_failure(
                f"sidebar:{extension_name}:{key}",
                exc,
                notify=True,
                extension_name=extension_name,
            )
            return None

    def _set_extension_sidebar_section(
        self,
        extension_name: str,
        key: str,
        *,
        title: str,
        content: SidebarContent,
    ) -> None:

        if self.tui_settings.sidebar_position == "off":
            return
        owner = (extension_name, key)
        normalized_content: SidebarContent
        if callable(content):
            normalized_content = content
        elif isinstance(content, str):
            normalized_content = (content,)
        else:
            normalized_content = tuple(content)
        contribution = _SidebarContribution(title=title, content=normalized_content)
        theme = self.tui_settings.resolved_theme
        previous = self._extension_sidebar_contributions.get(owner)
        if previous == contribution and self._extension_sidebar_theme == theme:
            return
        mounted = self._extension_sidebar_mounted.get(owner)
        target = self._extension_sidebar_widgets.get(owner)
        if (
            previous is not None
            and not callable(previous.content)
            and not callable(normalized_content)
            and mounted is not None
            and mounted is target
            and self._extension_sidebar_theme == theme
        ):
            try:
                header = Text(title, style=f"bold {theme.prompt_text}")
                mounted.query_one(".extension-sidebar-title", Static).update(header)
                body = mounted.query_one(".extension-sidebar-body", Container).query_one(Static)
                body.update(_custom_markup_to_text("\n".join(normalized_content)))
            except Exception as exc:
                self._record_extension_component_failure(
                    f"sidebar:{extension_name}:{key}",
                    exc,
                    notify=True,
                    extension_name=extension_name,
                )
                return
            self._extension_sidebar_contributions[owner] = contribution
            return
        widget = self._build_extension_sidebar_widget(owner, contribution, theme=theme)
        if widget is None:
            return
        self._extension_sidebar_contributions[owner] = contribution
        self._extension_sidebar_widgets[owner] = widget
        self._extension_sidebar_theme = theme
        self._schedule_extension_swap(self._reconcile_sidebar())

    def _remove_extension_sidebar_section(self, extension_name: str, key: str) -> None:

        owner = (extension_name, key)
        if owner not in self._extension_sidebar_contributions:
            return
        self._extension_sidebar_contributions.pop(owner, None)
        self._extension_sidebar_widgets.pop(owner, None)
        self._schedule_extension_swap(self._reconcile_sidebar())

    def _rebuild_extension_sidebar_sections(self, *, theme: TuiTheme) -> None:

        if self.tui_settings.sidebar_position == "off":
            return
        changed = False
        for owner, contribution in tuple(self._extension_sidebar_contributions.items()):
            widget = self._build_extension_sidebar_widget(owner, contribution, theme=theme)
            if widget is not None:
                self._extension_sidebar_widgets[owner] = widget
                changed = True
        self._extension_sidebar_theme = theme
        if changed:
            self._schedule_extension_swap(self._reconcile_sidebar())

    async def _reconcile_sidebar(self) -> None:

        async with self._extension_sidebar_lock:
            target_items = tuple(self._extension_sidebar_widgets.items())
            mounted_items = tuple(self._extension_sidebar_mounted.items())
            if mounted_items == target_items:
                return
            try:
                slot = self.query_one("#sidebar-extension-sections", Container)
            except NoMatches:
                return


            for owner, mounted in mounted_items:
                if self._extension_sidebar_widgets.get(owner) is mounted:
                    continue
                with suppress(Exception):
                    await mounted.remove()
                if self._extension_sidebar_mounted.get(owner) is mounted:
                    self._extension_sidebar_mounted.pop(owner, None)

            target_items = tuple(self._extension_sidebar_widgets.items())
            for index, (owner, target) in enumerate(target_items):
                if self._extension_sidebar_mounted.get(owner) is target:
                    continue
                later_mounted = next(
                    (
                        self._extension_sidebar_mounted.get(later_owner)
                        for later_owner, _ in target_items[index + 1 :]
                        if self._extension_sidebar_mounted.get(later_owner) is not None
                    ),
                    None,
                )
                try:
                    await slot.mount(target, before=later_mounted)
                except Exception as exc:
                    extension_name, key = owner
                    if self._extension_sidebar_widgets.get(owner) is target:
                        self._extension_sidebar_widgets.pop(owner, None)
                        self._extension_sidebar_contributions.pop(owner, None)
                    self._record_extension_component_failure(
                        f"sidebar:{extension_name}:{key}",
                        exc,
                        notify=True,
                        extension_name=extension_name,
                    )
                    continue
                self._extension_sidebar_mounted[owner] = target
            self._extension_sidebar_mounted = {
                owner: target
                for owner, target in target_items
                if self._extension_sidebar_mounted.get(owner) is target
            }

    def _open_extension_main_view(self, factory: MainViewFactory) -> MainViewHandle:

        handle = _MainViewHandle(self, asyncio.get_event_loop().create_future())
        try:
            widget = factory(handle, self.tui_settings.resolved_theme)
        except Exception as exc:
            self._record_extension_component_failure("main_view", exc, notify=True)


            handle._resolve(None)
            return _DeadMainViewHandle()
        handle.widget = widget
        previous = self._extension_main_view
        if previous is not None and previous is not handle:


            self._release_main_view_handle(previous)
        self._extension_main_view = handle
        self._schedule_extension_swap(self._reconcile_main_view())
        return handle

    async def _reconcile_main_view(self) -> None:

        async with self._extension_main_view_lock:
            target = self._extension_main_view
            target_widget = target.widget if target is not None else None
            mounted = self._extension_main_view_mounted
            if mounted is not None and mounted is not target_widget:
                with suppress(Exception):
                    await mounted.remove()
                if self._extension_main_view_mounted is mounted:
                    self._extension_main_view_mounted = None

            target = self._extension_main_view
            target_widget = target.widget if target is not None else None
            if target_widget is not None and self._extension_main_view_mounted is not target_widget:
                try:
                    slot = self.query_one("#main-slot", Container)
                    slot.mount(target_widget)
                except Exception as exc:
                    if self._extension_main_view is target:
                        self._extension_main_view = None
                    self._release_main_view_handle(target)
                    self._record_extension_component_failure("main_view", exc, notify=True)
                    self._restore_main_transcript()
                    return
                self._extension_main_view_mounted = target_widget
                with suppress(NoMatches):
                    self.query_one("#transcript", TranscriptView).display = False
                slot.display = True
            elif self._extension_main_view is None and self._extension_main_view_mounted is None:

                self._restore_main_transcript()

    def _close_extension_main_view(self, handle: _MainViewHandle) -> None:

        if self._extension_main_view is not handle:
            return
        self._extension_main_view = None
        self._schedule_extension_swap(self._reconcile_main_view())

    def _release_main_view_handle(self, handle: _MainViewHandle | None) -> None:

        if handle is None:
            return
        handle._open = False
        handle._resolve(None)

    def _restore_main_transcript(self) -> None:

        if not self.screen_stack:
            return
        with suppress(NoMatches):
            self.query_one("#main-slot", Container).display = False
        with suppress(NoMatches):
            pane = self.query_one("#transcript", TranscriptView)
            pane.display = True


            pane.follow_output()
        with suppress(NoMatches):
            self.query_one("#prompt", PromptInput).focus()

    def _refresh_extension_components(self) -> None:

        for widget in (
            *self._extension_slot_widgets.values(),
            *self._extension_sidebar_widgets.values(),
        ):
            with suppress(Exception):
                widget.refresh()
        handle = self._extension_main_view
        if handle is not None and handle.widget is not None:
            with suppress(Exception):
                handle.widget.refresh()

    def _clear_extension_components(self) -> None:

        slot_keys = {*self._extension_slot_widgets, *self._extension_slot_mounted}
        self._extension_slot_widgets.clear()
        self._extension_slot_slot_ids.clear()
        for key in slot_keys:
            self._schedule_extension_swap(self._reconcile_slot(key))
        self._extension_sidebar_contributions.clear()
        self._extension_sidebar_widgets.clear()
        self._extension_sidebar_theme = None
        self._schedule_extension_swap(self._reconcile_sidebar())
        handle = self._extension_main_view
        self._extension_main_view = None
        self._release_main_view_handle(handle)
        self._schedule_extension_swap(self._reconcile_main_view())
        self._extension_key_interceptors.clear()

        self._extension_component_failures_reported.clear()

    def _tracked_extension_widgets(self) -> tuple[Widget, ...]:

        widgets: list[Widget] = []
        seen: set[int] = set()
        for widget in (
            *self._extension_slot_widgets.values(),
            *self._extension_slot_mounted.values(),
            *self._extension_sidebar_widgets.values(),
            *self._extension_sidebar_mounted.values(),
        ):
            if id(widget) not in seen:
                seen.add(id(widget))
                widgets.append(widget)
        handle = self._extension_main_view
        main_widgets = (
            handle.widget if handle is not None else None,
            self._extension_main_view_mounted,
        )
        for main_widget in main_widgets:
            if main_widget is not None and id(main_widget) not in seen:
                seen.add(id(main_widget))
                widgets.append(main_widget)
        return tuple(widgets)

    def _extension_root_for(self, widget: Widget, tracked: tuple[Widget, ...]) -> Widget | None:

        node: Widget | None = widget
        while node is not None:
            for root in tracked:
                if node is root:
                    return root
            node = node.parent if isinstance(node.parent, Widget) else None
        return None

    def _quarantine_extension_widget(self, error: BaseException) -> bool:

        tracked = self._tracked_extension_widgets()
        if not tracked:
            return False
        tb = error.__traceback__
        culprit: Widget | None = None
        while tb is not None:
            candidate = tb.tb_frame.f_locals.get("self")
            if isinstance(candidate, Widget):
                root = self._extension_root_for(candidate, tracked)
                if root is not None:
                    culprit = root
                    break
            tb = tb.tb_next
        if culprit is None:
            return False


        with suppress(Exception):
            culprit.display = False
        with suppress(Exception):
            culprit.disabled = True
        sidebar_owner: tuple[str, str] | None = None
        if (
            self._extension_main_view is not None and self._extension_main_view.widget is culprit
        ) or self._extension_main_view_mounted is culprit:
            if self._extension_main_view_mounted is culprit:
                self._extension_main_view_mounted = None
            handle = self._extension_main_view
            self._extension_main_view = None
            self._release_main_view_handle(handle)
            with suppress(Exception):
                culprit.remove()
            self._restore_main_transcript()
        else:
            sidebar_owner = next(
                (
                    owner
                    for owner, widget in (
                        *self._extension_sidebar_widgets.items(),
                        *self._extension_sidebar_mounted.items(),
                    )
                    if widget is culprit
                ),
                None,
            )
            if sidebar_owner is not None:
                self._extension_sidebar_widgets.pop(sidebar_owner, None)
                self._extension_sidebar_mounted.pop(sidebar_owner, None)
                self._extension_sidebar_contributions.pop(sidebar_owner, None)
            else:
                for tracker in (self._extension_slot_widgets, self._extension_slot_mounted):
                    key = next((k for k, w in tracker.items() if w is culprit), None)
                    if key is not None:
                        tracker.pop(key, None)
            with suppress(Exception):
                culprit.remove()
        extension_name = sidebar_owner[0] if sidebar_owner else None
        context = (
            f"sidebar:{sidebar_owner[0]}:{sidebar_owner[1]}"
            if sidebar_owner
            else f"render:{id(culprit)}"
        )
        self._record_extension_component_failure(
            context,
            error,
            notify=True,
            extension_name=extension_name,
        )
        return True

    def _handle_exception(self, error: Exception) -> None:

        if self._quarantine_extension_widget(error):
            return
        super()._handle_exception(error)

    def _record_extension_component_failure(
        self,
        context: str,
        error: BaseException,
        *,
        notify: bool = False,
        extension_name: str | None = None,
    ) -> None:


        with suppress(Exception):
            self.log.error(
                f"Extension component failed ({context}):\n"
                + "".join(traceback.format_exception(type(error), error, error.__traceback__))
            )
        if context in self._extension_component_failures_reported:
            return
        self._extension_component_failures_reported.add(context)
        if extension_name is not None:
            runtime = getattr(self.session, "extension_runtime", None)
            if runtime is not None:
                runtime.record_ui_failure(extension_name, context, error)
        if notify:
            summary = f"{type(error).__name__}: {error}"
            if len(summary) > 120:
                summary = summary[:117] + "..."
            self._notify(
                f"An extension component failed ({context}) and was removed ({summary}).",
                severity="error",
            )

    def _follow_transcript_output(self) -> None:

        if not self.screen_stack:
            return
        with suppress(NoMatches):
            self.query_one("#transcript", TranscriptView).follow_output()

    async def _run_terminal_command(self, command: str, *, add_to_context: bool) -> None:
        run_terminal_command = getattr(self.session, "run_terminal_command", None)
        if not callable(run_terminal_command):
            self._notify("Terminal commands are not available.", severity="error")
            return

        item_index = len(self.state.items)
        self.state.add_item(
            "tool",
            f"$ {command.strip()}",
            always_show_tool_result=True,
        )
        item = self.state.items[item_index]
        self._follow_transcript_output()
        transcript = self.query_one("#transcript", TranscriptView)
        await transcript.append_item(
            item,
            theme=self.tui_settings.resolved_theme,
            show_tool_results=True,
            scroll_end=True,
        )
        self._refresh_chrome()

        try:
            result = await run_terminal_command(command, add_to_context=add_to_context)
        except Exception as exc:
            if item_index < len(self.state.items):
                item = self.state.items[item_index]
                item.tool_result_text = format_terminal_command_result_block(
                    ok=False,
                    added_to_context=add_to_context,
                    output=str(exc),
                )
            self._notify(f"Could not run command: {exc}", severity="error")
            await transcript.update_item(
                item,
                theme=self.tui_settings.resolved_theme,
                show_tool_results=True,
            )
            self._refresh_chrome()
            return

        if item_index >= len(self.state.items):
            return
        item = self.state.items[item_index]
        item.text = f"$ {result.command}"
        item.tool_result_text = format_terminal_command_result_block(
            ok=result.ok,
            added_to_context=result.added_to_context,
            output=result.output,
        )
        self._follow_transcript_output()
        await transcript.update_item(
            item,
            theme=self.tui_settings.resolved_theme,
            show_tool_results=True,
        )
        self._refresh_chrome()

    def _replace_tui_settings(self, *, theme: TuiThemeName) -> None:

        self.tui_settings = TuiSettings(
            keybindings=self.tui_settings.keybindings,
            theme=theme,
            auto_copy_selection=self.tui_settings.auto_copy_selection,
            sidebar_position=self.tui_settings.sidebar_position,
            turn_notification=self.tui_settings.turn_notification,
        )

    def _set_tui_theme(self, theme: TuiThemeName) -> None:
        if theme not in available_tui_theme_names():
            self._notify(f"Unknown theme: {theme}", severity="error")
            return
        self._replace_tui_settings(theme=theme)
        save_tui_settings(self.tui_settings)
        self.theme = theme
        self._refresh()

    async def _queue_prompt(
        self,
        text: str,
        *,
        streaming_behavior: Literal["steer", "follow_up"],
    ) -> None:

        try:
            async for event in self.session.prompt(text, streaming_behavior=streaming_behavior):
                self.adapter.apply(event)
        except Exception as exc:
            self._notify(f"Could not queue message: {exc}", severity="error")
            return
        self._refresh_chrome()

    async def _run_prompt(
        self,
        text: str,
        run_id: int | None = None,
        *,
        source: Literal["interactive", "extension"] = "interactive",
        custom_type: str | None = None,
        details: dict[str, JSONValue] | None = None,
        topic_id: str | None = None,
    ) -> None:

        active_run_id = self._prompt_run_id if run_id is None else run_id
        try:
            async for event in self.session.prompt(
                text, source=source, custom_type=custom_type, details=details, topic_id=topic_id
            ):
                if active_run_id != self._prompt_run_id:
                    return
                if self._consume_optimistic_user_event(event, run_id=active_run_id):
                    self._sync_text_selection_state()
                    self._refresh_chrome()
                    continue
                if self._replace_transformed_optimistic_user_message(event, run_id=active_run_id):
                    self._sync_text_selection_state()
                    continue
                if not (_is_user_message_end_event(event) and self.screen_stack):
                    self.adapter.apply(event)
                self._sync_text_selection_state()
                if (
                    isinstance(event, MessageEndEvent)
                    and isinstance(event.message, AssistantMessage)
                    and event.message.stop_reason == "error"
                ):
                    _attach_diagnostic_log_path_to_error(self.state, self.session)
                    will_auto_retry = getattr(self.session, "will_auto_retry", None)
                    if not (callable(will_auto_retry) and will_auto_retry(event.message)):
                        _attach_retry_hint_to_error(self.state, event.message)
                elif (
                    isinstance(event, CompactionEndEvent)
                    and event.reason == "overflow"
                    and (event.aborted or event.error_message)
                ):
                    _attach_diagnostic_log_path_to_error(self.state, self.session)
                await self._apply_streaming_transcript_event(event)
                if isinstance(event, AgentSettledEvent) and not self._app_has_focus:
                    self._terminal_notification.notify_turn_finished()
        except Exception as exc:
            if active_run_id != self._prompt_run_id:
                return
            message = _format_prompt_error(exc, self.session)
            self.state.error = message
            self.state.add_item("error", message)
            self.state.running = False
            self._sync_text_selection_state()
            self._refresh()
        finally:
            self._clear_optimistic_user_messages(run_id=active_run_id)
            if active_run_id == self._prompt_run_id:
                self._prompt_worker = None

    async def _apply_streaming_transcript_event(self, event: CodingSessionEvent) -> None:

        if not self.screen_stack:
            self._refresh()
            return
        theme = self.tui_settings.resolved_theme
        try:
            transcript = self.query_one("#transcript", TranscriptView)
        except NoMatches:
            self._refresh()
            return
        if isinstance(event, (ContextRecoveryStartEvent, ContextRecoveryEndEvent)):
            await transcript.finish_assistant_message()
            if self.state.items:
                await transcript.append_item(self.state.items[-1], theme=theme,
                    show_tool_results=self.state.show_tool_results)
            self._refresh_chrome()
            return
        if isinstance(event, ContextBudgetEvent):
            if event.decision.status == "NEEDS_COMPACTION":
                self.notify(event.decision.summary(), title="上下文接近容量，需要压缩", severity="warning")
            self._refresh_chrome()
            return
        if isinstance(event, AgentStartEvent):
            self._refresh_chrome()
            return
        if isinstance(event, AgentEndEvent):
            await transcript.finish_assistant_message()
            self._refresh_chrome()
            return
        if isinstance(event, MessageStartEvent):
            return
        if isinstance(event, MessageUpdateEvent):
            nested = event.assistant_message_event
            if isinstance(nested, TextDeltaEvent):
                await transcript.append_assistant_delta(nested.delta, theme=theme)
            elif isinstance(nested, ThinkingEndEvent):
                await transcript.finish_thinking_message()
            elif isinstance(nested, ThinkingDeltaEvent):
                await transcript.append_thinking_delta(
                    nested.delta,
                    theme=theme,
                    show_thinking=self.state.show_thinking,
                )
            return
        if isinstance(event, MessageEndEvent):
            if isinstance(event.message, (UserMessage, CustomMessage)):
                await self._append_confirmed_user_message(event.message)
                self._sync_session_title()
                return
            if isinstance(event.message, AssistantMessage):
                if event.message.stop_reason in {"error", "aborted"}:


                    self._refresh()
                    return
                visible_blocks = [
                    block
                    for block in event.message.content
                    if (
                        isinstance(block, TextContent)
                        and bool(block.text)
                        or isinstance(block, ThinkingContent)
                        and bool(block.thinking)
                    )
                ]
                canonical_items = self.state.items[-len(visible_blocks) :] if visible_blocks else []
                if (
                    any(isinstance(block, ThinkingContent) for block in visible_blocks)
                    or len(visible_blocks) > 1
                ):


                    await transcript.finish_structured_assistant_message(
                        canonical_items,
                        theme=theme,
                        show_thinking=self.state.show_thinking,
                    )
                else:
                    canonical_item = canonical_items[-1] if canonical_items else None
                    await transcript.finish_assistant_message(
                        event.message.text,
                        item=canonical_item,
                    )
                self._refresh_chrome()
                return
            return
        if isinstance(event, ToolExecutionStartEvent):
            await transcript.finish_assistant_message()
            item = self.state.find_tool_item(event.tool_call_id)
            if item is not None:
                expanded = self.state.show_tool_results or item.always_show_tool_result
                updated = await transcript.update_item(
                    item,
                    theme=theme,
                    show_tool_results=expanded,
                    invocation=self.state.resolve_tool_invocation(item, expanded=expanded),
                    result_markup=self.state.resolve_tool_result(item, expanded=expanded),
                )
                if not updated:
                    await transcript.append_item(
                        item,
                        theme=theme,
                        show_tool_results=expanded,
                        invocation=self.state.resolve_tool_invocation(item, expanded=expanded),
                    )
            self._refresh_chrome()
            return
        if isinstance(event, ToolExecutionUpdateEvent):
            await transcript.finish_assistant_message()
            updated_item = self.state.find_tool_item(event.tool_call_id)
            if updated_item is not None:
                expanded = self.state.show_tool_results or updated_item.always_show_tool_result
                await transcript.update_item(
                    updated_item,
                    theme=theme,
                    show_tool_results=expanded,
                    invocation=self.state.resolve_tool_invocation(updated_item, expanded=expanded),
                    result_markup=self.state.resolve_tool_result(updated_item, expanded=expanded),
                )
            self._refresh_chrome()
            return
        if isinstance(event, (AutoRetryStartEvent, CompactionStartEvent)):
            await transcript.finish_assistant_message()
            if self.state.items and (
                isinstance(event, AutoRetryStartEvent) or event.reason == "overflow"
            ):
                await transcript.append_item(
                    self.state.items[-1],
                    theme=theme,
                    show_tool_results=self.state.show_tool_results,
                )
            self._refresh_chrome()
            return
        if isinstance(event, CompactionEndEvent):
            if event.reason == "overflow" and (event.aborted or event.error_message):
                self._refresh()
            else:
                self._refresh_chrome()
            return
        if isinstance(event, ToolExecutionEndEvent):
            updated_item = self.state.find_tool_item(event.tool_call_id)
            if updated_item is not None:
                expanded = self.state.show_tool_results or updated_item.always_show_tool_result
                await transcript.update_item(
                    updated_item,
                    theme=theme,
                    show_tool_results=expanded,
                    invocation=self.state.resolve_tool_invocation(updated_item, expanded=expanded),
                    result_markup=self.state.resolve_tool_result(updated_item, expanded=expanded),
                )
            self._refresh_chrome()
            return
        if isinstance(event, QueueUpdateEvent):
            self._refresh_chrome()
            return
        self._refresh_chrome()

    def action_cancel(self) -> None:

        if self._cancel_active_compaction(notify=True):
            return
        self._cancel_active_prompt(notify=True)

    def _cancel_active_compaction(self, *, notify: bool) -> bool:

        worker = self._compaction_worker
        if worker is None or worker.is_finished or worker.is_cancelled:
            return False

        worker.cancel()
        self._compaction_run_id += 1
        self._compaction_worker = None
        self._compacting = False
        self.state.clear()
        self.state.set_skills(self.session.skills)
        self._load_session_messages_from_session()
        self._refresh()
        if notify:
            self._notify("Cancelled compaction.")
        return True

    def _cancel_active_prompt(self, *, notify: bool, interrupt: bool = False) -> None:

        del interrupt
        worker = self._prompt_worker
        is_worker_active = worker is not None and not worker.is_cancelled
        is_session_running = bool(getattr(self.session, "is_running", False))
        if not (self.state.running or is_session_running or is_worker_active):
            return

        self._prompt_run_id += 1
        cancel = getattr(self.session, "cancel", None)
        if callable(cancel):
            cancel()
        if worker is not None and not worker.is_cancelled:
            worker.cancel()
        self._prompt_worker = None
        self.state.running = False
        self.state.assistant_buffer = ""
        self._sync_text_selection_state()
        self._refresh()
        if notify:
            self._notify("Interrupted current operation.")

    def action_accept_completion(self) -> None:

        if isinstance(self.screen, ModelPickerScreen):
            self.screen.action_toggle_mode()
            return
        if isinstance(self.screen, ToolsReferenceScreen):
            self.screen.action_open_selected()
            return
        if isinstance(
            self.screen,
            SessionPickerScreen
            | PromptTemplatePickerScreen
            | SkillPickerScreen
            | LoginMethodPickerScreen
            | LoginProviderPickerScreen
            | ThemePickerScreen
            | ExtensionSelectScreen
            | ExtensionConfirmScreen
            | LocalBackendPickerScreen
            | LocalBackendScreen
            | LocalChoiceConfirmScreen
            | LocalConfirmScreen
            | LocalSearchResultsScreen
            | ProjectTrustScreen,
        ):
            self.screen.action_select_cursor()
            return
        prompt = self.query_one("#prompt", PromptInput)
        item = self._completion_state.selected
        applied = self._apply_selected_completion(prompt.text)
        if applied is None or item is None:
            return
        prompt.text = applied
        cursor = item.cursor_after_apply()
        prompt.cursor_position = cursor
        self._completion_state = self._build_completion_state(prompt.text, cursor=cursor)
        self._refresh_completions()

    def action_completion_next(self) -> None:

        if isinstance(self.focused, TextArea) and self.focused.id == "sidebar-file-editor-input":
            self.focused.action_cursor_down()
            return
        if isinstance(self.screen, PromptTemplateEditorScreen):
            self.screen.query_one("#prompt-template-editor-input", TextArea).action_cursor_down()
            return
        if isinstance(self.screen, CommandOutputScreen):
            self.screen.action_scroll_down()
            return
        if isinstance(
            self.screen,
            SessionPickerScreen
            | PromptTemplatePickerScreen
            | SkillPickerScreen
            | LoginMethodPickerScreen
            | LoginProviderPickerScreen
            | ThemePickerScreen
            | ModelPickerScreen
            | ToolsReferenceScreen
            | ExtensionSelectScreen
            | ExtensionConfirmScreen
            | LocalBackendPickerScreen
            | LocalBackendScreen
            | LocalChoiceConfirmScreen
            | LocalConfirmScreen
            | LocalSearchResultsScreen
            | ProjectTrustScreen,
        ):
            self.screen.action_cursor_down()
            return
        if not self._completion_state.items:
            self.query_one("#prompt", PromptInput).action_cursor_down()
            return
        self._completion_state = self._completion_state.select_next()
        self._refresh_completions()

    def action_completion_previous(self) -> None:

        if isinstance(self.focused, TextArea) and self.focused.id == "sidebar-file-editor-input":
            self.focused.action_cursor_up()
            return
        if isinstance(self.screen, PromptTemplateEditorScreen):
            self.screen.query_one("#prompt-template-editor-input", TextArea).action_cursor_up()
            return
        if isinstance(self.screen, CommandOutputScreen):
            self.screen.action_scroll_up()
            return
        if isinstance(
            self.screen,
            SessionPickerScreen
            | PromptTemplatePickerScreen
            | SkillPickerScreen
            | LoginMethodPickerScreen
            | LoginProviderPickerScreen
            | ThemePickerScreen
            | ModelPickerScreen
            | ToolsReferenceScreen
            | ExtensionSelectScreen
            | ExtensionConfirmScreen
            | LocalBackendPickerScreen
            | LocalBackendScreen
            | LocalChoiceConfirmScreen
            | LocalConfirmScreen
            | LocalSearchResultsScreen
            | ProjectTrustScreen,
        ):
            self.screen.action_cursor_up()
            return
        if not self._completion_state.items:
            if self.action_edit_queued_message():
                return
            if self.action_recall_previous_prompt():
                return
            self.query_one("#prompt", PromptInput).action_cursor_up()
            return
        self._completion_state = self._completion_state.select_previous()
        self._refresh_completions()

    def action_recall_previous_prompt(self) -> bool:

        prompt = self.query_one("#prompt", PromptInput)


        if prompt.text.strip() or not self._prompt_history:
            return False
        previous_prompt = self._prompt_history[-1]
        prompt.text = previous_prompt
        prompt.move_cursor(_text_end_location(previous_prompt))
        self._completion_state = self._build_completion_state(prompt.text)
        self._refresh_completions()
        return True

    def action_edit_queued_message(self) -> bool:

        if not self.state.running:
            return False
        prompt = self.query_one("#prompt", PromptInput)
        if prompt.text.strip():
            return False

        message = self._pop_latest_queued_message()
        if not message:
            return False
        prompt.text = message
        prompt.move_cursor(_text_end_location(message))
        self._sync_queue_state()
        self._completion_state = self._build_completion_state(prompt.text)
        self._refresh()
        return True

    def action_edit_queued_follow_up(self) -> bool:

        return self.action_edit_queued_message()

    def _pop_latest_queued_message(self) -> str | None:

        pop_follow_up = getattr(self.session, "pop_latest_follow_up_message", None)
        if callable(pop_follow_up):
            message = pop_follow_up()
            if isinstance(message, str) and message:
                return message

        pop_steering = getattr(self.session, "pop_latest_steering_message", None)
        if callable(pop_steering):
            message = pop_steering()
            if isinstance(message, str) and message:
                return message

        return None

    def action_open_command_palette(self) -> None:

        prompt = self.query_one("#prompt", PromptInput)
        prompt.focus()
        prompt.text = "/"
        prompt.move_cursor((0, 1))
        self._completion_state = self._build_completion_state(prompt.text)
        self._refresh_completions()

    def action_open_session_picker(self) -> None:

        if self.state.running:
            self._notify("Octo is already working. Press Escape to cancel.")
            return
        if getattr(self.session, "session_manager", None) is None:
            self._notify("No sessions found.")
            return
        picker = SessionPickerScreen(
            (),
            local_cwd=Path(self.session.cwd),
            theme=self.tui_settings.resolved_theme,
            loading_other_projects=True,
            current_project_loaded=False,
        )
        self.push_screen(picker, callback=self._handle_session_picker_result)
        self.run_worker(self._refresh_open_session_picker(picker), exclusive=False)

    async def _refresh_open_session_picker(self, picker: SessionPickerScreen) -> None:

        try:
            local_records = await asyncio.to_thread(_local_session_records, self.session)
        except Exception as exc:
            if self.screen is picker:
                self._notify(f"Could not load current project sessions: {exc}", severity="warning")
        else:
            if not await self._wait_for_open_session_picker(picker):
                return
            picker.update_records(local_records, loading_other_projects=True)

        try:
            records = await asyncio.to_thread(_session_records, self.session)
        except Exception as exc:
            if self.screen is picker:
                picker.finish_loading()
                self._notify(f"Could not load other projects: {exc}", severity="warning")
            return
        if await self._wait_for_open_session_picker(picker):
            picker.update_records(records)

    async def _wait_for_open_session_picker(self, picker: SessionPickerScreen) -> bool:

        if self.screen is not picker:
            return False
        while not picker.is_mounted:
            await asyncio.sleep(0)
            if self.screen is not picker:
                return False
        return True

    def _open_prompt_template_picker(self) -> None:
        self.push_screen(
            PromptTemplatePickerScreen(self.session.prompt_templates),
            callback=self._handle_prompt_template_picker_result,
        )

    def _handle_prompt_template_picker_result(
        self, result: PromptTemplatePickerResult | None
    ) -> None:
        prompt = self.query_one("#prompt", PromptInput)
        prompt.focus()
        if result is None:
            return
        if result.action == "edit":
            try:
                source = result.template.path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                self._notify(f"Could not read /{result.template.name}: {exc}", severity="error")
                self._open_prompt_template_picker()
                return
            self.push_screen(
                PromptTemplateEditorScreen(result.template, source),
                callback=lambda edited: self._handle_prompt_template_edit(result.template, edited),
            )
            return
        invocation = f"/{result.template.name}"
        prompt.text = invocation
        prompt.move_cursor(_text_end_location(invocation))
        self._completion_state = self._build_completion_state(invocation)
        self._refresh_completions()

    def _handle_prompt_template_edit(self, template: PromptTemplate, source: str | None) -> None:
        if source is None:
            self._open_prompt_template_picker()
            return
        self.run_worker(self._save_prompt_template_edit(template, source), exclusive=False)

    async def _save_prompt_template_edit(self, template: PromptTemplate, source: str) -> None:
        try:
            template.path.write_text(source, encoding="utf-8")
        except OSError as exc:
            self._notify(f"Could not save /{template.name}: {exc}", severity="error")
            self._open_prompt_template_picker()
            return

        try:
            reload_result = self.session.reload()
            if isawaitable(reload_result):
                await reload_result
        except Exception as exc:
            self._notify(
                f"Saved /{template.name}, but could not reload resources: {exc}",
                severity="error",
            )
        else:
            self.state.set_skills(self.session.skills)
            self._completion_state = self._build_completion_state("")
            self._refresh()
            self._notify(f"Saved /{template.name} and reloaded resources.")
        self._open_prompt_template_picker()

    def _open_skills_picker(self) -> None:

        self.push_screen(
            SkillPickerScreen(self.session.skills, theme=self.tui_settings.resolved_theme),
            callback=self._handle_skill_picker_result,
        )

    def _handle_skill_picker_result(self, result: SkillPickerResult | None) -> None:
        prompt = self.query_one("#prompt", PromptInput)
        if result is None:
            prompt.text = ""
        elif result.action == "insert":
            prompt.text = f"/skill:{result.skill.name}"
        else:
            prompt.text = ""
            self.state.add_item(
                "status",
                f"Skill: {result.skill.name} (not added to context)\n{result.skill.content}",
            )
            self._refresh()
        prompt.move_cursor(_text_end_location(prompt.text))
        prompt.focus()

    def action_cycle_thinking(self) -> None:

        self.run_worker(self._cycle_thinking_level(), exclusive=False)

    def action_cycle_model(self) -> None:

        self._cycle_model(reverse=False)

    def action_cycle_model_reverse(self) -> None:

        self._cycle_model(reverse=True)

    def _cycle_model(self, *, reverse: bool) -> None:
        if self.state.running:
            self._notify("Octo is already working. Press Escape to cancel.")
            return
        self.run_worker(self._cycle_scoped_model(reverse=reverse), exclusive=False)

    def action_toggle_tool_results(self) -> None:

        self.state.toggle_tool_results()
        self.run_worker(self._update_tool_results_visibility(), exclusive=False)

    async def _update_tool_results_visibility(self) -> None:
        transcript = self.query_one("#transcript", TranscriptView)
        await transcript.update_tool_results_visibility(
            self.state,
            theme=self.tui_settings.resolved_theme,
        )

    def action_toggle_thinking(self) -> None:

        self.state.toggle_thinking()
        transcript = self.query_one("#transcript", TranscriptView)
        transcript.update_thinking_visibility(
            self.state,
            theme=self.tui_settings.resolved_theme,
        )

    def _handle_session_picker_result(self, session_id: str | None) -> None:
        if session_id is None:
            return
        self.run_worker(self._resume_session(session_id), exclusive=False)

    async def _resume_session(self, session_id: str) -> None:
        try:
            previous_cwd = Path(self.session.cwd).resolve()
            resume_message = await self.session.resume(session_id)
            self._reload_session_themes()
            self.state.clear()
            self.state.set_skills(self.session.skills)
            self._load_session_messages_from_session()
            current_cwd = Path(self.session.cwd).resolve()
            if current_cwd != previous_cwd:
                resume_message = f"{resume_message} ({_short_path(current_cwd)})"
            self._notify(resume_message)
        except Exception as exc:
            self._notify(f"Error: {exc}", severity="error")
        self._refresh()

    async def _new_session(self) -> None:
        self._cancel_active_prompt(notify=False, interrupt=True)
        new_session = getattr(self.session, "new_session", None)
        if new_session is None:
            self._notify("Session manager is not available.")
            return
        try:
            await new_session()
            self._reload_session_themes()
            self.state.clear()
            self.state.set_skills(self.session.skills)
            self._load_session_messages_from_session()
        except Exception as exc:
            self._notify(f"Error: {exc}", severity="error")
        self._refresh()

    def _apply_selected_completion(self, value: str) -> str | None:
        item = self._completion_state.selected
        if item is None:
            return None
        return item.apply(value)

    def _append_command_message(
        self,
        command_text: str,
        message: str,
        *,
        system_prompt_inspection: SystemPromptInspection | None = None,
    ) -> None:

        is_system_prompt = command_text.split(maxsplit=1)[0].casefold() == "/system"
        separator = "\n\n" if is_system_prompt else "\n"
        title = _command_output_title(command_text)
        if is_system_prompt:
            title = f"### {title}"
        self.state.add_item(
            "status",
            f"{title}{separator}{message}",
            system_prompt=is_system_prompt,
            system_prompt_sources=(
                system_prompt_inspection.sources if system_prompt_inspection is not None else None
            ),
        )

    def _show_command_message(self, command_text: str, message: str) -> None:
        self.push_screen(
            CommandOutputScreen(
                _command_output_title(command_text),
                message,
                theme=self.tui_settings.resolved_theme,
                auto_copy_selection=command_text.strip().split(maxsplit=1)[0] == "/session",
            )
        )

    def _open_login_picker(self) -> None:
        self.push_screen(
            LoginMethodPickerScreen(theme=self.tui_settings.resolved_theme),
            callback=self._handle_login_method_result,
        )

    def _handle_login_method_result(self, method: str | None) -> None:
        if method is None:
            return
        if method == "subscription":
            providers = _subscription_login_providers(BUILTIN_PROVIDER_CATALOG)
        elif method == "api-key":
            providers = _api_key_login_providers(BUILTIN_PROVIDER_CATALOG)
        elif method == "custom":
            self._open_custom_provider_login()
            return
        else:
            self._notify(f"Unknown login method: {method}", severity="error")
            return
        if not providers:
            self._notify("No login providers are available for that method.", severity="warning")
            return
        self.push_screen(
            LoginProviderPickerScreen(
                providers,
                theme=self.tui_settings.resolved_theme,
                back_on_cancel=True,
            ),
            callback=lambda provider_name: self._handle_login_provider_result(
                provider_name,
                method=method,
            ),
        )

    def _handle_login_provider_result(
        self,
        provider_name: str | _LoginFlowAction | None,
        *,
        method: str | None = None,
    ) -> None:
        if provider_name is _LoginFlowAction.BACK:
            self._open_login_picker()
        elif provider_name is not None:
            self._open_login(provider_name, method=method)

    def _open_custom_provider_login(self) -> None:
        self.push_screen(
            CustomProviderLoginScreen(theme=self.tui_settings.resolved_theme),
            callback=self._handle_custom_provider_login_result,
        )

    def _handle_custom_provider_login_result(
        self,
        result: CustomProviderLoginResult | _LoginFlowAction | None,
    ) -> None:
        if result is _LoginFlowAction.BACK:
            self._open_login_picker()
            return
        if result is None:
            return
        provider = OpenAICompatibleProviderConfig(
            name=result.provider_name,
            base_url=result.base_url.rstrip("/"),
            api_key_env=result.api_key_env,
            credential_name=result.provider_name,
            models=result.models,
            default_model=result.default_model,
        )
        catalog_entry = ProviderCatalogEntry(
            name=provider.name,
            display_name=result.display_name,
            kind="openai-compatible",
            base_url=provider.base_url,
            api_key_env=provider.api_key_env,
            credential_name=provider.credential_name,
            models=provider.models,
            default_model=provider.default_model,
            docs_url=provider.base_url,
        )
        try:
            save_user_catalog_entries((catalog_entry,))
            FileCredentialStore().set(provider.credential_name or provider.name, result.api_key)
            settings = load_provider_settings()
            updated = upsert_openai_compatible_provider(settings, provider, set_default=False)
            save_provider_settings(updated)
            self.session.reload_provider_settings()
            try:
                self.session.set_provider(provider.name, persist_default=False)
            except TypeError:
                self.session.set_provider(provider.name)
        except Exception as exc:
            self._notify(f"Could not save custom provider: {exc}", severity="error")
            return
        self._notify(f"Saved custom provider {result.display_name}.")
        self._refresh()

    def _open_login(self, provider_name: str, *, method: str | None = None) -> None:
        entry = builtin_provider_entry(provider_name)
        if entry is None:
            self._notify(f"Unknown provider: {provider_name}", severity="error")
            return
        use_oauth = method == "subscription" or (
            method is None and "api_key" not in entry.auth_methods
        )
        if use_oauth and get_oauth_provider(entry.name) is not None:
            login = None
            if entry.name == "openai-codex":

                async def login(callbacks: OAuthLoginCallbacks) -> OAuthCredential:
                    return await login_openai_codex(
                        on_auth=callbacks.on_auth,
                        on_prompt=callbacks.on_prompt,
                        on_manual_code_input=callbacks.on_manual_code_input,
                        on_progress=callbacks.on_progress,
                    )

            self.push_screen(
                OAuthLoginScreen(
                    entry,
                    theme=self.tui_settings.resolved_theme,
                    login=login,
                ),
                callback=lambda credential: self._handle_oauth_login_navigation_result(
                    entry, credential
                ),
            )
            return
        self.push_screen(
            LoginScreen(entry, theme=self.tui_settings.resolved_theme),
            callback=lambda api_key: self._handle_api_key_login_navigation_result(entry, api_key),
        )

    def _handle_api_key_login_navigation_result(
        self,
        entry: ProviderCatalogEntry,
        result: str | _LoginFlowAction | None,
    ) -> None:
        if result is _LoginFlowAction.BACK:
            self._open_login_picker()
        else:
            self._handle_login_result(entry, result)

    def _handle_login_result(self, entry: ProviderCatalogEntry, api_key: str | None) -> None:
        if api_key is None:
            return
        if entry.credential_name is None:
            self._notify(
                f"Provider {entry.name} does not support saved credentials.",
                severity="error",
            )
            return
        try:
            FileCredentialStore().set(entry.credential_name, api_key)
            provider = provider_config_from_catalog_entry(entry.name)
            upsert_saved_provider(provider, set_default=False)
            self.session.reload_provider_settings()
            try:
                self.session.set_provider(entry.name, persist_default=False)
            except TypeError:
                self.session.set_provider(entry.name)
        except Exception as exc:
            self._notify(f"Could not save login: {exc}", severity="error")
            return
        self._notify(f"Saved login for {entry.display_name}.")
        self._refresh()

    def _handle_oauth_login_navigation_result(
        self,
        entry: ProviderCatalogEntry,
        result: OAuthCredential | _LoginFlowAction | None,
    ) -> None:
        if result is _LoginFlowAction.BACK:
            self._open_login_picker()
        else:
            self._handle_oauth_login_result(entry, result)

    def _handle_oauth_login_result(
        self,
        entry: ProviderCatalogEntry,
        credential: OAuthCredential | None,
    ) -> None:
        if credential is None:
            return
        if entry.credential_name is None:
            self._notify(
                f"Provider {entry.name} does not support saved credentials.",
                severity="error",
            )
            return
        try:
            FileCredentialStore().set_oauth(entry.credential_name, credential)
            provider = provider_config_from_catalog_entry(entry.name)
            upsert_saved_provider(provider, set_default=False)
            self.session.reload_provider_settings()
            try:
                self.session.set_provider(entry.name, persist_default=False)
            except TypeError:
                self.session.set_provider(entry.name)
        except Exception as exc:
            self._notify(f"Could not save login: {exc}", severity="error")
            return
        self._notify(f"Saved login for {entry.display_name}.")
        self._refresh()

    def _open_logout_picker(self) -> None:
        providers = _stored_credential_providers(BUILTIN_PROVIDER_CATALOG)
        if not providers:
            self._notify(NO_STORED_CREDENTIALS_MESSAGE, severity="warning")
            return
        self.push_screen(
            LoginProviderPickerScreen(
                providers,
                theme=self.tui_settings.resolved_theme,
                title="Logout",
            ),
            callback=self._handle_logout_provider_result,
        )

    def _handle_logout_provider_result(self, provider_name: str | _LoginFlowAction | None) -> None:
        if isinstance(provider_name, str):
            self._logout(provider_name)

    def _logout(self, provider_name: str) -> None:
        entry = builtin_provider_entry(provider_name)
        if entry is None:
            self._notify(f"Unknown provider: {provider_name}", severity="error")
            return

        if entry.credential_name is None:
            self._notify(NO_STORED_CREDENTIALS_MESSAGE, severity="warning")
            return
        credential_store = FileCredentialStore()
        if not _credential_store_has_entry(credential_store, entry.credential_name):
            self._notify(NO_STORED_CREDENTIALS_MESSAGE, severity="warning")
            return

        try:
            credential_store.delete(entry.credential_name)
            self.session.reload_provider_settings()
        except Exception as exc:
            self._notify(f"Could not log out: {exc}", severity="error")
            return

        if entry.kind == "openai-codex":
            self._notify(f"Logged out of {entry.display_name}.")
        else:
            self._notify(
                f"Removed stored API key for {entry.display_name}. "
                "Environment variables and providers.json config are unchanged."
            )
        self._refresh()

    def _available_model_choices(self) -> tuple[ModelChoice, ...]:
        fallback_choices = (
            ModelChoice(provider_name=self.session.provider_name, model=model)
            for model in self.session.available_models
        )
        return tuple(
            getattr(
                self.session,
                "available_model_choices",
                fallback_choices,
            )
        )

    def _open_local_backend_picker(self) -> None:

        runtime = getattr(self.session, "extension_runtime", None)
        registry = getattr(runtime, "local_backend_registry", None)
        if registry is None:
            self._notify("Local backend controls are unavailable.", severity="warning")
            return
        self.push_screen(
            LocalBackendPickerScreen(registry, theme=self.tui_settings.resolved_theme),
            callback=self._handle_local_backend_picker_result,
        )

    def _handle_local_backend_picker_result(self, backend_id: str | None) -> None:


        self.call_later(self._finish_local_backend_picker, backend_id)

    def _finish_local_backend_picker(self, backend_id: str | None) -> None:
        self._restore_prompt_focus()
        if backend_id is None:
            return
        runtime = getattr(self.session, "extension_runtime", None)
        registry = getattr(runtime, "local_backend_registry", None)
        if registry is None or registry.effective(backend_id) is None:
            self._notify("The selected local backend is no longer available.", severity="warning")
            return
        self.push_screen(
            LocalBackendScreen(
                registry,
                backend_id,
                theme=self.tui_settings.resolved_theme,
                on_use=self._use_local_model,
                notify_callback=self._notify_local_backend,
                is_idle=lambda: not self._is_agent_or_queue_active(),
            )
        )

    def _restore_prompt_focus(self) -> None:
        with suppress(NoMatches):
            self.query_one("#prompt", PromptInput).focus()

    def _notify_local_backend(self, message: str, level: str) -> None:
        severity: Literal["information", "warning", "error"] = {
            "info": "information",
            "warning": "warning",
            "error": "error",
        }.get(level, "information")
        self._notify(message, severity=severity)

    async def _use_local_model(self, provider_id: str, model_id: str) -> None:
        if self._is_agent_or_queue_active():
            self._notify(
                "Octo is still working. Press Escape to interrupt before switching models.",
                severity="warning",
            )
            return
        await self._switch_model(ModelChoice(provider_name=provider_id, model=model_id))

    def _open_tools_reference(self) -> None:

        self.push_screen(
            ToolsReferenceScreen(
                self.session.tools,
                extension_sources=self.session.extension_tool_sources,
                theme=self.tui_settings.resolved_theme,
            )
        )

    def _open_model_picker(self) -> None:
        choices = self._available_model_choices()
        scoped = tuple(getattr(self.session, "scoped_model_choices", ()))
        if not choices and not scoped:
            self._notify(
                "No configured providers are usable. Run /login to set up a provider.",
                severity="warning",
            )
            return
        self.push_screen(
            ModelPickerScreen(
                choices,
                scoped_choices=scoped,
                current_model=self.session.model,
                provider_name=self.session.provider_name,
                theme=self.tui_settings.resolved_theme,
                on_toggle_scoped=None,
                picker_kind="model",
            ),
            callback=self._handle_model_picker_result,
        )
        self.run_worker(self._refresh_open_model_picker(), exclusive=False)

    async def _refresh_open_model_picker(self) -> None:
        refresh = getattr(self.session, "refresh_model_catalogs", None)
        if not callable(refresh):
            return
        try:
            await refresh()
        except Exception as error:
            if isinstance(self.screen, ModelPickerScreen):
                self._notify(f"Could not refresh model catalogs: {error}", severity="warning")
            return
        if not isinstance(self.screen, ModelPickerScreen):
            return
        picker = self.screen
        while not picker.is_mounted:
            await asyncio.sleep(0)
            if self.screen is not picker:
                return
        picker.update_choices(
            self._available_model_choices(),
            tuple(getattr(self.session, "scoped_model_choices", ())),
        )

    def _open_scoped_models_picker(self) -> None:
        choices = self._available_model_choices()
        scoped = tuple(getattr(self.session, "scoped_model_choices", ()))
        if not choices and not scoped:
            self._notify(
                "No configured providers are usable. Run /login to set up a provider.",
                severity="warning",
            )
            return
        self.push_screen(
            ModelPickerScreen(
                choices,
                scoped_choices=scoped,
                current_model=self.session.model,
                provider_name=self.session.provider_name,
                theme=self.tui_settings.resolved_theme,
                on_toggle_scoped=self._toggle_scoped_model,
                picker_kind="scoped",
            ),
            callback=self._handle_scoped_models_picker_result,
        )
        self.run_worker(self._refresh_open_model_picker(), exclusive=False)

    def _toggle_scoped_model(self, choice: ModelChoice) -> Sequence[ModelChoice]:
        toggle_scoped_model = getattr(self.session, "toggle_scoped_model", None)
        if toggle_scoped_model is None:
            self._notify("Scoped model controls are not available.", severity="warning")
            return tuple(getattr(self.session, "scoped_model_choices", ()))
        try:
            return tuple(toggle_scoped_model(choice))
        except Exception as exc:
            self._notify(f"Could not update scoped models: {exc}", severity="error")
            return tuple(getattr(self.session, "scoped_model_choices", ()))

    def _handle_scoped_models_picker_result(self, choice: ModelChoice | None) -> None:
        del choice
        self._refresh_chrome()

    def _handle_model_picker_result(self, choice: ModelChoice | None) -> None:
        if choice is None:
            return
        self.run_worker(self._switch_model(choice), exclusive=False)

    async def _switch_model(self, choice: ModelChoice) -> None:
        try:
            select = getattr(self.session, "select_provider_model", None)
            if select is not None:
                result = select(choice)
                if isawaitable(result):
                    await result
            else:
                set_model_choice = getattr(self.session, "set_model_choice", None)
                if set_model_choice is None:
                    if choice.provider_name != self.session.provider_name:
                        self.session.set_provider(choice.provider_name)
                    self.session.set_model(choice.model)
                else:
                    set_model_choice(choice)
        except Exception as exc:
            self._notify(f"Could not switch model: {exc}", severity="error")
            return
        self._refresh_chrome()

    def _open_theme_picker(self) -> None:
        self.push_screen(
            ThemePickerScreen(
                current_theme=self.tui_settings.theme,
                theme=self.tui_settings.resolved_theme,
                theme_names=available_tui_theme_names(),
            ),
            callback=self._handle_theme_picker_result,
        )

    def _handle_theme_picker_result(self, theme: TuiThemeName | None) -> None:
        if theme is None:
            return
        self._set_tui_theme(theme)

    async def _set_thinking_level(self, level: str) -> None:
        setter = getattr(self.session, "set_thinking_level", None)
        if setter is None:
            self._notify("Thinking controls are not available.", severity="warning")
            return
        try:
            result = setter(level)
            if isawaitable(result):
                await result
        except Exception as exc:
            self._notify(f"Could not change thinking mode: {exc}", severity="error")
            return
        self._refresh_chrome()

    async def _cycle_thinking_level(self) -> None:
        cycler = getattr(self.session, "cycle_thinking_level", None)
        if cycler is None:
            self._notify("Thinking controls are not available.", severity="warning")
            return
        try:
            result = cycler()
            if isawaitable(result):
                await result
        except Exception as exc:
            self._notify(f"Could not change thinking mode: {exc}", severity="error")
            return
        self._refresh_chrome()

    async def _cycle_scoped_model(self, *, reverse: bool = False) -> None:
        cycler = getattr(self.session, "cycle_scoped_model", None)
        if cycler is None:
            self._notify("Scoped model controls are not available.", severity="warning")
            return
        try:
            result = cycler(reverse=reverse)
            if isawaitable(result):
                result = await result
        except Exception as exc:
            self._notify(f"Could not switch scoped model: {exc}", severity="error")
            return
        self._refresh_chrome()

    def _notify(
        self,
        message: str,
        *,
        severity: Literal["information", "warning", "error"] = "information",
    ) -> None:
        key = (message, severity)
        if key in self._active_notification_keys:
            return
        self._active_notification_keys.add(key)
        self.set_timer(
            self.NOTIFICATION_TIMEOUT,
            lambda: self._active_notification_keys.discard(key),
            name=f"notification-dedupe-{hash(key)}",
        )
        self.notify(message, severity=severity, markup=False)

    def _refresh(self) -> None:
        theme = self.tui_settings.resolved_theme
        self._refresh_chrome(theme=theme)
        transcript = self.query_one("#transcript", TranscriptView)
        transcript.update_from_state(self.state, theme=theme)

    def _refresh_chrome(self, *, theme: TuiTheme | None = None) -> None:

        theme = theme or self.tui_settings.resolved_theme
        self._sync_session_title()
        self._sync_text_selection_state()
        self._sync_queue_state()
        topic = self.query_one("#topic-indicator", Static)
        topic.update("当前话题：" + getattr(self.session, "current_topic_path", "主线"))
        topic.styles.color = theme.muted_text
        sidebar = self.query_one("#sidebar", SessionSidebar)
        sidebar.update_from_session(self.session, theme=theme)
        if self._extension_sidebar_theme != theme:
            self._rebuild_extension_sidebar_sections(theme=theme)
        compact_info = self.query_one("#compact-session-info", CompactSessionInfo)
        compact_info.update_from_session(self.session, theme=theme)
        queued_messages = self.query_one("#queued-messages", Static)
        queue_render_key = (
            self.state.queued_steering,
            self.state.queued_follow_up,
            theme.name,
            theme.muted_text,
        )
        if queue_render_key != self._last_queue_render_key:
            self._last_queue_render_key = queue_render_key
            queued_messages.display = self.state.queued_message_count > 0
            queued_messages.update(_render_queued_messages(self.state, theme=theme))
        self._sync_activity_indicator()
        self._refresh_footer_bindings()

    def _sync_queue_state(self) -> None:
        queue_event = getattr(self.session, "queue_update_event", None)
        if not callable(queue_event):
            return
        self.adapter.apply(queue_event())

    def _refresh_chrome_if_mounted(self) -> None:

        if not self.screen_stack:
            return
        with suppress(NoMatches):
            self._refresh_chrome()

    def _sync_activity_indicator(self) -> None:
        self._sync_terminal_title()
        if self._is_working():
            if self._activity_timer is None:
                self._activity_timer = self.set_interval(
                    ACTIVITY_TICK_SECONDS,
                    self._tick_activity,
                    name="activity-indicator",
                )
            else:
                self._activity_timer.resume()
            self._apply_activity_indicator()
            return
        self._activity_frame = 0
        if self._activity_timer is not None:
            self._activity_timer.pause()
        self._apply_activity_indicator()

    def _tick_activity(self) -> None:
        if not self._is_working():
            return
        self._activity_frame += 1
        self._apply_activity_indicator()
        self._sync_terminal_title()
        now = asyncio.get_running_loop().time()
        if now - self._last_tool_timer_refresh_at >= 1.0:
            self._last_tool_timer_refresh_at = now
            self.call_later(self._refresh_pending_tool_timer)

    async def _refresh_pending_tool_timer(self) -> None:

        if not self.state.running:
            return
        item = next(
            (
                candidate
                for candidate in reversed(self.state.items)
                if candidate.role == "tool" and candidate.tool_result_text is None
            ),
            None,
        )
        if item is None:
            return
        try:
            transcript = self.query_one("#transcript", TranscriptView)
        except NoMatches:
            return
        expanded = self.state.show_tool_results or item.always_show_tool_result
        await transcript.update_item(
            item,
            theme=self.tui_settings.resolved_theme,
            show_tool_results=expanded,
            invocation=self.state.resolve_tool_invocation(item, expanded=expanded),
            result_markup=self.state.resolve_tool_result(item, expanded=expanded),
        )

    def _apply_activity_indicator(self) -> None:
        theme = self.tui_settings.resolved_theme
        try:
            prompt = self.query_one("#prompt", PromptInput)
            prompt_prefix = self.query_one("#prompt-prefix", Static)
        except NoMatches:
            return
        shell_mode = _is_terminal_command_prompt(prompt.text)
        render_key = (
            theme.name,
            theme.accent,
            theme.screen_background,
            theme.prompt_border,
            self._activity_frame,
            self._is_working(),
            shell_mode,
        )
        if render_key == self._last_activity_indicator_key:
            return
        self._last_activity_indicator_key = render_key
        prompt.styles.border_left = (
            "tall",
            _activity_prompt_border_color(
                theme,
                frame=self._activity_frame,
                running=self._is_working(),
                shell_mode=shell_mode,
            ),
        )
        prompt_prefix.update(
            _render_activity_indicator(
                theme,
                frame=self._activity_frame,
                running=self._is_working(),
                shell_mode=shell_mode,
            ),
            layout=False,
        )

    def _refresh_completions(self) -> None:
        suggestions = self.query_one("#autocomplete", Static)
        suggestions.display = bool(self._completion_state.items)
        if not self._completion_state.items:
            self._completion_visible_line_budget = None
            suggestions.update(
                render_completion_suggestions(
                    CompletionState(),
                    theme=self.tui_settings.resolved_theme,
                )
            )
            self._refresh_footer_bindings()
            return
        max_lines = self._completion_window_line_budget(suggestions)
        suggestions.update(
            render_completion_suggestions(
                _visible_completion_state(
                    self._completion_state,
                    max_lines=max_lines,
                    width=max(suggestions.content_size.width or suggestions.size.width, 1),
                ),
                theme=self.tui_settings.resolved_theme,
            )
        )
        self._refresh_footer_bindings()

    def _completion_window_line_budget(self, suggestions: Static) -> int:

        measured_limit = _completion_visible_line_limit(suggestions)
        if suggestions.size.height <= 0:
            if self._completion_visible_line_budget is None:
                self._completion_visible_line_budget = self._initial_completion_line_budget()
            return self._completion_visible_line_budget
        self._completion_visible_line_budget = max(
            self._completion_visible_line_budget or measured_limit,
            measured_limit,
        )
        return self._completion_visible_line_budget

    def _initial_completion_line_budget(self) -> int:

        terminal_height = self.size.height
        if terminal_height <= 0:
            return COMPLETION_MAX_VISIBLE_LINES

        reserved_rows = COMPLETION_MIN_TRANSCRIPT_LINES + COMPLETION_WIDGET_CHROME_LINES
        for selector in ("#prompt-row", "#compact-session-info", "#queued-messages"):
            with suppress(NoMatches):
                widget = self.query_one(selector)
                if widget.display:
                    reserved_rows += widget.outer_size.height

        available_rows = terminal_height - reserved_rows
        terminal_fraction_rows = max(1, terminal_height // COMPLETION_INITIAL_TERMINAL_FRACTION)
        return max(
            1,
            min(COMPLETION_MAX_VISIBLE_LINES, available_rows, terminal_fraction_rows),
        )

    def _update_responsive_layout(self, width: int, height: int) -> None:
        if self._sidebar_visibility_override is not None:
            show_sidebar = self._sidebar_visibility_override
        elif self.tui_settings.sidebar_position == "off":
            show_sidebar = False
        else:
            show_sidebar = width >= SIDEBAR_MIN_WIDTH and height >= SIDEBAR_MIN_HEIGHT
        self.set_class(not show_sidebar, "-hide-sidebar")

    def _apply_sidebar_position(self) -> None:

        pos = self.tui_settings.sidebar_position


        show_right = pos == "right" or (pos == "off" and self._sidebar_visibility_override is True)
        self.set_class(show_right, "-sidebar-right")

    def _toggle_sidebar_visibility(self) -> None:

        currently_visible = not self.has_class("-hide-sidebar")
        self._sidebar_visibility_override = not currently_visible
        self._apply_sidebar_position()
        self._update_responsive_layout(self.size.width, self.size.height)
        state = "shown" if self._sidebar_visibility_override else "hidden"
        self._notify(f"Sidebar {state} for this session.")

    def _build_completion_state(self, text: str, *, cursor: int | None = None) -> CompletionState:
        registry = _session_command_registry(self.session)
        return build_completion_state(
            text,
            cursor=cursor,
            command_registry=registry,
            skills=self.session.skills,
            prompt_templates=self.session.prompt_templates,
            model_names=self.session.available_models,
            provider_names=(
                *self.session.available_providers,
                *LOGIN_PROVIDER_ALIASES,
            ),
            thinking_levels=getattr(self.session, "available_thinking_levels", ()),
            theme_names=available_tui_theme_names(),
            session_options=(_session_options(self.session) if text.startswith("/resume ") else ()),
            cwd=self.session.cwd,
        )

    def _refresh_footer_bindings(self) -> None:
        prompt = self.query_one("#prompt", PromptInput)
        prompt.set_footer_mode(
            _prompt_footer_mode(self._completion_state, working=self._is_working())
        )

    def _sync_prompt_shell_mode(self, text: str) -> None:
        prompt = self.query_one("#prompt", PromptInput)
        prompt.shell_mode_style = self.tui_settings.resolved_theme.role_styles["tool"].border
        prompt.set_class(_is_terminal_command_prompt(text), "-shell-mode")
        prompt.refresh()
        self._apply_activity_indicator()


def _activity_prompt_border_color(
    theme: TuiTheme,
    *,
    frame: int,
    running: bool,
    shell_mode: bool,
) -> str:

    del frame, running
    if shell_mode:
        return theme.role_styles["tool"].border
    return theme.prompt_border


def _render_activity_indicator(
    theme: TuiTheme,
    *,
    frame: int,
    running: bool,
    shell_mode: bool = False,
) -> Text:

    if shell_mode and not running:
        return Text("$", style=f"bold {theme.role_styles['tool'].border}")
    if not running:
        return Text("Octo", style=f"bold {theme.accent}")

    cycle_length = (ACTIVITY_INDICATOR_HEIGHT - 1) * 2
    cycle_position = frame % cycle_length
    active_row = (
        cycle_position
        if cycle_position < ACTIVITY_INDICATOR_HEIGHT
        else cycle_length - cycle_position
    )
    direction = 1 if cycle_position < ACTIVITY_INDICATOR_HEIGHT else -1
    trail_rows = {
        active_row: theme.accent,
        active_row - direction: _blend_hex_colors(
            theme.accent,
            theme.screen_background,
            fraction=0.35,
        ),
        active_row - (direction * 2): _blend_hex_colors(
            theme.accent,
            theme.screen_background,
            fraction=0.65,
        ),
    }

    rendered = Text()
    for row in range(ACTIVITY_INDICATOR_HEIGHT):
        color = trail_rows.get(row)
        if color is None:
            rendered.append(" ")
        else:
            rendered.append("■", style=color)
        if row < ACTIVITY_INDICATOR_HEIGHT - 1:
            rendered.append("\n")
    return rendered


def _is_terminal_command_prompt(text: str) -> bool:

    return _terminal_command_prefix_span(text) is not None


def _should_optimistically_render_prompt(text: str) -> bool:

    stripped = text.strip()
    return bool(stripped) and not stripped.startswith("/")


def _is_user_message_end_event(event: CodingSessionEvent) -> bool:

    return isinstance(event, MessageEndEvent) and isinstance(
        event.message, (UserMessage, CustomMessage)
    )


def _terminal_command_prefix_span(text: str) -> tuple[int, int] | None:

    leading_whitespace = len(text) - len(text.lstrip())
    stripped = text[leading_whitespace:]
    if stripped.startswith("!!"):
        return (leading_whitespace, leading_whitespace + 2)
    if stripped.startswith("!"):
        return (leading_whitespace, leading_whitespace + 1)
    return None


def _blend_hex_colors(start: str, end: str, *, fraction: float) -> str:

    start_rgb = _hex_to_rgb(start)
    end_rgb = _hex_to_rgb(end)
    blended = tuple(
        round(start_channel + (end_channel - start_channel) * fraction)
        for start_channel, end_channel in zip(start_rgb, end_rgb, strict=True)
    )
    return f"#{blended[0]:02x}{blended[1]:02x}{blended[2]:02x}"


def _hex_to_rgb(color: str) -> tuple[int, int, int]:
    value = color.removeprefix("#")
    if len(value) != 6:
        raise ValueError(f"Expected #rrggbb color, got {color!r}")
    return (int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16))


def _completion_visible_line_limit(suggestions: Static) -> int:

    if suggestions.size.height > 0:
        return max(min(COMPLETION_MAX_VISIBLE_LINES, suggestions.size.height), 1)
    return COMPLETION_MAX_VISIBLE_LINES


def _visible_completion_state(
    state: CompletionState,
    *,
    max_lines: int,
    width: int | None = None,
) -> CompletionState:

    if not state.items or max_lines <= 0:
        return CompletionState()

    selected_line_limit = max(max_lines - 1, 1)
    start = 0
    while start < state.selected_index:
        candidate = CompletionState(
            items=state.items[start:],
            selected_index=state.selected_index - start,
        )
        if _completion_selected_render_line(candidate, width=width) < selected_line_limit:
            break
        start += 1

    end = len(state.items)
    while end > state.selected_index + 1:
        candidate = CompletionState(
            items=state.items[start:end],
            selected_index=state.selected_index - start,
        )
        if _completion_render_line_count(candidate, width=width) <= max_lines:
            break
        end -= 1

    while start < state.selected_index:
        candidate = CompletionState(
            items=state.items[start:end],
            selected_index=state.selected_index - start,
        )
        if _completion_render_line_count(candidate, width=width) <= max_lines:
            break
        start += 1

    return CompletionState(
        items=state.items[start:end],
        selected_index=state.selected_index - start,
    )


def _completion_selected_render_line(state: CompletionState, *, width: int | None = None) -> int:

    line = 0
    has_rendered_text = False
    previous_category: str | None = None
    for index, item in enumerate(state.items):
        if item.category != previous_category:
            if has_rendered_text:
                line += 1
            if item.category:
                line += 1
                has_rendered_text = True
            previous_category = item.category
        elif has_rendered_text:
            line += 1
        if index == state.selected_index:
            return line
        line += _completion_item_extra_wrapped_lines(item, width=width)
        has_rendered_text = True
    return line


def _completion_render_line_count(state: CompletionState, *, width: int | None = None) -> int:

    if not state.items:
        return 0
    line_count = 0
    previous_category: str | None = None
    for index, item in enumerate(state.items):
        if item.category != previous_category:
            if index:
                line_count += 1
            if item.category:
                line_count += 1
            previous_category = item.category
        line_count += 1 + _completion_item_extra_wrapped_lines(item, width=width)
    return line_count


def _completion_item_extra_wrapped_lines(
    item: CompletionItem,
    *,
    width: int | None,
) -> int:

    if width is None or width <= 0 or not item.description:
        return 0
    output = StringIO()
    console = Console(
        file=output,
        width=width,
        force_terminal=False,
        color_system=None,
        legacy_windows=False,
    )
    console.print(
        render_completion_suggestions(
            CompletionState(items=(item,), selected_index=0),
            theme=OCTO_DARK_THEME,
        ),
        end="",
    )
    line_count = len(output.getvalue().splitlines())
    return max(line_count - 1, 0)


def _session_command_registry(session: CodingSession) -> CommandRegistry:
    registry = getattr(session, "command_registry", None)
    if isinstance(registry, CommandRegistry):
        return registry
    return create_default_command_registry()


def _session_options(session: CodingSession) -> tuple[CompletionOption, ...]:
    return tuple(_session_option(record) for record in _session_records(session))


def _local_session_records(session: CodingSession) -> tuple[SessionCompletionRecord, ...]:

    manager = getattr(session, "session_manager", None)
    if manager is None:
        return ()
    try:
        records = manager.list_sessions(session.cwd)
    except TypeError:
        records = manager.list_sessions()
    local_cwd = Path(session.cwd).resolve()
    return tuple(record for record in records if Path(record.cwd).resolve() == local_cwd)


def _session_records(session: CodingSession) -> tuple[SessionCompletionRecord, ...]:

    manager = getattr(session, "session_manager", None)
    if manager is None:
        return ()
    try:
        records = list(manager.list_sessions())
    except TypeError:

        records = list(manager.list_sessions(session.cwd))
    local_cwd = Path(session.cwd).resolve()
    local = [record for record in records if Path(record.cwd).resolve() == local_cwd]
    other = [record for record in records if Path(record.cwd).resolve() != local_cwd]
    return tuple(local) + tuple(other)


def _session_option(record: SessionCompletionRecord) -> CompletionOption:
    description_parts = [record.title if record.title else "Untitled session"]
    if record.model:
        description_parts.append(record.model)
    description_parts.append(_short_path(record.cwd))
    return CompletionOption(value=record.id, description=" - ".join(description_parts))


def _short_path(path: Path) -> str:
    home = Path.home()
    try:
        relative = path.relative_to(home)
        return "~" if relative == Path(".") else f"~/{relative}"
    except ValueError:
        return str(path)


def _session_picker_label(record: SessionCompletionRecord) -> str:


    parts = [_session_updated_at_label(record.updated_at)]
    title = _named_session_title(record.title)
    if title is not None:
        parts.append(title)
    if record.model:
        parts.append(record.model)
    return "  ".join(parts)


def _filter_session_records(
    records: Sequence[SessionCompletionRecord],
    query: str,
) -> tuple[SessionCompletionRecord, ...]:
    normalized = query.strip().casefold()
    if not normalized:
        return tuple(records)
    return tuple(
        record
        for record in records
        if normalized in (record.title or "").casefold() or normalized in record.model.casefold()
    )


def _session_updated_at_label(timestamp: float) -> str:

    delta = (datetime.now() - datetime.fromtimestamp(timestamp)).total_seconds()
    minutes = int(delta // 60)
    if minutes < 1:
        return "now"
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    if days == 1:
        return "yesterday"
    if days < 7:
        return f"{days}d ago"
    return datetime.fromtimestamp(timestamp).strftime("%b %d")


def _named_session_title(title: str | None) -> str | None:
    if title is None:
        return None
    stripped = title.strip()
    if not stripped or stripped.lower() == "untitled session":
        return None
    return stripped


def _login_provider_label(provider: ProviderCatalogEntry) -> str:
    return f"{provider.display_name} — {provider.name}"


def _subscription_login_providers(
    providers: Sequence[ProviderCatalogEntry],
) -> tuple[ProviderCatalogEntry, ...]:
    provider_ids = oauth_provider_ids()
    return tuple(provider for provider in providers if provider.name in provider_ids)


def _api_key_login_providers(
    providers: Sequence[ProviderCatalogEntry],
) -> tuple[ProviderCatalogEntry, ...]:
    return tuple(provider for provider in providers if "api_key" in provider.auth_methods)


def _stored_credential_providers(
    providers: Sequence[ProviderCatalogEntry],
) -> tuple[ProviderCatalogEntry, ...]:
    credential_store = FileCredentialStore()
    return tuple(
        provider
        for provider in providers
        if provider.credential_name is not None
        and _credential_store_has_entry(credential_store, provider.credential_name)
    )


def _credential_store_has_entry(
    credential_store: FileCredentialStore,
    credential_name: str,
) -> bool:
    return (
        credential_store.get(credential_name) is not None
        or credential_store.get_oauth(credential_name) is not None
    )


def _theme_picker_label(theme_name: TuiThemeName, *, current_theme: TuiThemeName) -> str:
    marker = "✓" if theme_name == current_theme else " "
    return f"{marker} {theme_name}"


def _model_picker_label(
    choice: ModelChoice,
    *,
    current_model: str,
    current_provider: str,
    scoped: bool = False,
    unavailable: bool = False,
) -> str:
    marker = (
        "* "
        if (choice.provider_name == current_provider and choice.model == current_model)
        else "  "
    )
    suffix = (" [scoped]" if scoped else "") + (" [unavailable]" if unavailable else "")
    return f"{marker}{choice.provider_name}:{choice.model}{suffix}"


def _filter_login_providers(
    providers: Sequence[ProviderCatalogEntry],
    query: str,
) -> tuple[ProviderCatalogEntry, ...]:
    normalized = query.strip().casefold()
    if not normalized:
        return tuple(providers)
    return tuple(
        provider
        for provider in providers
        if normalized in provider.name.casefold() or normalized in provider.display_name.casefold()
    )


def _filter_model_choices(choices: Sequence[ModelChoice], query: str) -> tuple[ModelChoice, ...]:
    normalized = query.strip().lower()
    if not normalized:
        return tuple(choices)
    return tuple(
        choice
        for choice in choices
        if normalized in choice.provider_name.lower() or normalized in choice.model.lower()
    )


def _command_message_uses_transcript(command_text: str) -> bool:

    command_name = command_text.split(maxsplit=1)[0].casefold()
    return command_name in {"/reload", "/system"}


def _command_message_uses_notification(command_text: str, message: str) -> bool:

    command_name = command_text.split(maxsplit=1)[0].casefold()
    return command_name == "/name" and message.startswith("Session renamed: ")


def _command_output_title(command_text: str) -> str:
    command_name = command_text.split(maxsplit=1)[0].removeprefix("/")
    return f"/{command_name or 'help'}"


def _is_thinking_cycle_key(key: str, configured_key: str) -> bool:
    if key == configured_key:
        return True
    return configured_key == "shift+tab" and key == "backtab"


def _render_queued_messages(state: TuiState, *, theme: TuiTheme) -> Group:

    rows: list[Text] = []
    for message in state.queued_steering:
        row = Text("↪ steering · queued: ", style=theme.muted_text)
        row.append(_queued_message_preview(message), style=theme.prompt_text)
        rows.append(row)
    for message in state.queued_follow_up:
        row = Text("↳ follow-up · queued: ", style=theme.muted_text)
        row.append(_queued_message_preview(message), style=theme.prompt_text)
        rows.append(row)
    return Group(*rows)


def _queued_message_preview(message: str) -> str:

    lines = message.splitlines()
    return lines[0] if lines else ""


def _prompt_footer_mode(
    completion_state: CompletionState,
    *,
    working: bool,
) -> Literal["normal", "completion", "file_completion", "running"]:
    selected = completion_state.selected
    if selected is not None:
        if selected.kind is CompletionKind.FILE_REFERENCE:
            return "file_completion"
        return "completion"
    if working:
        return "running"
    return "normal"


def _key_hint(key: str) -> str:
    return "+".join(part.capitalize() for part in key.split("+"))


def _app_bindings(keybindings: TuiKeybindings) -> list[Binding]:
    return [
        Binding(keybindings.cancel, "cancel", "Cancel"),
        Binding(keybindings.command_palette, "open_command_palette", "Commands"),
        Binding(keybindings.session_picker, "open_session_picker", "Sessions"),
        Binding(keybindings.thinking_cycle, "cycle_thinking", "Thinking"),
        Binding(keybindings.model_cycle, "cycle_model", "Model"),
        Binding(
            keybindings.model_cycle_reverse,
            "cycle_model_reverse",
            "Previous model",
            show=False,
        ),
        Binding(
            keybindings.accept_completion,
            "accept_completion",
            "Complete",
            priority=True,
        ),
        Binding(
            keybindings.queue_follow_up,
            "submit_follow_up",
            "Follow-up",
            priority=True,
        ),
        Binding(
            keybindings.completion_next,
            "completion_next",
            "Next completion",
            priority=True,
        ),
        Binding(
            keybindings.completion_previous,
            "completion_previous",
            "Previous completion",
            priority=True,
        ),
        Binding(keybindings.toggle_tool_results, "toggle_tool_results", "Tool results"),
        Binding(keybindings.toggle_thinking, "toggle_thinking", "Thinking tokens"),
        Binding(keybindings.copy_message, "clear_prompt", "Clear input"),
        Binding(keybindings.quit, "quit", "Quit"),
    ]


def _prompt_bindings(
    keybindings: TuiKeybindings,
    *,
    mode: Literal["normal", "completion", "file_completion", "running"],
) -> list[Binding]:
    if mode in {"completion", "file_completion"}:
        bindings = [
            Binding(
                keybindings.accept_completion,
                "accept_completion",
                "Complete",
                key_display=(
                    _key_hint(keybindings.accept_completion)
                    if mode == "file_completion"
                    else f"{_key_hint(keybindings.accept_completion)}/Enter"
                ),
                priority=True,
            ),
            Binding(
                keybindings.completion_next,
                "completion_next",
                "Choose",
                key_display=(
                    f"{_key_hint(keybindings.completion_previous)}/"
                    f"{_key_hint(keybindings.completion_next)}"
                ),
                priority=True,
            ),
            Binding(keybindings.cancel, "cancel", "Close", priority=True),
        ]
        if mode == "file_completion":
            bindings.insert(1, Binding("enter", "submit_prompt", "Submit raw", priority=True))
        return bindings + _hidden_prompt_bindings(keybindings, visible_bindings=bindings)
    if mode == "running":
        bindings = [
            Binding("enter", "submit_prompt", "Steer", priority=True),
            Binding(keybindings.queue_follow_up, "submit_follow_up", "Follow-up", priority=True),
            Binding(keybindings.cancel, "cancel", "Cancel", priority=True),
            Binding(
                keybindings.toggle_thinking,
                "toggle_thinking",
                "Thinking",
                priority=True,
            ),
            Binding(
                keybindings.toggle_tool_results,
                "toggle_tool_results",
                "Tools",
                priority=True,
            ),
        ]
        return bindings + _hidden_prompt_bindings(keybindings, visible_bindings=bindings)
    bindings = [
        Binding("enter", "submit_prompt", "Submit", priority=True),
        Binding(
            keybindings.insert_newline,
            "insert_newline",
            "Newline",
            priority=True,
        ),
        Binding(keybindings.command_palette, "open_command_palette", "Commands", priority=True),
        Binding(keybindings.session_picker, "open_session_picker", "Sessions", priority=True),
        Binding(keybindings.thinking_cycle, "cycle_thinking", "Thinking", priority=True),
        Binding(keybindings.model_cycle, "cycle_model", "Model", priority=True),
        Binding(
            keybindings.model_cycle_reverse,
            "cycle_model_reverse",
            "Previous model",
            show=False,
            priority=True,
        ),
        Binding(
            keybindings.copy_message,
            "clear_prompt",
            "Clear",
            priority=True,
        ),
        Binding(keybindings.quit, "quit", "Quit", priority=True),
    ]
    return bindings + _hidden_prompt_bindings(keybindings, visible_bindings=bindings)


def _hidden_prompt_bindings(
    keybindings: TuiKeybindings,
    *,
    visible_bindings: Sequence[Binding],
) -> list[Binding]:
    visible_keys = {key for binding in visible_bindings for key in binding.key.split(",")}
    candidates = (
        (keybindings.command_palette, "open_command_palette"),
        (keybindings.session_picker, "open_session_picker"),
        (keybindings.queue_follow_up, "submit_follow_up"),
        (keybindings.insert_newline, "insert_newline"),
        (keybindings.thinking_cycle, "cycle_thinking"),
        (keybindings.model_cycle, "cycle_model"),
        (keybindings.model_cycle_reverse, "cycle_model_reverse"),
        (keybindings.toggle_tool_results, "toggle_tool_results"),
        (keybindings.toggle_thinking, "toggle_thinking"),
        (keybindings.copy_message, "clear_prompt"),
        (keybindings.accept_completion, "accept_completion"),
        (keybindings.completion_next, "completion_next"),
        (keybindings.completion_previous, "completion_previous"),
        (keybindings.quit, "quit"),
    )
    return [
        Binding(key, action, show=False, priority=True)
        for key, action in candidates
        if key not in visible_keys
    ]


def _text_end_location(text: str) -> tuple[int, int]:

    line, _, column_text = text.rpartition("\n")
    return (line.count("\n") + 1 if line else 0, len(column_text))


def _format_prompt_error(exc: BaseException, session: CodingSession) -> str:
    detail = str(exc) or type(exc).__name__
    message = f"Error: {detail}"
    log_path = getattr(session, "last_diagnostic_log_path", None)
    if isinstance(log_path, Path):
        return f"{message}\nLog: {log_path}"
    return message


_TERMINAL_ERROR_RETRY_HINT = "Run ended before completion. Send a message to retry."


def _attach_retry_hint_to_error(state: TuiState, message: AssistantMessage) -> None:

    if is_context_overflow_error(message):
        return
    if state.error is not None and _TERMINAL_ERROR_RETRY_HINT not in state.error:
        state.error = f"{state.error}\n{_TERMINAL_ERROR_RETRY_HINT}"
    for item in reversed(state.items):
        if item.role == "error":
            if _TERMINAL_ERROR_RETRY_HINT not in item.text:
                item.text = f"{item.text}\n{_TERMINAL_ERROR_RETRY_HINT}"
            return


def _attach_diagnostic_log_path_to_error(state: TuiState, session: CodingSession) -> None:
    log_path = getattr(session, "last_diagnostic_log_path", None)
    if not isinstance(log_path, Path) or state.error is None:
        return
    message = f"Error: {state.error}\nLog: {log_path}"
    state.error = message
    for item in reversed(state.items):
        if item.role == "error":
            item.text = message
            return
    state.add_item("error", message)


def _explicit_resume_record(
    manager: SessionManager,
    *,
    session_id: str | None,
) -> CodingSessionRecord | None:
    if session_id is None:
        return None
    record = manager.get_session(session_id)
    if record is None:
        raise RuntimeError(f"Unknown session: {session_id}")
    return record


def _create_startup_session_record(
    manager: SessionManager,
    *,
    cwd: Path,
    selection: ProviderSelection,
    inference_provider: str | None = None,
) -> CodingSessionRecord:
    if inference_provider is None:
        return manager.prepare_session(
            cwd=cwd,
            model=selection.model,
            provider_name=selection.provider.name,
        )
    try:
        return manager.prepare_session(
            cwd=cwd,
            model=selection.model,
            provider_name=selection.provider.name,
            inference_provider=inference_provider,
            inference_provider_mode="fixed",
        )
    except TypeError:
        try:
            return manager.prepare_session(
                cwd=cwd,
                model=selection.model,
                provider_name=selection.provider.name,
                inference_provider=inference_provider,
            )
        except TypeError:
            return manager.prepare_session(
                cwd=cwd,
                model=selection.model,
                provider_name=selection.provider.name,
            )


def _resolve_tui_startup_selection(
    settings: Any,
    *,
    record: Any | None,
    provider_name: str | None,
    model: str | None,
    explicit_resume: bool,
) -> ProviderSelection:
    if provider_name is not None or model is not None:
        return resolve_provider_selection(settings, provider_name=provider_name, model=model)

    if explicit_resume:
        record_selection = _selection_from_session_record(settings, record)
        if record_selection is not None:
            return record_selection

    default_selection = resolve_provider_selection(settings)
    if provider_has_usable_credentials(
        default_selection.provider,
        credential_reader=FileCredentialStore(),
    ):
        return default_selection

    fallback_selection = _first_usable_startup_selection(settings)
    return fallback_selection or default_selection


def _first_usable_startup_selection(settings: Any) -> ProviderSelection | None:
    credential_store = FileCredentialStore()
    for provider in settings.providers:
        if provider_has_usable_credentials(provider, credential_reader=credential_store):
            return ProviderSelection(provider=provider, model=provider.default_model)
    return None


def _selection_from_session_record(settings: Any, record: Any | None) -> ProviderSelection | None:
    if record is None:
        return None
    record_model = getattr(record, "model", None)
    if not isinstance(record_model, str) or not record_model:
        return None

    record_provider = getattr(record, "provider_name", None)
    if isinstance(record_provider, str) and record_provider:
        try:
            return resolve_provider_selection(
                settings,
                provider_name=record_provider,
                model=record_model,
            )
        except Exception:
            return None

    for choice in _usable_scoped_startup_choices(settings):
        if choice.model == record_model:
            return resolve_provider_selection(
                settings,
                provider_name=choice.provider_name,
                model=choice.model,
            )

    credential_store = FileCredentialStore()
    for provider in settings.providers:
        if record_model not in provider.models:
            continue
        if not provider_has_usable_credentials(provider, credential_reader=credential_store):
            continue
        return ProviderSelection(provider=provider, model=record_model)
    return None


def _usable_scoped_startup_choices(settings: Any) -> tuple[ModelChoice, ...]:
    credential_store = FileCredentialStore()
    choices: list[ModelChoice] = []
    for item in settings.scoped_models:
        try:
            provider = settings.get_provider(item.provider)
        except Exception:
            continue
        if item.model not in provider.models:
            continue
        if not provider_has_usable_credentials(provider, credential_reader=credential_store):
            continue
        choices.append(ModelChoice(provider_name=item.provider, model=item.model))
    return tuple(choices)


def _resource_conflict_alert(
    diagnostics: Sequence[ResourceDiagnostic],
) -> str | None:

    prefix = "overrides lower-precedence resource at "
    conflicts = [
        diagnostic
        for diagnostic in diagnostics
        if diagnostic.kind in {"skill", "prompt"}
        and diagnostic.name is not None
        and diagnostic.path is not None
        and diagnostic.message.startswith(prefix)
    ]
    if not conflicts:
        return None

    lines = ["Conflicting skills/prompts detected:"]
    for diagnostic in conflicts:
        resource_kind = "skill" if diagnostic.kind == "skill" else "prompt template"
        shadowed_path = diagnostic.message.removeprefix(prefix)
        lines.append(
            f"- {resource_kind} '{diagnostic.name}': {diagnostic.path} overrides {shadowed_path}"
        )
    lines.append("Rename or remove duplicate resources to clear this alert.")
    return "\n".join(lines)


def _startup_inference_provider(
    selection: ProviderSelection,
    record: CodingSessionRecord | None,
) -> str | None:
    provider = selection.provider
    if not isinstance(provider, OpenAICompatibleProviderConfig) or provider.name != "huggingface":
        return None
    if record is not None and record.model == selection.model:
        return record.inference_provider
    return provider.inference_providers.get(selection.model)


def _startup_inference_provider_mode(
    selection: ProviderSelection,
    record: CodingSessionRecord | None,
) -> Literal["automatic", "fixed"]:
    if record is not None and record.model == selection.model:
        return record.inference_provider_mode
    return "fixed" if _startup_inference_provider(selection, None) is not None else "automatic"


async def run_tui_app(
    *,
    model: str | None,
    cwd: Path,
    session_id: str | None = None,
    new_session: bool = False,
    provider_name: str | None = None,
    auto_compact_token_threshold: int | None = None,
    initial_prompt: str | None = None,
    session_manager: SessionManager | None = None,
    startup_notice: str | None = None,
    startup_notices: Sequence[str] = (),
    extension_paths: tuple[Path, ...] = (),
    extensions_enabled: bool = True,
    project_extensions_enabled: bool = False,
    custom_system_prompt: str | None = None,
    append_system_prompt: str | None = None,
    trust_override: TrustOverride | None = None,
    thinking_level_override: ThinkingLevel | None = None,
) -> str | None:

    _configure_herdr_textual_mouse()
    if new_session and session_id is not None:
        raise RuntimeError("--session and --new-session cannot be used together")

    provider_settings = load_provider_settings()
    shell_settings = load_shell_settings()
    manager = session_manager or SessionManager()
    record = _explicit_resume_record(
        manager,
        session_id=session_id,
    )
    selection: ProviderSelection | None = None
    try:
        selection = _resolve_tui_startup_selection(
            provider_settings,
            record=record,
            provider_name=provider_name,
            model=model,
            explicit_resume=session_id is not None,
        )
    except ProviderConfigError:


        dynamic_resume = (
            session_id is not None
            and record is not None
            and record.provider_name is not None
            and provider_name is None
            and model is None
        )
        explicit_dynamic = provider_name is not None and model is not None
        if not dynamic_resume and not explicit_dynamic:
            raise
    startup_message: str | None = None
    startup_error_notice: str | None = None
    explicit_selection = provider_name is not None or model is not None
    selected_provider_name: str = (
        provider_name
        if provider_name is not None
        else (record.provider_name if record is not None else None)
        or (selection.provider.name if selection is not None else DEFAULT_PROVIDER_NAME)
    )
    selected_model = (
        model
        if explicit_selection and model is not None
        else (record.model if record is not None else None)
        or (selection.model if selection is not None else DEFAULT_MODEL)
    )


    initial_provider: ClosableModelProvider | None = None
    runtime_provider_config: ProviderConfig | None = selection.provider if selection else None
    inference_provider = _startup_inference_provider(selection, record) if selection else None
    inference_provider_mode: Literal["automatic", "fixed"] = (
        _startup_inference_provider_mode(selection, record) if selection else "automatic"
    )
    if selection is not None:
        try:
            initial_provider = create_model_provider(
                selection.provider,
                model=selection.model,
                inference_provider=inference_provider,
                thinking_level=resolve_startup_thinking_level(
                    selection.provider,
                    selection.model,
                    cli_override=thinking_level_override,
                ),
            )
        except RuntimeError as exc:
            login_required_message = (
                "Login required. Run /login to choose a provider, "
                f"or /login {selected_provider_name} to continue with the current provider."
            )
            startup_message = f"{login_required_message}\n\nStartup error: {exc}"
            startup_error_notice = (
                f"Startup provider creation failed for "
                f"{selection.provider.name}:{selection.model}: {exc}"
            )
            initial_provider = LoginRequiredProvider(startup_message)
            runtime_provider_config = None
    elif not explicit_selection:
        startup_message = (
            "Login required. Run /login to choose a provider, "
            f"or /login {selected_provider_name} to continue with the current provider."
        )
        initial_provider = LoginRequiredProvider(startup_message)
    session: CodingSession | None = None
    try:
        index_on_first_persist = False
        if record is None:
            if selection is not None:
                record = _create_startup_session_record(
                    manager,
                    cwd=cwd,
                    selection=selection,
                    inference_provider=inference_provider,
                )
            else:
                if provider_name is None or model is None:
                    raise ProviderConfigError(
                        "An explicit provider and model are required for this startup."
                    )
                record = manager.prepare_session(
                    cwd=cwd,
                    model=model,
                    provider_name=provider_name,
                )
            index_on_first_persist = manager.get_session(record.id) is None

        prepared = await prepare_coding_session(
            CodingSessionConfig(
                provider=initial_provider,
                model=record.model or selected_model,
                cwd=record.cwd,
                storage=manager.storage_for(record),
                session_id=record.id,
                session_manager=manager,
                provider_name=selected_provider_name,
                inference_provider=inference_provider,
                inference_provider_mode=inference_provider_mode,
                requested_provider=provider_name if explicit_selection else None,
                requested_model=model if explicit_selection else None,
                session_provider_name=record.provider_name,
                provider_settings=provider_settings,
                runtime_provider_config=runtime_provider_config,
                auto_compact_token_threshold=auto_compact_token_threshold,
                index_on_first_persist=index_on_first_persist,
                shell_command_prefix=shell_settings.shell_command_prefix,
                extension_paths=extension_paths,
                extensions_enabled=extensions_enabled,
                project_extensions_enabled=project_extensions_enabled,
                custom_system_prompt=custom_system_prompt,
                append_system_prompt=append_system_prompt,
                thinking_level_override=thinking_level_override,
                trust_override=trust_override,
                trust_default=shell_settings.default_project_trust,
                trust_interactive=True,
                trust_prompt=prompt_project_trust,
                defer_authoritative_writes=True,
                owns_initial_provider=initial_provider is not None,
            ),
            session_loader=CodingSession,
        )
        try:
            session = await prepared.adopt()
        except ValueError:
            candidate = prepared.session
            trust_resolution = getattr(candidate, "project_trust_resolution", None)
            if trust_resolution is None or not trust_resolution.cancelled:
                raise


            del candidate
            return None
        trust_resolution = getattr(session, "project_trust_resolution", None)
        if trust_resolution is not None and trust_resolution.cancelled:
            return None

        theme_dirs = getattr(session, "theme_dirs", None)
        if theme_dirs is None:
            trusted = trust_resolution is None or trust_resolution.trusted
            theme_dirs = OctoResourcePaths(
                cwd=record.cwd,
                project_resources_enabled=trusted,
            ).themes_dirs
        custom_themes, theme_diagnostics = load_custom_tui_themes(theme_dirs)
        set_custom_tui_themes(custom_themes)
        legacy_notices = (startup_notice,) if startup_notice else ()
        error_notices = (startup_error_notice,) if startup_error_notice else ()
        theme_notices = tuple(diagnostic.format() for diagnostic in theme_diagnostics)
        all_startup_notices = tuple(
            (*error_notices, *startup_notices, *legacy_notices, *theme_notices)
        )
        resource_conflict_alert = _resource_conflict_alert(
            getattr(session, "resource_diagnostics", ())
        )
        startup_alerts = (resource_conflict_alert,) if resource_conflict_alert is not None else ()
        app = OctoTuiApp(
            session,
            tui_settings=load_tui_settings(),
            startup_message=startup_message,
            startup_alerts=startup_alerts,
            startup_notices=all_startup_notices,
            initial_prompt=initial_prompt,
        )
        set_trust_prompt = getattr(session, "set_project_trust_prompt", None)
        if set_trust_prompt is not None:
            prompt_trust = getattr(app, "prompt_project_trust", None)
            if prompt_trust is not None:
                set_trust_prompt(prompt_trust)
        await app.run_async()
    finally:
        if session is not None:
            close_session = getattr(session, "aclose", None)
            if close_session is not None:
                await close_session()


        if (
            initial_provider is not None
            and getattr(session, "provider", None) is not initial_provider
        ):
            with suppress(Exception):
                await initial_provider.aclose()

    active_session_id: str | None = getattr(session, "session_id", None)
    if active_session_id is None or manager.get_session(active_session_id) is None:
        return None
    return active_session_id
