from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from octo_agent.context_budget import ContextBudgetDecision
from octo_agent.context_recovery import ContextRecoveryResult, RecoveryReason
from octo_agent.messages import AgentMessage, ToolResultMessage, WireModel
from octo_agent.provider_events import AssistantMessageEvent
from octo_agent.tools import AgentToolResult
from octo_agent.types import JSONValue


class ContextBudgetEvent(WireModel):
    type: Literal["context_budget"] = "context_budget"
    decision: ContextBudgetDecision


class ContextRecoveryStartEvent(WireModel):
    type: Literal["context_recovery_start"] = "context_recovery_start"
    reason: RecoveryReason


class ContextRecoveryEndEvent(WireModel):
    type: Literal["context_recovery_end"] = "context_recovery_end"
    reason: RecoveryReason
    result: ContextRecoveryResult


class AgentStartEvent(WireModel):
    type: Literal["agent_start"] = "agent_start"


class AgentEndEvent(WireModel):
    type: Literal["agent_end"] = "agent_end"
    messages: list[AgentMessage] = Field(default_factory=list)


class TurnStartEvent(WireModel):
    type: Literal["turn_start"] = "turn_start"


class TurnEndEvent(WireModel):
    type: Literal["turn_end"] = "turn_end"
    message: AgentMessage
    tool_results: list[ToolResultMessage] = Field(default_factory=list)


class MessageStartEvent(WireModel):
    type: Literal["message_start"] = "message_start"
    message: AgentMessage


class MessageUpdateEvent(WireModel):
    type: Literal["message_update"] = "message_update"
    message: AgentMessage
    assistant_message_event: AssistantMessageEvent = Field(
        serialization_alias="assistantMessageEvent"
    )


class MessageEndEvent(WireModel):
    type: Literal["message_end"] = "message_end"
    message: AgentMessage


class ToolExecutionStartEvent(WireModel):
    type: Literal["tool_execution_start"] = "tool_execution_start"
    tool_call_id: str
    tool_name: str
    args: dict[str, JSONValue] = Field(default_factory=dict)


class ToolExecutionUpdateEvent(WireModel):
    type: Literal["tool_execution_update"] = "tool_execution_update"
    tool_call_id: str
    tool_name: str
    args: dict[str, JSONValue] = Field(default_factory=dict)
    partial_result: AgentToolResult


class ToolExecutionEndEvent(WireModel):
    type: Literal["tool_execution_end"] = "tool_execution_end"
    tool_call_id: str
    tool_name: str
    result: AgentToolResult
    is_error: bool


type AgentEvent = Annotated[
    AgentStartEvent
    | ContextBudgetEvent
    | ContextRecoveryStartEvent
    | ContextRecoveryEndEvent
    | AgentEndEvent
    | TurnStartEvent
    | TurnEndEvent
    | MessageStartEvent
    | MessageUpdateEvent
    | MessageEndEvent
    | ToolExecutionStartEvent
    | ToolExecutionUpdateEvent
    | ToolExecutionEndEvent,
    Field(discriminator="type"),
]
