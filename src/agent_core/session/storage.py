from __future__ import annotations

import asyncio
import os
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import BinaryIO, Protocol

from agent_core.session.entries import SessionEntry
from agent_core.session.jsonl import entries_from_json_lines, entry_to_json_line


class SessionStorage(Protocol):


    async def append(self, entry: SessionEntry) -> None:

        ...

    # 批量提交是事务边界，不能只写入同一批消息中的一部分。
    async def append_batch(self, entries: Sequence[SessionEntry]) -> None:

        ...

    async def read_all(self) -> list[SessionEntry]:

        ...


class JsonlSessionStorage:


    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.lock_path = self.path.with_name(f".{self.path.name}.lock")
        self.temp_path = self.path.with_name(f".{self.path.name}.tmp")

    async def append(self, entry: SessionEntry) -> None:

        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._locked(exclusive=True):
            self._remove_incomplete_temp()
            with self.path.open("ab") as file:
                file.write(entry_to_json_line(entry).encode("utf-8"))
                file.flush()
                os.fsync(file.fileno())

    async def append_batch(self, entries: Sequence[SessionEntry]) -> None:

        batch = tuple(entries)
        if not batch:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        encoded = b"".join(entry_to_json_line(entry).encode("utf-8") for entry in batch)
        with self._locked(exclusive=True):
            self._remove_incomplete_temp()
            previous = self.path.read_bytes() if self.path.exists() else b""
            data = previous + encoded
            self._atomic_replace(data)

    async def read_all(self) -> list[SessionEntry]:

        self.path.parent.mkdir(parents=True, exist_ok=True)


        if self.temp_path.exists():
            with self._locked(exclusive=True):
                self._remove_incomplete_temp()
                return self._read_unlocked()
        with self._locked(exclusive=False):
            return self._read_unlocked()

    def _read_unlocked(self) -> list[SessionEntry]:
        if not self.path.exists():
            return []
        return entries_from_json_lines(self.path.read_text(encoding="utf-8").split("\n"))

    # 先写入并同步临时文件，再原子替换正式日志，避免半份会话成为当前状态。
    def _atomic_replace(self, data: bytes) -> None:
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{self.path.name}.",
            suffix=".tmp",
            dir=self.path.parent,
        )


        try:
            os.close(descriptor)
            temporary_path = Path(temporary)
            with temporary_path.open("wb") as file:
                file.write(data)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary_path, self.path)
            _fsync_directory(self.path.parent)
        except BaseException:
            with _suppress_os_error():
                Path(temporary).unlink()
            raise

    def _remove_incomplete_temp(self) -> None:
        with _suppress_os_error():
            self.temp_path.unlink()


        prefix = f".{self.path.name}."
        for candidate in self.path.parent.glob(f"{prefix}*.tmp"):
            with _suppress_os_error():
                candidate.unlink()

    @contextmanager
    def _locked(self, *, exclusive: bool) -> Iterator[None]:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+b") as lock_file:
            os.chmod(self.lock_path, 0o600)
            _lock_file(lock_file, exclusive=exclusive)
            try:
                yield
            finally:
                _unlock_file(lock_file)


class InMemorySessionStorage:


    def __init__(self, entries: Sequence[SessionEntry] = ()) -> None:
        self.entries = list(entries)
        self._lock = asyncio.Lock()

    async def append(self, entry: SessionEntry) -> None:
        async with self._lock:
            self.entries.append(entry)

    async def append_batch(self, entries: Sequence[SessionEntry]) -> None:
        async with self._lock:
            self.entries.extend(entries)

    async def read_all(self) -> list[SessionEntry]:
        async with self._lock:
            return list(self.entries)


@contextmanager
def _suppress_os_error() -> Iterator[None]:
    with suppress(OSError):
        yield


def _lock_file(file: BinaryIO, *, exclusive: bool) -> None:

    if os.name == "nt":
        import msvcrt


        del exclusive
        msvcrt.locking(file.fileno(), msvcrt.LK_LOCK, 1)
        return
    import fcntl

    mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
    fcntl.flock(file.fileno(), mode)


def _unlock_file(file: BinaryIO) -> None:
    if os.name == "nt":
        import msvcrt

        file.seek(0)
        msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(file.fileno(), fcntl.LOCK_UN)


def _fsync_directory(path: Path) -> None:

    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        with suppress(OSError):
            os.fsync(descriptor)
    finally:
        os.close(descriptor)


__all__ = ["InMemorySessionStorage", "JsonlSessionStorage", "SessionStorage"]
