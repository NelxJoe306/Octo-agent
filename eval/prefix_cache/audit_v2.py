import asyncio
from collections import Counter
import copy
from dataclasses import replace
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import statistics
import sys
import tempfile
from unittest.mock import patch

import httpx

repo=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(repo))
sys.path.insert(0,str(repo/"src"))
from eval.prefix_cache.run_v2 import load_cases,assemble
from model_adapters.openai_compatible import OpenAICompatibleProvider
from model_adapters.env import OpenAICompatibleConfig

data=repo/"eval/prefix_cache/results-v2-2026-11-16"
rows=[json.loads(s) for s in (data/"run.jsonl").read_text(encoding="utf-8").splitlines()]
corpus=repo/"eval/prefix_cache/corpus-v2-2026-11-16.jsonl"
manifest=json.loads((data/"run.manifest.json").read_text(encoding="utf-8"))
assert hashlib.sha256(corpus.read_bytes()).hexdigest()==manifest["corpus_sha256"]
keys=[(r["id"],r["step"],r["enabled"]) for r in rows]
assert len(rows)==180 and len(set(keys))==180
assert Counter(r["phase"] for r in rows)=={"priming":30,"measured":150}
assert all(r["status"]=="stop" and r["output_ok"] and r["retries"]==0 and 0<=r["cached_input_tokens"]<=r["input_tokens"] for r in rows)
independent=[]
saved=json.loads((data/"run.summary.json").read_text(encoding="utf-8"))
for group in ("short","long","dynamic"):
    deltas=[]
    for index in range(1,6):
        id=f"{group}-{index:02d}"
        medians={}
        for enabled in (False,True):
            part=[r for r in rows if r["id"]==id and r["enabled"]==enabled and r["phase"]=="measured"]
            assert len(part)==5 and {r["step"] for r in part}==set(range(1,6))
            medians[enabled]=statistics.median(Decimal(r["cached_input_tokens"])/Decimal(r["input_tokens"]) for r in part)
        deltas.append(medians[True]-medians[False])
    value=statistics.median(deltas)*100
    old=next(g for g in saved["groups"] if g["group"]==group and g["phase"]=="measured")["paired_ratio_delta"]["median"]*100
    assert abs(float(value)-old)<1e-9
    independent.append({"group":group,"independent_delta_pp":str(value),
        "recorded_total_input":sum(r["input_tokens"] for r in rows if r["group"]==group)})

async def audit_transport():
    cases=load_cases(corpus)
    campaign=hashlib.sha256(corpus.read_bytes()).hexdigest()
    payloads=[]
    async def handle(request):
        payloads.append(json.loads(request.content))
        body='data: {"choices":[{"delta":{"content":"OK"},"finish_reason":"stop"}],"usage":{"prompt_tokens":100,"prompt_cache_hit_tokens":80,"prompt_cache_miss_tokens":20,"prompt_tokens_details":{"cached_tokens":80},"completion_tokens":1}}\n\ndata: [DONE]\n\n'
        return httpx.Response(200,text=body,headers={"content-type":"text/event-stream"})
    checked=0
    with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ,{"OCTO_PREFIX_LOG":""}):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            provider=OpenAICompatibleProvider(OpenAICompatibleConfig(api_key="fixture",base_url="https://fixture.invalid/v1",infer_api_from_model=False,max_tokens=32),client=client)
            for ordinal,case in enumerate(cases):
                if case["group"]!="dynamic":continue
                case.update(ordinal=ordinal,corpus_size=15)
                for step in range(6):
                    pair={}
                    for enabled in (False,True):
                        payload=await assemble(case,Path(temp),enabled,step,campaign,"deepseek-flash")
                        with patch.dict(os.environ,{"OCTO_DISABLE_PREFIX_CACHE":"0"}):
                            async for _ in provider.stream_response(**payload):pass
                        pair[enabled]=copy.deepcopy(payloads[-1])
                    a,b=pair[False],pair[True]
                    marker=b["messages"].pop()
                    assert marker["role"]=="user" and marker["content"].startswith("Evaluation metadata")
                    suffix="\n"+marker["content"]
                    assert a["messages"][0]["content"].endswith(suffix)
                    a["messages"][0]["content"]=a["messages"][0]["content"][:-len(suffix)]
                    assert a==b,(case["id"],step)
                    checked+=1
    return checked

checked=asyncio.run(audit_transport())
report={"rows":180,"paired_measurement_requests":150,"independent_recalculation":independent,
        "dynamic_mock_http_pairs_equal_after_relocating_metadata":checked,
        "historical_raw_server_usage_values_retained":False}
(data/"audit.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
print(json.dumps(report,ensure_ascii=False,indent=2))
