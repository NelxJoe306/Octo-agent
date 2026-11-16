from __future__ import annotations

from hashlib import sha256
import json
import os
from pathlib import Path
from typing import Mapping

from agent_core.types import JSONValue


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
    path = os.environ.get("OCTO_PREFIX_LOG")
    if not path:
        return
    record = {"event": "request_prefix", "model": model, "session_id": session_id, **build_prefix(payload)}
    # 观测只写摘要；日志不可用时不阻断模型请求。
    try:
        with Path(path).open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass
