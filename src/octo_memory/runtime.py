from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from contextlib import suppress
import json
from os import environ
from pathlib import Path
import re

from pydantic import BaseModel, ConfigDict, Field

from octo_agent.context_budget import estimate_budget_text
from octo_memory import MemoryConflict, MemoryFact, MemoryJob, MemoryOperation, MemorySource, MemoryStore


class MemorySettings(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    enabled: bool = Field(default=True, strict=True)
    message_threshold: int = Field(default=32, ge=1, le=1000, strict=True)
    request_timeout: float = Field(default=60, ge=1, le=600, allow_inf_nan=False)
    close_timeout: float = Field(default=3, ge=0.05, le=30, allow_inf_nan=False)


MemoryExtractor = Callable[[MemoryJob, tuple[MemorySource, ...], tuple[MemoryFact, ...]], Awaitable[Sequence[MemoryOperation]]]


def explicit_memory_request(text: str) -> bool:
    return bool(re.search(r'记住|记一下|记下来|更正|纠正|忘掉|不要再记|\bremember\b|\bforget\b|\bcorrect\b', text, re.IGNORECASE))


def load_memory_settings(path: Path) -> MemorySettings:
    data = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    if not isinstance(data, dict):
        raise ValueError('settings.json must be an object')
    settings = MemorySettings.model_validate(data.get('memory', {}))
    disabled = environ.get('OCTO_DISABLE_MEMORY', '0')
    if disabled not in ('0', '1'):
        raise ValueError('OCTO_DISABLE_MEMORY must be 0 or 1')
    return settings.model_copy(update={'enabled': False}) if disabled == '1' else settings


def source_data(source: MemorySource) -> dict:
    ref = source.reference
    return {'entry_id': ref.entry_id, 'session_id': ref.session_id, 'timestamp': ref.timestamp,
            'role': ref.role, 'request_id': ref.request_id, 'topic_id': ref.topic_id,
            'topic_path': ref.topic_path, 'text': source.message.text,
            'read_scope': 'original visible text only; images, thinking and external artifact bodies not read'}


def fact_data(fact: MemoryFact) -> dict:
    return {'id': fact.id, 'text': fact.text, 'category': fact.category,
            'sources': [{'session_id': source.session_id, 'entry_id': source.entry_id,
                         'timestamp': source.timestamp} for source in fact.sources]}


class MemoryRuntime:
    def __init__(self, store: MemoryStore, session_id: str, extractor: MemoryExtractor, settings: MemorySettings,
                 *, source_budget: Callable[[], int] = lambda: 8000) -> None:
        self.store = store
        self.session_id = session_id
        self.extractor = extractor
        self.settings = settings
        self.source_budget = source_budget
        self.last_error: str | None = None
        self._wake = asyncio.Event()
        self._idle = asyncio.Event()
        self._idle.set()
        self._task: asyncio.Task | None = None
        self._closed = False
        self._force_requested = False
        self._failures: dict[str, int] = {}
        self._retry_at: dict[str, float] = {}

    def notify(self, *, force: bool = False) -> None:
        if self._closed or not self.settings.enabled:
            return
        self._force_requested |= force
        self._idle.clear()
        self._wake.set()
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name='octo-memory-worker')

    async def wait_idle(self) -> None:
        await self._idle.wait()

    async def _run(self) -> None:
        try:
            while not self._closed:
                with suppress(TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), 30)
                self._wake.clear()
                self._idle.clear()
                try:
                    await self._cycle()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.last_error = f'{type(exc).__name__}: {exc}'[:2000]
                finally:
                    if not self._wake.is_set():
                        self._idle.set()
        finally:
            self._idle.set()

    async def _trigger_local(self) -> bool:
        sources = await self.store.unlearned(self.session_id, limit=max(32, self.settings.message_threshold))
        explicit = any(source.reference.role == 'user' and explicit_memory_request(source.message.text) for source in sources)
        if not sources or (not explicit and len(sources) < self.settings.message_threshold):
            return False
        target = await self.store.request_flush(self.session_id)
        return await self._enqueue_range(self.session_id, target)

    async def _enqueue_range(self, session_id: str, through_seq: int) -> bool:
        if await self.store.pending_job(session_id) is not None:
            return False
        sources = await self.store.unlearned(session_id, limit=32, through_seq=through_seq)
        if not sources:
            return False
        selected = []
        tokens = 0
        for source in sources[:32]:
            cost = estimate_budget_text(json.dumps(source_data(source), ensure_ascii=False))
            if selected and tokens + cost > self.source_budget():
                break
            selected.append(source)
            tokens += cost
        try:
            await self.store.enqueue(session_id, selected[-1].reference.entry_id)
        except MemoryConflict:
            return False
        return True

    async def _cycle(self) -> None:
        while not self._closed:
            if self._force_requested:
                await self.store.request_flush(self.session_id)
                self._force_requested = False
            now = asyncio.get_running_loop().time()
            ready = [job for job in await self.store.ready_jobs()
                     if self._failures.get(job.id, 0) < 3 and self._retry_at.get(job.id, 0) <= now]
            if ready:
                await self._process(ready[0])
                continue
            enqueued = False
            for session_id, target in await self.store.flush_targets():
                if await self._enqueue_range(session_id, target):
                    enqueued = True
                    break
            if enqueued:
                continue
            if not await self._trigger_local():
                return

    async def _process(self, job: MemoryJob) -> None:
        lease = None
        try:
            lease = await self.store.claim(job.id, lease_seconds=self.settings.request_timeout + 30)
            sources = await self.store.sources(job.id)
            preferences = await self.store.facts(category='preference', limit=100)
            local = await self.store.facts(source_session_id=job.session_id, limit=100)
            facts = tuple({fact.id: fact for fact in (*preferences, *local)}.values())
            async with asyncio.timeout(self.settings.request_timeout):
                operations = await self.extractor(job, sources, facts)
            # 校验和落库仍走唯一存储入口；模型失败不会推进学习进度。
            await self.store.apply(lease, operations)
            self.last_error = None
            self._failures.pop(job.id, None)
            self._retry_at.pop(job.id, None)
        except asyncio.CancelledError:
            if lease is not None:
                with suppress(Exception):
                    await self.store.release(lease, 'Memory worker cancelled; extraction not committed')
            raise
        except Exception as exc:
            self.last_error = f'{type(exc).__name__}: {exc}'[:2000]
            self._failures[job.id] = self._failures.get(job.id, 0) + 1
            self._retry_at[job.id] = asyncio.get_running_loop().time() + 30
            if lease is not None:
                with suppress(Exception):
                    await self.store.release(lease, self.last_error)

    async def recall(self, *, max_tokens: int = 1024) -> str:
        selected = []
        for fact in await self.store.facts(category='preference', limit=100):
            candidate = json.dumps([*selected, fact_data(fact)], ensure_ascii=False)
            if estimate_budget_text(candidate) <= max_tokens:
                selected.append(fact_data(fact))
        return json.dumps(selected, ensure_ascii=False) if selected else ''

    async def pause(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def aclose(self, *, flush: bool = True) -> None:
        if self._closed:
            return
        # 先持久化尾部任务，再给网络提炼有限的退出等待时间。
        try:
            if flush and self.settings.enabled:
                await self.store.request_flush(self.session_id)
                self.notify()
                with suppress(TimeoutError):
                    await asyncio.wait_for(self.wait_idle(), self.settings.close_timeout)
        except Exception as exc:
            self.last_error = f'{type(exc).__name__}: {exc}'[:2000]
        finally:
            self._closed = True
            if self._task is not None:
                self._task.cancel()
                with suppress(asyncio.CancelledError):
                    await self._task
