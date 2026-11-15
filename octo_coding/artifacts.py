from __future__ import annotations

from collections.abc import Mapping, Sequence
import json

from pydantic import BaseModel, ConfigDict, Field

from octo_agent.messages import ImageContent, TextContent, ToolCall
from octo_agent.session.artifacts import MAX_ARTIFACT_READ_BYTES, MAX_ARTIFACT_OFFSET, ToolArtifactStore
from octo_agent.session.topics import TopicRequest
from octo_agent.tools import AgentTool, AgentToolResult
from octo_agent.types import JSONValue

SPILL_BYTES = 8192
PREVIEW_BYTES = 1024
READ_ARTIFACT_TOOL = 'read_artifact'


class ReadArtifactArguments(BaseModel):
    model_config = ConfigDict(extra='forbid')
    artifact_id: str = Field(pattern=r'^art_[0-9a-f]{64}$')
    offset: int = Field(default=0, ge=0, le=MAX_ARTIFACT_OFFSET, strict=True)
    limit: int = Field(default=2048, ge=4, le=MAX_ARTIFACT_READ_BYTES, strict=True)


class ArtifactTools:
    def __init__(self, store: ToolArtifactStore) -> None:
        self.store = store

    def include_read_tool(self, tools: Sequence[AgentTool]) -> list[AgentTool]:
        if any(tool.name == READ_ARTIFACT_TOOL for tool in tools):
            raise ValueError('read_artifact is a reserved session tool name')
        return [*tools, self.read_tool()]

    def read_tool(self) -> AgentTool:
        async def execute(tool_call_id: str, arguments: Mapping[str, JSONValue], signal=None, on_update=None):
            parsed = ReadArtifactArguments.model_validate(arguments)
            if signal is not None and signal.is_cancelled():
                raise ValueError('Artifact read cancelled')
            chunk = await self.store.read(parsed.artifact_id, offset=parsed.offset, limit=parsed.limit)
            metadata = {'artifact_id': chunk.reference.id, 'offset': chunk.offset,
                        'next_offset': chunk.next_offset, 'total_bytes': chunk.reference.byte_count,
                        'tool_name': chunk.reference.tool_name, 'tool_call_id': chunk.reference.tool_call_id,
                        'topic_id': chunk.reference.topic_id, 'request_id': chunk.reference.request_id}
            return AgentToolResult(content=json.dumps(metadata, ensure_ascii=False) + '\n' + chunk.text,
                                   details=metadata)

        return AgentTool(name=READ_ARTIFACT_TOOL, label='Read artifact',
            description='Read a bounded part of a persistent tool output in the current session. '
                        'Use artifact IDs from output previews. offset and limit are UTF-8 bytes; '
                        'follow next_offset to continue. At most 4096 bytes per read. IDs survive resume and compaction.',
            parameters=ReadArtifactArguments.model_json_schema(), execute_fn=execute)

    async def externalize(self, call: ToolCall, result: AgentToolResult, is_error: bool, *,
                          request: TopicRequest | None = None) -> tuple[AgentToolResult, bool]:
        if call.name == READ_ARTIFACT_TOOL:
            return result, is_error
        text = result.unabridged_text if result.unabridged_text is not None else result.text
        data = text.encode('utf-8')
        if result.unabridged_text is None and len(data) <= SPILL_BYTES:
            return result, is_error
        images = [block for block in result.content if isinstance(block, ImageContent)]
        try:
            reference = await self.store.put(call, text, request=request, is_error=is_error)
        except Exception as error:
            failure = (f'Tool {call.name} has already executed, but its full output could not be persisted '
                       f'({type(error).__name__}). No artifact reference was created. Do not assume the command did not run.')
            failed = result.model_copy(update={'content': [TextContent(text=failure), *images],
                'unabridged_text': None, 'is_error': True,
                'details': {'artifact_error': type(error).__name__, 'execution_is_error': is_error,
                            'original_details': result.details}})
            return failed, True
        head = data[:PREVIEW_BYTES // 2].decode('utf-8', errors='ignore')
        tail = data[-PREVIEW_BYTES // 2:].decode('utf-8', errors='ignore')
        preview = (f'[Full output saved as artifact {reference.id}; {reference.byte_count} UTF-8 bytes. '
                   'Read a bounded slice with read_artifact(artifact_id, offset, limit).]\n'
                   + head + '\n[... output omitted; complete text is persisted ...]\n' + tail)
        metadata = {'id': reference.id, 'sha256': reference.sha256, 'byte_count': reference.byte_count,
                    'char_count': reference.char_count, 'request_id': reference.request_id,
                    'topic_id': reference.topic_id}
        details = dict(result.details) if isinstance(result.details, dict) else {'original_details': result.details}
        truncation = details.get('truncation')
        if isinstance(truncation, dict):
            details['truncation'] = {key: value for key, value in truncation.items() if key != 'content'}
        details['artifact'] = metadata
        return result.model_copy(update={'content': [TextContent(text=preview), *images],
            'details': details, 'unabridged_text': None, 'is_error': is_error}), is_error
