from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
import json
import math
from typing import Literal

from agent_core.messages import (
    AgentMessage, ImageContent, TextContent, ThinkingContent,
    ToolCall, ToolResultMessage,
)
from agent_core.tools import AgentTool


@dataclass(frozen=True, slots=True)
class ContextBudgetPolicy:
    context_window: int
    output_reserve: int
    safety_margin: int = 1024
    soft_ratio: float = 0.8
    image_reserve: int = 4096
    output_source: str = "request_parameter"

    def __post_init__(self) -> None:
        if self.context_window <= 0 or self.output_reserve <= 0:
            raise ValueError("Context window and output reserve must be positive")
        if self.safety_margin < 0 or self.image_reserve <= 0:
            raise ValueError("Invalid context budget safety or image reserve")
        if not math.isfinite(self.soft_ratio) or not 0 < self.soft_ratio <= 1:
            raise ValueError("Context budget soft_ratio must be in (0, 1]")

    @property
    def hard_input_limit(self) -> int:
        return self.context_window - self.output_reserve - self.safety_margin

    @property
    def soft_input_limit(self) -> int:
        return min(math.floor(self.context_window * self.soft_ratio), self.hard_input_limit)


@dataclass(frozen=True, slots=True)
class ContextBudgetDecision:
    status: Literal["ALLOW", "NEEDS_COMPACTION", "BLOCK"]
    estimated_input: int
    system_tokens: int
    message_tokens: int
    tool_tokens: int
    image_tokens: int
    context_window: int
    output_reserve: int
    safety_margin: int
    soft_input_limit: int
    hard_input_limit: int
    output_source: str

    @property
    def remaining_input(self) -> int:
        return self.hard_input_limit - self.estimated_input

    def summary(self) -> str:
        return (
            f"Context budget {self.status}: estimated input={self.estimated_input}, "
            f"remaining={self.remaining_input}, soft={self.soft_input_limit}, "
            f"hard={self.hard_input_limit}, output={self.output_reserve} "
            f"({self.output_source}), safety={self.safety_margin}"
            + ("; output reserve and safety margin leave no input capacity" if self.hard_input_limit <= 0 else "")
        )


class ContextBudgetExceeded(ValueError):
    def __init__(self, decision: ContextBudgetDecision) -> None:
        self.decision = decision
        super().__init__(decision.summary() + "; compact the context or adjust model limits before retrying")


def estimate_budget_text(text: str) -> int:
    if text.isascii():
        return (len(text) + 3) // 4
    ascii_count = sum(ord(char) < 128 for char in text)
    return (ascii_count + 3) // 4 + len(text.encode("utf-8")) - ascii_count


def _json_tokens(value: object) -> int:
    return estimate_budget_text(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def estimate_budget_messages(messages: Sequence[AgentMessage], *, image_reserve: int) -> tuple[int, int]:
    message_tokens = 0
    image_tokens = 0
    for message in messages:
        message_tokens += 16
        if isinstance(message, ToolResultMessage):
            message_tokens += estimate_budget_text(message.tool_name + message.tool_call_id)
        if isinstance(message.content, str):
            message_tokens += estimate_budget_text(message.content)
            continue
        for block in message.content:
            if isinstance(block, TextContent):
                message_tokens += estimate_budget_text(block.text)
                message_tokens += estimate_budget_text(block.text_signature or "")
            elif isinstance(block, ThinkingContent):
                message_tokens += estimate_budget_text(block.thinking)
                message_tokens += estimate_budget_text(block.thinking_signature or "")
            elif isinstance(block, ToolCall):
                message_tokens += estimate_budget_text(block.name + block.id)
                message_tokens += _json_tokens(block.arguments)
                message_tokens += estimate_budget_text(block.thought_signature or "")
            elif isinstance(block, ImageContent):
                image_tokens += image_reserve
    return message_tokens, image_tokens


def evaluate_context_budget(
    policy: ContextBudgetPolicy, *, system: str,
    messages: Sequence[AgentMessage], tools: Sequence[AgentTool],
) -> ContextBudgetDecision:
    system_tokens = estimate_budget_text(system) + 16
    message_tokens, image_tokens = estimate_budget_messages(messages, image_reserve=policy.image_reserve)
    tool_tokens = sum(16 + _json_tokens({
        "name": tool.name, "description": tool.description, "parameters": dict(tool.input_schema),
    }) for tool in tools)
    estimated = system_tokens + message_tokens + tool_tokens + image_tokens
    status = ("BLOCK" if estimated > policy.hard_input_limit else
              "NEEDS_COMPACTION" if estimated >= policy.soft_input_limit else "ALLOW")
    return ContextBudgetDecision(
        status=status, estimated_input=estimated, system_tokens=system_tokens,
        message_tokens=message_tokens, tool_tokens=tool_tokens, image_tokens=image_tokens,
        context_window=policy.context_window, output_reserve=policy.output_reserve,
        safety_margin=policy.safety_margin, soft_input_limit=policy.soft_input_limit,
        hard_input_limit=policy.hard_input_limit, output_source=policy.output_source,
    )
