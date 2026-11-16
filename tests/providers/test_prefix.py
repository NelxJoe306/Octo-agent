import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import httpx

from agent_core.messages import UserMessage
from model_adapters.env import OpenAICompatibleConfig
from model_adapters.openai_compatible import OpenAICompatibleProvider

from model_adapters.prefix import build_prefix, log_prefix


class PrefixTests(unittest.TestCase):
    def test_observation_tracks_only_request_prefix_without_mutating_it(self):
        payload = {"messages": [{"role":"system","content":"rules"}, {"role":"user","content":"hello"}],
                   "tools": [{"type":"function","function":{"name":"read","parameters":{"type":"object"}}}]}
        before = copy.deepcopy(payload)
        fingerprint = build_prefix(payload)
        self.assertEqual(payload, before)
        payload["messages"][-1]["content"] = "different"
        self.assertEqual(build_prefix(payload), fingerprint)
        payload["tools"][0]["function"]["parameters"]["description"] = "changed"
        self.assertNotEqual(build_prefix(payload)["tools_hash"], fingerprint["tools_hash"])
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {"OCTO_PREFIX_LOG":str(Path(temp)/"log.jsonl")}):
            log_prefix(payload, model="fixture", session_id="fixture")
            record = json.loads((Path(temp)/"log.jsonl").read_text(encoding="utf-8"))
            self.assertEqual(record["prefix_hash"], build_prefix(payload)["prefix_hash"])
            self.assertNotIn("rules", json.dumps(record))


class PrefixTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_logged_hash_matches_transmitted_payload(self):
        sent = []
        async def handle(request):
            sent.append(json.loads(request.content))
            body = 'data: {"choices":[{"delta":{"content":"OK"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'
            return httpx.Response(200, text=body, headers={"content-type":"text/event-stream"})
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {"OCTO_PREFIX_LOG":str(Path(temp)/"trace")}):
            async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
                provider = OpenAICompatibleProvider(OpenAICompatibleConfig(api_key="fixture", base_url="https://fixture.invalid/v1"), client=client)
                async for _ in provider.stream_response(model="fixture", system="rules", tools=[],
                        messages=[UserMessage(content="hello")], session_id="session"):
                    pass
            record = json.loads((Path(temp)/"trace").read_text(encoding="utf-8"))
            self.assertEqual(record["prefix_hash"], build_prefix(sent[0])["prefix_hash"])
