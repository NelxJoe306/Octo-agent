from __future__ import annotations

import asyncio
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from octo_coding.memory_runtime import MemorySettings

from octo_agent.messages import UserMessage
from octo_agent.session import MessageEntry, entry_to_json_line


class SQLiteStorageTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.path = self.base / "octo.db"
        self.environment = patch.dict(os.environ, {"OCTO_HOME": str(self.base / "isolated-home"),
                                                 "SQLITE_TEST_KEY": "offline-fixture", "OCTO_OFFLINE": "1"})
        fixture_home = self.base / "isolated-home"
        fixture_home.mkdir(parents=True, exist_ok=True)
        (fixture_home / "settings.json").write_text('{"memory":{"enabled":false}}', encoding="utf-8")
        self.environment.start()
        self.addCleanup(self.environment.stop)

    async def test_reopen_recovers_stable_entries_and_isolates_sessions(self):
        from octo_agent.session import SQLiteSessionStorage

        entry = MessageEntry(id="message-1", message=UserMessage(content="remember this"))
        await SQLiteSessionStorage(self.path, "session-a").append(entry)
        reopened = SQLiteSessionStorage(self.path, "session-a")
        self.assertEqual(await reopened.read_all(), [entry])
        self.assertEqual(await reopened.read_entry("message-1"), entry)
        self.assertEqual(await SQLiteSessionStorage(self.path, "session-b").read_all(), [])

    async def test_retry_is_idempotent_and_conflicting_batch_rolls_back(self):
        from octo_agent.session import SQLiteSessionStorage

        storage = SQLiteSessionStorage(self.path, "session-a")
        original = MessageEntry(id="original", message=UserMessage(content="original"))
        await storage.append(original)
        await storage.append(original.model_copy(deep=True))
        self.assertEqual(await storage.read_all(), [original])
        new = MessageEntry(id="new", message=UserMessage(content="new"))
        changed = original.model_copy(update={"message": UserMessage(content="changed")})
        with self.assertRaisesRegex(ValueError, "conflict"):
            await storage.append_batch((new, changed))
        self.assertEqual(await storage.read_all(), [original])
        self.assertIsNone(await storage.read_entry("new"))

    async def test_new_managed_sessions_use_only_sqlite_and_catalog_survives_restart(self):
        from octo_agent.session import SQLiteSessionStorage
        from octo_coding.paths import OctoPaths
        from octo_coding.session_manager import SessionManager

        paths = OctoPaths(home=self.base / "home")
        manager = SessionManager(paths)
        record = manager.create_session_exclusive(cwd=self.base, model="offline", session_id="new-session")
        storage = manager.storage_for(record)
        self.assertIsInstance(storage, SQLiteSessionStorage)
        self.assertEqual(storage.path, paths.home / "octo.db")
        await storage.append(MessageEntry(id="first", message=UserMessage(content="hello")))
        restarted = SessionManager(paths)
        self.assertEqual(restarted.get_session("new-session"), record)
        self.assertEqual([item.id for item in restarted.list_sessions(self.base)], ["new-session"])
        self.assertEqual((await restarted.storage_for(record).read_entry("first")).message.text, "hello")
        self.assertEqual(list(paths.home.rglob("*.jsonl")), [])
        with self.assertRaisesRegex(RuntimeError, "already exists"):
            restarted.create_session_exclusive(cwd=self.base, model="offline", session_id="new-session")

    async def test_legacy_import_preserves_source_ids_and_sqlite_becomes_authoritative(self):
        from octo_coding.paths import OctoPaths
        from octo_coding.session_manager import CodingSessionRecord, SessionManager

        paths = OctoPaths(home=self.base / "home")
        manager = SessionManager(paths)
        source = paths.project_session_dir(self.base) / "legacy.jsonl"
        source.parent.mkdir(parents=True)
        entry = MessageEntry(id="legacy-message", message=UserMessage(content="legacy content"))
        source.write_text(entry_to_json_line(entry), encoding="utf-8")
        record = CodingSessionRecord(id="legacy", path=source, cwd=self.base, model="offline",
                                     title="Legacy", created_at=1, updated_at=1)
        manager.project_index_path(self.base).write_text(record.to_model().model_dump_json() + "\n", encoding="utf-8")
        source_bytes = source.read_bytes()
        indexed = manager.get_session("legacy")
        storage = manager.storage_for(indexed)
        self.assertEqual(await storage.read_all(), [entry])
        await storage.import_jsonl(source)
        await storage.import_jsonl(source)
        self.assertEqual(await storage.read_all(), [entry])
        added = MessageEntry(id="new-message", parent_id=entry.id, message=UserMessage(content="new content"))
        await storage.append(added)
        self.assertEqual(source.read_bytes(), source_bytes)
        source.unlink()
        restarted = SessionManager(paths)
        imported = restarted.get_session("legacy")
        self.assertEqual(imported.path, paths.database_path)
        self.assertEqual(await restarted.storage_for(imported).read_all(), [entry, added])

    async def test_coding_session_and_cli_export_resume_from_sqlite(self):
        from octo_agent.messages import AssistantMessage
        from octo_agent.provider_events import AssistantDoneEvent
        from octo_agent.session import JsonlSessionStorage
        from octo_ai.fake import FakeProvider
        from octo_coding.cli import export_session_command
        from octo_coding.paths import OctoPaths
        from octo_coding.resources import OctoResourcePaths
        from octo_coding.session import CodingSession, CodingSessionConfig
        from octo_coding.session_manager import SessionManager

        paths = OctoPaths(home=self.base / "home", agents_home=self.base / "agents")
        manager = SessionManager(paths)
        record = manager.create_session(cwd=self.base, model="offline", session_id="runtime", title="Runtime")
        answer = AssistantMessage(content="stored answer", model="offline", stop_reason="stop")
        provider = FakeProvider([[AssistantDoneEvent(reason="stop", message=answer)]])
        config = CodingSessionConfig(
            memory_settings=MemorySettings(enabled=False),
            provider=provider, model="offline", storage=manager.storage_for(record),
            session_id=record.id, session_manager=manager, cwd=self.base,
            resource_paths=OctoResourcePaths(root=paths.home, agents_root=paths.agents_home,
                                             cwd=self.base, paths=paths),
            auto_compact_enabled=False, extensions_enabled=False, trust_override="approve",
            skills_enabled=False,
        )
        session = await CodingSession.load(config)
        try:
            _ = [event async for event in session.prompt("persistent question")]
            saved = await session.storage.read_all()
        finally:
            await session.aclose()
        exported = await export_session_command(record.id, self.base / "export.jsonl", "jsonl", manager)
        self.assertEqual(await JsonlSessionStorage(exported).read_all(), saved)
        resumed = await CodingSession.load(config)
        try:
            self.assertEqual([message.text for message in resumed.messages], ["persistent question", "stored answer"])
            exported_path = await resumed.export(format="jsonl")
            self.assertIn(record.id, exported_path.name)
            await resumed.new_session()
            resumed.ensure_session_indexed()
            await resumed.set_session_name("SQLite new session")
            self.assertEqual(manager.get_session(resumed.session_id).title, "SQLite new session")
        finally:
            await resumed.aclose()

    async def test_concurrent_batches_stay_complete_and_do_not_lose_entries(self):
        from octo_agent.session import SQLiteSessionStorage

        async def write_batch(index):
            storage = SQLiteSessionStorage(self.path, "shared-session")
            entries = [MessageEntry(id=f"{index}-{position}", message=UserMessage(content=str(index)))
                       for position in range(2)]
            await storage.append_batch(entries)

        await asyncio.gather(*(write_batch(index) for index in range(12)))
        entries = await SQLiteSessionStorage(self.path, "shared-session").read_all()
        self.assertEqual({entry.id for entry in entries}, {f"{index}-{position}" for index in range(12) for position in range(2)})
        for offset in range(0, len(entries), 2):
            self.assertEqual(entries[offset].message.text, entries[offset + 1].message.text)

    async def test_invalid_import_cannot_commit_a_valid_prefix(self):
        from octo_agent.session import SQLiteSessionStorage, SessionJsonlError

        entry = MessageEntry(id="valid-prefix", message=UserMessage(content="do not commit"))
        source = self.base / "broken.jsonl"
        source.write_text(entry_to_json_line(entry) + '{"type": "message", invalid}', encoding="utf-8")
        storage = SQLiteSessionStorage(self.path, "broken-session")
        with self.assertRaises(SessionJsonlError):
            await storage.import_jsonl(source)
        self.assertEqual(await storage.read_all(), [])

    async def test_export_cannot_overwrite_the_authoritative_database(self):
        from octo_coding.cli import export_session_command
        from octo_coding.paths import OctoPaths
        from octo_coding.session_manager import SessionManager

        manager = SessionManager(OctoPaths(home=self.base))
        record = manager.create_session(cwd=self.base, model="offline", session_id="protected")
        storage = manager.storage_for(record)
        entry = MessageEntry(id="protected", message=UserMessage(content="keep this"))
        await storage.append(entry)
        with self.assertRaisesRegex(ValueError, "overwrite"):
            await export_session_command(record.id, record.path, "jsonl", manager)
        self.assertEqual(await storage.read_all(), [entry])

    async def test_first_persist_commits_catalog_and_messages_together(self):
        from octo_coding.paths import OctoPaths
        from octo_coding.session_manager import SessionManager

        paths = OctoPaths(home=self.base / "home")
        manager = SessionManager(paths)
        prepared = manager.prepare_session(cwd=self.base, model="offline", session_id="deferred")
        self.assertIsNone(manager.get_session(prepared.id))
        entry = MessageEntry(id="first-durable", message=UserMessage(content="recover after crash"))
        await manager.storage_for(prepared).append(entry)
        restarted = SessionManager(paths)
        recovered = restarted.get_session(prepared.id)
        self.assertIsNotNone(recovered)
        self.assertEqual(await restarted.storage_for(recovered).read_all(), [entry])

    async def test_broken_legacy_index_does_not_block_sqlite_catalog(self):
        from octo_coding.paths import OctoPaths
        from octo_coding.session_manager import SessionManager

        manager = SessionManager(OctoPaths(home=self.base))
        record = manager.create_session(cwd=self.base, model="offline", session_id="healthy")
        manager.index_path.parent.mkdir(parents=True, exist_ok=True)
        manager.index_path.write_text("{broken legacy index", encoding="utf-8")
        with self.assertLogs("octo_coding.session_manager", level="WARNING") as diagnostic:
            listed = manager.list_sessions()
        self.assertEqual([item.id for item in listed], [record.id])
        self.assertEqual(manager.get_session(record.id), record)
        self.assertIn(str(manager.index_path), diagnostic.output[0])

    async def test_rpc_session_file_roundtrip_identifies_one_sqlite_session(self):
        from octo_ai.fake import FakeProvider
        from octo_coding.paths import OctoPaths
        from octo_coding.provider_config import OpenAICompatibleProviderConfig, ProviderSettings
        from octo_coding.resources import OctoResourcePaths
        from octo_coding.rpc import RpcServer
        from octo_coding.session import CodingSession, CodingSessionConfig
        from octo_coding.session_manager import SessionManager

        paths = OctoPaths(home=self.base / "home", agents_home=self.base / "agents")
        manager = SessionManager(paths)
        provider_config = OpenAICompatibleProviderConfig(name="fixture", models=("offline",),
                                                        default_model="offline", api_key_env="SQLITE_TEST_KEY")
        settings = ProviderSettings(default_provider="fixture", providers=(provider_config,))

        async def open_record(record):
            return await CodingSession.load(CodingSessionConfig(
                memory_settings=MemorySettings(enabled=False),
                provider=FakeProvider([]), model="offline", provider_name="fixture",
                storage=manager.storage_for(record), session_id=record.id, session_manager=manager,
                cwd=self.base, provider_settings=settings, runtime_provider_config=provider_config,
                resource_paths=OctoResourcePaths(root=paths.home, agents_root=paths.agents_home, cwd=self.base, paths=paths),
                auto_compact_enabled=False, extensions_enabled=False, trust_override="approve", skills_enabled=False,
            ))

        first = manager.create_session(cwd=self.base, model="offline", provider_name="fixture", session_id="first", title="First")
        first_session = await open_record(first)
        first_output = io.StringIO()
        await RpcServer(first_session, stdin=io.StringIO('{"type":"get_state"}\n'), stdout=first_output).run()
        locator = json.loads(first_output.getvalue().splitlines()[0])["data"]["sessionFile"]
        second = manager.create_session(cwd=self.base, model="offline", provider_name="fixture", session_id="second", title="Second")
        second_session = await open_record(second)
        commands = [
            {"type": "switch_session", "sessionPath": locator},
            {"type": "get_state"},
            {"type": "switch_session", "sessionPath": str(paths.database_path)},
        ]
        output = io.StringIO()
        with patch.dict(os.environ, {"SQLITE_TEST_KEY": "offline-fixture", "OCTO_OFFLINE": "1"}):
            await RpcServer(second_session, stdin=io.StringIO("\n".join(map(json.dumps, commands)) + "\n"), stdout=output).run()
        responses = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertTrue(responses[0]["success"], responses[0])
        self.assertEqual(responses[1]["data"]["sessionId"], first.id)
        self.assertFalse(responses[2]["success"])
        self.assertIn("Ambiguous", responses[2]["error"])

    async def test_html_download_keeps_the_session_id(self):
        from octo_coding.cli import export_session_command
        from octo_coding.paths import OctoPaths
        from octo_coding.session_manager import SessionManager

        manager = SessionManager(OctoPaths(home=self.base))
        record = manager.create_session(cwd=self.base, model="offline", session_id="html-session")
        output = await export_session_command(record.id, self.base / "session.html", "html", manager)
        self.assertTrue('link.download = "html-session.jsonl";' in output.read_text(encoding="utf-8"),
                        "HTML download must use its session id")


if __name__ == "__main__":
    unittest.main()
