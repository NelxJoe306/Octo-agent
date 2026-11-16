import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_core.messages import AssistantMessage, ToolCall, ToolResultMessage
from agent_core.provider_events import AssistantDoneEvent
from agent_core.session import SQLiteSessionStorage
from agent_core.tools import AgentTool, AgentToolResult
from long_term_memory.memory_runtime import MemorySettings
from model_adapters.fake import FakeProvider
from model_adapters.prefix import build_prefix, stable_tools
from terminal_app.paths import OctoPaths
from terminal_app.resources import OctoResourcePaths
from terminal_app.session import CodingSession, CodingSessionConfig
from terminal_app.system_prompt import BuildSystemPromptOptions, build_system_prompt
from datetime import date


def response(calls=()):
    return [AssistantDoneEvent(reason="toolUse" if calls else "stop",
        message=AssistantMessage(content=list(calls) if calls else "OK", model="offline",
                                 stop_reason="toolUse" if calls else "stop"))]


class ModeTests(unittest.IsolatedAsyncioTestCase):
    async def test_plan_blocks_side_effects_allows_read_and_resumes_with_fixed_schemas(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {"OCTO_OFFLINE":"1", "OCTO_DISABLE_PREFIX_CACHE":"0"}):
            root = Path(temp)
            (root/"input.txt").write_text("hello", encoding="utf-8")
            calls = [
                ToolCall(id="enter", name="enter_plan_mode", arguments={}),
                ToolCall(id="write", name="write", arguments={"path":"output.txt","content":"forbidden"}),
                ToolCall(id="shell", name="bash", arguments={"command":"echo forbidden"}),
                ToolCall(id="bash_lc", name="bash", arguments={"command":"bash -lc 'echo forbidden'"}),
                ToolCall(id="sh_c", name="bash", arguments={"command":"sh -c 'echo forbidden'"}),
                ToolCall(id="powershell", name="bash", arguments={"command":"powershell -Command 'Write-Output forbidden'"}),
                ToolCall(id="unknown", name="unknown", arguments={}),
                ToolCall(id="read", name="read", arguments={"path":"input.txt"}),
            ]
            first = FakeProvider([response(calls), response()])
            paths = OctoPaths(home=root/"home", agents_home=root/"agents")
            def config(provider):
                return CodingSessionConfig(provider=provider, model="offline", cwd=root,
                    storage=SQLiteSessionStorage(root/"octo.db", "source"), extensions_enabled=False, skills_enabled=False,
                    resource_paths=OctoResourcePaths(root=paths.home, agents_root=paths.agents_home, cwd=root, paths=paths),
                    memory_settings=MemorySettings(enabled=False))
            session = await CodingSession.load(config(first))
            unknown_executed = []
            async def unknown_tool(call_id, arguments, signal=None, on_update=None):
                unknown_executed.append(call_id)
                return AgentToolResult(content="unexpected execution")
            session._harness.config.tools.append(AgentTool(name="unknown", label="fixture",
                description="Fixture extension with undeclared effects",
                parameters={"type":"object","properties":{}}, execute_fn=unknown_tool))
            try:
                events = [event async for event in session.prompt("Plan")]
                end = next(event for event in reversed(events) if event.type == "agent_end")
                self.assertEqual(end.messages[-1].text, "OK")
                self.assertEqual(session.mode, "plan")
                self.assertFalse((root/"output.txt").exists())
                self.assertEqual(unknown_executed, [])
                results = {m.tool_call_id:m for m in first.calls[1][2] if isinstance(m, ToolResultMessage)}
                self.assertTrue(all(results[k].is_error for k in ("write", "shell", "bash_lc", "sh_c", "powershell", "unknown")))
                self.assertFalse(results["read"].is_error)
                self.assertIn("hello", results["read"].text)
                self.assertIn("Plan mode:", first.calls[1][2][-1].text)
                self.assertEqual(build_prefix({"instructions":first.calls[0][1], "tools":[t.parameters for t in first.calls[0][3]]}),
                                 build_prefix({"instructions":first.calls[1][1], "tools":[t.parameters for t in first.calls[1][3]]}))
            finally:
                await session.aclose()
            second = FakeProvider([response([
                ToolCall(id="exit", name="exit_plan_mode", arguments={}),
                ToolCall(id="allowed", name="write", arguments={"path":"output.txt","content":"allowed"}),
            ]), response()])
            resumed = await CodingSession.load(config(second))
            try:
                self.assertEqual(resumed.mode, "plan")
                await resumed.reload()
                self.assertEqual(resumed.mode, "plan")
                async for _ in resumed.prompt("Execute"):
                    pass
                self.assertEqual(resumed.mode, "execute")
                self.assertEqual((root/"output.txt").read_text(), "allowed")
            finally:
                await resumed.aclose()

    async def test_stable_prefix_moves_runtime_and_preserves_schema_arrays(self):
        with patch.dict(os.environ, {"OCTO_DISABLE_PREFIX_CACHE":"0"}):
            a = build_system_prompt(BuildSystemPromptOptions(cwd=Path("/a"), current_date=date(2026,1,1)))
            b = build_system_prompt(BuildSystemPromptOptions(cwd=Path("/b"), current_date=date(2026,1,2)))
            self.assertEqual(a, b)
            tools = [{"name":"z", "parameters":{"enum":["z","a"], "type":"string"}}, {"name":"a"}]
            reordered = [{"name":"a"}, {"parameters":{"type":"string","enum":["z","a"]}, "name":"z"}]
            self.assertEqual(stable_tools(tools), stable_tools(reordered))
            self.assertEqual(stable_tools(tools)[1]["parameters"]["enum"], ["z","a"])
        with patch.dict(os.environ, {"OCTO_DISABLE_PREFIX_CACHE":"1"}):
            self.assertIn("2026-01-01", build_system_prompt(BuildSystemPromptOptions(cwd=Path("/a"), current_date=date(2026,1,1))))
            self.assertIs(stable_tools(tools), tools)
