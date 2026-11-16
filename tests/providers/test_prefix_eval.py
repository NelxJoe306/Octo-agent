import unittest
from pathlib import Path
import tempfile

from eval.prefix_cache.run_v2 import input_cost, summarize, load_cases, assemble, cache_accounting_ok


class PrefixEvalTests(unittest.TestCase):
    def test_raw_usage_disagreement_fails_accounting(self):
        row = {"input_tokens":100, "cached_input_tokens":80, "raw_usage":{
            "prompt_tokens":100, "prompt_cache_hit_tokens":80, "prompt_cache_miss_tokens":20, "cached_tokens":80}}
        self.assertTrue(cache_accounting_ok(row))
        row["raw_usage"]["cached_tokens"] = 0
        self.assertFalse(cache_accounting_ok(row))
        row["raw_usage"] = {}
        self.assertIsNone(cache_accounting_ok(row))

    def test_pairs_repeats_by_history_and_retains_missing_cases(self):
        cases = [{"id":group+"-0", "group":group} for group in ("short", "long", "dynamic")]
        rows = []
        for case in cases:
            for phase, repeats in (("priming", 1), ("measured", 5)):
                for step in range(repeats):
                    for enabled in (False, True):
                        rows.append({"id":case["id"], "group":case["group"], "phase":phase, "enabled":enabled,
                            "status":"stop", "output_ok":True, "retries":0, "input_tokens":100,
                            "cached_input_tokens":80 if enabled else 40, "first_text_ms":100+step})
        rates = {"hit":1, "miss":10}
        groups = summarize(rows, cases, "full", rates)
        short = next(g for g in groups if g["group"] == "short" and g["phase"] == "measured")
        self.assertEqual(short["independent_histories"], 1)
        self.assertEqual(short["paired_ratio_delta"]["n"], 1)
        self.assertAlmostEqual(short["paired_ratio_delta"]["median"], 0.4)
        self.assertAlmostEqual(input_cost(rows[0], rates), 0.00064)
        rows[-1]["cached_input_tokens"] = None
        dynamic = summarize(rows, cases, "full", rates)[-1]
        self.assertEqual(dynamic["status"], "incomplete")
        self.assertEqual(dynamic["missing_cache_fields"], 1)
        self.assertIsNone(dynamic["paired_ratio_delta"])


class ReplayAssemblyTests(unittest.IsolatedAsyncioTestCase):
    async def test_dynamic_case_changes_only_metadata_position_and_increments_version(self):
        path = Path(__file__).resolve().parents[2]/"eval/prefix_cache/preview-cases.jsonl"
        case = next(case for case in load_cases(path) if case["group"] == "dynamic")
        case.update(ordinal=0, corpus_size=3)
        with tempfile.TemporaryDirectory() as temp:
            baseline = await assemble(case, Path(temp), False, 0, "fixture", "offline")
            optimized = await assemble(case, Path(temp), True, 0, "fixture", "offline")
            next_step = await assemble(case, Path(temp), False, 1, "fixture", "offline")
        metadata = optimized["messages"][-1].text
        self.assertEqual(baseline["system"], optimized["system"]+"\n"+metadata)
        self.assertEqual([m.text for m in baseline["messages"]], [m.text for m in optimized["messages"][:-1]])
        self.assertIn("preference_version=0", baseline["system"])
        self.assertIn("preference_version=3", next_step["system"])
