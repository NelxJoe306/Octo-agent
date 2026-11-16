import argparse
import asyncio
import csv
from datetime import date, timedelta
import hashlib
import json
import os
from pathlib import Path
import statistics
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/"src"))

from agent_core.messages import AssistantMessage, UserMessage
from agent_core.provider_events import AssistantDoneEvent, AssistantErrorEvent
from agent_core.session import MessageEntry, SQLiteSessionStorage
from long_term_memory.memory_runtime import MemorySettings
from model_adapters.fake import FakeProvider
from terminal_app.paths import OctoPaths
from terminal_app.provider_config import load_provider_settings, resolve_provider_selection
from terminal_app.provider_runtime import create_model_provider
from terminal_app.resources import OctoResourcePaths
from terminal_app.session import CodingSession, CodingSessionConfig

STATIC = (
    "You are Octo, a helpful programming assistant. Treat the supplied conversation as synthetic test data. "
    "Follow the last user question. If the user asks for OK, respond only OK without tools. "
    "Keep tool schemas and their meanings consistent across the conversation. "
    "Project convention: describe actions accurately, use clear explanations, and cite actual tool results. "
)


async def fixture(root, group, index, enabled, model):
    day = date(2026, 11, 1) + timedelta(days=index % 3)
    class FixedDate(date):
        @classmethod
        def today(cls):
            return day
    paths = OctoPaths(home=root/"home", agents_home=root/"agents")
    storage = SQLiteSessionStorage(root/f"{group}-{index}-{enabled}.db", "eval")
    rounds = 22 if group == "long" else 2
    for turn in range(rounds):
        await storage.append(MessageEntry(id=f"u{turn}", parent_id=f"a{turn-1}" if turn else None,
            message=UserMessage(content=f"第 {turn+1} 轮，请解释事件驱动里消息和工具结果的关系。")))
        await storage.append(MessageEntry(id=f"a{turn}", parent_id=f"u{turn}",
            message=AssistantMessage(content="消息结束事件用于保存完整内容。工具调用通过 ID 对应工具结果，主循环获得结果后继续推理。")))
    with patch.dict(os.environ, {"OCTO_DISABLE_PREFIX_CACHE":"0" if enabled else "1", "OCTO_OFFLINE":"1"}), \
         patch("terminal_app.system_prompt.date", FixedDate), patch("terminal_app.session.date", FixedDate):
        session = await CodingSession.load(CodingSessionConfig(provider=FakeProvider([]), model=model, cwd=root,
            custom_system_prompt=STATIC, storage=storage, extensions_enabled=False, skills_enabled=False,
            resource_paths=OctoResourcePaths(root=paths.home, agents_root=paths.agents_home, cwd=root, paths=paths),
            memory_settings=MemorySettings(enabled=False)))
        try:
            if group == "mode" and index % 2:
                await session.set_mode("plan")
            session._memory_snapshot = json.dumps([{"text":f"合成偏好版本 {index % 3}，解释时先给例子。"}], ensure_ascii=False)
            history = tuple(message for _, message in session._context_rows())
            if len(history) != rounds * 2:
                raise RuntimeError("Fixture history was truncated")
            messages = await session._prepare_topic_context((*history, UserMessage(content=f"样本 {group}-{index:02d}，请只回答 OK，不要调用工具。")), signal=None)
            return dict(system=session.system_prompt, messages=list(messages), tools=session.tools,
                        group=group, index=index, date=day.isoformat(), rounds=rounds, mode=session.mode)
        finally:
            await session.aclose()


def summarize(rows):
    result = []
    for group in ("short", "long", "mode"):
        for enabled in (False, True):
            subset = [r for r in rows if r["group"] == group and r["enabled"] == enabled]
            valid = [r for r in subset if r["status"] == "stop" and r["retries"] == 0]
            measured = [r for r in valid if r["cached_input_tokens"] is not None]
            tokens = sum(r["input_tokens"] for r in measured)
            per_sample = []
            for index in range(20):
                values = [r["first_text_ms"] for r in valid if r["index"] == index and r["first_text_ms"] is not None]
                if values:
                    per_sample.append(statistics.median(values))
            result.append(dict(group=group, enabled=enabled, requests=len(subset), successful=len(valid),
                cache_reported=len(measured), cache_ratio=sum(r["cached_input_tokens"] for r in measured)/tokens if tokens else None,
                median_sample_first_text_ms=statistics.median(per_sample) if per_sample else None))
    return result


async def run(args):
    selected = resolve_provider_selection(load_provider_settings(), provider_name="deepseek", model=args.model)
    provider = create_model_provider(selected.provider, model=selected.model)
    provider.configure_request_budget(context_window=32768, safety_margin=1024, output_override=32)
    trace_path = args.output.with_suffix(".trace.jsonl").resolve()
    rows = []
    try:
        with tempfile.TemporaryDirectory() as temp, args.output.open("x", encoding="utf-8") as output:
            root = Path(temp)
            fixtures = {}
            for group in ("short", "long", "mode"):
                for index in range(20):
                    for enabled in (False, True):
                        fixtures[group,index,enabled] = await fixture(root, group, index, enabled, selected.model)
            for repeat in range(5):
                for group in ("short", "long", "mode"):
                    for index in range(20):
                        for enabled in ((False,True) if (repeat+index)%2 == 0 else (True,False)):
                            f = fixtures[group,index,enabled]
                            row = dict(group=group, index=index, repeat=repeat+1, enabled=enabled,
                                model=selected.model, date=f["date"], rounds=f["rounds"], mode=f["mode"])
                            error = None
                            with patch.dict(os.environ, {"OCTO_DISABLE_PREFIX_CACHE":"0" if enabled else "1",
                                                        "OCTO_PREFIX_LOG":str(trace_path)}):
                                async for event in provider.stream_response(model=selected.model, system=f["system"],
                                        messages=f["messages"], tools=f["tools"], session_id="prefix-eval"):
                                    if isinstance(event, AssistantErrorEvent):
                                        error = event.reason
                            # 每个请求的最后一条记录含适配器处的真实载荷指纹与原始用量可用性。
                            record = json.loads(trace_path.read_text(encoding="utf-8").splitlines()[-1])
                            if record["event"] != "request_metrics":
                                raise RuntimeError("Missing request metrics")
                            row.update(record)
                            if error:
                                row["status"] = "error"
                            rows.append(row)
                            output.write(json.dumps(row, ensure_ascii=False)+"\n")
                            output.flush()
                            print(len(rows), group, index, enabled, row["status"], row["cached_input_tokens"], flush=True)
    finally:
        await provider.aclose()
    with args.output.with_suffix(".csv").open("x", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    args.output.with_suffix(".summary.json").write_text(json.dumps(summarize(rows), ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.suffix != ".jsonl" or any(args.output.with_suffix(suffix).exists() for suffix in (".jsonl",".csv",".trace.jsonl",".summary.json")):
        parser.error("Use a fresh .jsonl output path")
    asyncio.run(run(args))
