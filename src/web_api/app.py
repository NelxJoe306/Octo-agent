from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from pydantic import ValidationError

from web_api.runtime import WebApiError, WebRuntime
from web_api.schemas import (
    CreateRunRequest,
    CreateSessionRequest,
    ErrorResponse,
    HealthResponse,
    RunAcceptedResponse,
    RunEventEnvelope,
    RunSnapshotResponse,
    SessionActionResponse,
    SessionListResponse,
    StateResponse,
    TopicCommandRequest,
    TopicCommandResponse,
)

_MAX_WRITE_BODY_BYTES = 262144
_TERMINAL_RUN_STATUSES = frozenset({"completed", "cancelled", "failed"})
_DEFAULT_ALLOWED_ORIGINS = (
    "http://127.0.0.1:18300",
    "http://localhost:18300",
)
_DEFAULT_TRUSTED_PROXIES = ("127.0.0.1", "::1")


def _normalize_origin(raw: str) -> str | None:
    candidate = raw.strip()
    if not candidate:
        return None
    try:
        parsed = urlsplit(candidate)
    except ValueError:
        return None
    scheme = parsed.scheme.lower()
    netloc = parsed.netloc.lower()
    if scheme not in {"http", "https"} or not netloc:
        return None
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        return None
    return f"{scheme}://{netloc}"


def _error_payload(code: str, message: str) -> dict[str, Any]:
    return ErrorResponse(code=code, message=message, error=message).model_dump()


def _error_response(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=_error_payload(code, message),
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )


def _format_sse(event_name: str, data: str) -> str:
    return f"event: {event_name}\ndata: {data}\n\n"


def _resolve_static_file(static_root: Path, request_path: str) -> Path | None:
    decoded = unquote(urlsplit(request_path).path)
    if "\x00" in decoded:
        return None
    relative = decoded.lstrip("/")
    if not relative:
        candidate = static_root / "index.html"
    else:
        parts = [part for part in relative.split("/") if part]
        if any(part in {".", ".."} for part in parts):
            return None
        candidate = static_root.joinpath(*parts)
        if candidate.is_dir():
            candidate = candidate / "index.html"
        elif not candidate.is_file():
            html_candidate = static_root.joinpath(*parts[:-1], f"{parts[-1]}.html")
            if html_candidate.is_file():
                candidate = html_candidate
    try:
        resolved = candidate.resolve()
    except OSError:
        return None
    if not resolved.is_relative_to(static_root):
        return None
    if not resolved.is_file():
        return None
    return resolved


def create_app(
    runtime_or_factory: WebRuntime | Callable[[], WebRuntime],
    *,
    static_dir: Path | None = None,
    allowed_origins: Sequence[str] | None = None,
    trusted_proxies: Sequence[str] | None = None,
    heartbeat_seconds: float = 5.0,
) -> FastAPI:
    raw_origins = _DEFAULT_ALLOWED_ORIGINS if allowed_origins is None else allowed_origins
    normalized_origins = frozenset(
        item for raw in raw_origins if (item := _normalize_origin(raw)) is not None
    )
    if not normalized_origins:
        raise ValueError("At least one valid allowed origin is required")
    trusted_proxy_set = frozenset(_DEFAULT_TRUSTED_PROXIES if trusted_proxies is None else trusted_proxies)
    resolved_static_dir = static_dir.resolve() if static_dir is not None else None

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        runtime = (
            runtime_or_factory()
            if callable(runtime_or_factory) and not isinstance(runtime_or_factory, WebRuntime)
            else runtime_or_factory
        )
        app.state.runtime = runtime
        await runtime.start()
        try:
            yield
        finally:
            await runtime.aclose()

    app = FastAPI(
        title="Octo Web",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    def _runtime() -> WebRuntime:
        runtime = getattr(app.state, "runtime", None)
        if runtime is None:
            raise WebApiError(503, "runtime_not_ready", "服务尚未初始化")
        return runtime

    @app.middleware("http")
    async def security_middleware(request: Request, call_next: Callable[[Request], Any]) -> Response:
        client_host = request.client.host if request.client is not None else ""
        is_trusted_proxy = client_host in trusted_proxy_set

        effective_scheme = request.url.scheme.lower()
        effective_host = request.headers.get("host", "").strip().lower()
        if is_trusted_proxy:
            forwarded_proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
            forwarded_host = request.headers.get("x-forwarded-host", "").split(",")[0].strip().lower()
            if forwarded_proto in {"http", "https"}:
                effective_scheme = forwarded_proto
            if forwarded_host:
                effective_host = forwarded_host

        effective_origin = f"{effective_scheme}://{effective_host}" if effective_host else ""
        if effective_origin not in normalized_origins:
            return _error_response(403, "forbidden_host", "不允许访问该主机名或端口")

        origin_header = request.headers.get("origin")
        is_write = request.method.upper() in {"POST", "PUT", "PATCH", "DELETE"}
        if is_write:
            if not origin_header:
                return _error_response(403, "forbidden_origin", "写操作缺少同源 Origin 请求头")
            normalized_origin = _normalize_origin(origin_header)
            if normalized_origin is None or normalized_origin != effective_origin:
                return _error_response(403, "forbidden_origin", "跨源写请求已被拒绝")
            content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if content_type != "application/json":
                return _error_response(403, "invalid_content_type", "写接口只允许 application/json")
            content_length = request.headers.get("content-length")
            if content_length is not None:
                try:
                    if int(content_length) > _MAX_WRITE_BODY_BYTES:
                        return _error_response(413, "payload_too_large", "请求体过大")
                except ValueError:
                    return _error_response(400, "invalid_content_length", "无效的 Content-Length")
        elif origin_header is not None:
            normalized_origin = _normalize_origin(origin_header)
            if normalized_origin is None or normalized_origin != effective_origin:
                return _error_response(403, "forbidden_origin", "跨源请求已被拒绝")

        response = await call_next(request)
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        if request.url.path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    @app.exception_handler(WebApiError)
    async def handle_web_api_error(_request: Request, exc: WebApiError) -> JSONResponse:
        return _error_response(exc.status_code, exc.code, exc.message)

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(_request: Request, _exc: RequestValidationError) -> JSONResponse:
        return _error_response(422, "validation_error", "请求参数不合法")

    @app.get("/api/health", response_model=HealthResponse)
    async def get_health() -> HealthResponse:
        return _runtime().get_health()

    @app.get("/api/state", response_model=StateResponse)
    async def get_state() -> StateResponse:
        return _runtime().get_state()

    @app.get("/api/sessions", response_model=SessionListResponse)
    async def get_sessions() -> SessionListResponse:
        return _runtime().list_sessions()

    @app.post("/api/sessions", response_model=SessionActionResponse)
    async def post_sessions(request: Request) -> SessionActionResponse:
        raw = await request.body()
        if len(raw) > _MAX_WRITE_BODY_BYTES:
            raise WebApiError(413, "payload_too_large", "请求体过大")
        if not raw.strip():
            payload = CreateSessionRequest()
        else:
            try:
                payload = CreateSessionRequest.model_validate_json(raw)
            except ValidationError as exc:
                raise WebApiError(422, "validation_error", "请求参数不合法") from exc
        return await _runtime().create_session(title=payload.title)

    @app.post("/api/sessions/{session_id}/activate", response_model=SessionActionResponse)
    async def activate_session(session_id: str) -> SessionActionResponse:
        return await _runtime().activate_session(session_id)

    @app.get("/api/snapshot")
    async def get_snapshot(
        session_id: str | None = Query(default=None),
        topic_id: str | None = Query(default=None),
        before: int | None = Query(default=None),
        after: int | None = Query(default=None),
    ) -> JSONResponse:
        if before is not None and after is not None:
            raise WebApiError(422, "invalid_cursor", "Use only one cursor direction")
        effective_before = 9223372036854775807 if before is None else before
        if not 0 < effective_before <= 9223372036854775807 or (
            after is not None and not 0 <= after <= 9223372036854775807
        ):
            raise WebApiError(422, "invalid_cursor", "Invalid cursor")
        data = await _runtime().topic_snapshot(
            session_id=session_id,
            topic_id=topic_id,
            before=effective_before,
            after=after,
        )
        return JSONResponse(content=data, headers={"Cache-Control": "no-store"})

    @app.get("/api/anchor")
    async def get_anchor(
        session_id: str | None = Query(default=None),
        entry_id: str = Query(default=""),
    ) -> JSONResponse:
        data = await _runtime().locate_topic_entry(session_id=session_id, entry_id=entry_id)
        return JSONResponse(content=data, headers={"Cache-Control": "no-store"})

    @app.post("/api/topics", response_model=TopicCommandResponse)
    async def post_topics(command: TopicCommandRequest) -> TopicCommandResponse:
        return await _runtime().edit_topic(command)

    @app.get("/api/events")
    async def stream_map_events(request: Request) -> StreamingResponse:
        runtime = _runtime()
        initial_revision, queue, unsubscribe = runtime.subscribe_map_events()

        async def event_stream() -> AsyncIterator[str]:
            try:
                yield f"data: {initial_revision}\n\n"
                while not runtime.is_closing:
                    if await request.is_disconnected():
                        break
                    try:
                        revision = await asyncio.wait_for(queue.get(), timeout=heartbeat_seconds)
                    except asyncio.TimeoutError:
                        if runtime.is_closing:
                            break
                        yield ": heartbeat\n\n"
                        continue
                    if revision is None:
                        break
                    yield f"data: {revision}\n\n"
            finally:
                unsubscribe()

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/sessions/{session_id}/runs", status_code=202, response_model=RunAcceptedResponse)
    async def post_run(session_id: str, payload: CreateRunRequest) -> RunAcceptedResponse:
        return await _runtime().submit_run(session_id, payload)

    @app.get("/api/runs/{run_id}", response_model=RunSnapshotResponse)
    async def get_run(run_id: str) -> RunSnapshotResponse:
        return _runtime().get_run(run_id)

    @app.get("/api/runs/{run_id}/events")
    async def stream_run_events(run_id: str, request: Request) -> StreamingResponse:
        runtime = _runtime()
        snapshot, queue, unsubscribe = runtime.subscribe_run_events(run_id)

        async def event_stream() -> AsyncIterator[str]:
            try:
                snapshot_envelope = RunEventEnvelope(
                    run_id=snapshot.run_id,
                    session_id=snapshot.session_id,
                    topic_id=snapshot.topic_id,
                    seq=snapshot.seq,
                    kind="snapshot",
                    payload=snapshot.model_dump(mode="json"),
                )
                yield _format_sse("run_event", snapshot_envelope.model_dump_json())
                if queue is None:
                    final_envelope = RunEventEnvelope(
                        run_id=snapshot.run_id,
                        session_id=snapshot.session_id,
                        topic_id=snapshot.topic_id,
                        seq=snapshot.seq,
                        kind="run_status",
                        payload={
                            "status": snapshot.status,
                            "errorCode": snapshot.error_code,
                            "errorMessage": snapshot.error_message,
                            "finishedAt": snapshot.finished_at,
                        },
                    )
                    yield _format_sse("run_event", final_envelope.model_dump_json())
                    return

                while not runtime.is_closing:
                    if await request.is_disconnected():
                        break
                    try:
                        envelope = await asyncio.wait_for(queue.get(), timeout=heartbeat_seconds)
                    except asyncio.TimeoutError:
                        if runtime.is_closing:
                            break
                        yield ": heartbeat\n\n"
                        continue
                    if envelope is None:
                        break
                    if envelope.seq <= snapshot.seq:
                        continue
                    yield _format_sse("run_event", envelope.model_dump_json())
                    if (
                        envelope.kind == "run_status"
                        and envelope.payload.get("status") in _TERMINAL_RUN_STATUSES
                    ):
                        break
            finally:
                unsubscribe()

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/runs/{run_id}/cancel", response_model=RunSnapshotResponse)
    async def post_cancel_run(run_id: str) -> RunSnapshotResponse:
        return await _runtime().cancel_run(run_id)

    @app.api_route("/api/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
    async def unknown_api_endpoint(path: str) -> JSONResponse:
        return _error_response(404, "not_found", f"Unknown API endpoint: /api/{path}")

    @app.get("/{path:path}", include_in_schema=False)
    async def serve_static(path: str) -> Response:
        if resolved_static_dir is None:
            return _error_response(404, "static_not_configured", "静态资源目录未配置")
        target = _resolve_static_file(resolved_static_dir, "/" + path)
        if target is None:
            not_found = _resolve_static_file(resolved_static_dir, "/404.html")
            if not_found is not None:
                return FileResponse(not_found, status_code=404)
            return _error_response(404, "not_found", "页面或资源不存在")
        return FileResponse(target)

    return app
