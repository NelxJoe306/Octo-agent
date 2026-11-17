from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

import httpx
from typer.testing import CliRunner

from agent_core.messages import AssistantMessage, ToolCall, UserMessage
from agent_core.provider import CancellationToken
from agent_core.provider_events import (
    AssistantDoneEvent,
    AssistantErrorEvent,
    AssistantMessageEvent,
    AssistantStartEvent,
    TextDeltaEvent,
)
from agent_core.session import MessageEntry, ModelChangeEntry, SessionInfoEntry
from agent_core.tools import AgentTool, AgentToolResult
from long_term_memory.memory_runtime import MemorySettings
from model_adapters.fake import FakeProvider
from model_adapters.model_limits import DEFAULT_REQUEST_OUTPUT_TOKENS, RequestTokenBudget
from terminal_app.paths import OctoPaths
from terminal_app.provider_config import OpenAICompatibleProviderConfig, ProviderSettings
from terminal_app.resources import OctoResourcePaths
from terminal_app.session import CodingSessionConfig
from terminal_app.session_manager import SessionManager
from web_api.app import create_app
from web_api.cli import cli_app
from web_api.runtime import WebRuntime, WebRuntimeConfig


def assistant_stream(content: object, reason: str = "stop") -> list[AssistantMessageEvent]:
    message = AssistantMessage(content=content, model="offline", stop_reason=reason)
    events: list[AssistantMessageEvent] = [AssistantStartEvent(partial=message)]
    if isinstance(content, str):
        events.append(TextDeltaEvent(content_index=0, delta=content, partial=message))
    if reason in {"error", "aborted"}:
        events.append(AssistantErrorEvent(reason=reason, error=message))
    else:
        events.append(AssistantDoneEvent(reason=reason, message=message))
    return events


class TrackedProvider(FakeProvider):
    def __init__(self, streams: Iterable[Iterable[AssistantMessageEvent]], events_log: list[str] | None = None) -> None:
        super().__init__(streams)
        self.events_log = events_log if events_log is not None else []
        self.closed_count = 0

    async def aclose(self) -> None:
        self.closed_count += 1
        self.events_log.append("provider.aclose")


class GatedProvider:
    def __init__(self, *, answer: str = "gated answer", events_log: list[str] | None = None) -> None:
        self._request_output_tokens = DEFAULT_REQUEST_OUTPUT_TOKENS
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.answer = answer
        self.calls: list[tuple[str, str]] = []
        self.events_log = events_log if events_log is not None else []
        self.closed_count = 0

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
        self.calls.append((model, system))

        async def iterator() -> AsyncIterator[AssistantMessageEvent]:
            partial = AssistantMessage(content="", model=model, stop_reason="stop")
            yield AssistantStartEvent(partial=partial)
            self.started.set()
            while not self.release.is_set():
                if signal is not None and signal.is_cancelled():
                    aborted = AssistantMessage(
                        content=[],
                        model=model,
                        stop_reason="aborted",
                        error_message="Aborted by user",
                    )
                    yield AssistantErrorEvent(reason="aborted", error=aborted)
                    return
                await asyncio.sleep(0.01)
            if signal is not None and signal.is_cancelled():
                aborted = AssistantMessage(
                    content=[],
                    model=model,
                    stop_reason="aborted",
                    error_message="Aborted by user",
                )
                yield AssistantErrorEvent(reason="aborted", error=aborted)
                return
            done = AssistantMessage(content=self.answer, model=model, stop_reason="stop")
            yield TextDeltaEvent(content_index=0, delta=self.answer, partial=done)
            yield AssistantDoneEvent(reason="stop", message=done)

        return iterator()

    async def aclose(self) -> None:
        self.closed_count += 1
        self.events_log.append("provider.aclose")


@asynccontextmanager
async def app_client(app, *, base_url: str = "http://testserver", client_addr: tuple[str, int] = ("127.0.0.1", 12345)):
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, client=client_addr)
        async with httpx.AsyncClient(transport=transport, base_url=base_url, trust_env=False) as client:
            yield client


class WebApiRuntimeAndHttpTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.home = self.base / "home"
        self.cwd = self.base / "workspace"
        self.cwd.mkdir(parents=True)
        self.static_dir = self.base / "web_out"
        self.static_dir.mkdir(parents=True)
        (self.static_dir / "index.html").write_text("<html><body>Octo Map</body></html>", encoding="utf-8")
        self.env = patch.dict(os.environ, {"OCTO_HOME": str(self.home), "OCTO_OFFLINE": "1"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def _build_session_config(
        self,
        provider: object,
        *,
        session_id: str | None = None,
        tools: list[AgentTool] | None = None,
        context_output_reserve_tokens: int | None = None,
        provider_settings: object | None = None,
    ) -> tuple[CodingSessionConfig, SessionManager, str]:
        paths = OctoPaths(home=self.home, agents_home=self.base / "agents")
        manager = SessionManager(paths)
        if session_id is None:
            record = manager.create_session(cwd=self.cwd, model="offline", title="Initial Web Session")
        else:
            record = manager.get_session(session_id)
            assert record is not None
        config = CodingSessionConfig(
            provider=provider,
            model="offline",
            storage=manager.storage_for(record),
            session_id=record.id,
            session_manager=manager,
            cwd=self.cwd,
            tools=tools,
            resource_paths=OctoResourcePaths(root=paths.home, agents_root=paths.agents_home, cwd=self.cwd, paths=paths),
            provider_settings=provider_settings,
            auto_compact_enabled=False,
            memory_settings=MemorySettings(enabled=False),
            extensions_enabled=False,
            skills_enabled=False,
            trust_override="approve",
            owns_initial_provider=False,
            context_output_reserve_tokens=context_output_reserve_tokens,
        )
        return config, manager, record.id

    async def _wait_terminal(self, client: httpx.AsyncClient, run_id: str, timeout: float = 5.0) -> dict:
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            resp = await client.get(f"/api/runs/{run_id}")
            self.assertEqual(resp.status_code, 200, resp.text)
            data = resp.json()
            if data["status"] in {"completed", "cancelled", "failed"}:
                return data
            await asyncio.sleep(0.02)
        self.fail(f"Run {run_id} did not settle within {timeout}s")

    def test_cli_help(self) -> None:
        runner = CliRunner()
        result = runner.invoke(cli_app, ["--help"])
        self.assertEqual(result.exit_code, 0, result.output)
        plain = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", result.output)
        self.assertIn("--host", plain)
        self.assertIn("--port", plain)
        self.assertIn("--cwd", plain)

    async def test_headless_tool_loop_persists_to_sqlite_and_reopens(self) -> None:
        async def exec_echo(call_id: str, arguments: dict, signal=None, on_update=None):
            return AgentToolResult(content=f"echo:{arguments.get('msg', '')}")

        echo_tool = AgentTool(
            name="echo_fixture",
            label="Echo",
            description="Echo fixture tool",
            parameters={"type": "object", "properties": {"msg": {"type": "string"}}},
            execute_fn=exec_echo,
        )
        provider = TrackedProvider([
            assistant_stream([ToolCall(id="call-1", name="echo_fixture", arguments={"msg": "hello"})], "toolUse"),
            assistant_stream("tool loop completed"),
        ])
        config, manager, session_id = self._build_session_config(provider, tools=[echo_tool])
        runtime = WebRuntime(WebRuntimeConfig(cwd=self.cwd, session_config=config, owns_initial_provider=True))
        app = create_app(runtime, static_dir=self.static_dir, allowed_origins=("http://testserver",))
        headers = {"Origin": "http://testserver"}

        async with app_client(app) as client:
            health = await client.get("/api/health")
            self.assertEqual(health.status_code, 200)
            self.assertTrue(health.json()["ok"])
            self.assertEqual(health.json()["session_id"], session_id)

            state = await client.get("/api/state")
            self.assertEqual(state.status_code, 200)
            self.assertEqual(state.json()["session_id"], session_id)
            self.assertEqual(state.json()["current_topic_id"], "main")

            post = await client.post(
                f"/api/sessions/{session_id}/runs",
                headers=headers,
                json={"client_request_id": "req-tool-1", "topic_id": "main", "text": "run echo"},
            )
            self.assertEqual(post.status_code, 202, post.text)
            run_id = post.json()["run_id"]
            final = await self._wait_terminal(client, run_id)
            self.assertEqual(final["status"], "completed")
            self.assertEqual(final["assistant_text"], "tool loop completed")
            self.assertEqual(len(final["tool_executions"]), 1)
            self.assertEqual(final["tool_executions"][0]["tool_call_id"], "call-1")
            self.assertEqual(final["tool_executions"][0]["status"], "completed")
            self.assertEqual(final["tool_executions"][0]["result_text"], "echo:hello")

            snap = (await client.get("/api/snapshot", params={"session_id": session_id, "topic_id": "main"})).json()
            roles = [msg["role"] for msg in snap["messages"]]
            self.assertEqual(roles, ["user", "assistant", "toolResult", "assistant"])
            self.assertEqual(snap["messages"][0]["text"], "run echo")
            self.assertEqual(snap["messages"][2]["text"], "echo:hello")
            self.assertEqual(snap["messages"][3]["text"], "tool loop completed")
            self.assertEqual(len(snap["turns"]), 1)
            self.assertEqual(snap["turns"][0]["message_count"], 4)

            retry = await client.post(
                f"/api/sessions/{session_id}/runs",
                headers=headers,
                json={"client_request_id": "req-tool-1", "topic_id": "main", "text": "run echo"},
            )
            self.assertEqual(retry.status_code, 202)
            self.assertEqual(retry.json()["run_id"], run_id)
            self.assertEqual(len(provider.calls), 2)

            conflict = await client.post(
                f"/api/sessions/{session_id}/runs",
                headers=headers,
                json={"client_request_id": "req-tool-1", "topic_id": "main", "text": "different text"},
            )
            self.assertEqual(conflict.status_code, 409)
            self.assertEqual(conflict.json()["code"], "client_request_conflict")

        self.assertEqual(provider.closed_count, 1)

        # 关闭后重新打开临时 SQLite，核对历史和旧模型回退路径。
        reopened_provider = TrackedProvider([assistant_stream("after reopen")])
        reopened_config, _, _ = self._build_session_config(reopened_provider, session_id=session_id)
        reopened_runtime = WebRuntime(
            WebRuntimeConfig(cwd=self.cwd, session_config=reopened_config, owns_initial_provider=True)
        )
        reopened_app = create_app(reopened_runtime, static_dir=self.static_dir, allowed_origins=("http://testserver",))
        async with app_client(reopened_app) as client:
            snap = (await client.get("/api/snapshot", params={"session_id": session_id})).json()
            self.assertEqual(len(snap["messages"]), 4)

    async def test_concurrent_submit_topic_switch_during_run_and_cancel_isolation(self) -> None:
        provider = GatedProvider(answer="topic A answer")
        config, _, session_id = self._build_session_config(provider)
        runtime = WebRuntime(WebRuntimeConfig(cwd=self.cwd, session_config=config, owns_initial_provider=True))
        app = create_app(runtime, static_dir=self.static_dir, allowed_origins=("http://testserver",))
        headers = {"Origin": "http://testserver"}

        async with app_client(app) as client:
            topic_a = (
                await client.post(
                    "/api/topics",
                    headers=headers,
                    json={"action": "create", "session_id": session_id, "topic_id": "main", "title": "Topic A"},
                )
            ).json()["topic_id"]
            topic_b = (
                await client.post(
                    "/api/topics",
                    headers=headers,
                    json={"action": "create", "session_id": session_id, "topic_id": "main", "title": "Topic B"},
                )
            ).json()["topic_id"]

            r1, r2 = await asyncio.gather(
                client.post(
                    f"/api/sessions/{session_id}/runs",
                    headers=headers,
                    json={"client_request_id": "c-1", "topic_id": topic_a, "text": "question A"},
                ),
                client.post(
                    f"/api/sessions/{session_id}/runs",
                    headers=headers,
                    json={"client_request_id": "c-2", "topic_id": topic_a, "text": "question A2"},
                ),
            )
            statuses = sorted([r1.status_code, r2.status_code])
            self.assertEqual(statuses, [202, 409])
            accepted = r1 if r1.status_code == 202 else r2
            rejected = r2 if r1.status_code == 202 else r1
            self.assertEqual(rejected.json()["code"], "agent_busy")
            run_1 = accepted.json()["run_id"]

            await asyncio.wait_for(provider.started.wait(), timeout=2.0)

            # 生成中允许切换当前查看/默认话题到 B，但 run_1 的回答必须归属 A。
            switch = await client.post(
                "/api/topics",
                headers=headers,
                json={"action": "select", "session_id": session_id, "topic_id": topic_b},
            )
            self.assertEqual(switch.status_code, 200)

            # 忙碌时切换或新建会话返回 409。
            busy_new = await client.post("/api/sessions", headers=headers, json={})
            self.assertEqual(busy_new.status_code, 409)
            self.assertEqual(busy_new.json()["code"], "agent_busy")

            provider.release.set()
            final_1 = await self._wait_terminal(client, run_1)
            self.assertEqual(final_1["status"], "completed")
            self.assertEqual(final_1["topic_id"], topic_a)

            snap_a = (await client.get("/api/snapshot", params={"session_id": session_id, "topic_id": topic_a})).json()
            snap_b = (await client.get("/api/snapshot", params={"session_id": session_id, "topic_id": topic_b})).json()
            self.assertEqual([m["text"] for m in snap_a["messages"]], ["question A", "topic A answer"])
            self.assertEqual(snap_b["messages"], [])

            # 启动 run_2，再对已完成的 run_1 发 cancel 不能误停 run_2，然后真实取消 run_2。
            provider.started.clear()
            provider.release.clear()
            p2 = await client.post(
                f"/api/sessions/{session_id}/runs",
                headers=headers,
                json={"client_request_id": "c-3", "topic_id": topic_b, "text": "question B"},
            )
            self.assertEqual(p2.status_code, 202)
            run_2 = p2.json()["run_id"]
            await asyncio.wait_for(provider.started.wait(), timeout=2.0)

            stale_cancel = await client.post(f"/api/runs/{run_1}/cancel", headers=headers, json={})
            self.assertEqual(stale_cancel.status_code, 200)
            self.assertEqual(stale_cancel.json()["status"], "completed")
            self.assertEqual((await client.get(f"/api/runs/{run_2}")).json()["status"], "running")

            real_cancel = await client.post(f"/api/runs/{run_2}/cancel", headers=headers, json={})
            self.assertEqual(real_cancel.status_code, 200)
            final_2 = await self._wait_terminal(client, run_2)
            self.assertEqual(final_2["status"], "cancelled")
            self.assertFalse((await client.get("/api/state")).json()["running"])

    async def test_session_switch_stale_guard_provider_error_and_budget_block(self) -> None:
        provider = TrackedProvider([
            assistant_stream("provider blew up", reason="error"),
            assistant_stream("should not run"),
        ])
        config, _, s1 = self._build_session_config(provider, context_output_reserve_tokens=2048)
        runtime = WebRuntime(WebRuntimeConfig(cwd=self.cwd, session_config=config, owns_initial_provider=True))
        app = create_app(runtime, static_dir=self.static_dir, allowed_origins=("http://testserver",))
        headers = {"Origin": "http://testserver"}

        async with app_client(app) as client:
            err_post = await client.post(
                f"/api/sessions/{s1}/runs",
                headers=headers,
                json={"client_request_id": "err-1", "topic_id": "main", "text": "trigger error"},
            )
            self.assertEqual(err_post.status_code, 202)
            err_run = await self._wait_terminal(client, err_post.json()["run_id"])
            self.assertEqual(err_run["status"], "failed")
            self.assertEqual(err_run["error_code"], "provider_error")

            # 创建新会话后，旧 session_id 的快照/话题/发送必须返回 409 session_not_active。
            created = await client.post("/api/sessions", headers=headers, json={"title": "Second Session"})
            self.assertEqual(created.status_code, 200, created.text)
            s2 = created.json()["state"]["session_id"]
            self.assertNotEqual(s1, s2)

            sessions = (await client.get("/api/sessions")).json()
            self.assertEqual(sessions["active_session_id"], s2)
            self.assertEqual({item["id"] for item in sessions["sessions"]}, {s1, s2})

            stale_snap = await client.get("/api/snapshot", params={"session_id": s1})
            self.assertEqual(stale_snap.status_code, 409)
            self.assertEqual(stale_snap.json()["code"], "session_not_active")

            stale_topic = await client.post(
                "/api/topics",
                headers=headers,
                json={"action": "create", "session_id": s1, "topic_id": "main", "title": "Stale"},
            )
            self.assertEqual(stale_topic.status_code, 409)
            self.assertEqual(stale_topic.json()["code"], "session_not_active")

            missing_snap = await client.get("/api/snapshot", params={"session_id": "does-not-exist"})
            self.assertEqual(missing_snap.status_code, 404)

            # 恢复回 s1。
            activated = await client.post(f"/api/sessions/{s1}/activate", headers=headers, json={})
            self.assertEqual(activated.status_code, 200)
            self.assertEqual(activated.json()["state"]["session_id"], s1)

            # 预算硬拦截不触发 provider 调用。
            calls_before = len(provider.calls)
            active_session = runtime._require_session()
            from dataclasses import replace as dc_replace
            active_session._config = dc_replace(active_session._config, context_output_reserve_tokens=200000)
            budget_post = await client.post(
                f"/api/sessions/{s1}/runs",
                headers=headers,
                json={"client_request_id": "budget-1", "topic_id": "main", "text": "x" * 200},
            )
            self.assertEqual(budget_post.status_code, 202)
            budget_run = await self._wait_terminal(client, budget_post.json()["run_id"])
            self.assertEqual(budget_run["status"], "failed")
            self.assertEqual(budget_run["error_code"], "context_budget_exceeded")
            self.assertEqual(len(provider.calls), calls_before)

    async def test_security_headers_proxy_and_static_boundaries(self) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        (self.home / "secret.env").write_text("API_KEY=secret", encoding="utf-8")
        provider = TrackedProvider([])
        config, _, session_id = self._build_session_config(provider)
        runtime = WebRuntime(WebRuntimeConfig(cwd=self.cwd, session_config=config, owns_initial_provider=True))
        app = create_app(
            runtime,
            static_dir=self.static_dir,
            allowed_origins=("https://octo.example.com", "http://127.0.0.1:18300"),
            trusted_proxies=("127.0.0.1",),
        )

        # 来自可信本机代理的转发请求可以通过，且越界静态路径、跨源写、缺 Origin、非 JSON 均被拒绝。
        async with app_client(app, base_url="http://127.0.0.1:18300", client_addr=("127.0.0.1", 5000)) as client:
            bad_host = await client.get("/api/health", headers={"Host": "evil.example"})
            self.assertEqual(bad_host.status_code, 403)

            no_origin = await client.post("/api/sessions", json={})
            self.assertEqual(no_origin.status_code, 403)
            self.assertEqual(no_origin.json()["code"], "forbidden_origin")

            cross_origin = await client.post(
                "/api/sessions",
                headers={"Origin": "https://evil.example"},
                json={},
            )
            self.assertEqual(cross_origin.status_code, 403)

            wrong_type = await client.post(
                "/api/sessions",
                headers={"Origin": "http://127.0.0.1:18300", "Content-Type": "text/plain"},
                content="{}",
            )
            self.assertEqual(wrong_type.status_code, 403)
            self.assertEqual(wrong_type.json()["code"], "invalid_content_type")

            unknown_api = await client.get("/api/does-not-exist")
            self.assertEqual(unknown_api.status_code, 404)
            self.assertEqual(unknown_api.headers["content-type"].split(";")[0], "application/json")

            traversal = await client.get("/../home/secret.env")
            self.assertEqual(traversal.status_code, 404)
            self.assertNotIn("API_KEY=secret", traversal.text)

            proxied_get = await client.get(
                "/api/state",
                headers={
                    "Host": "octo.example.com",
                    "X-Forwarded-Host": "octo.example.com",
                    "X-Forwarded-Proto": "https",
                },
            )
            self.assertEqual(proxied_get.status_code, 200)
            self.assertEqual(proxied_get.json()["session_id"], session_id)

            proxied_post = await client.post(
                "/api/topics",
                headers={
                    "Host": "octo.example.com",
                    "X-Forwarded-Host": "octo.example.com",
                    "X-Forwarded-Proto": "https",
                    "Origin": "https://octo.example.com",
                },
                json={"action": "create", "session_id": session_id, "topic_id": "main", "title": "Proxied"},
            )
            self.assertEqual(proxied_post.status_code, 200)

        # 不可信客户端伪造 X-Forwarded-* 必须被拒绝。
        async with app_client(app, base_url="http://127.0.0.1:18300", client_addr=("203.0.113.9", 5000)) as untrusted:
            spoofed = await untrusted.get(
                "/api/state",
                headers={
                    "Host": "evil.example",
                    "X-Forwarded-Host": "octo.example.com",
                    "X-Forwarded-Proto": "https",
                },
            )
            self.assertEqual(spoofed.status_code, 403)

    async def test_aclose_waits_for_driver_then_session_then_provider(self) -> None:
        events_log: list[str] = []
        provider = GatedProvider(events_log=events_log)
        config, _, session_id = self._build_session_config(provider)
        runtime = WebRuntime(WebRuntimeConfig(cwd=self.cwd, session_config=config, owns_initial_provider=True))
        await runtime.start()

        original_aclose = runtime._require_session().aclose

        async def tracked_session_aclose() -> None:
            events_log.append("session.aclose")
            await original_aclose()

        runtime._require_session().aclose = tracked_session_aclose  # type: ignore[method-assign]

        accepted = await runtime.submit_run(
            session_id,
            # 验证关闭顺序：先停 driver，再关会话/记忆，最后关工厂 provider。
            __import__("web_api.schemas", fromlist=["CreateRunRequest"]).CreateRunRequest(
                client_request_id="close-1",
                topic_id="main",
                text="wait and close",
            ),
        )
        await asyncio.wait_for(provider.started.wait(), timeout=2.0)
        await runtime.aclose()
        events_log.insert(0, f"run:{runtime.get_run(accepted.run_id).status}")
        self.assertEqual(events_log[0], "run:cancelled")
        self.assertEqual(events_log[1:], ["session.aclose", "provider.aclose"])

    async def test_old_model_fallback_and_startup_failure_closes_provider(self) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        (self.home / "settings.json").write_text('{"memory":{"enabled":false}}', encoding="utf-8")
        paths = OctoPaths(home=self.home, agents_home=self.base / "agents")
        manager = SessionManager(paths)
        record = manager.create_session(cwd=self.cwd, model="deepseek-v4-flash", provider_name="deepseek", title="Old Model")
        info = SessionInfoEntry(cwd=str(self.cwd))
        change = ModelChangeEntry(parent_id=info.id, provider="deepseek", model="deepseek-v4-flash")
        question = MessageEntry(parent_id=change.id, message=UserMessage(content="legacy q"))
        answer = MessageEntry(parent_id=question.id, message=AssistantMessage(content="legacy a", model="deepseek-v4-flash"))
        await manager.storage_for(record).append_batch([info, change, question, answer])

        settings = ProviderSettings(
            default_provider="deepseek",
            providers=(
                OpenAICompatibleProviderConfig(
                    name="deepseek",
                    models=("deepseek-flash",),
                    default_model="deepseek-flash",
                    context_windows={"deepseek-flash": 128000},
                ),
            ),
        )
        provider = TrackedProvider([assistant_stream("ok")])
        with (
            patch("web_api.runtime.load_provider_settings", return_value=settings),
            patch("web_api.runtime.provider_has_usable_credentials", return_value=True),
            patch("web_api.runtime.create_model_provider", return_value=provider),
            patch("terminal_app.session.create_model_provider", return_value=provider),
        ):
            runtime = WebRuntime(WebRuntimeConfig(cwd=self.cwd))
            app = create_app(runtime, static_dir=self.static_dir, allowed_origins=("http://testserver",))
            async with app_client(app) as client:
                state = (await client.get("/api/state")).json()
                self.assertEqual(state["session_id"], record.id)
                self.assertEqual(state["model"], "deepseek-flash")
                self.assertTrue(state["notice"])
                snap = (await client.get("/api/snapshot", params={"session_id": record.id})).json()
                self.assertEqual([m["text"] for m in snap["messages"]], ["legacy q", "legacy a"])
        self.assertEqual(provider.closed_count, 1)

        # 启动中途失败时仍必须释放由工厂创建的 provider。
        failing_provider = TrackedProvider([])
        with (
            patch("web_api.runtime.load_provider_settings", return_value=settings),
            patch("web_api.runtime.provider_has_usable_credentials", return_value=True),
            patch("web_api.runtime.create_model_provider", return_value=failing_provider),
            patch("web_api.runtime.prepare_coding_session", side_effect=RuntimeError("boom on startup")),
        ):
            failing_runtime = WebRuntime(WebRuntimeConfig(cwd=self.cwd))
            with self.assertRaises(RuntimeError):
                await failing_runtime.start()
        self.assertEqual(failing_provider.closed_count, 1)


if __name__ == "__main__":
    unittest.main()
