from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from agent_core.context_budget import estimate_budget_messages
from agent_core.context_harness import validate_model_context
from agent_core.messages import AgentMessage, AssistantMessage, UserMessage

COMPACTION_STATE_NAMESPACE = "octo.context-compaction"
TRIM_NOTICE = "Older context was omitted without summarization to fit the model window. Original messages remain in session storage."


@dataclass(frozen=True, slots=True)
class ContextBlock:
    entry_ids: tuple[str, ...]
    messages: tuple[AgentMessage, ...]
    tokens: int


def context_blocks(rows: Sequence[tuple[str, AgentMessage]], *, protected_ids: set[str], image_reserve: int) -> tuple[ContextBlock, ...]:
    validate_model_context(tuple(message for _, message in rows))
    atoms: list[list[tuple[str, AgentMessage]]] = []
    index = 0
    while index < len(rows):
        entry_id, message = rows[index]
        size = 1 + (len(message.tool_calls) if isinstance(message, AssistantMessage) else 0)
        atoms.append(list(rows[index:index + size]))
        index += size
    groups: list[list[tuple[str, AgentMessage]]] = []
    pending: list[tuple[str, AgentMessage]] = []
    for atom in atoms:
        is_user = isinstance(atom[0][1], UserMessage) or atom[0][1].role == "custom"
        protected = any(entry_id in protected_ids for entry_id, _ in atom)
        if is_user or protected:
            if pending:
                groups.append(pending)
                pending = []
        if protected:
            groups.append(atom)
        elif is_user:
            pending = atom
        elif pending:
            pending.extend(atom)
        else:
            groups.append(atom)
    if pending:
        groups.append(pending)
    return tuple(ContextBlock(
        tuple(entry_id for entry_id, _ in group), tuple(message for _, message in group),
        sum(estimate_budget_messages([message for _, message in group], image_reserve=image_reserve)),
    ) for group in groups)


def select_context_blocks(
    rows: Sequence[tuple[str, AgentMessage]], *, remove_tokens: int,
    protected_ids: set[str], image_reserve: int,
) -> tuple[ContextBlock, ...]:
    selected = []
    removed = 0
    for block in context_blocks(rows, protected_ids=protected_ids, image_reserve=image_reserve):
        if protected_ids.intersection(block.entry_ids):
            continue
        selected.append(block)
        removed += block.tokens
        if removed >= max(1, remove_tokens):
            break
    return tuple(selected)
