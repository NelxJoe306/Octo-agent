from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from agent_core.types import JSONValue

RunStatus = Literal[
    "starting",
    "running",
    "cancelling",
    "completed",
    "cancelled",
    "failed",
]


class ErrorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    error: str


class HealthResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool = True
    mode: Literal["service"] = "service"
    session_id: str | None = None
    busy: bool = False


class CapabilitiesResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chat: bool = True
    sessions: bool = True
    topics: bool = True
    streaming: bool = True
    cancel: bool = True


class StateResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["service"] = "service"
    session_id: str
    session_title: str
    current_topic_id: str
    active_run_id: str | None = None
    active_run_topic_id: str | None = None
    active_run_status: RunStatus | None = None
    running: bool = False
    model: str
    provider_name: str
    model_display_name: str
    notice: str | None = None
    capabilities: CapabilitiesResponse = Field(default_factory=CapabilitiesResponse)


class SessionSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    title: str
    model: str
    provider_name: str | None = None
    created_at: float
    updated_at: float
    is_active: bool = False


class SessionListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    active_session_id: str
    sessions: list[SessionSummary]


class CreateSessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, max_length=120)


class SessionActionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: StateResponse
    notice: str | None = None


class TopicCommandRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["create", "select", "rename"]
    session_id: str = Field(min_length=1, max_length=128)
    topic_id: str = Field(default="main", min_length=1, max_length=128)
    title: str = Field(default="", max_length=120)
    anchor_entry_id: str | None = Field(default=None, max_length=128)
    activate: bool = False


class TopicCommandResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    topic_id: str
    session_id: str


class CreateRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_request_id: str = Field(min_length=1, max_length=128)
    topic_id: str = Field(default="main", min_length=1, max_length=128)
    text: str = Field(min_length=1, max_length=65536)


class RunAcceptedResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    client_request_id: str
    session_id: str
    topic_id: str
    status: RunStatus


class ToolExecutionState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool_call_id: str
    tool_name: str
    status: Literal["running", "completed", "error"]
    args: dict[str, JSONValue] = Field(default_factory=dict)
    partial_text: str = ""
    result_text: str = ""
    is_error: bool = False


class RunSnapshotResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    client_request_id: str
    session_id: str
    topic_id: str
    status: RunStatus
    seq: int
    user_text: str
    assistant_text: str
    thinking_text: str
    tool_executions: list[ToolExecutionState] = Field(default_factory=list)
    error_code: str | None = None
    error_message: str | None = None
    budget: dict[str, JSONValue] | None = None
    recovery: dict[str, JSONValue] | None = None
    created_at: float
    updated_at: float
    finished_at: float | None = None


class RunEventEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    session_id: str
    topic_id: str
    seq: int
    kind: Literal["snapshot", "agent_event", "run_status"]
    payload: dict[str, JSONValue]
