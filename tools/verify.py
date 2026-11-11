import ast
import asyncio
import importlib
import io
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import tokenize
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")
sys.dont_write_bytecode = True
PACKAGES = ("octo_agent", "octo_ai", "octo_coding")


def all_sources():
    return [path for package in PACKAGES for path in (ROOT / package).rglob("*.py")]


def assistant_stream(content, reason="stop"):
    from octo_agent.messages import AssistantMessage
    from octo_agent.provider_events import AssistantDoneEvent, AssistantStartEvent, TextDeltaEvent

    message = AssistantMessage(content=content, model="offline", stop_reason=reason)
    events = [AssistantStartEvent(partial=message)]
    if isinstance(content, str):
        events.append(TextDeltaEvent(content_index=0, delta=content, partial=message))
    events.append(AssistantDoneEvent(reason=reason, message=message))
    return events


class SourceChecks(unittest.TestCase):
    def test_all_modules_compile_and_import(self):
        for path in all_sources():
            with self.subTest(path=path.relative_to(ROOT).as_posix()):
                compile(path.read_text(encoding="utf-8"), str(path), "exec")
                parts = list(path.relative_to(ROOT).with_suffix("").parts)
                if parts[-1] == "__init__":
                    parts.pop()
                importlib.import_module(".".join(parts))

    def test_old_names_and_deleted_features_are_absent(self):
        old_name = "".join(map(chr, (116, 97, 117)))
        for package in PACKAGES:
            for path in (ROOT / package).rglob("*"):
                if not path.is_file() or "__pycache__" in path.parts:
                    continue
                self.assertNotIn(old_name, str(path.relative_to(ROOT)).lower())
                text = path.read_text(encoding="utf-8")
                self.assertNotIn(old_name, text.lower())
                self.assertNotIn(chr(0x3C4), text)
                self.assertNotIn("startup_update_notice", text)
                self.assertNotIn("self_docs", text)
        for removed in ("self_docs.py", "updater.py", "update_check.py", "data/docs", "data/examples", "data/release-notes"):
            self.assertFalse((ROOT / "octo_coding" / removed).exists())

    def test_only_core_comments_remain(self):
        count = 0
        for path in all_sources():
            source = path.read_text(encoding="utf-8")
            for node in ast.walk(ast.parse(source)):
                self.assertFalse(isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str), path)
            for token in tokenize.generate_tokens(io.StringIO(source).readline):
                if token.type == tokenize.COMMENT:
                    count += 1
                    self.assertTrue(any("\u4e00" <= char <= "\u9fff" for char in token.string), (path, token.string))
        self.assertEqual(count, 13)

    def test_cli_help_and_version(self):
        from typer.testing import CliRunner
        from octo_coding.cli import app

        runner = CliRunner()
        result = runner.invoke(app, ["--help"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("models-refresh", result.output)
        self.assertNotIn("octo update", result.output)
        self.assertNotIn("--models ", result.output)
        result = runner.invoke(app, ["--version"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("octo ", result.output.lower())

    def test_packaged_resources_and_home(self):
        from importlib.resources import files
        from octo_coding.paths import OctoPaths
        from octo_coding.tui.themes import get_tui_theme

        for name in ("catalog.toml", "models-dev-catalog.json"):
            self.assertTrue(files("octo_coding").joinpath("data", name).is_file())
        for name in ("octo-dark", "octo-light"):
            self.assertEqual(get_tui_theme(name).name, name)
        with tempfile.TemporaryDirectory() as temporary:
            with patch.dict(os.environ, {"OCTO_HOME": temporary}):
                paths = OctoPaths()
                self.assertEqual(paths.home, Path(temporary))
                self.assertEqual(paths.project_octo_dir(Path(temporary)), Path(temporary) / ".octo")

    def test_project_instructions_survive(self):
        from octo_coding.context import discover_project_context
        from octo_coding.resources import OctoResourcePaths
        from octo_coding.system_prompt import BuildSystemPromptOptions, build_system_prompt

        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            project = base / "project"
            nested = project / "nested"
            nested.mkdir(parents=True)
            (project / "pyproject.toml").write_text("[project]\nname='fixture'\n", encoding="utf-8")
            for folder, content in ((base / "home", "USER_CONTEXT"), (project, "ROOT_CONTEXT"), (nested, "NESTED_CONTEXT"), (nested / ".octo", "PROJECT_CONTEXT")):
                folder.mkdir(parents=True, exist_ok=True)
                (folder / "AGENTS.md").write_text(content, encoding="utf-8")
            context = discover_project_context(OctoResourcePaths(root=base / "home", agents_root=base / "agents", cwd=nested))
            prompt = build_system_prompt(BuildSystemPromptOptions(cwd=nested, context_files=context))
            for value in ("USER_CONTEXT", "ROOT_CONTEXT", "NESTED_CONTEXT", "PROJECT_CONTEXT"):
                self.assertIn(value, prompt)
            self.assertNotIn("Additional docs:", prompt)
            self.assertNotIn("Examples:", prompt)


class RuntimeChecks(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.environment = patch.dict(os.environ, {"OCTO_HOME": str(self.base / "home")})
        self.environment.start()
        self.network = patch("httpx.AsyncClient.send", side_effect=AssertionError("Offline checks must not access HTTP"))
        self.network.start()
        self.sessions = []

    async def asyncTearDown(self):
        for session in reversed(self.sessions):
            await session.aclose()
        self.network.stop()
        self.environment.stop()
        self.temporary.cleanup()

    async def session(self, streams, storage=None, extension_paths=()):
        from octo_ai.fake import FakeProvider
        from octo_agent.session import JsonlSessionStorage
        from octo_coding.paths import OctoPaths
        from octo_coding.resources import OctoResourcePaths
        from octo_coding.session import CodingSession, CodingSessionConfig

        provider = FakeProvider(streams)
        paths = OctoPaths(home=self.base / "home", agents_home=self.base / "agents")
        config = CodingSessionConfig(
            provider=provider, model="offline", storage=storage or JsonlSessionStorage(self.base / "session.jsonl"),
            cwd=self.base, resource_paths=OctoResourcePaths(root=paths.home, agents_root=paths.agents_home, cwd=self.base, paths=paths),
            auto_compact_enabled=False, extensions_enabled=False, extension_paths=extension_paths,
            trust_override="approve", skills_enabled=False,
        )
        session = await CodingSession.load(config)
        self.sessions.append(session)
        return session, provider

    async def test_file_tools_and_shell(self):
        from octo_coding.tools import create_coding_tools

        tools = {tool.name: tool for tool in create_coding_tools(cwd=self.base)}
        self.assertEqual(set(tools), {"read", "write", "edit", "bash"})
        await tools["write"].execute("w", {"path": "check.txt", "content": "alpha\n"})
        await tools["edit"].execute("e", {"path": "check.txt", "oldText": "alpha", "newText": "beta"})
        result = await tools["read"].execute("r", {"path": "check.txt"})
        self.assertIn("beta", result.text)
        result = await tools["bash"].execute("b", {"command": "echo OCTO_SHELL_OK", "timeout": 5})
        self.assertIn("OCTO_SHELL_OK", result.text)
        self.assertEqual(result.details["exit_code"], 0)

    async def test_agent_tool_loop_and_durable_resume(self):
        from octo_agent.messages import ToolCall, ToolResultMessage
        from octo_agent.session import JsonlSessionStorage, MessageEntry

        storage = JsonlSessionStorage(self.base / "session.jsonl")
        session, provider = await self.session([
            assistant_stream([ToolCall(id="write-1", name="write", arguments={"path": "from-agent.txt", "content": "alpha"})], "toolUse"),
            assistant_stream([ToolCall(id="read-1", name="read", arguments={"path": "from-agent.txt"})], "toolUse"),
            assistant_stream("verified alpha"),
        ], storage)
        events = [event async for event in session.prompt("Create and read a file")]
        self.assertEqual((self.base / "from-agent.txt").read_text(), "alpha")
        self.assertEqual(len(provider.calls), 3)
        self.assertTrue(any(event.type == "message_update" for event in events))
        messages = [entry.message for entry in await storage.read_all() if isinstance(entry, MessageEntry)]
        self.assertEqual([message.tool_call_id for message in messages if isinstance(message, ToolResultMessage)], ["write-1", "read-1"])
        await session.aclose()
        self.sessions.remove(session)
        resumed, next_provider = await self.session([assistant_stream("continued")], storage)
        _ = [event async for event in resumed.prompt("Continue")]
        history = next_provider.calls[0][2]
        self.assertTrue(any(isinstance(message, ToolResultMessage) and "alpha" in message.text for message in history))
        self.assertTrue(any(getattr(message, "text", "") == "verified alpha" for message in history))

    async def test_extension_permission_gate(self):
        from octo_agent.messages import ToolCall

        extension = self.base / "deny_write.py"
        extension.write_text(
            'from octo_coding.extensions import ToolCallHookResult\n'
            'def setup(octo):\n'
            '    @octo.on("tool_call")\n'
            '    def deny(event, context):\n'
            '        if event.tool_name == "write":\n'
            '            return ToolCallHookResult(block=True, reason="read-only fixture")\n', encoding="utf-8")
        session, provider = await self.session([
            assistant_stream([ToolCall(id="blocked", name="write", arguments={"path": "forbidden.txt", "content": "bad"})], "toolUse"),
            assistant_stream("blocked as expected"),
        ], extension_paths=(extension,))
        _ = [event async for event in session.prompt("Try write")]
        self.assertFalse((self.base / "forbidden.txt").exists())
        self.assertTrue(any("read-only fixture" in getattr(message, "text", "") for message in provider.calls[1][2]))
        self.assertIn("write", [tool.name for tool in provider.calls[0][3]])

    async def test_tui_mounts_and_renders_new_brand(self):
        from octo_coding.tui.app import OctoTuiApp

        session, _ = await self.session([])
        app = OctoTuiApp(session)
        async with app.run_test(size=(110, 35)) as pilot:
            await pilot.pause()
            self.assertIn("Octo", str(app.query_one("#prompt-prefix").render()))
            self.assertEqual(app.theme, "octo-dark")

    async def test_html_export_keeps_rendering(self):
        from octo_agent.messages import UserMessage
        from octo_agent.session import MessageEntry
        from octo_coding.session_export import render_session_html

        html = render_session_html([MessageEntry(message=UserMessage(content="fixture content"))])
        self.assertIn("fixture content", html)
        self.assertIn("Octo", html)
        self.assertIn("<script>", html)
        self.assertNotIn("/* localStorage", html)


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    report = {"tests": result.testsRun, "failures": len(result.failures), "errors": len(result.errors), "success": result.wasSuccessful(), "python": sys.version.split()[0]}
    print(json.dumps(report))
    if len(sys.argv) == 3 and sys.argv[1] == "--report":
        Path(sys.argv[2]).write_text(json.dumps(report, indent=2), encoding="utf-8")
    raise SystemExit(0 if result.wasSuccessful() else 1)
