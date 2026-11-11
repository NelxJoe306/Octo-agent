from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from inspect import isawaitable
from pathlib import Path
from time import time_ns
from typing import Literal, Protocol, cast

import httpx

import octo_coding.built_in_extensions as built_in_extension_registry
from octo_agent.events import AgentEvent, AgentStartEvent
from octo_agent.events import TurnEndEvent as AgentTurnEndEvent
from octo_agent.events import TurnStartEvent as AgentTurnStartEvent
from octo_agent.messages import AgentMessage, TextContent
from octo_agent.tools import (
    AgentTool,
    AgentToolResult,
    ToolCancellationToken,
    ToolUpdateCallback,
)
from octo_agent.types import JSONValue
from octo_coding.built_in_extensions import BuiltInExtension, BuiltInExtensionContext
from octo_coding.commands import (
    CommandContext,
    CommandRegistry,
    CommandResult,
    SlashCommand,
    create_default_command_registry,
)
from octo_coding.credentials import CredentialStore, FileCredentialStore, credentials_path
from octo_coding.extensions.api import (
    AGENT_EVENT_TYPES,
    AGENT_EVENT_WILDCARD,
    LIFECYCLE_EVENT_TYPES,
    CustomMessageView,
    ExtensionAPI,
    ExtensionCommandContext,
    ExtensionCommandHandler,
    ExtensionContext,
    ExtensionError,
    ExtensionGeneration,
    ExtensionHandler,
    InputEvent,
    InputHookResult,
    MessageRenderer,
    MessageRenderOptions,
    NullUiBridge,
    RegisteredExtension,
    SessionLifecycleReason,
    SessionShutdownEvent,
    SessionStartEvent,
    ToolCallHookEvent,
    ToolCallHookResult,
    ToolResultHookEvent,
    ToolResultHookResult,
    TurnEndEvent,
    TurnStartEvent,
    UiBridge,
)
from octo_coding.extensions.loader import (
    ExtensionSourceMetadata,
    LoadedExtension,
    load_extensions,
    unload_extension_modules,
)
from octo_coding.extensions.provider_registry import (
    DynamicProviderRegistry,
    ProviderRegistryCloseResult,
)
from octo_coding.extensions.providers import CredentialReader, DynamicProvider
from octo_coding.local_backends import LocalBackend, LocalBackendRegistry
from octo_coding.paths import OctoPaths
from octo_coding.project_trust import ExtensionTrustResult, ProjectTrustEvent
from octo_coding.provider_config import ProviderConfig
from octo_coding.resources import ResourceDiagnostic, OctoResourcePaths
from octo_coding.system_prompt import PromptSection


TurnRequestedCallback = Callable[[str, "str | None", "dict[str, JSONValue] | None"], None]


class BoundSession(Protocol):


    @property
    def cwd(self) -> Path: ...

    @property
    def model(self) -> str: ...

    @property
    def provider_name(self) -> str: ...

    @property
    def inference_provider(self) -> str | None: ...

    @property
    def inference_provider_mode(self) -> str: ...

    @property
    def session_id(self) -> str | None: ...

    @property
    def session_name(self) -> str | None: ...

    @property
    def thinking_level(self) -> str: ...

    @property
    def system_prompt(self) -> str: ...

    @property
    def is_running(self) -> bool: ...

    @property
    def messages(self) -> tuple[AgentMessage, ...]: ...

    def queue_steering_message(
        self,
        content: str,
        *,
        custom_type: str | None = None,
        details: dict[str, JSONValue] | None = None,
    ) -> None: ...

    def queue_follow_up_message(
        self,
        content: str,
        *,
        custom_type: str | None = None,
        details: dict[str, JSONValue] | None = None,
    ) -> None: ...

    async def append_custom_entry(self, namespace: str, data: dict[str, JSONValue]) -> None: ...

    async def set_label(self, target_id: str, label: str | None) -> object: ...

    def set_inference_provider(self, route: str | None) -> str: ...


@dataclass(frozen=True, slots=True)
class ExtensionCommand:


    extension: str
    source_id: str
    name: str
    description: str
    usage: str
    aliases: tuple[str, ...]
    handler: ExtensionCommandHandler


@dataclass(frozen=True, slots=True)
class RegisteredExtensionTool:


    extension: str
    source_id: str
    tool: AgentTool


@dataclass(frozen=True, slots=True)
class InputHookOutcome:


    handled: bool
    text: str
    message: str | None = None


class ExtensionRuntime:


    def __init__(
        self,
        *,
        ui: UiBridge | None = None,
        durable_providers: Sequence[ProviderConfig] = (),
        credentials: CredentialReader | None = None,
        environment: Mapping[str, str] | None = None,
        built_in_extensions: Sequence[BuiltInExtension] | None = None,
        paths: OctoPaths | None = None,
        built_in_credentials: CredentialStore | None = None,
        built_in_http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._generation = ExtensionGeneration()
        self._built_in_extensions = tuple(
            built_in_extension_registry.BUILT_IN_EXTENSIONS
            if built_in_extensions is None
            else built_in_extensions
        )
        self._built_ins_loaded = False
        self._durable_providers = tuple(durable_providers)
        self._provider_credentials = credentials
        self._provider_environment = environment
        built_in_paths = paths or OctoPaths()
        resolved_built_in_credentials: CredentialStore = (
            built_in_credentials
            if built_in_credentials is not None
            else (
                cast(CredentialStore, credentials)
                if credentials is not None
                and all(
                    callable(getattr(credentials, name, None))
                    for name in ("set", "delete", "names")
                )
                else FileCredentialStore(credentials_path(built_in_paths))
            )
        )
        self._built_in_context = BuiltInExtensionContext(
            paths=built_in_paths,
            credential_store=resolved_built_in_credentials,
            environment=dict(environment) if environment is not None else dict(os.environ),
            http_client=built_in_http_client,
        )
        self._provider_registry = DynamicProviderRegistry(
            self._durable_providers,
            generation_id=self._generation.id,
            credentials=credentials,
            environment=environment,
        )
        self._local_backend_registry = LocalBackendRegistry(
            self._provider_registry,
            generation_id=self._generation.id,
        )
        self._retired_provider_registries: list[DynamicProviderRegistry] = []
        self._retired_local_backend_registries: list[LocalBackendRegistry] = []
        self._extensions: list[RegisteredExtension] = []
        self._tools: dict[str, RegisteredExtensionTool] = {}
        self._commands: dict[str, ExtensionCommand] = {}
        self._prompt_guidelines: list[tuple[str, str, str]] = []
        self._prompt_sections: list[tuple[str, str, PromptSection]] = []
        self._message_renderers: dict[str, tuple[str, str, MessageRenderer]] = {}
        self._renderer_failures_reported: set[str] = set()
        self._load_diagnostics: list[ResourceDiagnostic] = []
        self._runtime_diagnostics: list[ResourceDiagnostic] = []


        self._paths: OctoPaths = paths or OctoPaths()
        self._session: BoundSession | None = None
        self._ui: UiBridge = ui or NullUiBridge()
        self._turn_requested: TurnRequestedCallback | None = None
        self._harness_unsubscribe: Callable[[], None] | None = None
        self._extension_turn_index = 0


    def load(
        self,
        paths: OctoResourcePaths,
        *,
        extra_paths: Sequence[Path] = (),
        include_resource_dirs: bool = True,
        include_project_dir: bool = False,
        include_user_dir: bool = True,
    ) -> None:

        self._paths = paths.paths or OctoPaths(
            home=paths.root,
            agents_home=paths.agents_root or Path.home() / ".agents",
        )
        self._load_built_ins()
        result = load_extensions(
            paths,
            extra_paths=extra_paths,
            include_resource_dirs=include_resource_dirs,
            include_project_dir=include_project_dir,
            include_user_dir=include_user_dir,
        )
        self._load_diagnostics.extend(result.diagnostics)
        for extension in result.extensions:
            self._setup_extension(extension)

    def _load_built_ins(self) -> None:

        if self._built_ins_loaded:
            return
        self._built_ins_loaded = True
        for declaration in self._built_in_extensions:
            setup = declaration.setup
            if declaration.setup_with_context is not None:

                def setup_with_runtime_context(
                    api: ExtensionAPI,
                    declaration: BuiltInExtension = declaration,
                ) -> None:
                    assert declaration.setup_with_context is not None
                    declaration.setup_with_context(api, self._built_in_context)

                setup = setup_with_runtime_context
            self._setup_extension(
                LoadedExtension(
                    name=declaration.name,
                    path=None,
                    source_id=declaration.source_id,
                    setup=setup,
                    source="built-in",
                    hidden=declaration.hidden,
                )
            )

    @property
    def active(self) -> bool:

        return self._generation.active

    def retire(self) -> None:

        if not self._generation.active:
            return


        self._local_backend_registry.retire()
        self._provider_registry.retire()
        self._generation.invalidate()
        if self._harness_unsubscribe is not None:
            self._harness_unsubscribe()
            self._harness_unsubscribe = None
        self._extensions.clear()
        self._tools.clear()
        self._commands.clear()
        self._prompt_guidelines.clear()
        self._prompt_sections.clear()
        self._message_renderers.clear()
        self._renderer_failures_reported.clear()
        self._turn_requested = None
        self._session = None

    async def aclose(self) -> ProviderRegistryCloseResult:

        self.retire()
        provider_registries = (*self._retired_provider_registries, self._provider_registry)
        backend_registries = (
            *self._retired_local_backend_registries,
            self._local_backend_registry,
        )
        try:
            _, provider_results = await asyncio.gather(
                asyncio.gather(*(registry.aclose() for registry in backend_registries)),
                asyncio.gather(*(registry.aclose() for registry in provider_registries)),
            )
            self._retired_provider_registries = [
                registry
                for registry, result in zip(provider_registries, provider_results, strict=True)
                if not result.drained and registry is not self._provider_registry
            ]
            self._retired_local_backend_registries = []
            contained = sum(result.contained_discovery_tasks for result in provider_results)
            return ProviderRegistryCloseResult(
                drained=contained == 0,
                contained_discovery_tasks=contained,
            )
        finally:
            self._generation.invalidate()
            if self._harness_unsubscribe is not None:
                self._harness_unsubscribe()
                self._harness_unsubscribe = None

    def reset_for_reload(self) -> None:


        self.clear_ui_components()
        self._local_backend_registry.retire()
        self._retired_local_backend_registries.append(self._local_backend_registry)
        self._provider_registry.retire()
        self._retired_provider_registries.append(self._provider_registry)
        self._generation.invalidate()
        self._generation = ExtensionGeneration()
        self._built_ins_loaded = False
        self._provider_registry = DynamicProviderRegistry(
            self._durable_providers,
            generation_id=self._generation.id,
            credentials=self._provider_credentials,
            environment=self._provider_environment,
        )
        self._local_backend_registry = LocalBackendRegistry(
            self._provider_registry,
            generation_id=self._generation.id,
        )
        if self._harness_unsubscribe is not None:
            self._harness_unsubscribe()
            self._harness_unsubscribe = None
        self._extensions.clear()
        self._tools.clear()
        self._commands.clear()
        self._prompt_guidelines.clear()
        self._prompt_sections.clear()
        self._message_renderers.clear()
        self._renderer_failures_reported.clear()
        self._load_diagnostics.clear()
        self._runtime_diagnostics.clear()
        unload_extension_modules()

    def _setup_extension(self, extension: LoadedExtension) -> None:
        source_id = extension.source_id
        if self._extension_by_source(source_id) is not None:
            self._load_diagnostics.append(
                ResourceDiagnostic(
                    kind="extension",
                    name=extension.name,
                    path=extension.path,
                    message="duplicate extension source ignored (first-loaded wins)",
                )
            )
            return
        api = ExtensionAPI(
            self,
            extension.name,
            self._generation,
            source_id=source_id,
        )
        registered = RegisteredExtension(
            name=extension.name,
            source_id=source_id,
            path=extension.path,
            api=api,
            source=extension.source,
            hidden=extension.hidden,
        )
        self._extensions.append(registered)
        try:
            extension.setup(api)
        except Exception as exc:
            self._extensions.remove(registered)
            self._remove_registrations(source_id)
            self._load_diagnostics.append(
                ResourceDiagnostic(
                    kind="extension",
                    name=extension.name,
                    path=extension.path,
                    message=(
                        f"built-in setup failed: {exc!r}"
                        if extension.source == "built-in"
                        else f"setup failed: {exc!r}"
                    ),
                    severity="error",
                )
            )

    def _remove_registrations(self, source_id: str) -> None:
        self._tools = {
            name: registration
            for name, registration in self._tools.items()
            if registration.source_id != source_id
        }
        self._commands = {
            name: command
            for name, command in self._commands.items()
            if command.source_id != source_id
        }
        self._prompt_guidelines = [
            (owner, extension, guideline)
            for owner, extension, guideline in self._prompt_guidelines
            if owner != source_id
        ]
        self._prompt_sections = [
            (owner, extension, section)
            for owner, extension, section in self._prompt_sections
            if owner != source_id
        ]
        self._message_renderers = {
            custom_type: registration
            for custom_type, registration in self._message_renderers.items()
            if registration[0] != source_id
        }
        self._provider_registry.unregister_source(source_id)
        self._local_backend_registry.unregister_source(source_id)


    def register_provider(self, source_id: str, provider: DynamicProvider) -> None:

        self._provider_registry.register(source_id, provider)

    def update_provider(self, source_id: str, provider: DynamicProvider) -> bool:

        return self._provider_registry.update(source_id, provider)

    def register_local_backend(self, source_id: str, backend: LocalBackend) -> None:

        self._local_backend_registry.register(source_id, backend)

    def register_tool(self, source_id: str, extension_name: str, tool: AgentTool) -> None:

        existing = self._tools.get(tool.name)
        if existing is not None:
            self._load_diagnostics.append(
                ResourceDiagnostic(
                    kind="extension",
                    name=extension_name,
                    message=(
                        f"tool `{tool.name}` already registered by extension"
                        f" `{existing.extension}`; ignoring duplicate"
                    ),
                )
            )
            return
        self._tools[tool.name] = RegisteredExtensionTool(
            extension=extension_name,
            source_id=source_id,
            tool=tool,
        )

    def register_command(
        self,
        source_id: str,
        extension_name: str,
        name: str,
        handler: ExtensionCommandHandler,
        *,
        description: str = "",
        usage: str | None = None,
        aliases: tuple[str, ...] = (),
    ) -> None:

        normalized = name.strip().removeprefix("/").lower()
        existing = self._commands.get(normalized)
        if existing is not None:
            self._load_diagnostics.append(
                ResourceDiagnostic(
                    kind="extension",
                    name=extension_name,
                    message=(
                        f"command `/{normalized}` already registered by extension"
                        f" `{existing.extension}`; ignoring duplicate"
                    ),
                )
            )
            return
        self._commands[normalized] = ExtensionCommand(
            extension=extension_name,
            source_id=source_id,
            name=normalized,
            description=description,
            usage=usage or f"/{normalized}",
            aliases=aliases,
            handler=handler,
        )

    def register_message_renderer(
        self,
        source_id: str,
        extension_name: str,
        custom_type: str,
        renderer: MessageRenderer,
    ) -> None:

        normalized = custom_type.strip()
        if not normalized:
            self._load_diagnostics.append(
                ResourceDiagnostic(
                    kind="extension",
                    name=extension_name,
                    message="empty custom_type for message renderer ignored",
                )
            )
            return
        existing = self._message_renderers.get(normalized)
        if existing is not None:
            self._load_diagnostics.append(
                ResourceDiagnostic(
                    kind="extension",
                    name=extension_name,
                    message=(
                        f"message renderer for `{normalized}` already registered by"
                        f" extension `{existing[1]}`; ignoring duplicate"
                    ),
                )
            )
            return
        self._message_renderers[normalized] = (source_id, extension_name, renderer)

    def render_custom_message(
        self,
        custom_type: str,
        content: str,
        details: Mapping[str, JSONValue] | None,
        expanded: bool,
    ) -> str | None:

        registration = self._message_renderers.get(custom_type)
        if registration is None:
            return None
        _, extension_name, renderer = registration
        view = CustomMessageView(custom_type=custom_type, content=content, details=details)
        options = MessageRenderOptions(expanded=expanded)
        try:
            markup = renderer(view, options)
        except Exception as exc:
            if custom_type not in self._renderer_failures_reported:
                self._renderer_failures_reported.add(custom_type)
                self._record_runtime_failure(extension_name, f"message_renderer:{custom_type}", exc)
            return None
        if not isinstance(markup, str):
            if custom_type not in self._renderer_failures_reported:
                self._renderer_failures_reported.add(custom_type)
                self._record_bad_result(extension_name, f"message_renderer:{custom_type}", markup)
            return None
        return markup

    def render_tool_call(
        self,
        name: str,
        arguments: Mapping[str, JSONValue],
    ) -> str | None:

        registered = self._tools.get(name)
        if registered is None or registered.tool.render_call is None:
            return None
        try:
            line = registered.tool.render_call(arguments)
        except Exception as exc:
            if name not in self._renderer_failures_reported:
                self._renderer_failures_reported.add(name)
                self._record_runtime_failure(registered.extension, f"render_call:{name}", exc)
            return None
        if line is not None and not isinstance(line, str):
            if name not in self._renderer_failures_reported:
                self._renderer_failures_reported.add(name)
                self._record_bad_result(registered.extension, f"render_call:{name}", line)
            return None
        return line

    def render_tool_result(
        self,
        tool_name: str,
        result: AgentToolResult,
        expanded: bool,
    ) -> str | None:

        registered = self._tools.get(tool_name)
        if registered is None or registered.tool.render_result is None:
            return None
        failure_key = f"render_result:{tool_name}"
        try:
            markup = registered.tool.render_result(result, expanded=expanded)
        except Exception as exc:
            if failure_key not in self._renderer_failures_reported:
                self._renderer_failures_reported.add(failure_key)
                self._record_runtime_failure(
                    registered.extension, f"render_result:{tool_name}", exc
                )
            return None
        if markup is not None and not isinstance(markup, str):
            if failure_key not in self._renderer_failures_reported:
                self._renderer_failures_reported.add(failure_key)
                self._record_bad_result(registered.extension, f"render_result:{tool_name}", markup)
            return None
        return markup

    def register_prompt_guideline(
        self, source_id: str, extension_name: str, guideline: str
    ) -> None:

        normalized = guideline.strip()
        if not normalized:
            self._load_diagnostics.append(
                ResourceDiagnostic(
                    kind="extension",
                    name=extension_name,
                    message="empty prompt guideline ignored",
                )
            )
            return
        self._prompt_guidelines.append((source_id, extension_name, normalized))

    def register_prompt_section(
        self,
        source_id: str,
        extension_name: str,
        title: str | None,
        body: str,
    ) -> None:

        normalized_body = body.strip()
        if not normalized_body:
            self._load_diagnostics.append(
                ResourceDiagnostic(
                    kind="extension",
                    name=extension_name,
                    message="empty prompt section ignored",
                )
            )
            return
        normalized_title = title.strip() if title is not None else None
        if normalized_title == "":
            normalized_title = None
        if normalized_title is not None and any(
            separator in normalized_title for separator in ("\r", "\n")
        ):
            self._load_diagnostics.append(
                ResourceDiagnostic(
                    kind="extension",
                    name=extension_name,
                    message="prompt section ignored because its title spans multiple lines",
                )
            )
            return
        self._prompt_sections.append(
            (
                source_id,
                extension_name,
                PromptSection(title=normalized_title, body=normalized_body),
            )
        )

    def subscribe(self, source_id: str, event: str, handler: ExtensionHandler) -> None:

        known = (
            event in AGENT_EVENT_TYPES
            or event in LIFECYCLE_EVENT_TYPES
            or event == AGENT_EVENT_WILDCARD
        )
        if not known:
            self._load_diagnostics.append(
                ResourceDiagnostic(
                    kind="extension",
                    name=self._extension_display_name(source_id),
                    message=f"unknown event `{event}`; handler ignored",
                )
            )
            return
        extension = self._extension_by_source(source_id)
        if extension is None:
            raise ExtensionError(f"unknown extension source: {source_id}")
        extension.handlers.setdefault(event, []).append(handler)


    def bind(self, session: BoundSession) -> None:

        self._session = session

    def attach_harness_listener(
        self,
        subscribe: Callable[[Callable[[AgentEvent], Awaitable[None] | None]], Callable[[], None]],
    ) -> None:

        if self._harness_unsubscribe is not None:
            self._harness_unsubscribe()
        self._harness_unsubscribe = subscribe(self._on_agent_event)

    @property
    def provider_credentials(self) -> CredentialReader | None:

        return self._provider_credentials

    @property
    def provider_environment(self) -> Mapping[str, str] | None:

        return self._provider_environment

    @property
    def built_in_credentials(self) -> CredentialStore:

        return self._built_in_context.credential_store

    @property
    def built_in_http_client(self) -> httpx.AsyncClient | None:

        return self._built_in_context.http_client

    def set_ui_bridge(self, ui: UiBridge) -> None:

        self._ui = ui

    def clear_ui_components(self) -> None:

        self._ui.clear_components()

    def set_turn_requested_callback(self, callback: TurnRequestedCallback | None) -> None:

        self._turn_requested = callback

    @property
    def ui(self) -> UiBridge:

        return self._ui

    @property
    def session_view(self) -> BoundSession:

        if self._session is None:
            raise ExtensionError(
                "extension API used before the session was bound; "
                "register handlers in setup() and act on events instead"
            )
        return self._session

    @property
    def provider_registry(self) -> DynamicProviderRegistry:

        return self._provider_registry

    @property
    def local_backend_registry(self) -> LocalBackendRegistry:

        return self._local_backend_registry

    @property
    def paths(self) -> OctoPaths:

        return self._paths

    @property
    def extension_names(self) -> tuple[str, ...]:

        return tuple(extension.name for extension in self._extensions if not extension.hidden)

    @property
    def extension_metadata(self) -> tuple[ExtensionSourceMetadata, ...]:

        return tuple(
            ExtensionSourceMetadata(
                name=extension.name,
                source_id=extension.source_id,
                source=extension.source,
                hidden=extension.hidden,
                path=extension.path,
            )
            for extension in self._extensions
        )

    @property
    def diagnostics(self) -> tuple[ResourceDiagnostic, ...]:

        provider_diagnostics = tuple(
            ResourceDiagnostic(
                kind="provider",
                name=diagnostic.token.source_id,
                message=(
                    f"{diagnostic.message} for provider "
                    f"`{diagnostic.token.provider_id}` ({diagnostic.reason})"
                ),
            )
            for diagnostic in self._provider_registry.diagnostics
        )
        return (
            tuple(self._load_diagnostics) + tuple(self._runtime_diagnostics) + provider_diagnostics
        )

    @property
    def extension_tools(self) -> tuple[AgentTool, ...]:

        return tuple(registration.tool for registration in self._tools.values())

    @property
    def extension_tool_sources(self) -> dict[str, str]:

        return {name: registration.extension for name, registration in self._tools.items()}

    @property
    def prompt_guidelines(self) -> tuple[str, ...]:

        return tuple(guideline for _, _, guideline in self._prompt_guidelines)

    @property
    def prompt_sections(self) -> tuple[PromptSection, ...]:

        return tuple(section for _, _, section in self._prompt_sections)

    @property
    def sourced_prompt_sections(self) -> tuple[PromptSection, ...]:

        return tuple(
            PromptSection(
                title=section.title,
                body=section.body,
                source=f"extension: {extension}",
            )
            for _owner, extension, section in self._prompt_sections
        )


    def send_user_message(self, content: str, *, deliver_as: str = "follow_up") -> None:

        self._deliver_message(content, deliver_as=deliver_as, trigger_turn=True)

    def send_custom_message(
        self,
        content: str,
        *,
        custom_type: str,
        details: dict[str, JSONValue] | None = None,
        deliver_as: str = "follow_up",
        trigger_turn: bool = True,
    ) -> None:

        self._deliver_message(
            content,
            deliver_as=deliver_as,
            trigger_turn=trigger_turn,
            custom_type=custom_type,
            details=details,
        )

    def _deliver_message(
        self,
        content: str,
        *,
        deliver_as: str,
        trigger_turn: bool,
        custom_type: str | None = None,
        details: dict[str, JSONValue] | None = None,
    ) -> None:
        session = self.session_view
        if session.is_running:
            if deliver_as == "steer":
                session.queue_steering_message(content, custom_type=custom_type, details=details)
            else:
                session.queue_follow_up_message(content, custom_type=custom_type, details=details)
            return
        if trigger_turn and self._turn_requested is not None:
            self._turn_requested(content, custom_type, details)
            return


        session.queue_follow_up_message(content, custom_type=custom_type, details=details)

    async def append_custom_entry(self, namespace: str, data: dict[str, JSONValue]) -> None:

        await self.session_view.append_custom_entry(namespace, data)

    async def set_label(self, target_id: str, label: str | None) -> None:

        await self.session_view.set_label(target_id, label)


    # 只包装工具执行器，保留工具名称与 Schema，权限钩子不会改变工具定义。
    def compose_tools(self, builtin_tools: Sequence[AgentTool]) -> list[AgentTool]:

        merged: list[AgentTool] = []
        extension_tools = dict(self._tools)
        for tool in builtin_tools:
            override = extension_tools.pop(tool.name, None)
            merged.append(override.tool if override is not None else tool)
        merged.extend(registration.tool for registration in extension_tools.values())
        return [self._wrap_tool(tool) for tool in merged]

    def _wrap_tool(self, tool: AgentTool) -> AgentTool:
        async def executor(
            tool_call_id: str,
            arguments: Mapping[str, JSONValue],
            signal: ToolCancellationToken | None = None,
            on_update: ToolUpdateCallback | None = None,
        ) -> AgentToolResult:
            call_outcome = await self._run_tool_call_hooks(tool.name, arguments)
            if call_outcome.block:
                reason = call_outcome.reason or "blocked by an extension"
                return AgentToolResult(content=[TextContent(text=f"Tool call blocked: {reason}")])
            effective_arguments = (
                call_outcome.arguments if call_outcome.arguments is not None else arguments
            )
            result = await tool.execute(
                tool_call_id,
                effective_arguments,
                signal=signal,
                on_update=on_update,
            )
            return await self._run_tool_result_hooks(tool.name, effective_arguments, result)

        return AgentTool(
            name=tool.name,
            label=tool.label,
            description=tool.description,
            parameters=tool.parameters,
            execute_fn=executor,
            prompt_snippet=tool.prompt_snippet,
            prompt_guidelines=tool.prompt_guidelines,
            prepare_arguments=tool.prepare_arguments,
            execution_mode=tool.execution_mode,
            render_call=tool.render_call,
            render_result=tool.render_result,
        )

    # 权限钩子异常时阻止调用，避免策略失败后继续执行。
    async def _run_tool_call_hooks(
        self,
        tool_name: str,
        arguments: Mapping[str, JSONValue],
    ) -> ToolCallHookResult:
        effective: Mapping[str, JSONValue] = arguments
        for owner, handler in self._handlers_for("tool_call"):
            event = ToolCallHookEvent(tool_name=tool_name, arguments=effective)
            try:
                result = await _resolve(handler(event, self._fresh_context(owner.source_id)))
            except Exception as exc:
                self._record_runtime_failure(owner.name, "tool_call", exc)
                return ToolCallHookResult(
                    block=True,
                    reason=f"extension `{owner.name}` tool_call hook failed: {exc}",
                )
            if result is None:
                continue
            if not isinstance(result, ToolCallHookResult):
                self._record_bad_result(owner.name, "tool_call", result)
                continue
            if result.block:
                return ToolCallHookResult(block=True, reason=result.reason)
            if result.arguments is not None:
                effective = result.arguments
        if effective is arguments:
            return ToolCallHookResult()
        return ToolCallHookResult(arguments=effective)

    async def _run_tool_result_hooks(
        self,
        tool_name: str,
        arguments: Mapping[str, JSONValue],
        result: AgentToolResult,
    ) -> AgentToolResult:
        current = result
        for owner, handler in self._handlers_for("tool_result"):
            event = ToolResultHookEvent(tool_name=tool_name, arguments=arguments, result=current)
            try:
                outcome = await _resolve(handler(event, self._fresh_context(owner.source_id)))
            except Exception as exc:
                self._record_runtime_failure(owner.name, "tool_result", exc)
                continue
            if outcome is None:
                continue
            if not isinstance(outcome, ToolResultHookResult):
                self._record_bad_result(owner.name, "tool_result", outcome)
                continue
            updates: dict[str, object] = {}
            if outcome.content is not None:
                updates["content"] = [TextContent(text=outcome.content)]
            if outcome.details is not None:
                updates["details"] = outcome.details
            if updates:
                current = current.model_copy(update=updates)
        return current


    def build_command_registry(self) -> CommandRegistry:

        registry = create_default_command_registry()
        for command in self._commands.values():
            slash_command = SlashCommand(
                name=command.name,
                description=command.description or f"Extension command ({command.extension}).",
                usage=command.usage,
                handler=self._command_handler(command),
                aliases=command.aliases,
                search_terms=(command.extension, "extension"),
            )
            try:
                registry.register(slash_command)
            except ValueError as exc:
                self._load_diagnostics.append(
                    ResourceDiagnostic(
                        kind="extension",
                        name=command.extension,
                        message=f"could not register command `/{command.name}`: {exc}",
                    )
                )
        return registry

    def _command_handler(
        self, command: ExtensionCommand
    ) -> Callable[[CommandContext], CommandResult]:
        def handler(context: CommandContext) -> CommandResult:
            extension_context = ExtensionCommandContext(
                name=command.name,
                args=context.args,
                api=self._api_for(command.source_id),
            )
            try:
                message = command.handler(context.args, extension_context)
            except Exception as exc:
                self._record_runtime_failure(command.extension, f"command:/{command.name}", exc)
                return CommandResult(
                    handled=True,
                    message=f"Extension command /{command.name} failed: {exc}",
                )
            return CommandResult(handled=True, message=message)

        return handler


    async def decide_project_trust(self, event: ProjectTrustEvent) -> ExtensionTrustResult | None:

        for owner, handler in self._handlers_for("project_trust"):
            try:
                result = await _resolve(handler(event, self._fresh_context(owner.source_id)))
            except Exception as exc:
                self._record_runtime_failure(owner.name, "project_trust", exc)
                continue
            if result is None:
                continue
            if not isinstance(result, ExtensionTrustResult):
                self._record_bad_result(owner.name, "project_trust", result)
                continue
            if result.decision != "defer":
                return result
        return None

    async def emit_session_start(self, reason: SessionLifecycleReason) -> None:

        await self._emit_lifecycle("session_start", SessionStartEvent(reason=reason))

    async def emit_session_shutdown(self, reason: SessionLifecycleReason) -> None:

        await self._emit_lifecycle("session_shutdown", SessionShutdownEvent(reason=reason))

    async def run_input_hooks(
        self,
        text: str,
        *,
        source: Literal["interactive", "extension"] = "interactive",
        streaming_behavior: Literal["steer", "follow_up"] | None = None,
    ) -> InputHookOutcome:

        current = text
        for owner, handler in self._handlers_for("input"):
            try:
                result = await _resolve(
                    handler(
                        InputEvent(
                            text=current,
                            source=source,
                            streaming_behavior=streaming_behavior,
                        ),
                        self._fresh_context(owner.source_id),
                    )
                )
            except Exception as exc:
                self._record_runtime_failure(owner.name, "input", exc)
                continue
            if result is None:
                continue
            if not isinstance(result, InputHookResult):
                self._record_bad_result(owner.name, "input", result)
                continue
            if result.action == "handled":
                return InputHookOutcome(handled=True, text=current, message=result.message)
            if result.action == "transform" and result.text is not None:
                current = result.text
        return InputHookOutcome(handled=False, text=current)

    async def emit_event(self, event: object) -> None:

        event_type = getattr(event, "type", None)
        if not isinstance(event_type, str):
            raise TypeError("Extension events must expose a string type")
        handlers = list(self._handlers_for(event_type))
        handlers.extend(self._handlers_for(AGENT_EVENT_WILDCARD))
        for owner, handler in handlers:
            try:
                await _resolve(handler(event, self._fresh_context(owner.source_id)))
            except Exception as exc:
                self._record_runtime_failure(owner.name, event_type, exc)

    async def _on_agent_event(self, event: AgentEvent) -> None:

        extension_event: object = event
        if isinstance(event, AgentStartEvent):
            self._extension_turn_index = 0
        elif isinstance(event, AgentTurnStartEvent):
            extension_event = TurnStartEvent(
                turn_index=self._extension_turn_index,
                timestamp=time_ns() // 1_000_000,
            )
        elif isinstance(event, AgentTurnEndEvent):
            extension_event = TurnEndEvent(
                turn_index=self._extension_turn_index,
                message=event.message,
                tool_results=list(event.tool_results),
            )
        await self.emit_event(extension_event)
        if isinstance(event, AgentTurnEndEvent):
            self._extension_turn_index += 1

    async def _emit_lifecycle(self, event_name: str, payload: object) -> None:
        for owner, handler in self._handlers_for(event_name):
            try:
                await _resolve(handler(payload, self._fresh_context(owner.source_id)))
            except Exception as exc:
                self._record_runtime_failure(owner.name, event_name, exc)


    def _handlers_for(self, event: str) -> Iterator[tuple[RegisteredExtension, ExtensionHandler]]:
        for extension in self._extensions:
            for handler in extension.handlers.get(event, ()):
                yield extension, handler

    def _extension_by_source(self, source_id: str) -> RegisteredExtension | None:
        for extension in self._extensions:
            if extension.source_id == source_id:
                return extension
        return None

    def _extension_display_name(self, source_id: str) -> str:
        extension = self._extension_by_source(source_id)
        return extension.name if extension is not None else source_id

    def _fresh_context(self, source_id: str) -> ExtensionContext:

        api = self._api_for(source_id)
        return ExtensionContext(
            self,
            api._generation,
            extension_name=self._extension_display_name(source_id),
        )

    def _api_for(self, source_id: str) -> ExtensionAPI:
        extension = self._extension_by_source(source_id)
        if extension is None:
            raise ExtensionError(f"unknown extension source: {source_id}")
        return extension.api

    def record_ui_failure(self, extension: str, context: str, exc: BaseException) -> None:

        self._runtime_diagnostics.append(
            ResourceDiagnostic(
                kind="extension",
                name=extension,
                message=f"UI component `{context}` failed: {exc!r}",
                severity="error",
            )
        )

    def _record_runtime_failure(self, extension: str, event: str, exc: Exception) -> None:
        self._runtime_diagnostics.append(
            ResourceDiagnostic(
                kind="extension",
                name=extension,
                message=f"handler for `{event}` raised: {exc!r}",
                severity="error",
            )
        )

    def _record_bad_result(self, extension: str, event: str, result: object) -> None:
        self._runtime_diagnostics.append(
            ResourceDiagnostic(
                kind="extension",
                name=extension,
                message=(
                    f"handler for `{event}` returned unsupported"
                    f" result type {type(result).__name__}; ignored"
                ),
            )
        )


async def _resolve(value: object) -> object:
    if isawaitable(value):
        return await value
    return value
