from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from agent_core.messages import AssistantMessage, CustomMessage, UserMessage
from agent_core.session import BranchSummaryEntry, CompactionEntry, MessageEntry, ModelChangeEntry
from agent_core.session.entries import CustomMessageEntry, SessionEntry

PricingResolver = Callable[[str, str, int], Mapping[str, float] | None]
_TOKENS_PER_MILLION = 1_000_000


@dataclass(frozen=True, slots=True)
class SessionStats:


    turn_count: int = 0
    tool_call_count: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    cache_write_tokens: int = 0
    latest_prompt_tokens: int = 0
    latest_cached_input_tokens: int = 0
    timed_output_tokens: int = 0
    response_duration_ms: int = 0
    time_to_first_output_ms: int = 0
    timed_first_output_count: int = 0
    estimated_cost: float | None = None

    @property
    def cache_hit_rate(self) -> float | None:

        if self.input_tokens <= 0:
            return None
        if self.cached_input_tokens == 0 and self.cache_write_tokens == 0:
            return None
        return self.cached_input_tokens / self.input_tokens

    @property
    def latest_cache_hit_rate(self) -> float | None:

        if self.latest_prompt_tokens <= 0:
            return None
        if self.cached_input_tokens == 0 and self.cache_write_tokens == 0:
            return None
        return self.latest_cached_input_tokens / self.latest_prompt_tokens

    @property
    def output_tokens_per_second(self) -> float | None:

        if self.timed_output_tokens <= 0 or self.response_duration_ms <= 0:
            return None
        return self.timed_output_tokens * 1000 / self.response_duration_ms

    @property
    def average_time_to_first_output_ms(self) -> float | None:

        if self.timed_first_output_count <= 0:
            return None
        return self.time_to_first_output_ms / self.timed_first_output_count


def calculate_session_stats(
    entries: Sequence[SessionEntry],
    *,
    pricing: PricingResolver,
) -> SessionStats:

    turn_count = 0
    tool_call_count = 0
    input_tokens = 0
    output_tokens = 0
    cached_input_tokens = 0
    cache_write_tokens = 0
    latest_prompt_tokens = 0
    latest_cached_input_tokens = 0
    timed_output_tokens = 0
    response_duration_ms = 0
    time_to_first_output_ms = 0
    timed_first_output_count = 0
    estimated_cost = 0.0
    has_billable_usage = False
    has_complete_pricing = True
    current_provider = "unknown"
    current_model = "unknown"

    for entry in entries:
        if isinstance(entry, CustomMessageEntry):
            turn_count += 1
            continue
        if isinstance(entry, ModelChangeEntry):
            current_model = entry.model
            if entry.provider is not None:
                current_provider = entry.provider
            continue

        message: AssistantMessage | None = None
        if isinstance(entry, MessageEntry):
            if isinstance(entry.message, (UserMessage, CustomMessage)):
                turn_count += 1
                continue
            if not isinstance(entry.message, AssistantMessage):
                continue
            message = entry.message
            current_provider = message.provider
            current_model = message.model
            tool_call_count += len(message.tool_calls)
            usage = message.usage
            provider = message.provider
            model = message.model
        elif isinstance(entry, CompactionEntry | BranchSummaryEntry):
            if entry.usage is None:
                continue
            usage = entry.usage
            provider = entry.provider or current_provider
            model = entry.model or current_model
        else:
            continue

        prompt_tokens = usage.input + usage.cache_read + usage.cache_write
        latest_prompt_tokens = prompt_tokens
        latest_cached_input_tokens = usage.cache_read
        input_tokens += prompt_tokens
        cached_input_tokens += usage.cache_read
        cache_write_tokens += usage.cache_write
        output_tokens += usage.output
        timing = message.timing if message is not None else None
        if timing is not None:


            if usage.output > 0 and timing.total_duration_ms > 0:
                timed_output_tokens += usage.output
                response_duration_ms += timing.total_duration_ms
            if timing.time_to_first_output_ms is not None:
                time_to_first_output_ms += timing.time_to_first_output_ms
                timed_first_output_count += 1
        if prompt_tokens == 0 and usage.output == 0:
            continue

        has_billable_usage = True
        rates = pricing(provider, model, prompt_tokens)
        if rates is None:
            if usage.cost.total > 0:
                estimated_cost += usage.cost.total
            else:
                has_complete_pricing = False
            continue
        estimated_cost += _response_cost(
            input_tokens=usage.input,
            output_tokens=usage.output,
            cache_read_tokens=usage.cache_read,
            cache_write_tokens=usage.cache_write,
            cache_write_1h_tokens=usage.cache_write_1h or 0,
            rates=rates,
        )

    return SessionStats(
        turn_count=turn_count,
        tool_call_count=tool_call_count,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_input_tokens=cached_input_tokens,
        cache_write_tokens=cache_write_tokens,
        latest_prompt_tokens=latest_prompt_tokens,
        latest_cached_input_tokens=latest_cached_input_tokens,
        timed_output_tokens=timed_output_tokens,
        response_duration_ms=response_duration_ms,
        time_to_first_output_ms=time_to_first_output_ms,
        timed_first_output_count=timed_first_output_count,
        estimated_cost=(estimated_cost if has_billable_usage and has_complete_pricing else None),
    )


def _response_cost(
    *,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int,
    cache_write_tokens: int,
    cache_write_1h_tokens: int,
    rates: Mapping[str, float],
) -> float:

    write_1h = min(cache_write_1h_tokens, cache_write_tokens)
    return (
        input_tokens * rates.get("input", 0.0)
        + output_tokens * rates.get("output", 0.0)
        + cache_read_tokens * rates.get("cacheRead", 0.0)
        + (cache_write_tokens - write_1h) * rates.get("cacheWrite", 0.0)
        + write_1h * rates.get("cacheWrite1h", rates.get("cacheWrite", 0.0))
    ) / _TOKENS_PER_MILLION
