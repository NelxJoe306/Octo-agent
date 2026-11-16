from __future__ import annotations

from hashlib import sha256
import json
import os
from time import monotonic
from uuid import uuid4
from pathlib import Path
from typing import Mapping

from agent_core.types import JSONValue


def prefix_optimization_enabled() -> bool:
    return os.environ.get("OCTO_DISABLE_PREFIX_CACHE") != "1"


def stable_tools(tools: list[dict]) -> list[dict]:
    if not prefix_optimization_enabled():
        return tools
    # 只排序对象键和工具顺序，保留 Schema 内数组的语义顺序。
    ordered = sorted(tools, key=lambda tool: tool.get("function", tool).get("name", ""))
    return json.loads(json.dumps(ordered, ensure_ascii=False, sort_keys=True))


def write_diagnostic(record: dict) -> None:
    path = os.environ.get("OCTO_PREFIX_LOG")
    if not path:
        return
    try:
        with Path(path).open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass


def build_prefix(payload: Mapping[str, JSONValue]) -> dict[str, str]:
    system = payload.get("instructions", [
        message for message in payload.get("messages", [])
        if isinstance(message, dict) and message.get("role") in {"system", "developer"}
    ])
    tools = payload.get("tools", [])
    def fingerprint(value: object) -> str:
        return sha256(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()
    return {
        "system_hash": fingerprint(system),
        "tools_hash": fingerprint(tools),
        "prefix_hash": fingerprint({"system": system, "tools": tools}),
    }


def log_prefix(payload: Mapping[str, JSONValue], *, model: str, session_id: str | None) -> None:
    write_diagnostic({"event": "request_prefix", "model": model, "session_id": session_id,
                      "prefix_optimization_enabled": prefix_optimization_enabled(), **build_prefix(payload)})


class RequestTrace:
    def __init__(self, payload, model, session_id):
        self.enabled = bool(os.environ.get("OCTO_PREFIX_LOG"))
        self.started = monotonic()
        self.first_output_ms = None
        self.first_text_ms = None
        self.usage = None
        self.cache_usage_reported = None
        self.retries = 0
        self.status = "incomplete"
        self.record = {}
        if self.enabled:
            self.record = {"event":"request_metrics", "request_id":uuid4().hex, "model":model,
                "session_id":session_id, "prefix_optimization_enabled":prefix_optimization_enabled(),
                **build_prefix(payload)}
            messages = payload.get("messages", payload.get("input", []))
            text = json.dumps(messages[-1:] if isinstance(messages, list) else messages, ensure_ascii=False)
            self.record["mode"] = "plan" if "Plan mode:" in text else "execute"

    def raw(self, data):
        if not self.enabled or data == "[DONE]":
            return
        chunk = json.loads(data)
        usage = chunk.get("usage") or chunk.get("response", {}).get("usage")
        if isinstance(usage, dict):
            details = usage.get("prompt_tokens_details") or usage.get("input_tokens_details") or {}
            self.cache_usage_reported = "prompt_cache_hit_tokens" in usage or "cached_tokens" in details

    def event(self, event):
        if not self.enabled:
            return
        elapsed = round((monotonic() - self.started) * 1000, 2)
        if event.type in ("text_delta", "thinking_delta", "tool_call") and self.first_output_ms is None:
            self.first_output_ms = elapsed
        if event.type == "text_delta" and event.delta and self.first_text_ms is None:
            self.first_text_ms = elapsed
        if event.type == "retry":
            self.retries += 1
        elif event.type == "error":
            self.status = "error"
        elif event.type == "response_end":
            self.usage = event.message.usage
            self.status = event.message.stop_reason

    def finish(self):
        if not self.enabled:
            return
        self.record.update({"status":self.status, "retries":self.retries,
            "first_output_ms":self.first_output_ms, "first_text_ms":self.first_text_ms,
            "elapsed_ms":round((monotonic()-self.started)*1000, 2),
            "cache_usage_reported":self.cache_usage_reported,
            "input_tokens":self.usage.input + self.usage.cache_read + self.usage.cache_write if self.usage else None,
            "cached_input_tokens":self.usage.cache_read if self.usage and self.cache_usage_reported else None})
        write_diagnostic(self.record)
