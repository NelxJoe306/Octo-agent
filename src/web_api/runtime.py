from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import asdict, dataclass, field, is_dataclass, replace
from os import environ
from pathlib import Path
import re
from time import time
from typing import Any, Literal, cast
from uuid import uuid4

from pydantic import BaseModel

from agent_core.context_budget import ContextBudgetExceeded
from agent_core.events import (
    ContextBudgetEvent,
    ContextRecoveryEndEvent,
    ContextRecoveryStartEvent,
    MessageEndEvent,
    MessageStartEvent,
    MessageUpdateEvent,
    ToolExecutionEndEvent,
    ToolExecutionStartEvent,
    ToolExecutionUpdateEvent,
)
from agent_core.messages import AssistantMessage
from agent_core.session.sqlite import SQLiteSessionDatabase, SQLiteSessionStorage
from agent_core.types import JSONValue
from terminal_app.credentials import FileCredentialStore
from terminal_app.events import CodingSessionEvent, CompactionEndEvent, CompactionStartEvent
from terminal_app.provider_config import (
    DEFAULT_MODEL,
    DEFAULT_PROVIDER_NAME,
    OpenAICompatibleProviderConfig,
    ProviderConfig,
    ProviderConfigError,
    ProviderSelection,
    ProviderSettings,
    load_provider_settings,
    provider_has_usable_credentials,
    resolve_provider_selection,
    resolve_restored_provider_selection,
    resolve_startup_thinking_level,
)
from terminal_app.provider_runtime import ClosableModelProvider, create_model_provider
from terminal_app.session import CodingSession, CodingSessionConfig
from terminal_app.session_manager import CodingSessionRecord, SessionManager
from terminal_app.session_preparation import PreparedCodingSession, prepare_coding_session
from terminal_app.shell_config import load_shell_settings
from terminal_app.thinking import ThinkingLevel
from web_api.schemas import (
    CreateRunRequest,
    HealthResponse,
    RunAcceptedResponse,
    RunEventEnvelope,
    RunSnapshotResponse,
    RunStatus,
    SessionActionResponse,
    SessionListResponse,
    SessionSummary,
    StateResponse,
    ToolExecutionState,
    TopicCommandRequest,
    TopicCommandResponse,
)

_MAX_RUN_HISTORY = 200


class WebApiError(Exception):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        self.status_code = status_code
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class WebRuntimeConfig:
    cwd: Path
    session_id: str | None = None
    provider_name: str | None = None
    model: str | None = None
    thinking_level_override: ThinkingLevel | None = None
    session_config: CodingSessionConfig | None = None
    owns_initial_provider: bool = False
    driver_shutdown_timeout: float = 5.0
    subscriber_queue_size: int = 128


@dataclass(slots=True)
class _SubscriberQueue:
    queue: asyncio.Queue[Any]
    overflowed: bool = False


@dataclass(slots=True)
class _RunRecord:
    run_id: str
    client_request_id: str
    session_id: str
    topic_id: str
    user_text: str
    status: RunStatus
    created_at: float
    updated_at: float
    seq: int = 0
    assistant_text: str = ""
    thinking_text: str = ""
    current_assistant_text: str = ""
    current_thinking_text: str = ""
    completed_assistant_texts: list[str] = field(default_factory=list)
    completed_thinking_texts: list[str] = field(default_factory=list)
    tool_executions: list[ToolExecutionState] = field(default_factory=list)
    tool_index: dict[str, int] = field(default_factory=dict)
    last_assistant: AssistantMessage | None = None
    error_code: str | None = None
    error_message: str | None = None
    budget: dict[str, JSONValue] | None = None
    recovery: dict[str, JSONValue] | None = None
    finished_at: float | None = None
    cancel_requested: bool = False
    cancel_forwarded: bool = False
    task: asyncio.Task[None] | None = None
    subscribers: list[_SubscriberQueue] = field(default_factory=list)

    def to_accepted(self) -> RunAcceptedResponse:
        return RunAcceptedResponse(
            run_id=self.run_id,
            client_request_id=self.client_request_id,
            session_id=self.session_id,
            topic_id=self.topic_id,
            status=self.status,
        )

    def to_snapshot(self) -> RunSnapshotResponse:
        return RunSnapshotResponse(
            run_id=self.run_id,
            client_request_id=self.client_request_id,
            session_id=self.session_id,
            topic_id=self.topic_id,
            status=self.status,
            seq=self.seq,
            user_text=self.user_text,
            assistant_text=self.assistant_text,
            thinking_text=self.thinking_text,
            committed_assistant_text="\n\n".join(self.completed_assistant_texts),
            committed_thinking_text="\n\n".join(self.completed_thinking_texts),
            tool_executions=[item.model_copy(deep=True) for item in self.tool_executions],
            error_code=self.error_code,
            error_message=self.error_message,
            budget=self.budget,
            recovery=self.recovery,
            created_at=self.created_at,
            updated_at=self.updated_at,
            finished_at=self.finished_at,
        )

    def to_status_payload(self) -> dict[str, JSONValue]:
        return {
            "status": self.status,
            "errorCode": self.error_code,
            "errorMessage": self.error_message,
            "finishedAt": self.finished_at,
        }


def serialize_wire_value(value: object) -> JSONValue:
    if isinstance(value, BaseModel):
        return cast(JSONValue, value.model_dump(mode="json", by_alias=True))
    if is_dataclass(value) and not isinstance(value, type):
        return cast(JSONValue, serialize_wire_value(asdict(value)))
    if isinstance(value, Mapping):
        return cast(
            JSONValue,
            {str(key): serialize_wire_value(item) for key, item in value.items()},
        )
    if isinstance(value, (list, tuple)):
        return cast(JSONValue, [serialize_wire_value(item) for item in value])
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if hasattr(value, "model_dump"):
        return cast(JSONValue, value.model_dump(mode="json", by_alias=True))
    return str(value)


def _sanitize_error_message(exc: BaseException) -> str:
    text = str(exc).strip() or type(exc).__name__
    # 过滤可能包含凭据或内部路径的异常细节，保留可读错误描述。
    text = re.sub(r"(?i)(sk-[A-Za-z0-9_-]{8,}|bearer\s+\S+)", "[redacted]", text)
    return text[:400]


def _model_display_name(session: CodingSession) -> str:
    config = session.provider_config(session.provider_name)
    if config is not None:
        metadata = config.model_metadata.get(session.model)
        if metadata is not None and metadata.name:
            return metadata.name
    return session.model


def _resolve_startup_selection(
    settings: ProviderSettings,
    *,
    record: CodingSessionRecord | None,
    provider_name: str | None,
    model: str | None,
    explicit_resume: bool,
) -> ProviderSelection:
    if provider_name is not None or model is not None:
        return resolve_provider_selection(settings, provider_name=provider_name, model=model)
    if record is not None and (explicit_resume or record.model):
        if record.provider_name:
            try:
                return resolve_restored_provider_selection(
                    settings,
                    provider_name=record.provider_name,
                    model=record.model,
                )
            except ProviderConfigError:
                pass
        credentials = FileCredentialStore()
        for item in settings.providers:
            if record.model in item.models and provider_has_usable_credentials(item, credential_reader=credentials):
                return ProviderSelection(provider=item, model=record.model)
    default_selection = resolve_provider_selection(settings)
    credentials = FileCredentialStore()
    if provider_has_usable_credentials(default_selection.provider, credential_reader=credentials):
        return default_selection
    for item in settings.providers:
        if provider_has_usable_credentials(item, credential_reader=credentials):
            return ProviderSelection(provider=item, model=item.default_model)
    return default_selection


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


async def _close_provider_safely(provider: object | None) -> None:
    if provider is None:
        return
    aclose = getattr(provider, "aclose", None)
    if callable(aclose):
        result = aclose()
        if isinstance(result, Awaitable):
            await result


class WebRuntime:
    def __init__(self, config: WebRuntimeConfig) -> None:
        self._config = config
        self._lock = asyncio.Lock()
        self._session: CodingSession | None = None
        self._factory_provider: object | None = None
        self._unsubscribe_topics: Callable[[], None] | None = None
        self._active_run: _RunRecord | None = None
        self._session_op_in_progress = False
        self._runs_by_id: dict[str, _RunRecord] = {}
        self._runs_by_client_id: dict[str, _RunRecord] = {}
        self._run_order: list[str] = []
        self._revision = 1
        self._map_subscribers: list[_SubscriberQueue] = []
        self._last_notice: str | None = None
        self._closing = False
        self._started = False

    @property
    def revision(self) -> int:
        return self._revision

    @property
    def is_closing(self) -> bool:
        return self._closing

    def _require_session(self) -> CodingSession:
        if self._session is None:
            raise WebApiError(503, "runtime_not_ready", "服务尚未完成会话加载")
        return self._session

    async def start(self) -> None:
        if self._started:
            return
        prepared: PreparedCodingSession | None = None
        owned_provider: object | None = None
        try:
            if self._config.session_config is not None:
                session_config = self._config.session_config
                if self._config.owns_initial_provider:
                    owned_provider = session_config.provider
                prepared = await prepare_coding_session(session_config)
                if session_config.owns_initial_provider:
                    owned_provider = None
            else:
                session_config, owned_provider = self._build_production_session_config()
                prepared = await prepare_coding_session(session_config)
                owned_provider = None

            session = await prepared.adopt()
            await session.emit_pending_session_start()
            session.ensure_session_indexed()
            self._session = session
            self._factory_provider = owned_provider
            self._unsubscribe_topics = session.subscribe_topics(self._on_topics_changed)
            self._last_notice = self._extract_model_notice(session)
            self._started = True
        except BaseException:
            if prepared is not None:
                with suppress(Exception):
                    await prepared.abort()
            await _close_provider_safely(owned_provider)
            raise

    def _build_production_session_config(self) -> tuple[CodingSessionConfig, ClosableModelProvider]:
        settings = load_provider_settings()
        shell_settings = load_shell_settings()
        manager = SessionManager()
        cwd = self._config.cwd.resolve()
        record: CodingSessionRecord | None = None
        if self._config.session_id is not None:
            record = manager.get_session(self._config.session_id)
            if record is None:
                raise ValueError(f"Unknown session: {self._config.session_id}")
        else:
            record = manager.latest_session_for_cwd(cwd)

        selection = _resolve_startup_selection(
            settings,
            record=record,
            provider_name=self._config.provider_name,
            model=self._config.model,
            explicit_resume=self._config.session_id is not None,
        )
        inference_provider = _startup_inference_provider(selection, record)
        inference_provider_mode = _startup_inference_provider_mode(selection, record)
        provider = create_model_provider(
            selection.provider,
            model=selection.model,
            inference_provider=inference_provider,
            thinking_level=resolve_startup_thinking_level(
                selection.provider,
                selection.model,
                cli_override=self._config.thinking_level_override,
            ),
        )
        index_on_first_persist = False
        if record is None:
            record = manager.prepare_session(
                cwd=cwd,
                model=selection.model,
                provider_name=selection.provider.name,
                inference_provider=inference_provider,
                inference_provider_mode=inference_provider_mode,
            )
            index_on_first_persist = manager.get_session(record.id) is None

        explicit_selection = self._config.provider_name is not None or self._config.model is not None
        session_config = CodingSessionConfig(
            provider=provider,
            model=selection.model,
            cwd=record.cwd,
            storage=manager.storage_for(record),
            session_id=record.id,
            session_manager=manager,
            provider_name=selection.provider.name,
            inference_provider=inference_provider,
            inference_provider_mode=inference_provider_mode,
            requested_provider=self._config.provider_name if explicit_selection else None,
            requested_model=self._config.model if explicit_selection else None,
            session_provider_name=record.provider_name,
            provider_settings=settings,
            runtime_provider_config=selection.provider,
            index_on_first_persist=index_on_first_persist,
            shell_command_prefix=shell_settings.shell_command_prefix,
            thinking_level_override=self._config.thinking_level_override,
            trust_default=shell_settings.default_project_trust,
            defer_authoritative_writes=True,
            owns_initial_provider=True,
        )
        return session_config, provider

    def _extract_model_notice(self, session: CodingSession) -> str | None:
        return next(
            (item.message for item in session.resource_diagnostics if item.kind == "session-model"),
            None,
        )

    def _on_topics_changed(self) -> None:
        self._revision += 1
        self._broadcast_map_revision()

    def _broadcast_map_revision(self) -> None:
        for subscriber in tuple(self._map_subscribers):
            if subscriber.overflowed:
                continue
            try:
                subscriber.queue.put_nowait(self._revision)
            except asyncio.QueueFull:
                subscriber.overflowed = True
                with suppress(ValueError):
                    self._map_subscribers.remove(subscriber)
                self._drain_and_close_queue(subscriber.queue)

    @staticmethod
    def _drain_and_close_queue(queue: asyncio.Queue[Any]) -> None:
        while not queue.empty():
            with suppress(asyncio.QueueEmpty):
                queue.get_nowait()
        with suppress(asyncio.QueueFull):
            queue.put_nowait(None)

    def _session_exists(self, session_id: str) -> bool:
        session = self._require_session()
        manager = session.session_manager
        if manager is not None:
            return manager.get_session(session_id) is not None
        storage = session.storage
        if isinstance(storage, SQLiteSessionStorage):
            records = SQLiteSessionDatabase(storage.path).list_records()
            return any(item.get("id") == session_id for item in records)
        return session.session_id == session_id

    def _ensure_active_session_id(self, session_id: str) -> None:
        session = self._require_session()
        if session_id == session.session_id:
            return
        if self._session_exists(session_id):
            raise WebApiError(409, "session_not_active", "当前活动会话已切换，请刷新页面")
        raise WebApiError(404, "session_not_found", "找不到指定会话")

    def get_health(self) -> HealthResponse:
        session_id = self._session.session_id if self._session is not None else None
        busy = self._active_run is not None or bool(self._session and self._session.is_running)
        return HealthResponse(ok=not self._closing and self._started, session_id=session_id, busy=busy)

    def get_state(self) -> StateResponse:
        session = self._require_session()
        active = self._active_run
        return StateResponse(
            session_id=session.session_id or "",
            session_title=session.session_title or "未命名会话",
            current_topic_id=session.current_topic_id,
            active_run_id=active.run_id if active is not None else None,
            active_run_topic_id=active.topic_id if active is not None else None,
            active_run_status=active.status if active is not None else None,
            running=(active is not None) or session.is_running,
            model=session.model,
            provider_name=session.provider_name,
            model_display_name=_model_display_name(session),
            notice=self._last_notice,
        )

    def list_sessions(self) -> SessionListResponse:
        session = self._require_session()
        manager = session.session_manager
        active_id = session.session_id or ""
        if manager is None:
            return SessionListResponse(active_session_id=active_id, sessions=[])
        records = manager.list_sessions()
        summaries = [
            SessionSummary(
                id=record.id,
                title=record.title or "未命名会话",
                model=record.model,
                provider_name=record.provider_name,
                created_at=record.created_at,
                updated_at=record.updated_at,
                is_active=record.id == active_id,
            )
            for record in records
        ]
        return SessionListResponse(active_session_id=active_id, sessions=summaries)

    async def create_session(self, *, title: str | None = None) -> SessionActionResponse:
        session = self._require_session()
        async with self._lock:
            if self._closing:
                raise WebApiError(503, "server_shutting_down", "服务正在关闭")
            if self._active_run is not None or self._session_op_in_progress or session.is_running:
                raise WebApiError(409, "agent_busy", "当前会话正在运行中，请先停止生成再新建会话")
            self._session_op_in_progress = True
        try:
            await session.new_session()
            session.ensure_session_indexed()
            if title is not None and title.strip():
                await session.set_session_name(title)
            self._last_notice = self._extract_model_notice(session)
            self._on_topics_changed()
            return SessionActionResponse(state=self.get_state(), notice=self._last_notice)
        except ValueError as exc:
            raise WebApiError(422, "invalid_request", str(exc)) from exc
        except RuntimeError as exc:
            raise WebApiError(409, "agent_busy", str(exc)) from exc
        finally:
            async with self._lock:
                self._session_op_in_progress = False

    async def activate_session(self, session_id: str) -> SessionActionResponse:
        session = self._require_session()
        async with self._lock:
            if self._closing:
                raise WebApiError(503, "server_shutting_down", "服务正在关闭")
            if not self._session_exists(session_id):
                raise WebApiError(404, "session_not_found", f"找不到会话: {session_id}")
            if session_id == session.session_id:
                return SessionActionResponse(state=self.get_state(), notice=self._last_notice)
            if self._active_run is not None or self._session_op_in_progress or session.is_running:
                raise WebApiError(409, "agent_busy", "当前会话正在运行中，请先停止生成再切换会话")
            self._session_op_in_progress = True
        try:
            await session.resume(session_id)
            self._last_notice = self._extract_model_notice(session)
            self._on_topics_changed()
            return SessionActionResponse(state=self.get_state(), notice=self._last_notice)
        except ValueError as exc:
            message = str(exc)
            if message.startswith("Unknown session:"):
                raise WebApiError(404, "session_not_found", message) from exc
            raise WebApiError(400, "session_activate_failed", message) from exc
        except RuntimeError as exc:
            raise WebApiError(409, "agent_busy", str(exc)) from exc
        finally:
            async with self._lock:
                self._session_op_in_progress = False

    async def topic_snapshot(
        self,
        *,
        session_id: str | None = None,
        topic_id: str | None = None,
        before: int = 9223372036854775807,
        after: int | None = None,
    ) -> dict[str, Any]:
        session = self._require_session()
        if session_id is not None:
            self._ensure_active_session_id(session_id)
        try:
            snapshot = await session.topic_snapshot(topic_id, before, after=after)
        except ValueError as exc:
            message = str(exc)
            if message == "Unknown topic":
                raise WebApiError(404, "topic_not_found", message) from exc
            if message.startswith("Session changed"):
                raise WebApiError(409, "session_not_active", message) from exc
            raise WebApiError(400, "invalid_snapshot_query", message) from exc
        active = self._active_run
        if active is not None and active.session_id == snapshot.get("session_id"):
            snapshot["running"] = True
            if snapshot.get("active_topic_id") is None:
                snapshot["active_topic_id"] = active.topic_id
        return snapshot

    async def locate_topic_entry(
        self,
        *,
        session_id: str | None = None,
        entry_id: str = "",
    ) -> dict[str, Any]:
        session = self._require_session()
        if session_id is not None:
            self._ensure_active_session_id(session_id)
        if not entry_id:
            raise WebApiError(422, "invalid_request", "entry_id 不能为空")
        try:
            return await session.locate_topic_entry(entry_id)
        except ValueError as exc:
            message = str(exc)
            if message.startswith("Session changed"):
                raise WebApiError(409, "session_not_active", message) from exc
            raise WebApiError(404, "entry_not_found", message) from exc

    async def edit_topic(self, command: TopicCommandRequest) -> TopicCommandResponse:
        session = self._require_session()
        self._ensure_active_session_id(command.session_id)
        try:
            topic_id = await session.edit_topic(**command.model_dump())
            return TopicCommandResponse(topic_id=topic_id, session_id=session.session_id or command.session_id)
        except ValueError as exc:
            message = str(exc)
            if message.startswith("Session changed"):
                raise WebApiError(409, "session_not_active", message) from exc
            if message in {"Unknown topic", "Unknown parent topic"}:
                raise WebApiError(404, "topic_not_found", message) from exc
            raise WebApiError(422, "invalid_topic_command", message) from exc

    async def submit_run(self, session_id: str, request: CreateRunRequest) -> RunAcceptedResponse:
        session = self._require_session()
        client_request_id = request.client_request_id.strip()
        if not client_request_id:
            raise WebApiError(422, "invalid_request", "client_request_id 不能为空")
        if not request.text.strip():
            raise WebApiError(422, "invalid_request", "消息内容不能为空")

        run: _RunRecord | None = None
        async with self._lock:
            if self._closing:
                raise WebApiError(503, "server_shutting_down", "服务正在关闭")
            existing = self._runs_by_client_id.get(client_request_id)
            if existing is not None:
                if (
                    existing.session_id == session_id
                    and existing.topic_id == request.topic_id
                    and existing.user_text == request.text
                ):
                    return existing.to_accepted()
                raise WebApiError(409, "client_request_conflict", "相同的 client_request_id 不能用于其他请求")
            self._ensure_active_session_id(session_id)
            if self._active_run is not None or self._session_op_in_progress or session.is_running:
                raise WebApiError(409, "agent_busy", "当前会话正在生成回复，请等待完成或先停止")
            now = time()
            run = _RunRecord(
                run_id=uuid4().hex,
                client_request_id=client_request_id,
                session_id=session_id,
                topic_id=request.topic_id,
                user_text=request.text,
                status="starting",
                created_at=now,
                updated_at=now,
            )
            # 先占用前台运行位再校验话题，避免并发 POST 穿透忙碌检查。
            self._active_run = run

        try:
            await session.topic_snapshot(request.topic_id)
        except ValueError as exc:
            async with self._lock:
                if self._active_run is run:
                    self._active_run = None
            message = str(exc)
            if message == "Unknown topic":
                raise WebApiError(404, "topic_not_found", message) from exc
            raise WebApiError(400, "invalid_topic", message) from exc
        except BaseException:
            async with self._lock:
                if self._active_run is run:
                    self._active_run = None
            raise

        async with self._lock:
            if self._closing:
                if self._active_run is run:
                    self._active_run = None
                raise WebApiError(503, "server_shutting_down", "服务正在关闭")
            run.status = "running"
            run.updated_at = time()
            self._register_run(run)
            run.task = asyncio.create_task(self._drive_run(run), name=f"octo-web-run-{run.run_id}")
        self._on_topics_changed()
        return run.to_accepted()

    def _register_run(self, run: _RunRecord) -> None:
        self._runs_by_id[run.run_id] = run
        self._runs_by_client_id[run.client_request_id] = run
        self._run_order.append(run.run_id)
        while len(self._run_order) > _MAX_RUN_HISTORY:
            oldest_id = self._run_order[0]
            oldest = self._runs_by_id.get(oldest_id)
            if oldest is self._active_run:
                break
            self._run_order.pop(0)
            self._runs_by_id.pop(oldest_id, None)
            if oldest is not None:
                self._runs_by_client_id.pop(oldest.client_request_id, None)

    def get_run(self, run_id: str) -> RunSnapshotResponse:
        run = self._runs_by_id.get(run_id)
        if run is None:
            raise WebApiError(404, "run_not_retained", "运行记录不存在或服务已重启")
        return run.to_snapshot()

    async def cancel_run(self, run_id: str) -> RunSnapshotResponse:
        session = self._require_session()
        run = self._runs_by_id.get(run_id)
        if run is None:
            raise WebApiError(404, "run_not_retained", "运行记录不存在或服务已重启")

        task_to_wait: asyncio.Task[None] | None = None
        async with self._lock:
            if self._active_run is not run or run.status in {"completed", "cancelled", "failed"}:
                return run.to_snapshot()
            run.cancel_requested = True
            if run.status != "cancelling":
                run.status = "cancelling"
                run.updated_at = time()
                run.seq += 1
                self._broadcast_run_envelope(
                    run,
                    RunEventEnvelope(
                        run_id=run.run_id,
                        session_id=run.session_id,
                        topic_id=run.topic_id,
                        seq=run.seq,
                        kind="run_status",
                        payload=run.to_status_payload(),
                    ),
                )
            if session.is_running:
                run.cancel_forwarded = True
                session.cancel()
            task_to_wait = run.task

        if task_to_wait is not None and not task_to_wait.done():
            with suppress(asyncio.TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(
                    asyncio.shield(task_to_wait),
                    timeout=min(2.0, self._config.driver_shutdown_timeout),
                )
        return run.to_snapshot()

    def subscribe_map_events(self) -> tuple[int, asyncio.Queue[int | None], Callable[[], None]]:
        if self._closing:
            raise WebApiError(503, "server_shutting_down", "服务正在关闭")
        subscriber = _SubscriberQueue(queue=asyncio.Queue(maxsize=self._config.subscriber_queue_size))
        self._map_subscribers.append(subscriber)

        def unsubscribe() -> None:
            with suppress(ValueError):
                self._map_subscribers.remove(subscriber)

        return self._revision, subscriber.queue, unsubscribe

    def subscribe_run_events(
        self,
        run_id: str,
    ) -> tuple[RunSnapshotResponse, asyncio.Queue[RunEventEnvelope | None] | None, Callable[[], None]]:
        run = self._runs_by_id.get(run_id)
        if run is None:
            raise WebApiError(404, "run_not_retained", "运行记录不存在或服务已重启")
        snapshot = run.to_snapshot()
        if run.status in {"completed", "cancelled", "failed"} or self._closing:
            return snapshot, None, lambda: None
        subscriber = _SubscriberQueue(queue=asyncio.Queue(maxsize=self._config.subscriber_queue_size))
        run.subscribers.append(subscriber)

        def unsubscribe() -> None:
            with suppress(ValueError):
                run.subscribers.remove(subscriber)

        return snapshot, subscriber.queue, unsubscribe

    async def _drive_run(self, run: _RunRecord) -> None:
        session = self._require_session()
        try:
            async for event in session.prompt(run.user_text, topic_id=run.topic_id):
                if run.cancel_requested and not run.cancel_forwarded and session.is_running:
                    run.cancel_forwarded = True
                    session.cancel()
                self._apply_event_to_run(run, event)
                run.seq += 1
                run.updated_at = time()
                envelope = RunEventEnvelope(
                    run_id=run.run_id,
                    session_id=run.session_id,
                    topic_id=run.topic_id,
                    seq=run.seq,
                    kind="agent_event",
                    payload=cast(dict[str, JSONValue], serialize_wire_value(event)),
                )
                self._broadcast_run_envelope(run, envelope)

            if (
                run.cancel_requested
                or run.status == "cancelling"
                or (run.last_assistant is not None and run.last_assistant.stop_reason == "aborted")
            ):
                run.status = "cancelled"
                run.error_code = "cancelled"
                run.error_message = run.error_message or "生成已停止"
            elif run.last_assistant is not None and run.last_assistant.stop_reason == "error":
                run.status = "failed"
                run.error_code = "provider_error"
                run.error_message = run.last_assistant.error_message or "模型调用失败"
            else:
                run.status = "completed"
        except ContextBudgetExceeded as exc:
            run.status = "failed"
            run.error_code = "context_budget_exceeded"
            run.error_message = str(exc)
        except asyncio.CancelledError:
            run.status = "cancelled"
            run.error_code = "cancelled"
            run.error_message = "生成已停止"
        except Exception as exc:
            run.status = "failed"
            run.error_code = "execution_error"
            run.error_message = _sanitize_error_message(exc)
        finally:
            run.finished_at = time()
            run.updated_at = run.finished_at
            async with self._lock:
                if self._active_run is run:
                    self._active_run = None
            run.seq += 1
            self._broadcast_run_envelope(
                run,
                RunEventEnvelope(
                    run_id=run.run_id,
                    session_id=run.session_id,
                    topic_id=run.topic_id,
                    seq=run.seq,
                    kind="run_status",
                    payload=run.to_status_payload(),
                ),
            )
            self._close_run_subscribers(run)
            self._on_topics_changed()

    def _apply_event_to_run(self, run: _RunRecord, event: CodingSessionEvent) -> None:
        if isinstance(event, MessageStartEvent) and isinstance(event.message, AssistantMessage):
            run.current_assistant_text = event.message.text
            run.current_thinking_text = event.message.thinking_text
            self._refresh_run_text(run)
        elif isinstance(event, MessageUpdateEvent) and isinstance(event.message, AssistantMessage):
            run.current_assistant_text = event.message.text
            run.current_thinking_text = event.message.thinking_text
            self._refresh_run_text(run)
        elif isinstance(event, MessageEndEvent) and isinstance(event.message, AssistantMessage):
            run.last_assistant = event.message
            if event.message.text:
                run.completed_assistant_texts.append(event.message.text)
            if event.message.thinking_text:
                run.completed_thinking_texts.append(event.message.thinking_text)
            run.current_assistant_text = ""
            run.current_thinking_text = ""
            self._refresh_run_text(run)
        elif isinstance(event, ToolExecutionStartEvent):
            self._upsert_tool_execution(
                run,
                ToolExecutionState(
                    tool_call_id=event.tool_call_id,
                    tool_name=event.tool_name,
                    status="running",
                    args=dict(event.args),
                ),
            )
        elif isinstance(event, ToolExecutionUpdateEvent):
            idx = run.tool_index.get(event.tool_call_id)
            if idx is not None:
                run.tool_executions[idx].partial_text = event.partial_result.text
        elif isinstance(event, ToolExecutionEndEvent):
            idx = run.tool_index.get(event.tool_call_id)
            state = ToolExecutionState(
                tool_call_id=event.tool_call_id,
                tool_name=event.tool_name,
                status="error" if event.is_error else "completed",
                args=run.tool_executions[idx].args if idx is not None else {},
                partial_text="",
                result_text=event.result.text,
                is_error=event.is_error,
            )
            self._upsert_tool_execution(run, state)
        elif isinstance(event, ContextBudgetEvent):
            run.budget = cast(dict[str, JSONValue], serialize_wire_value(event.decision))
        elif isinstance(
            event,
            (ContextRecoveryStartEvent, ContextRecoveryEndEvent, CompactionStartEvent, CompactionEndEvent),
        ):
            run.recovery = cast(dict[str, JSONValue], serialize_wire_value(event))

    @staticmethod
    def _refresh_run_text(run: _RunRecord) -> None:
        assistant_parts = [*run.completed_assistant_texts]
        if run.current_assistant_text:
            assistant_parts.append(run.current_assistant_text)
        run.assistant_text = "\n\n".join(assistant_parts)

        thinking_parts = [*run.completed_thinking_texts]
        if run.current_thinking_text:
            thinking_parts.append(run.current_thinking_text)
        run.thinking_text = "\n\n".join(thinking_parts)

    @staticmethod
    def _upsert_tool_execution(run: _RunRecord, state: ToolExecutionState) -> None:
        idx = run.tool_index.get(state.tool_call_id)
        if idx is None:
            run.tool_index[state.tool_call_id] = len(run.tool_executions)
            run.tool_executions.append(state)
        else:
            run.tool_executions[idx] = state

    def _broadcast_run_envelope(self, run: _RunRecord, envelope: RunEventEnvelope) -> None:
        for subscriber in tuple(run.subscribers):
            if subscriber.overflowed:
                continue
            try:
                subscriber.queue.put_nowait(envelope)
            except asyncio.QueueFull:
                subscriber.overflowed = True
                with suppress(ValueError):
                    run.subscribers.remove(subscriber)
                self._drain_and_close_queue(subscriber.queue)

    def _close_run_subscribers(self, run: _RunRecord, *, drain: bool = False) -> None:
        for subscriber in tuple(run.subscribers):
            if drain:
                self._drain_and_close_queue(subscriber.queue)
            else:
                with suppress(asyncio.QueueFull):
                    subscriber.queue.put_nowait(None)
        run.subscribers.clear()

    async def aclose(self) -> None:
        async with self._lock:
            if self._closing:
                return
            self._closing = True
            active = self._active_run
            if active is not None:
                active.cancel_requested = True
                if active.status not in {"completed", "cancelled", "failed"}:
                    active.status = "cancelling"

        if active is not None and active.task is not None and not active.task.done():
            if self._session is not None and self._session.is_running:
                self._session.cancel()
            try:
                await asyncio.wait_for(
                    asyncio.shield(active.task),
                    timeout=self._config.driver_shutdown_timeout,
                )
            except asyncio.TimeoutError:
                active.task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await active.task

        for subscriber in tuple(self._map_subscribers):
            self._drain_and_close_queue(subscriber.queue)
        self._map_subscribers.clear()

        for run in self._runs_by_id.values():
            self._close_run_subscribers(run, drain=True)

        if self._unsubscribe_topics is not None:
            self._unsubscribe_topics()
            self._unsubscribe_topics = None

        session_error: BaseException | None = None
        if self._session is not None:
            try:
                await self._session.aclose()
            except BaseException as exc:
                session_error = exc

        try:
            await _close_provider_safely(self._factory_provider)
        except BaseException as exc:
            if session_error is None:
                session_error = exc
        finally:
            self._factory_provider = None

        if session_error is not None:
            raise session_error
