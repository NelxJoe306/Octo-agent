from __future__ import annotations

import asyncio
import base64
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
import json
import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

import httpx
import uvicorn

from agent_core.messages import AssistantMessage
from agent_core.provider import CancellationToken
from agent_core.provider_events import (
    AssistantDoneEvent,
    AssistantMessageEvent,
    AssistantStartEvent,
    TextDeltaEvent,
)
from long_term_memory.memory_runtime import MemorySettings
from long_term_memory.models import AddFact
from model_adapters.fake import FakeProvider
from model_adapters.model_limits import DEFAULT_REQUEST_OUTPUT_TOKENS, RequestTokenBudget
from terminal_app.paths import OctoPaths
from terminal_app.resources import OctoResourcePaths
from terminal_app.session import CodingSessionConfig
from terminal_app.session_manager import SessionManager
from web_api.app import create_app
from web_api.runtime import WebRuntime, WebRuntimeConfig


class TwoStageStreamingProvider:
    def __init__(
        self,
        *,
        chunk1: str = "part 1",
        chunk2: str = " part 2",
        delta_count: int = 1,
        hold_stage1: bool = False,
    ) -> None:
        self._request_output_tokens = DEFAULT_REQUEST_OUTPUT_TOKENS
        self.chunk1 = chunk1
        self.chunk2 = chunk2
        self.delta_count = delta_count
        self.allow_stage1 = asyncio.Event()
        if not hold_stage1:
            self.allow_stage1.set()
        self.stage1_sent = asyncio.Event()
        self.release_stage2 = asyncio.Event()
        self.calls: list[str] = []

    def request_token_budget(self) -> RequestTokenBudget:
        return RequestTokenBudget(self._request_output_tokens)

    def configure_request_budget(
        self,
        *,
        context_window: int,
        safety_margin: int,
        output_override: int | None = None,
        model_max_output_tokens: int | None = None,
    ) -> RequestTokenBudget:
        self._request_output_tokens = DEFAULT_REQUEST_OUTPUT_TOKENS if output_override is None else output_override
        return self.request_token_budget()

    def stream_response(
        self,
        *,
        model: str,
        system: str,
        messages: list[object],
        tools: list[object],
        signal: CancellationToken | None = None,
        session_id: str | None = None,
    ) -> AsyncIterator[AssistantMessageEvent]:
        self.calls.append(model)

        async def iterator() -> AsyncIterator[AssistantMessageEvent]:
            await self.allow_stage1.wait()
            partial1 = AssistantMessage(content=self.chunk1, model=model, stop_reason="stop")
            yield AssistantStartEvent(partial=AssistantMessage(content="", model=model, stop_reason="stop"))
            yield TextDeltaEvent(content_index=0, delta=self.chunk1, partial=partial1)
            self.stage1_sent.set()
            await self.release_stage2.wait()
            text = self.chunk1
            for index in range(self.delta_count):
                step = self.chunk2 if self.delta_count == 1 else f" d{index}"
                text += step
                partial = AssistantMessage(content=text, model=model, stop_reason="stop")
                yield TextDeltaEvent(content_index=0, delta=step, partial=partial)
            done = AssistantMessage(content=text, model=model, stop_reason="stop")
            yield AssistantDoneEvent(reason="stop", message=done)

        return iterator()

    async def aclose(self) -> None:
        pass


class TrackedMemoryProvider(FakeProvider):
    def __init__(self, streams, log: list[str]) -> None:
        super().__init__(streams)
        self.log = log

    async def aclose(self) -> None:
        self.log.append("provider.aclose")


def _bind_loopback_socket() -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    sock.setblocking(False)
    return sock


@asynccontextmanager
async def run_uvicorn_server(app) -> AsyncIterator[tuple[str, int]]:
    sock = _bind_loopback_socket()
    host, port = sock.getsockname()
    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level="warning",
        proxy_headers=False,
        timeout_graceful_shutdown=2,
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        while not server.started:
            if task.done():
                await task
            await asyncio.sleep(0.01)
        yield f"http://{host}:{port}", port
    finally:
        server.should_exit = True
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(task, timeout=5.0)
        sock.close()


@asynccontextmanager
async def run_basic_auth_proxy(
    upstream_host: str,
    upstream_port: int,
    *,
    expected_user: str,
    expected_password: str,
    external_host: str,
) -> AsyncIterator[str]:
    expected_token = base64.b64encode(f"{expected_user}:{expected_password}".encode("utf-8")).decode("ascii")

    async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        upstream_writer: asyncio.StreamWriter | None = None
        try:
            raw_head = await reader.readuntil(b"\r\n\r\n")
            head_text = raw_head.decode("latin-1")
            lines = head_text.split("\r\n")
            request_line = lines[0]
            headers: list[tuple[str, str]] = []
            auth_header = ""
            content_length = 0
            for line in lines[1:]:
                if not line or ":" not in line:
                    continue
                name, value = line.split(":", 1)
                stripped_name = name.strip()
                stripped_value = value.strip()
                if stripped_name.lower() == "authorization":
                    auth_header = stripped_value
                elif stripped_name.lower() == "content-length":
                    content_length = int(stripped_value)
                elif stripped_name.lower() in {"x-forwarded-host", "x-forwarded-proto", "host"}:
                    continue
                headers.append((stripped_name, stripped_value))

            if auth_header != f"Basic {expected_token}":
                body = b'{"error":"unauthorized"}'
                writer.write(
                    b"HTTP/1.1 401 Unauthorized\r\n"
                    b'WWW-Authenticate: Basic realm="Octo"\r\n'
                    b"Content-Type: application/json\r\n"
                    + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode("ascii")
                    + body
                )
                await writer.drain()
                return

            body_bytes = await reader.readexactly(content_length) if content_length > 0 else b""
            upstream_reader, upstream_writer = await asyncio.open_connection(upstream_host, upstream_port)
            outgoing_lines = [
                request_line,
                f"Host: {external_host}",
                f"X-Forwarded-Host: {external_host}",
                "X-Forwarded-Proto: https",
                "Connection: close",
            ]
            for name, value in headers:
                if name.lower() == "connection":
                    continue
                outgoing_lines.append(f"{name}: {value}")
            outgoing_head = ("\r\n".join(outgoing_lines) + "\r\n\r\n").encode("latin-1")
            upstream_writer.write(outgoing_head + body_bytes)
            await upstream_writer.drain()

            while True:
                chunk = await upstream_reader.read(4096)
                if not chunk:
                    break
                writer.write(chunk)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            if upstream_writer is not None:
                upstream_writer.close()
                with suppress(Exception):
                    await upstream_writer.wait_closed()
            writer.close()
            with suppress(Exception):
                await writer.wait_closed()

    server = await asyncio.start_server(handle_client, "127.0.0.1", 0)
    _, port = server.sockets[0].getsockname()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.close()
        await server.wait_closed()


async def iter_sse_events(response: httpx.Response) -> AsyncIterator[tuple[str | None, str]]:
    event_name: str | None = None
    data_lines: list[str] = []
    async for line in response.aiter_lines():
        if line == "":
            if data_lines:
                yield event_name, "\n".join(data_lines)
            event_name = None
            data_lines = []
        elif line.startswith(":"):
            continue
        elif line.startswith("event:"):
            event_name = line[6:].strip()
        elif line.startswith("data:"):
            data_lines.append(line[5:].lstrip())


class RealHttpSseAndProxyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.home = self.base / "home"
        self.cwd = self.base / "workspace"
        self.cwd.mkdir(parents=True)
        self.static_dir = Path(__file__).resolve().parents[2] / "web" / "out"
        self.env = patch.dict(os.environ, {"OCTO_HOME": str(self.home), "OCTO_OFFLINE": "1"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def _session_config(
        self,
        provider: object,
        *,
        memory_settings: MemorySettings | None = None,
        memory_extractor=None,
    ) -> tuple[CodingSessionConfig, str]:
        paths = OctoPaths(home=self.home, agents_home=self.base / "agents")
        manager = SessionManager(paths)
        record = manager.create_session(cwd=self.cwd, model="offline", title="SSE Fixture")
        config = CodingSessionConfig(
            provider=provider,
            model="offline",
            storage=manager.storage_for(record),
            session_id=record.id,
            session_manager=manager,
            cwd=self.cwd,
            resource_paths=OctoResourcePaths(root=paths.home, agents_root=paths.agents_home, cwd=self.cwd, paths=paths),
            auto_compact_enabled=False,
            memory_settings=memory_settings or MemorySettings(enabled=False),
            memory_extractor=memory_extractor,
            extensions_enabled=False,
            skills_enabled=False,
            trust_override="approve",
            owns_initial_provider=False,
        )
        return config, record.id

    async def test_real_uvicorn_sse_disconnect_driver_continues_and_reconnect_snapshots(self) -> None:
        provider = TwoStageStreamingProvider(chunk1="hello", chunk2=" world", hold_stage1=True)
        config, session_id = self._session_config(provider)
        runtime = WebRuntime(WebRuntimeConfig(cwd=self.cwd, session_config=config, owns_initial_provider=True))

        # 先获取可用回环端口再注入 allowed_origins，真实通过 TCP 访问 Uvicorn。
        probe = _bind_loopback_socket()
        _, reserved_port = probe.getsockname()
        probe.close()
        origin = f"http://127.0.0.1:{reserved_port}"
        app = create_app(runtime, static_dir=self.static_dir, allowed_origins=(origin,), heartbeat_seconds=0.5)

        uv_config = uvicorn.Config(
            app,
            host="127.0.0.1",
            port=reserved_port,
            log_level="warning",
            proxy_headers=False,
            timeout_graceful_shutdown=2,
        )
        server = uvicorn.Server(uv_config)
        server_task = asyncio.create_task(server.serve())
        try:
            while not server.started:
                if server_task.done():
                    await server_task
                await asyncio.sleep(0.01)

            async with httpx.AsyncClient(base_url=origin, trust_env=False, timeout=5.0) as client:
                post = await client.post(
                    f"/api/sessions/{session_id}/runs",
                    headers={"Origin": origin},
                    json={"client_request_id": "sse-1", "topic_id": "main", "text": "stream please"},
                )
                self.assertEqual(post.status_code, 202)
                run_id = post.json()["run_id"]

                # 第一段输出到达后立刻断开 SSE，验证服务端 driver 不会随客户端断连而中断。
                async with client.stream("GET", f"/api/runs/{run_id}/events") as stream:
                    self.assertEqual(stream.status_code, 200)
                    seen_hello = False
                    async for event_name, raw_data in iter_sse_events(stream):
                        self.assertEqual(event_name, "run_event")
                        env = json.loads(raw_data)
                        if env["kind"] == "snapshot":
                            provider.allow_stage1.set()
                        elif env["kind"] == "agent_event" and env["payload"].get("type") == "message_update":
                            seen_hello = True
                            break
                    self.assertTrue(seen_hello)

                # 重连后先收到包含当前正文“hello”的 snapshot，放行第二段后继续收到增量与最终完成状态。
                async with client.stream("GET", f"/api/runs/{run_id}/events") as reconnected:
                    iterator = iter_sse_events(reconnected)
                    _, first_raw = await anext(iterator)
                    first_env = json.loads(first_raw)
                    self.assertEqual(first_env["kind"], "snapshot")
                    self.assertEqual(first_env["payload"]["assistant_text"], "hello")

                    provider.release_stage2.set()
                    terminal_status = None
                    async for _, raw_data in iterator:
                        env = json.loads(raw_data)
                        if env["kind"] == "run_status":
                            terminal_status = env["payload"]["status"]
                            break
                    self.assertEqual(terminal_status, "completed")

                final_run = (await client.get(f"/api/runs/{run_id}")).json()
                self.assertEqual(final_run["assistant_text"], "hello world")
                snap = (await client.get("/api/snapshot", params={"session_id": session_id})).json()
                self.assertEqual([m["text"] for m in snap["messages"]], ["stream please", "hello world"])
        finally:
            server.should_exit = True
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(server_task, timeout=5.0)

    async def test_slow_subscriber_does_not_block_driver_and_drops_cleanly(self) -> None:
        provider2 = TwoStageStreamingProvider(chunk1="start", delta_count=25)
        config2, sid2 = self._session_config(provider2)
        rt = WebRuntime(
            WebRuntimeConfig(
                cwd=self.cwd,
                session_config=config2,
                owns_initial_provider=True,
                subscriber_queue_size=2,
            )
        )
        await rt.start()
        try:
            from web_api.schemas import CreateRunRequest

            accepted = await rt.submit_run(
                sid2,
                CreateRunRequest(client_request_id="slow-1", topic_id="main", text="burst"),
            )
            _, slow_queue, unsub = rt.subscribe_run_events(accepted.run_id)
            assert slow_queue is not None
            await asyncio.wait_for(provider2.stage1_sent.wait(), timeout=2.0)
            provider2.release_stage2.set()

            # 不消费 slow_queue，driver 仍应在极短时间内完成。
            deadline = asyncio.get_running_loop().time() + 3.0
            while asyncio.get_running_loop().time() < deadline:
                if rt.get_run(accepted.run_id).status == "completed":
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(rt.get_run(accepted.run_id).status, "completed")
            # 满队列触发断开哨兵 None。
            self.assertIsNone(slow_queue.get_nowait())
            unsub()
        finally:
            await rt.aclose()

    async def test_real_reverse_proxy_basic_auth_and_live_sse_streaming(self) -> None:
        provider = TwoStageStreamingProvider(chunk1="proxied-1", chunk2=" proxied-2", hold_stage1=True)
        config, session_id = self._session_config(provider)
        runtime = WebRuntime(WebRuntimeConfig(cwd=self.cwd, session_config=config, owns_initial_provider=True))
        app = create_app(
            runtime,
            static_dir=self.static_dir,
            allowed_origins=("https://octo.example.com",),
            trusted_proxies=("127.0.0.1",),
            heartbeat_seconds=0.5,
        )

        async with run_uvicorn_server(app) as (_, upstream_port):
            async with run_basic_auth_proxy(
                "127.0.0.1",
                upstream_port,
                expected_user="octo",
                expected_password="secret-password",
                external_host="octo.example.com",
            ) as proxy_url:
                async with httpx.AsyncClient(base_url=proxy_url, trust_env=False, timeout=5.0) as anon:
                    self.assertEqual((await anon.get("/api/state")).status_code, 401)
                    self.assertEqual((await anon.get("/")).status_code, 401)

                auth = ("octo", "secret-password")
                async with httpx.AsyncClient(base_url=proxy_url, auth=auth, trust_env=False, timeout=5.0) as authed:
                    page = await authed.get("/")
                    self.assertEqual(page.status_code, 200)
                    self.assertIn("Octo", page.text)
                    self.assertNotIn("sk-", page.text)

                    cross = await authed.post(
                        f"/api/sessions/{session_id}/runs",
                        headers={"Origin": "https://attacker.example"},
                        json={"client_request_id": "x-1", "topic_id": "main", "text": "blocked"},
                    )
                    self.assertEqual(cross.status_code, 403)

                    accepted = await authed.post(
                        f"/api/sessions/{session_id}/runs",
                        headers={"Origin": "https://octo.example.com"},
                        json={"client_request_id": "p-1", "topic_id": "main", "text": "via proxy"},
                    )
                    self.assertEqual(accepted.status_code, 202)
                    run_id = accepted.json()["run_id"]

                    # 验证通过真实代理接收实时 SSE：先收到 snapshot 后放行第一段，第二段未放行前必须先收到第一段增量。
                    async with authed.stream("GET", f"/api/runs/{run_id}/events") as stream:
                        self.assertEqual(stream.status_code, 200)
                        received_first_before_stage2 = False
                        terminal = None
                        async for _, raw_data in iter_sse_events(stream):
                            env = json.loads(raw_data)
                            if env["kind"] == "snapshot":
                                provider.allow_stage1.set()
                            elif (
                                env["kind"] == "agent_event"
                                and env["payload"].get("type") == "message_update"
                                and not provider.release_stage2.is_set()
                            ):
                                received_first_before_stage2 = True
                                provider.release_stage2.set()
                            elif env["kind"] == "run_status":
                                terminal = env["payload"]["status"]
                                break
                        self.assertTrue(received_first_before_stage2)
                        self.assertEqual(terminal, "completed")

    async def test_aclose_flushes_active_memory_before_closing_provider(self) -> None:
        events_log: list[str] = []

        async def slow_extractor(job, sources, facts):
            events_log.append("memory.start")
            await asyncio.sleep(0.05)
            user_source = next(src for src in sources if src.reference.role == "user")
            events_log.append("memory.end")
            return [AddFact(text="User prefers concise logs", category="preference", source_entry_ids=(user_source.reference.entry_id,))]

        provider = TrackedMemoryProvider(
            [[AssistantDoneEvent(reason="stop", message=AssistantMessage(content="noted", model="offline"))]],
            events_log,
        )
        config, session_id = self._session_config(
            provider,
            memory_settings=MemorySettings(enabled=True, message_threshold=1, request_timeout=5, close_timeout=2),
            memory_extractor=slow_extractor,
        )
        runtime = WebRuntime(WebRuntimeConfig(cwd=self.cwd, session_config=config, owns_initial_provider=True))
        await runtime.start()

        from web_api.schemas import CreateRunRequest

        accepted = await runtime.submit_run(
            session_id,
            CreateRunRequest(client_request_id="mem-1", topic_id="main", text="记住我喜欢简洁日志"),
        )
        while runtime.get_run(accepted.run_id).status not in {"completed", "failed", "cancelled"}:
            await asyncio.sleep(0.01)

        await runtime.aclose()
        self.assertIn("memory.end", events_log)
        self.assertIn("provider.aclose", events_log)
        self.assertLess(events_log.index("memory.end"), events_log.index("provider.aclose"))


if __name__ == "__main__":
    unittest.main()
