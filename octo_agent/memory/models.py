from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictStr, StringConstraints, TypeAdapter, field_validator

from octo_agent.messages import AgentMessage

NonemptyText = Annotated[StrictStr, StringConstraints(strip_whitespace=True, min_length=1)]


class EvidenceOperation(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True, revalidate_instances='always')
    source_entry_ids: tuple[NonemptyText, ...] = Field(min_length=1)

    @field_validator('source_entry_ids')
    @classmethod
    def unique_sources(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError('Duplicate evidence IDs')
        return value


class AddFact(EvidenceOperation):
    kind: Literal['add'] = 'add'
    text: NonemptyText
    category: Literal['fact', 'preference'] = 'fact'


class SupersedeFact(EvidenceOperation):
    kind: Literal['supersede'] = 'supersede'
    target_fact_id: NonemptyText
    text: NonemptyText


class RetractFact(EvidenceOperation):
    kind: Literal['retract'] = 'retract'
    target_fact_id: NonemptyText


MemoryOperation = Annotated[AddFact | SupersedeFact | RetractFact, Field(discriminator='kind')]
operation_adapter = TypeAdapter(MemoryOperation)


@dataclass(frozen=True, slots=True)
class SourceReference:
    session_id: str
    entry_id: str
    timestamp: float
    role: str
    request_id: str | None
    topic_id: str | None
    topic_path: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MemorySource:
    reference: SourceReference
    message: AgentMessage


@dataclass(frozen=True, slots=True)
class MemoryJob:
    id: str
    session_id: str
    start_seq: int
    end_seq: int
    state: Literal['pending', 'running', 'applied']
    attempts: int
    lease_until: float | None
    last_error: str | None


@dataclass(frozen=True, slots=True)
class MemoryLease:
    job: MemoryJob
    token: str


@dataclass(frozen=True, slots=True)
class MemoryFact:
    id: str
    text: str
    category: Literal['fact', 'preference']
    sources: tuple[SourceReference, ...]
    state: Literal['active', 'superseded', 'retracted']
    replacement_id: str | None


@dataclass(frozen=True, slots=True)
class MemoryReceipt:
    job_id: str
    fact_ids: tuple[str, ...]
    operations: tuple[MemoryOperation, ...]
    applied_at: float


class MemoryConflict(ValueError):
    pass
