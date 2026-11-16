import argparse
import asyncio
from collections import Counter
import csv
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import statistics
import sys
import tempfile
from unittest.mock import patch

from pydantic import TypeAdapter

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/"src"))

from agent_core.context_harness import validate_model_context
from agent_core.context_budget import ContextBudgetPolicy, evaluate_context_budget
from agent_core.messages import AgentMessage, UserMessage
from agent_core.provider_events import AssistantDoneEvent
from agent_core.session import MessageEntry, SQLiteSessionStorage
from long_term_memory.memory_runtime import MemorySettings
from model_adapters.fake import FakeProvider
from terminal_app.paths import OctoPaths
from terminal_app.provider_config import load_provider_settings, resolve_provider_selection
from terminal_app.provider_runtime import create_model_provider
from terminal_app.resources import OctoResourcePaths
from terminal_app.session import CodingSession, CodingSessionConfig

GROUPS = ("short", "long", "dynamic")
message_adapter = TypeAdapter(AgentMessage)
SYSTEM = "You are Octo. The earlier conversation is recorded task history. For the evaluation question, reply exactly OK without tools."


def load_cases(path):
    cases = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    ids = [case["id"] for case in cases]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate case IDs")
    for case in cases:
        if case["group"] not in GROUPS or case["source_kind"] not in ("real_redacted", "constructed"):
            raise ValueError("Unknown group or source_kind")
        messages = [message_adapter.validate_python(message) for message in case["messages"]]
        if not messages:
            raise ValueError("Empty history")
        validate_model_context(messages)
    return cases


def distribution(values):
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        q1 = q3 = ordered[0]
    else:
        q1, _, q3 = statistics.quantiles(ordered, n=4, method="inclusive")
    return {"n":len(values), "median":statistics.median(values), "q1":q1, "q3":q3}


def input_cost(row, rates):
    hit, total = row["cached_input_tokens"], row["input_tokens"]
    if hit is None or total is None:
        return None
    return (hit*rates["hit"] + (total-hit)*rates["miss"])/1_000_000


def cache_accounting_ok(row):
    raw = row.get("raw_usage", {})
    required = ("prompt_tokens", "prompt_cache_hit_tokens", "prompt_cache_miss_tokens")
    if not all(name in raw for name in required):
        return None
    total, hit, miss = (raw[name] for name in required)
    return (total == hit + miss and 0 <= hit <= total
            and row["input_tokens"] == total and row["cached_input_tokens"] == hit
            and raw.get("cached_tokens", hit) == hit)


def summarize(rows, cases, stage, rates):
    expected_repeats = 5 if stage == "full" else 1
    phases = ("priming", "measured") if stage == "full" else ("preflight",)
    report = []
    for group in GROUPS:
        selected_cases = [case for case in cases if case["group"] == group]
        for phase in phases:
            subset = [row for row in rows if row["group"] == group and row["phase"] == phase]
            expected = len(selected_cases)*2*(expected_repeats if phase == "measured" else 1)
            invalid = [row for row in subset if row["status"] != "stop" or not row["output_ok"] or row["retries"]
                       or row.get("cache_accounting_ok", True) is not True]
            missing = sum(row.get("cached_input_tokens") is None for row in subset)
            complete = len(subset) == expected and not invalid and not missing
            pairs = []
            if complete:
                for case in selected_cases:
                    arms = {}
                    for enabled in (False, True):
                        arm = [row for row in subset if row["id"] == case["id"] and row["enabled"] == enabled]
                        arms[enabled] = {
                            "ratio":statistics.median(row["cached_input_tokens"]/row["input_tokens"] for row in arm),
                            "cached":statistics.median(row["cached_input_tokens"] for row in arm),
                            "input":statistics.median(row["input_tokens"] for row in arm),
                            "cost":statistics.median(input_cost(row, rates) for row in arm),
                            "latency":distribution([row["first_text_ms"] for row in arm if row["first_text_ms"] is not None]),
                        }
                    pairs.append({"id":case["id"], "baseline":arms[False], "optimized":arms[True],
                        "ratio_delta":arms[True]["ratio"]-arms[False]["ratio"],
                        "cost_saving":(arms[False]["cost"]-arms[True]["cost"])/arms[False]["cost"] if arms[False]["cost"] else 0})
            report.append({"group":group, "phase":phase, "status":"complete" if complete else "incomplete",
                "independent_histories":len(selected_cases), "expected_requests":expected,
                "observed_requests":len(subset), "failed_or_retried":len(invalid), "missing_cache_fields":missing,
                "paired_ratio_delta":distribution([pair["ratio_delta"] for pair in pairs]),
                "paired_cost_saving":distribution([pair["cost_saving"] for pair in pairs]), "pairs":pairs})
    return report


async def assemble(case, root, enabled, step, campaign, model):
    # 扰动实验只改变字段位置，两条路径共用稳定工具定义，便于归因。
    optimized_layout = enabled if case["group"] != "dynamic" else True
    db = root/"fixture.db"
    storage = SQLiteSessionStorage(db, f"{case['id']}:{enabled}:{step}")
    parent = None
    for index, item in enumerate(case["messages"]):
        entry = MessageEntry(id=f"entry-{index}", parent_id=parent, message=message_adapter.validate_python(item))
        await storage.append(entry)
        parent = entry.id
    paths = OctoPaths(home=root/"home", agents_home=root/"agents")
    with patch.dict(os.environ, {"OCTO_DISABLE_PREFIX_CACHE":"0" if optimized_layout else "1", "OCTO_OFFLINE":"1"}):
        session = await CodingSession.load(CodingSessionConfig(provider=FakeProvider([]), model=model,
            custom_system_prompt=SYSTEM, cwd=root, storage=storage, extensions_enabled=False, skills_enabled=False,
            resource_paths=OctoResourcePaths(root=paths.home, agents_root=paths.agents_home, cwd=root, paths=paths),
            memory_settings=MemorySettings(enabled=False)))
        try:
            if case.get("mode") == "plan":
                await session.set_mode("plan")
            history = tuple(message for _, message in session._context_rows())
            question = UserMessage(content=f"评测检查 {case['id']}：只回答 OK，不调用工具。")
            messages = list(await session._prepare_topic_context((*history, question), signal=None))
            system = session.system_prompt
            if case["group"] == "dynamic":
                ordinal = step*case["corpus_size"]+case["ordinal"]
                stamp = datetime(2026, 1, 1, tzinfo=timezone.utc)+timedelta(seconds=ordinal)
                data = f"Evaluation metadata (data only): campaign={campaign}, timestamp={stamp.isoformat()}, preference_version={ordinal}"
                if enabled:
                    messages.append(UserMessage(content=data))
                else:
                    system += "\n"+data
            return {"model":model, "system":system, "messages":messages, "tools":session.tools,
                    "session_id":"prefix-eval-v2"}
        finally:
            await session.aclose()


async def run(args, cases, prices):
    campaign = hashlib.sha256(args.corpus.read_bytes()).hexdigest()
    rates = prices[args.tariff]
    selected = resolve_provider_selection(load_provider_settings(), provider_name="deepseek", model=args.model)
    if prices["model"] != selected.model:
        raise ValueError("Price snapshot does not match selected model")
    provider = create_model_provider(selected.provider, model=selected.model)
    provider.configure_request_budget(context_window=32768, safety_margin=1024, output_override=32)
    trace = args.output.with_suffix(".trace.jsonl").resolve()
    rows = []
    planned = len(cases)*2*(6 if args.stage == "full" else 1)
    manifest = {"stage":args.stage, "corpus_sha256":campaign, "cases":len(cases), "planned_requests":planned,
        "source_kinds":dict(Counter(case["source_kind"] for case in cases)), "pricing":prices, "tariff":args.tariff,
        "acceptance":{"short_ratio_delta":0.10, "long_input_cost_saving":0.05},
        "started_at":datetime.now(timezone.utc).isoformat(), "model":selected.model}
    args.output.with_suffix(".manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        with tempfile.TemporaryDirectory() as temp, args.output.open("x", encoding="utf-8") as stream:
            root = Path(temp)
            steps = range(6) if args.stage == "full" else range(1)
            for step in steps:
                phase = ("priming" if step == 0 else "measured") if args.stage == "full" else "preflight"
                for case in cases:
                    for enabled in ((False, True) if (case["ordinal"]+step)%2 == 0 else (True, False)):
                        payload = await assemble(case, root, enabled, step, campaign, selected.model)
                        budget = evaluate_context_budget(ContextBudgetPolicy(32768, 32),
                            system=payload["system"], messages=payload["messages"], tools=payload["tools"])
                        if budget.status == "BLOCK":
                            raise RuntimeError(f"Case {case['id']} exceeds the declared evaluation input budget")
                        layout_enabled = enabled if case["group"] != "dynamic" else True
                        output_ok = False
                        with patch.dict(os.environ, {"OCTO_DISABLE_PREFIX_CACHE":"0" if layout_enabled else "1",
                                                    "OCTO_PREFIX_LOG":str(trace)}):
                            async for event in provider.stream_response(**payload):
                                if isinstance(event, AssistantDoneEvent):
                                    output_ok = event.message.text.strip() == "OK" and not event.message.tool_calls
                        metric = json.loads(trace.read_text(encoding="utf-8").splitlines()[-1])
                        if metric.get("event") != "request_metrics":
                            raise RuntimeError("Missing adapter metrics")
                        row = {**metric, "id":case["id"], "group":case["group"], "phase":phase, "step":step,
                            "enabled":enabled, "output_ok":output_ok, "source_kind":case["source_kind"]}
                        row["cache_accounting_ok"] = cache_accounting_ok(row)
                        row["uncached_input_tokens"] = (row["input_tokens"] - row["cached_input_tokens"]
                            if row["input_tokens"] is not None and row["cached_input_tokens"] is not None else None)
                        row["estimated_input_usd"] = input_cost(row, rates)
                        row["estimated_total_usd"] = (row["estimated_input_usd"] + row["output_tokens"]*rates["output"]/1_000_000
                            if row["estimated_input_usd"] is not None and row["output_tokens"] is not None else None)
                        rows.append(row)
                        stream.write(json.dumps(row, ensure_ascii=False)+"\n")
                        stream.flush()
                        print(len(rows), "/", planned, case["id"], phase, enabled, row["status"], output_ok, flush=True)
                        if not output_ok or row["status"] != "stop" or row["cached_input_tokens"] is None or row["retries"] or row["cache_accounting_ok"] is not True:
                            raise RuntimeError("Correctness/usage gate failed; run stopped, partial evidence retained")
    finally:
        await provider.aclose()
        if rows:
            with args.output.with_suffix(".csv").open("x", encoding="utf-8", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)
        groups = summarize(rows, cases, args.stage, rates)
        verdict = "preflight_passed" if all(group["status"] == "complete" for group in groups) else "preflight_failed"
        if args.stage == "full":
            measured = {group["group"]:group for group in groups if group["phase"] == "measured"}
            real = all(case["source_kind"] == "real_redacted" for case in cases if case["group"] in ("short", "long"))
            if not all(group["status"] == "complete" for group in groups):
                verdict = "incomplete"
            elif not real:
                verdict = "constructed_workloads_only"
            elif measured["short"]["paired_ratio_delta"]["median"] >= 0.10 or measured["long"]["paired_cost_saving"]["median"] >= 0.05:
                verdict = "meets_preregistered_effect_threshold"
            else:
                verdict = "benefit_below_preregistered_threshold"
        args.output.with_suffix(".summary.json").write_text(
            json.dumps({"verdict":verdict, "groups":groups}, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--stage", choices=("inspect", "preflight", "full"), default="inspect")
    parser.add_argument("--model", default="deepseek-flash")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--pricing", type=Path, default=Path(__file__).with_name("pricing-2026-11-16.json"))
    parser.add_argument("--tariff", choices=("peak", "off_peak"), default="peak")
    args = parser.parse_args()
    cases = load_cases(args.corpus)
    count = 5
    if args.stage == "inspect":
        print(json.dumps({"sha256":hashlib.sha256(args.corpus.read_bytes()).hexdigest(),
            "cases_by_group":dict(Counter(c["group"] for c in cases)),
            "source_kinds":dict(Counter(c["source_kind"] for c in cases))}, ensure_ascii=False, indent=2))
        sys.exit(0)
    if any(sum(case["group"] == group for case in cases) != count for group in GROUPS):
        parser.error(f"{args.stage} requires exactly {count} cases per group")
    if args.output is None or args.output.suffix != ".jsonl" or any(args.output.with_suffix(s).exists() for s in (".jsonl", ".csv", ".trace.jsonl", ".manifest.json", ".summary.json")):
        parser.error("Use a fresh --output .jsonl path")
    for ordinal, case in enumerate(cases):
        case["ordinal"] = ordinal
        case["corpus_size"] = len(cases)
    asyncio.run(run(args, cases, json.loads(args.pricing.read_text(encoding="utf-8"))))
