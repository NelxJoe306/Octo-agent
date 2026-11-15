from __future__ import annotations

import asyncio
import codecs
from dataclasses import dataclass
from hashlib import sha256
import json
import re
from time import time

from octo_agent.messages import ToolCall
from octo_agent.session.sqlite import SQLiteSessionStorage
from octo_agent.session.topics import TopicRequest

MAX_ARTIFACT_READ_BYTES = 4096
MAX_ARTIFACT_OFFSET = 9223372036854775806


@dataclass(frozen=True, slots=True)
class ArtifactReference:
    id: str
    tool_call_id: str
    tool_name: str
    request_id: str | None
    topic_id: str | None
    topic_path: tuple[str, ...]
    sha256: str
    byte_count: int
    char_count: int
    is_error: bool


@dataclass(frozen=True, slots=True)
class ArtifactSlice:
    text: str
    reference: ArtifactReference
    offset: int
    next_offset: int | None


class ToolArtifactStore:
    def __init__(self, storage: SQLiteSessionStorage) -> None:
        self.storage = storage

    async def put(self, call: ToolCall, text: str, *, request: TopicRequest | None = None,
                  is_error: bool = False) -> ArtifactReference:
        return await asyncio.to_thread(self._put, call, text, request, is_error)

    def _put(self, call: ToolCall, text: str, request: TopicRequest | None, is_error: bool) -> ArtifactReference:
        data = text.encode('utf-8')
        digest = sha256(data).hexdigest()
        identity = json.dumps([self.storage.session_id, call.id, call.name,
                               request.id if request else None, request.topic_id if request else None, request.path if request else (), digest, is_error], separators=(',', ':'))
        reference = ArtifactReference('art_' + sha256(identity.encode()).hexdigest(), call.id, call.name,
            request.id if request else None, request.topic_id if request else None,
            request.path if request else (), digest, len(data), len(text), is_error)
        with self.storage.database.transaction() as connection:
            self.storage._append_in_transaction(connection, ())
            connection.execute('''INSERT OR IGNORE INTO tool_artifacts
                (session_id,artifact_id,tool_call_id,tool_name,request_id,topic_id,topic_path,
                 sha256,byte_count,char_count,is_error,created_at,content)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''', (self.storage.session_id, reference.id, call.id, call.name,
                reference.request_id, reference.topic_id, json.dumps(reference.topic_path, ensure_ascii=False),
                digest, len(data), len(text), int(is_error), time(), data))
        return reference

    async def read(self, artifact_id: str, *, offset: int = 0, limit: int = 2048) -> ArtifactSlice:
        if not isinstance(artifact_id, str) or re.fullmatch(r'art_[0-9a-f]{64}', artifact_id) is None:
            raise ValueError('Invalid artifact ID')
        if type(offset) is not int or not 0 <= offset <= MAX_ARTIFACT_OFFSET:
            raise ValueError('offset must be a supported nonnegative byte offset')
        if type(limit) is not int or not 4 <= limit <= MAX_ARTIFACT_READ_BYTES:
            raise ValueError(f'limit must be between 4 and {MAX_ARTIFACT_READ_BYTES} UTF-8 bytes')
        return await asyncio.to_thread(self._read, artifact_id, offset, limit)

    def _read(self, artifact_id: str, offset: int, limit: int) -> ArtifactSlice:
        with self.storage.database.connection() as connection:
            row = connection.execute('''SELECT tool_call_id,tool_name,request_id,topic_id,topic_path,
                sha256,byte_count,char_count,is_error,substr(content,?,?) FROM tool_artifacts
                WHERE session_id=? AND artifact_id=?''',
                (offset + 1, limit + 3, self.storage.session_id, artifact_id)).fetchone()
        if row is None:
            raise ValueError('Artifact not found in this session')
        tool_call_id, tool_name, request_id, topic_id, path, digest, size, chars, is_error, data = row
        if offset > size:
            raise ValueError('offset exceeds artifact size')
        reference = ArtifactReference(artifact_id, tool_call_id, tool_name, request_id, topic_id,
                                      tuple(json.loads(path)), digest, size, chars, bool(is_error))
        skipped = 0
        while skipped < len(data) and data[skipped] & 0xc0 == 0x80:
            skipped += 1
        start = offset + skipped
        # 按字节回读，但只返回完整 UTF-8 字符；NUL 同样保留。
        text = codecs.getincrementaldecoder('utf-8')().decode(data[skipped:skipped + limit], final=False)
        end = start + len(text.encode('utf-8'))
        return ArtifactSlice(text, reference, start, end if end < size else None)
