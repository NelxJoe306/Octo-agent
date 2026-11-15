from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import asdict
from hashlib import sha256
import json
import math
import sqlite3
from time import time
from uuid import uuid4

from long_term_memory.models import (
    MemoryOperation, MemoryConflict, MemoryFact, MemoryJob, MemoryLease, MemoryReceipt,
    MemorySource, SourceReference, operation_adapter,
)
from agent_core.session.entries import MessageEntry
from agent_core.session.jsonl import entry_from_json_line
from agent_core.session.sqlite import SQLiteSessionDatabase


class MemoryStore:
    def __init__(self, database: SQLiteSessionDatabase, *, clock: Callable[[], float] = time) -> None:
        self.database = database
        self.clock = clock

    async def enqueue(self, session_id: str, through_entry_id: str) -> MemoryJob:
        return await asyncio.to_thread(self._enqueue, session_id, through_entry_id)

    def _enqueue(self, session_id: str, through_entry_id: str) -> MemoryJob:
        with self.database.transaction() as connection:
            row = connection.execute('SELECT seq FROM session_entries WHERE session_id=? AND id=?',
                                     (session_id, through_entry_id)).fetchone()
            if row is None:
                raise ValueError('Memory endpoint not found in this session')
            end = row[0]
            existing = connection.execute('SELECT job_id FROM memory_jobs WHERE session_id=? AND end_seq=?',
                                          (session_id, end)).fetchone()
            if existing is not None:
                return self._job(connection, existing[0])
            pending = connection.execute("SELECT job_id FROM memory_jobs WHERE session_id=? AND state!='applied'",
                                         (session_id,)).fetchone()
            if pending is not None:
                raise MemoryConflict('Finish the existing memory job before enqueueing a new range')
            connection.execute('INSERT OR IGNORE INTO memory_progress(session_id,last_seq) VALUES (?,0)', (session_id,))
            cursor = connection.execute('SELECT last_seq FROM memory_progress WHERE session_id=?', (session_id,)).fetchone()[0]
            if end <= cursor:
                raise MemoryConflict('Memory endpoint has already been learned')
            job_id = 'memjob_' + uuid4().hex
            connection.execute('''INSERT INTO memory_jobs
                (job_id,session_id,start_seq,end_seq,state,attempts,created_at) VALUES (?,?,?,?,'pending',0,?)''',
                (job_id, session_id, cursor + 1, end, self.clock()))
            return self._job(connection, job_id)

    @staticmethod
    def _job(connection: sqlite3.Connection, job_id: str) -> MemoryJob:
        row = connection.execute('''SELECT job_id,session_id,start_seq,end_seq,state,attempts,lease_until,last_error
            FROM memory_jobs WHERE job_id=?''', (job_id,)).fetchone()
        if row is None:
            raise ValueError('Memory job not found')
        return MemoryJob(*row)

    async def get_job(self, job_id: str) -> MemoryJob:
        return await asyncio.to_thread(self._get_job, job_id)

    def _get_job(self, job_id: str) -> MemoryJob:
        with self.database.connection() as connection:
            return self._job(connection, job_id)

    async def ready_jobs(self, *, limit: int = 100) -> tuple[MemoryJob, ...]:
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError('limit must be between 1 and 1000')
        return await asyncio.to_thread(self._ready_jobs, limit)

    def _ready_jobs(self, limit: int) -> tuple[MemoryJob, ...]:
        with self.database.connection() as connection:
            rows = connection.execute('''SELECT job_id FROM memory_jobs
                WHERE state='pending' OR (state='running' AND lease_until<=?) ORDER BY created_at,job_id LIMIT ?''',
                (self.clock(), limit)).fetchall()
            return tuple(self._job(connection, row[0]) for row in rows)

    async def request_flush(self, session_id: str) -> int:
        return await asyncio.to_thread(self._request_flush, session_id)

    def _request_flush(self, session_id: str) -> int:
        with self.database.transaction() as connection:
            target = connection.execute("""SELECT COALESCE(MAX(seq),0) FROM session_entries
                WHERE session_id=? AND json_extract(body_json,'$.type')='message'""", (session_id,)).fetchone()[0]
            if target:
                connection.execute("""INSERT INTO memory_progress(session_id,last_seq,flush_through_seq) VALUES (?,0,?)
                    ON CONFLICT(session_id) DO UPDATE SET flush_through_seq=MAX(memory_progress.flush_through_seq,excluded.flush_through_seq)""",
                    (session_id,target))
            return target

    async def flush_targets(self) -> tuple[tuple[str,int], ...]:
        return await asyncio.to_thread(self._flush_targets)

    def _flush_targets(self) -> tuple[tuple[str,int], ...]:
        with self.database.connection() as connection:
            return tuple(connection.execute("""SELECT session_id,flush_through_seq FROM memory_progress
                WHERE last_seq<flush_through_seq ORDER BY session_id""").fetchall())

    async def pending_job(self, session_id: str) -> MemoryJob | None:
        return await asyncio.to_thread(self._pending_job, session_id)

    def _pending_job(self, session_id: str) -> MemoryJob | None:
        with self.database.connection() as connection:
            row = connection.execute("SELECT job_id FROM memory_jobs WHERE session_id=? AND state!='applied'", (session_id,)).fetchone()
            return self._job(connection, row[0]) if row else None

    async def unlearned(self, session_id: str, *, limit: int = 100, through_seq: int = 9223372036854775807) -> tuple[MemorySource, ...]:
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError('limit must be between 1 and 1000')
        if type(through_seq) is not int or not 0 <= through_seq <= 9223372036854775807:
            raise ValueError('Invalid memory flush endpoint')
        return await asyncio.to_thread(self._unlearned, session_id, limit, through_seq)

    def _unlearned(self, session_id: str, limit: int, through_seq: int) -> tuple[MemorySource, ...]:
        with self.database.connection() as connection:
            row = connection.execute('SELECT last_seq FROM memory_progress WHERE session_id=?', (session_id,)).fetchone()
            cursor = row[0] if row else 0
            rows = connection.execute("""SELECT seq FROM session_entries WHERE session_id=? AND seq>?
                AND seq<=? AND json_extract(body_json,'$.type')='message' ORDER BY seq LIMIT ?""", (session_id, cursor, through_seq, limit)).fetchall()
            if not rows:
                return ()
            return self._sources(connection, MemoryJob('', session_id, cursor + 1, rows[-1][0], 'pending', 0, None, None))

    async def progress(self, session_id: str) -> int:
        return await asyncio.to_thread(self._progress, session_id)

    def _progress(self, session_id: str) -> int:
        with self.database.connection() as connection:
            row = connection.execute('SELECT last_seq FROM memory_progress WHERE session_id=?', (session_id,)).fetchone()
            return row[0] if row else 0

    async def claim(self, job_id: str, *, lease_seconds: float = 300) -> MemoryLease:
        if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, (int, float)) or not math.isfinite(lease_seconds) or not 0 < lease_seconds <= 3600:
            raise ValueError('lease_seconds must be finite and between 0 and 3600')
        return await asyncio.to_thread(self._claim, job_id, lease_seconds)

    def _claim(self, job_id: str, lease_seconds: float) -> MemoryLease:
        with self.database.transaction() as connection:
            job = self._job(connection, job_id)
            now = self.clock()
            if job.state == 'applied' or (job.state == 'running' and job.lease_until > now):
                raise MemoryConflict('Memory job is already applied or leased')
            token = uuid4().hex
            connection.execute("""UPDATE memory_jobs SET state='running',lease_token=?,lease_until=?,attempts=attempts+1
                WHERE job_id=?""", (token, now + lease_seconds, job_id))
            return MemoryLease(self._job(connection, job_id), token)

    async def sources(self, job_id: str) -> tuple[MemorySource, ...]:
        return await asyncio.to_thread(self._read_sources, job_id)

    def _read_sources(self, job_id: str) -> tuple[MemorySource, ...]:
        with self.database.connection() as connection:
            return self._sources(connection, self._job(connection, job_id))

    @staticmethod
    def _sources(connection: sqlite3.Connection, job: MemoryJob) -> tuple[MemorySource, ...]:
        rows = connection.execute('''SELECT e.body_json,r.request_id,r.topic_id,r.topic_path FROM session_entries e
            LEFT JOIN entry_requests a ON a.session_id=e.session_id AND a.entry_id=e.id
            LEFT JOIN topic_requests r ON r.session_id=a.session_id AND r.request_id=a.request_id
            WHERE e.session_id=? AND e.seq BETWEEN ? AND ? ORDER BY e.seq''',
            (job.session_id, job.start_seq, job.end_seq)).fetchall()
        sources = []
        for body, request_id, topic_id, path in rows:
            entry = entry_from_json_line(body)
            if isinstance(entry, MessageEntry):
                reference = SourceReference(job.session_id, entry.id, entry.timestamp, entry.message.role,
                                            request_id, topic_id, tuple(json.loads(path)) if path else ())
                sources.append(MemorySource(reference, entry.message))
        return tuple(sources)

    async def apply(self, lease: MemoryLease, operations: Sequence[MemoryOperation]) -> MemoryReceipt:
        validated = tuple(operation_adapter.validate_python(operation) for operation in operations)
        payload = json.dumps([operation.model_dump(mode='json') for operation in validated],
                             ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        return await asyncio.to_thread(self._apply, lease, payload)

    def _apply(self, lease: MemoryLease, payload: str) -> MemoryReceipt:
        fingerprint = sha256(payload.encode()).hexdigest()
        with self.database.transaction() as connection:
            receipt = connection.execute('SELECT lease_token,payload_hash FROM memory_receipts WHERE job_id=?',
                                         (lease.job.id,)).fetchone()
            if receipt is not None:
                if receipt != (lease.token, fingerprint):
                    raise MemoryConflict('Memory job already committed with a different lease or payload')
                return self._receipt(connection, lease.job.id)
            job = self._check_lease(connection, lease)
            sources = {source.reference.entry_id: source.reference for source in self._sources(connection, job)}
            fact_ids = []
            for index, operation in enumerate(json.loads(payload)):
                evidence = tuple(sources.get(entry_id) for entry_id in operation['source_entry_ids'])
                if not evidence or any(source is None for source in evidence):
                    raise ValueError('Evidence must reference original messages in this job')
                category = operation.get('category')
                if operation['kind'] != 'add':
                    target = connection.execute('SELECT category,state FROM memory_facts WHERE fact_id=?',
                                                (operation['target_fact_id'],)).fetchone()
                    if target is None or target[1] != 'active':
                        raise MemoryConflict('Memory correction target must be an active fact')
                    category = target[0]
                if category == 'preference' and any(source.role != 'user' for source in evidence):
                    raise ValueError('Preferences require original user-message evidence')
                if operation['kind'] == 'retract':
                    connection.execute("UPDATE memory_facts SET state='retracted' WHERE fact_id=?", (operation['target_fact_id'],))
                    continue
                fact_id = 'fact_' + sha256(f'{job.id}:{index}'.encode()).hexdigest()
                connection.execute('''INSERT INTO memory_facts
                    (fact_id,text,category,source_session_id,source_refs_json,state,job_id,created_at)
                    VALUES (?,?,?,?,?,'active',?,?)''',
                    (fact_id, operation['text'], category, job.session_id,
                     json.dumps([asdict(source) for source in evidence], ensure_ascii=False), job.id, self.clock()))
                if operation['kind'] == 'supersede':
                    connection.execute("UPDATE memory_facts SET state='superseded',replacement_id=? WHERE fact_id=?",
                                       (fact_id, operation['target_fact_id']))
                fact_ids.append(fact_id)
            # 事实、学习进度和收据同事务提交，失败时一起回滚。
            connection.execute('UPDATE memory_progress SET last_seq=? WHERE session_id=?', (job.end_seq, job.session_id))
            connection.execute("UPDATE memory_jobs SET state='applied',lease_until=NULL,last_error=NULL WHERE job_id=?", (job.id,))
            connection.execute('''INSERT INTO memory_receipts
                (job_id,lease_token,payload_hash,operations_json,fact_ids_json,applied_at) VALUES (?,?,?,?,?,?)''',
                (job.id, lease.token, fingerprint, payload, json.dumps(fact_ids), self.clock()))
            return self._receipt(connection, job.id)

    def _check_lease(self, connection: sqlite3.Connection, lease: MemoryLease) -> MemoryJob:
        job = self._job(connection, lease.job.id)
        token = connection.execute('SELECT lease_token FROM memory_jobs WHERE job_id=?', (job.id,)).fetchone()[0]
        if job.state != 'running' or token != lease.token or job.lease_until <= self.clock():
            raise MemoryConflict('Memory lease is stale or expired')
        cursor = connection.execute('SELECT last_seq FROM memory_progress WHERE session_id=?', (job.session_id,)).fetchone()[0]
        if cursor != job.start_seq - 1:
            raise MemoryConflict('Memory progress no longer matches this job')
        return job

    async def release(self, lease: MemoryLease, error: str) -> MemoryJob:
        if not isinstance(error, str):
            raise ValueError('error must be a string')
        return await asyncio.to_thread(self._release, lease, error[:2000])

    def _release(self, lease: MemoryLease, error: str) -> MemoryJob:
        with self.database.transaction() as connection:
            job = self._check_lease(connection, lease)
            connection.execute("""UPDATE memory_jobs SET state='pending',lease_token=NULL,lease_until=NULL,last_error=?
                WHERE job_id=?""", (error, job.id))
            return self._job(connection, job.id)

    async def facts(self, *, source_session_id: str | None = None, include_inactive: bool = False, category: str | None = None, limit: int | None = None) -> tuple[MemoryFact, ...]:
        if category not in (None, 'fact', 'preference') or (limit is not None and (type(limit) is not int or not 1 <= limit <= 1000)):
            raise ValueError('Invalid memory fact query')
        return await asyncio.to_thread(self._facts, source_session_id, include_inactive, category, limit)

    def _facts(self, session_id: str | None, include_inactive: bool, category: str | None, limit: int | None) -> tuple[MemoryFact, ...]:
        with self.database.connection() as connection:
            rows = connection.execute('''SELECT fact_id,text,category,source_refs_json,state,replacement_id FROM memory_facts
                WHERE (? IS NULL OR source_session_id=?) AND (? OR state='active') AND (? IS NULL OR category=?) ORDER BY created_at DESC,fact_id LIMIT ?''',
                (session_id, session_id, include_inactive, category, category, limit if limit is not None else -1)).fetchall()
        return tuple(MemoryFact(row[0], row[1], row[2], tuple(SourceReference(**{**source, 'topic_path': tuple(source['topic_path'])})
                     for source in json.loads(row[3])), row[4], row[5]) for row in rows)

    async def receipt(self, job_id: str) -> MemoryReceipt | None:
        return await asyncio.to_thread(self._read_receipt, job_id)

    def _read_receipt(self, job_id: str) -> MemoryReceipt | None:
        with self.database.connection() as connection:
            self._job(connection, job_id)
            return self._receipt(connection, job_id)

    @staticmethod
    def _receipt(connection: sqlite3.Connection, job_id: str) -> MemoryReceipt | None:
        row = connection.execute('SELECT fact_ids_json,operations_json,applied_at FROM memory_receipts WHERE job_id=?',
                                 (job_id,)).fetchone()
        return MemoryReceipt(job_id, tuple(json.loads(row[0])), tuple(operation_adapter.validate_python(item)
                             for item in json.loads(row[1])), row[2]) if row else None
