from __future__ import annotations

import asyncio
from collections.abc import Iterator, Sequence
from contextlib import closing, contextmanager
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from time import time

from agent_core.session.entries import SessionEntry
from agent_core.session.jsonl import entries_from_json_lines, entry_from_json_line, entry_to_json_line


def _encode(entry: SessionEntry) -> str:
    if not entry.id:
        raise ValueError("Session entry id cannot be empty")
    return json.dumps(json.loads(entry_to_json_line(entry)), sort_keys=True,
                      ensure_ascii=False, separators=(",", ":"))


class SQLiteSessionDatabase:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=10, isolation_level=None)) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA journal_mode = WAL")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, 2, 3, 4, 5, 6):
                raise RuntimeError(f"Unsupported session database version: {version}")
            if version == 0:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    connection.execute("""CREATE TABLE IF NOT EXISTS sessions (
                        session_id TEXT PRIMARY KEY, record_json TEXT)""")
                    connection.execute("""CREATE TABLE IF NOT EXISTS session_entries (
                        session_id TEXT NOT NULL REFERENCES sessions(session_id),
                        id TEXT NOT NULL, seq INTEGER NOT NULL, body_json TEXT NOT NULL,
                        PRIMARY KEY(session_id, id), UNIQUE(session_id, seq))""")
                    connection.execute("""CREATE TABLE IF NOT EXISTS legacy_imports (
                        session_id TEXT NOT NULL REFERENCES sessions(session_id),
                        source_path TEXT NOT NULL, sha256 TEXT NOT NULL, imported_at REAL NOT NULL,
                        PRIMARY KEY(session_id, source_path, sha256))""")
                    connection.execute("""CREATE TRIGGER IF NOT EXISTS entries_no_update
                        BEFORE UPDATE ON session_entries BEGIN
                        SELECT RAISE(ABORT, 'Session entries are immutable'); END""")
                    connection.execute("""CREATE TRIGGER IF NOT EXISTS entries_no_delete
                        BEFORE DELETE ON session_entries BEGIN
                        SELECT RAISE(ABORT, 'Session entries are immutable'); END""")
                    connection.execute("PRAGMA user_version = 1")
                    connection.commit()
                except BaseException:
                    connection.rollback()
                    raise
            if version < 2:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    connection.execute("""CREATE TABLE IF NOT EXISTS topics (
                        session_id TEXT NOT NULL REFERENCES sessions(session_id), topic_id TEXT NOT NULL,
                        parent_topic_id TEXT, anchor_entry_id TEXT, title TEXT NOT NULL,
                        PRIMARY KEY(session_id, topic_id),
                        FOREIGN KEY(session_id, parent_topic_id) REFERENCES topics(session_id, topic_id),
                        FOREIGN KEY(session_id, anchor_entry_id) REFERENCES session_entries(session_id, id))""")
                    connection.execute("""CREATE TABLE IF NOT EXISTS topic_selection (
                        session_id TEXT PRIMARY KEY REFERENCES sessions(session_id), topic_id TEXT NOT NULL,
                        FOREIGN KEY(session_id, topic_id) REFERENCES topics(session_id, topic_id))""")
                    connection.execute("""CREATE TABLE IF NOT EXISTS topic_requests (
                        session_id TEXT NOT NULL, request_id TEXT NOT NULL, topic_id TEXT NOT NULL,
                        topic_path TEXT NOT NULL, PRIMARY KEY(session_id, request_id),
                        FOREIGN KEY(session_id, topic_id) REFERENCES topics(session_id, topic_id))""")
                    connection.execute("""CREATE TABLE IF NOT EXISTS entry_requests (
                        session_id TEXT NOT NULL, entry_id TEXT NOT NULL, request_id TEXT NOT NULL,
                        PRIMARY KEY(session_id, entry_id),
                        FOREIGN KEY(session_id, entry_id) REFERENCES session_entries(session_id, id),
                        FOREIGN KEY(session_id, request_id) REFERENCES topic_requests(session_id, request_id))""")
                    connection.execute("CREATE INDEX IF NOT EXISTS topic_request_lookup ON topic_requests(session_id, topic_id)")
                    connection.execute("PRAGMA user_version = 2")
                    connection.commit()
                except BaseException:
                    connection.rollback()
                    raise
            if version < 3:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    columns = {row[1] for row in connection.execute("PRAGMA table_info(topic_requests)")}
                    if "continuation_entry_id" not in columns:
                        connection.execute("ALTER TABLE topic_requests ADD COLUMN continuation_entry_id TEXT")
                    connection.execute("PRAGMA user_version = 3")
                    connection.commit()
                except BaseException:
                    connection.rollback()
                    raise
            if version < 4:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    connection.execute("""CREATE TABLE IF NOT EXISTS tool_artifacts (
                        session_id TEXT NOT NULL REFERENCES sessions(session_id), artifact_id TEXT NOT NULL,
                        tool_call_id TEXT NOT NULL, tool_name TEXT NOT NULL, request_id TEXT, topic_id TEXT,
                        topic_path TEXT NOT NULL, sha256 TEXT NOT NULL, byte_count INTEGER NOT NULL,
                        char_count INTEGER NOT NULL, is_error INTEGER NOT NULL, created_at REAL NOT NULL,
                        content BLOB NOT NULL, PRIMARY KEY(session_id, artifact_id))""")
                    connection.execute("""CREATE TRIGGER IF NOT EXISTS artifacts_no_update BEFORE UPDATE ON tool_artifacts
                        BEGIN SELECT RAISE(ABORT, 'Tool artifacts are immutable'); END""")
                    connection.execute("""CREATE TRIGGER IF NOT EXISTS artifacts_no_delete BEFORE DELETE ON tool_artifacts
                        BEGIN SELECT RAISE(ABORT, 'Tool artifacts are immutable'); END""")
                    connection.execute("PRAGMA user_version = 4")
                    connection.commit()
                except BaseException:
                    connection.rollback()
                    raise
            if version < 5:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    connection.execute("""CREATE TABLE IF NOT EXISTS memory_progress (
                        session_id TEXT PRIMARY KEY REFERENCES sessions(session_id), last_seq INTEGER NOT NULL)""")
                    connection.execute("""CREATE TABLE IF NOT EXISTS memory_jobs (
                        job_id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(session_id),
                        start_seq INTEGER NOT NULL, end_seq INTEGER NOT NULL,
                        state TEXT NOT NULL CHECK(state IN ('pending','running','applied')),
                        lease_token TEXT, lease_until REAL, attempts INTEGER NOT NULL, last_error TEXT,
                        created_at REAL NOT NULL, UNIQUE(session_id,end_seq))""")
                    connection.execute("""CREATE UNIQUE INDEX IF NOT EXISTS memory_one_open_job
                        ON memory_jobs(session_id) WHERE state!='applied'""")
                    connection.execute("""CREATE TABLE IF NOT EXISTS memory_facts (
                        fact_id TEXT PRIMARY KEY, text TEXT NOT NULL, category TEXT NOT NULL,
                        source_session_id TEXT NOT NULL REFERENCES sessions(session_id), source_refs_json TEXT NOT NULL,
                        state TEXT NOT NULL CHECK(state IN ('active','superseded','retracted')),
                        replacement_id TEXT REFERENCES memory_facts(fact_id),
                        job_id TEXT NOT NULL REFERENCES memory_jobs(job_id), created_at REAL NOT NULL)""")
                    connection.execute("""CREATE TABLE IF NOT EXISTS memory_receipts (
                        job_id TEXT PRIMARY KEY REFERENCES memory_jobs(job_id), lease_token TEXT NOT NULL,
                        payload_hash TEXT NOT NULL, operations_json TEXT NOT NULL,
                        fact_ids_json TEXT NOT NULL, applied_at REAL NOT NULL)""")
                    connection.execute("""CREATE TRIGGER IF NOT EXISTS memory_receipts_no_update BEFORE UPDATE ON memory_receipts
                        BEGIN SELECT RAISE(ABORT, 'Memory receipts are immutable'); END""")
                    connection.execute("""CREATE TRIGGER IF NOT EXISTS memory_receipts_no_delete BEFORE DELETE ON memory_receipts
                        BEGIN SELECT RAISE(ABORT, 'Memory receipts are immutable'); END""")
                    connection.execute("PRAGMA user_version = 5")
                    connection.commit()
                except BaseException:
                    connection.rollback()
                    raise
            if version < 6:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    columns = {row[1] for row in connection.execute("PRAGMA table_info(memory_progress)")}
                    if "flush_through_seq" not in columns:
                        connection.execute("ALTER TABLE memory_progress ADD COLUMN flush_through_seq INTEGER NOT NULL DEFAULT 0")
                    connection.execute("PRAGMA user_version = 6")
                    connection.commit()
                except BaseException:
                    connection.rollback()
                    raise
            yield connection

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def list_records(self) -> list[dict]:
        if not self.path.exists():
            return []
        with self.connection() as connection:
            rows = connection.execute("SELECT record_json FROM sessions WHERE record_json IS NOT NULL").fetchall()
        return [json.loads(row[0]) for row in rows]

    def save_record(self, session_id: str, record: dict, *, exclusive: bool = False) -> None:
        payload = json.dumps(record, ensure_ascii=False, sort_keys=True)
        with self.transaction() as connection:
            if exclusive:
                try:
                    connection.execute("INSERT INTO sessions(session_id, record_json) VALUES (?, ?)",
                                       (session_id, payload))
                except sqlite3.IntegrityError as error:
                    raise RuntimeError(f"Session already exists with id '{session_id}'") from error
            else:
                connection.execute("""INSERT INTO sessions(session_id, record_json) VALUES (?, ?)
                    ON CONFLICT(session_id) DO UPDATE SET record_json=excluded.record_json""",
                                   (session_id, payload))


class SQLiteSessionStorage:
    def __init__(self, path: str | Path, session_id: str, *, record: dict | None = None) -> None:
        if not session_id:
            raise ValueError("Session id cannot be empty")
        self.path = Path(path)
        self.session_id = session_id
        self.database = SQLiteSessionDatabase(self.path)
        self._record_json = json.dumps(record, ensure_ascii=False) if record is not None else None

    @property
    def locator(self) -> str:
        return f"{self.path.resolve()}#session={self.session_id}"

    async def append(self, entry: SessionEntry) -> None:
        await self.append_batch((entry,))

    async def append_batch(self, entries: Sequence[SessionEntry]) -> None:
        encoded = tuple((entry.id, _encode(entry)) for entry in entries)
        await asyncio.to_thread(self._append_encoded, encoded)

    async def append_batch_if_head(self, entries: Sequence[SessionEntry], *, expected_head: str | None) -> None:
        encoded = tuple((entry.id, _encode(entry)) for entry in entries)
        await asyncio.to_thread(self._append_at_head, encoded, expected_head)

    def _append_at_head(self, encoded: tuple[tuple[str, str], ...], expected_head: str | None) -> None:
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT id FROM session_entries WHERE session_id = ? ORDER BY seq DESC LIMIT 1", (self.session_id,),
            ).fetchone()
            if (row[0] if row is not None else None) != expected_head:
                raise ValueError("Session head changed during context compaction")
            self._append_in_transaction(connection, encoded)

    def append_batch_sync(self, entries: Sequence[SessionEntry]) -> None:
        self._append_encoded(tuple((entry.id, _encode(entry)) for entry in entries))

    def _append_encoded(self, encoded: tuple[tuple[str, str], ...]) -> None:
        if not encoded:
            return
        with self.database.transaction() as connection:
            self._append_in_transaction(connection, encoded)

    def _append_in_transaction(self, connection: sqlite3.Connection,
                               encoded: tuple[tuple[str, str], ...]) -> None:
        connection.execute("""INSERT INTO sessions(session_id, record_json) VALUES (?, ?)
            ON CONFLICT(session_id) DO UPDATE SET record_json=COALESCE(sessions.record_json, excluded.record_json)""",
                           (self.session_id, self._record_json))
        seq = connection.execute(
            "SELECT COALESCE(MAX(seq), 0) FROM session_entries WHERE session_id = ?",
            (self.session_id,),
        ).fetchone()[0]
        for entry_id, body in encoded:
            existing = connection.execute(
                "SELECT body_json FROM session_entries WHERE session_id = ? AND id = ?",
                (self.session_id, entry_id),
            ).fetchone()
            if existing is not None:
                if existing[0] != body:
                    raise ValueError(f"Session entry conflict: {entry_id}")
                continue
            seq += 1
            connection.execute(
                "INSERT INTO session_entries(session_id, id, seq, body_json) VALUES (?, ?, ?, ?)",
                (self.session_id, entry_id, seq, body),
            )

    async def import_jsonl(self, source: str | Path) -> None:
        await asyncio.to_thread(self.import_jsonl_sync, source)

    def import_jsonl_sync(self, source: str | Path) -> None:
        source_path = Path(source).resolve()
        data = source_path.read_bytes()
        entries = entries_from_json_lines(data.decode("utf-8-sig").splitlines())
        encoded = tuple((entry.id, _encode(entry)) for entry in entries)
        with self.database.transaction() as connection:
            self._append_in_transaction(connection, encoded)
            connection.execute("""INSERT OR IGNORE INTO legacy_imports
                (session_id, source_path, sha256, imported_at) VALUES (?, ?, ?, ?)""",
                               (self.session_id, str(source_path), sha256(data).hexdigest(), time()))

    async def read_all(self) -> list[SessionEntry]:
        return await asyncio.to_thread(self.read_all_sync)

    def read_all_sync(self) -> list[SessionEntry]:
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT body_json FROM session_entries WHERE session_id = ? ORDER BY seq",
                (self.session_id,),
            ).fetchall()
        return [entry_from_json_line(row[0]) for row in rows]

    async def read_entry(self, entry_id: str) -> SessionEntry | None:
        return await asyncio.to_thread(self._read_entry, entry_id)

    def _read_entry(self, entry_id: str) -> SessionEntry | None:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT body_json FROM session_entries WHERE session_id = ? AND id = ?",
                (self.session_id, entry_id),
            ).fetchone()
        return entry_from_json_line(row[0]) if row is not None else None
