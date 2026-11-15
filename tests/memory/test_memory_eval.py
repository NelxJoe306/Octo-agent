import json
from pathlib import Path
import tempfile
import unittest

from eval.memory.run import CASES, evaluate
from long_term_memory import AddFact, SupersedeFact


class MemoryEvalTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        cases = [json.loads(line) for line in CASES.read_text(encoding="utf-8").splitlines()]
        self.cases = {case["id"]: case for case in cases}
        self.assertEqual(len(self.cases), 20)
        self.assertEqual(sum(c["expect"] == "ignore" for c in cases), 10)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    async def check(self, case_id, extract):
        return await evaluate(self.cases[case_id], Path(self.temp.name), extract)

    async def test_negative_written_as_generic_fact_is_false_memory(self):
        async def extract(job, sources, facts):
            return [AddFact(text="当前使用 uv", category="fact", source_entry_ids=("b0-0",))]
        result = await self.check("n-package", extract)
        self.assertEqual(result["reason"], "unexpected_write")
        self.assertEqual(result["recalled"], [])

    async def test_worker_error_cannot_pass_as_ignored(self):
        async def broken(*args):
            raise ValueError("invalid draft")
        with self.assertRaisesRegex(RuntimeError, "invalid draft"):
            await self.check("n-package", broken)

    async def test_old_preference_must_be_superseded_in_probe(self):
        async def extract(job, sources, facts):
            if facts:
                return [SupersedeFact(text="提交信息使用中文", target_fact_id=facts[0].id, source_entry_ids=("b1-0",))]
            return [AddFact(text="提交信息使用英文", category="preference", source_entry_ids=("b0-0",))]
        result = await self.check("p-update", extract)
        self.assertEqual(result["status"], "pass")
        self.assertEqual([f["text"] for f in result["recalled"]], ["提交信息使用中文"])

    async def test_second_add_does_not_count_as_correction(self):
        async def duplicate(job, sources, facts):
            text = "提交信息使用中文" if facts else "提交信息使用英文"
            return [AddFact(text=text, category="preference", source_entry_ids=(sources[0].reference.entry_id,))]
        result = await self.check("p-update", duplicate)
        self.assertEqual(result["status"], "fail")
        self.assertEqual(len(result["recalled"]), 2)
