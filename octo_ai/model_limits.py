from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


DEFAULT_REQUEST_OUTPUT_TOKENS = 16_384


def resolve_request_output_tokens(
    value: int | None, *, default: int = DEFAULT_REQUEST_OUTPUT_TOKENS,
    context_window: int | None = None, safety_margin: int = 1024,
    model_max_output_tokens: int | None = None,
) -> int:
    if value is not None:
        if value <= 0:
            raise ValueError("Request output token limit must be positive")
        if model_max_output_tokens is not None and value > model_max_output_tokens:
            raise ValueError("Request output token limit exceeds the model output maximum")
        return value
    ceilings = [default]
    if model_max_output_tokens is not None:
        if model_max_output_tokens <= 0:
            raise ValueError("Model output maximum must be positive")
        ceilings.append(model_max_output_tokens)
    if context_window is not None:
        if context_window <= 0 or safety_margin < 0:
            raise ValueError("Invalid context window or safety margin")
        ceilings.extend((max(1, context_window // 4), max(1, context_window - safety_margin - 1)))
    resolved = min(ceilings)
    if resolved <= 0:
        raise ValueError("Request output token limit must be positive")
    return resolved


@dataclass(frozen=True, slots=True)
class RequestTokenBudget:
    max_output_tokens: int
    additional_system: str = ""
    context_window: int | None = None


@runtime_checkable
class RequestBudgetProvider(Protocol):
    def request_token_budget(self) -> RequestTokenBudget: ...


@runtime_checkable
class ConfigurableRequestBudgetProvider(RequestBudgetProvider, Protocol):
    def configure_request_budget(
        self, *, context_window: int, safety_margin: int,
        output_override: int | None = None, model_max_output_tokens: int | None = None,
    ) -> RequestTokenBudget: ...


@dataclass(frozen=True, slots=True)
class RuntimeModelLimits:


    context_window: int
    max_output_tokens: int | None = None
    effective_context_window_percent: int = 100
    auto_compact_token_limit: int | None = None

    def __post_init__(self) -> None:
        if self.context_window <= 0:
            raise ValueError("context_window must be positive")
        if self.max_output_tokens is not None and self.max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be positive")
        if not 1 <= self.effective_context_window_percent <= 100:
            raise ValueError("effective_context_window_percent must be between 1 and 100")
        if self.auto_compact_token_limit is not None and self.auto_compact_token_limit <= 0:
            raise ValueError("auto_compact_token_limit must be positive")

    @property
    def effective_context_window(self) -> int:

        return max(1, self.context_window * self.effective_context_window_percent // 100)

    @property
    def effective_auto_compact_token_limit(self) -> int:

        default_limit = max(1, self.context_window * 9 // 10)
        if self.auto_compact_token_limit is None:
            return min(default_limit, self.effective_context_window)
        return min(self.auto_compact_token_limit, self.effective_context_window)


@runtime_checkable
class ModelLimitsProvider(Protocol):


    async def discover_model_limits(self, model: str) -> RuntimeModelLimits | None:

        ...
