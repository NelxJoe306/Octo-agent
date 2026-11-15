from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Literal, Protocol

from octo_agent.context_budget import ContextBudgetDecision
from octo_agent.provider import CancellationToken
from octo_agent.messages import AssistantMessage

RecoveryReason = Literal["soft", "hard", "overflow"]


@dataclass(frozen=True, slots=True)
class ContextRecoveryResult:
    changed: bool
    strategy: Literal["none", "summary", "trim"] = "none"
    detail: str | None = None


class RecoverContext(Protocol):
    async def __call__(
        self, *, reason: RecoveryReason, decision: ContextBudgetDecision,
        error_message: str | None = None, signal: CancellationToken | None = None,
    ) -> ContextRecoveryResult: ...


def is_context_overflow_error_text(text: str) -> bool:
    normalized = text.lower()
    if any(marker in normalized for marker in ("rate limit", "rate_limit", "quota", "tokens per minute", "tokens per day", "billing", "too many requests")):
        return False
    return any(marker in normalized for marker in (
        "context_length_exceeded", "context_window_exceeded", "prompt_too_long", "context length", "context window", "context limit", "maximum context", "max context",
        "input is too long", "input length", "prompt is too long", "too many tokens",
        "token limit",
    ))


def is_context_overflow_error(message: AssistantMessage) -> bool:
    return is_context_overflow_error_text(message.error_message or "")


def overflow_token_deficit(text: str) -> int | None:
    normalized = text.lower().replace(",", "")
    explicit = re.search(r"(?:exceed\w*|over|reduce).*?by\s+(\d+)\s+tokens", normalized)
    if explicit is not None:
        return max(1, int(explicit.group(1)))
    maximum = re.search(r"(?:maximum|max)\s+(?:context\s+(?:length|window)|context).*?(\d+)\s+tokens", normalized)
    requested = re.search(r"(?:requested|request|resulted in)\s+(\d+)\s+tokens", normalized)
    if maximum is not None and requested is not None:
        difference = int(requested.group(1)) - int(maximum.group(1))
        return difference if difference > 0 else None
    return None
