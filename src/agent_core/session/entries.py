from __future__ import annotations

from time import time
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from agent_core.messages import AgentMessage, Usage, UserContent
from agent_core.types import JSONValue


def new_entry_id() -> str:

    return uuid4().hex


def current_timestamp() -> float:

    return time()


class BaseSessionEntry(BaseModel):


    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=new_entry_id)
    parent_id: str | None = None
    timestamp: float = Field(default_factory=current_timestamp)


class MessageEntry(BaseSessionEntry):


    type: Literal["message"] = "message"
    message: AgentMessage


class CustomMessageEntry(BaseSessionEntry):


    type: Literal["custom_message"] = "custom_message"
    custom_type: str
    content: UserContent
    display: bool = True
    details: JSONValue = None


class ModelChangeEntry(BaseSessionEntry):


    type: Literal["model_change"] = "model_change"
    model: str
    provider: str | None = None


class ThinkingLevelChangeEntry(BaseSessionEntry):


    type: Literal["thinking_level_change"] = "thinking_level_change"
    thinking_level: str | None = None


class CompactionEntry(BaseSessionEntry):


    type: Literal["compaction"] = "compaction"
    summary: str


    replaces_entry_ids: list[str] = Field(default_factory=list, exclude_if=lambda ids: not ids)
    first_kept_entry_id: str | None = None
    tokens_before: int | None = None
    usage: Usage | None = None
    provider: str | None = None
    model: str | None = None
    response_provider: str | None = None


class BranchSummaryEntry(BaseSessionEntry):


    type: Literal["branch_summary"] = "branch_summary"
    summary: str
    branch_root_id: str | None = None
    usage: Usage | None = None
    provider: str | None = None
    model: str | None = None
    response_provider: str | None = None


class LabelEntry(BaseSessionEntry):


    type: Literal["label"] = "label"
    target_id: str
    label: str | None = None


class LeafEntry(BaseSessionEntry):


    type: Literal["leaf"] = "leaf"
    entry_id: str | None = None


class SessionInfoEntry(BaseSessionEntry):


    type: Literal["session_info"] = "session_info"
    created_at: float = Field(default_factory=current_timestamp)
    cwd: str | None = None
    title: str | None = None


class CustomEntry(BaseSessionEntry):


    type: Literal["custom"] = "custom"
    namespace: str
    data: dict[str, JSONValue] = Field(default_factory=dict)


type SessionEntry = Annotated[
    MessageEntry
    | CustomMessageEntry
    | ModelChangeEntry
    | ThinkingLevelChangeEntry
    | CompactionEntry
    | BranchSummaryEntry
    | LabelEntry
    | LeafEntry
    | SessionInfoEntry
    | CustomEntry,
    Field(discriminator="type"),
]
