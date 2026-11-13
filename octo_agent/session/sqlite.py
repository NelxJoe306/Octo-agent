from __future__ import annotations

import asyncio
from collections.abc import Iterator, Sequence
from contextlib import closing, contextmanager
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from time import time

from octo_agent.session.entries import SessionEntry
from octo_agent.session.jsonl import entries_from_json_lines, entry_from_json_line, entry_to_json_line


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
            if version not in (0, 1, 2, 3):
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
