from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
import asyncio
import subprocess
import sys
from unittest.mock import patch

from octo_agent.messages import UserMessage
from octo_agent.session import MessageEntry, SQLiteSessionStorage
from octo_agent.session.sqlite import SQLiteSessionDatabase
from octo_memory import AddFact, MemoryStore


class MemoryMirrorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.storage=SQLiteSessionStorage(self.root/'octo.db','source')
        self.store=MemoryStore(SQLiteSessionDatabase(self.root/'octo.db'))

    async def test_committed_memory_exports_readable_stable_view_with_provenance(self):
        from octo_memory.mirror import MemoryMirror
        await self.storage.append(MessageEntry(id='original',timestamp=0,message=UserMessage(content='我偏好 Python')))
        job=await self.store.enqueue('source','original')
        receipt=await self.store.apply(await self.store.claim(job.id),[
            AddFact(text='用户偏好 Python',category='preference',source_entry_ids=('original',))])
        path=self.root/'memory.md'
        mirror=MemoryMirror(self.store,path)
        self.assertTrue(await mirror.export())
        content=path.read_text(encoding='utf-8')
        self.assertIn('自动生成',content)
        self.assertIn('用户偏好 Python',content)
        self.assertIn(receipt.fact_ids[0],content)
        self.assertIn('1970-01-01',content)
        self.assertIn('original',content)
        self.assertIn('source',content)
        timestamp=path.stat().st_mtime_ns
        self.assertFalse(await mirror.export())
        self.assertEqual(path.stat().st_mtime_ns,timestamp)
        self.assertEqual(await self.store.progress('source'),1)

    async def test_publish_failure_keeps_previous_file_and_committed_database(self):
        from octo_memory.mirror import MemoryMirror
        for index in range(2):
            await self.storage.append(MessageEntry(id=f'm{index}',message=UserMessage(content=f'fact {index}')))
            job=await self.store.enqueue('source',f'm{index}')
            await self.store.apply(await self.store.claim(job.id),[AddFact(text=f'fact {index}',source_entry_ids=(f'm{index}',))])
            if index==0:
                mirror=MemoryMirror(self.store,self.root/'memory.md')
                await mirror.export()
                previous=mirror.path.read_bytes()
        with patch('octo_memory.mirror.os.replace',side_effect=OSError('simulated publish failure')):
            with self.assertRaises(OSError): await mirror.export()
        self.assertEqual(mirror.path.read_bytes(),previous)
        self.assertEqual(len(await self.store.facts()),2)
        self.assertEqual(await self.store.progress('source'),2)
        self.assertEqual(list(self.root.glob('.memory.md.*.tmp')),[])
        await mirror.export()
        self.assertIn('fact 1',mirror.path.read_text(encoding='utf-8'))

    async def test_manual_markdown_edits_are_never_imported_and_concurrent_export_is_stable(self):
        from octo_memory.mirror import MemoryMirror
        await self.storage.append(MessageEntry(id='m',message=UserMessage(content='Python')))
        job=await self.store.enqueue('source','m')
        await self.store.apply(await self.store.claim(job.id),[AddFact(text='Real database preference',category='preference',source_entry_ids=('m',))])
        path=self.root/'memory.md'
        changed=await asyncio.gather(*(MemoryMirror(self.store,path).export() for _ in range(3)))
        self.assertEqual(sum(changed),1)
        path.write_text('Injected fake preference',encoding='utf-8')
        self.assertEqual((await self.store.facts())[0].text,'Real database preference')
        await MemoryMirror(self.store,path).export()
        self.assertNotIn('Injected fake preference',path.read_text(encoding='utf-8'))

    async def test_superseded_and_retracted_facts_remain_in_folded_history(self):
        from octo_memory import SupersedeFact, RetractFact
        from octo_memory.mirror import MemoryMirror
        await self.storage.append(MessageEntry(id='m0',message=UserMessage(content='Python')))
        receipt=await self.store.apply(await self.store.claim((await self.store.enqueue('source','m0')).id),
            [AddFact(text='Python <script>\nsecond line',category='preference',source_entry_ids=('m0',))])
        await self.storage.append(MessageEntry(id='m1',message=UserMessage(content='Rust')))
        updated=await self.store.apply(await self.store.claim((await self.store.enqueue('source','m1')).id),
            [SupersedeFact(target_fact_id=receipt.fact_ids[0],text='Rust',source_entry_ids=('m1',))])
        await self.storage.append(MessageEntry(id='m2',message=UserMessage(content='Forget')))
        await self.store.apply(await self.store.claim((await self.store.enqueue('source','m2')).id),
            [RetractFact(target_fact_id=updated.fact_ids[0],source_entry_ids=('m2',))])
        mirror=MemoryMirror(self.store,self.root/'memory.md')
        await mirror.export()
        content=mirror.path.read_text(encoding='utf-8')
        self.assertIn('<details>',content)
        self.assertIn('已更正',content)
        self.assertIn('已撤回',content)
        self.assertNotIn('<script>',content)
        self.assertIn('&lt;script&gt; second line',content)
        self.assertIn(receipt.fact_ids[0],content)
        self.assertIn(updated.fact_ids[0],content)

    async def test_runtime_export_failure_does_not_retry_applied_job_and_startup_repairs_view(self):
        from octo_memory.mirror import MemoryMirror
        from octo_memory.runtime import MemoryRuntime,MemorySettings
        blocker=self.root/'blocked'
        blocker.write_text('file, not directory',encoding='utf-8')
        await self.storage.append(MessageEntry(id='user',message=UserMessage(content='I prefer Python')))
        async def extract(job,sources,facts):
            return [AddFact(text='Python preference',category='preference',source_entry_ids=('user',))]
        runtime=MemoryRuntime(self.store,'source',extract,MemorySettings(),mirror=MemoryMirror(self.store,blocker/'memory.md'))
        runtime.notify(force=True)
        await runtime.wait_idle()
        self.assertIsNone(runtime.last_error)
        self.assertIsNotNone(runtime.last_export_error)
        self.assertEqual(await self.store.progress('source'),1)
        self.assertIsNone(await self.store.pending_job('source'))
        await runtime.aclose()
        async def unexpected(*args): self.fail('Already applied job was extracted again')
        path=self.root/'memory.md'
        restarted=MemoryRuntime(self.store,'next',unexpected,MemorySettings(),mirror=MemoryMirror(self.store,path))
        self.addAsyncCleanup(restarted.aclose)
        restarted.notify()
        await restarted.wait_idle()
        self.assertIn('Python preference',path.read_text(encoding='utf-8'))

    async def test_export_command_rebuilds_view_without_starting_agent(self):
        await self.storage.append(MessageEntry(id='user',message=UserMessage(content='Python')))
        await self.store.apply(await self.store.claim((await self.store.enqueue('source','user')).id),
            [AddFact(text='Command export preference',category='preference',source_entry_ids=('user',))])
        repo=Path(__file__).resolve().parents[2]
        command=[sys.executable,str(repo/'tools/export_memory.py'),'--home',str(self.root)]
        result=await asyncio.to_thread(subprocess.run,command,capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn('Command export preference',(self.root/'memory.md').read_text(encoding='utf-8'))
        again=await asyncio.to_thread(subprocess.run,command,capture_output=True,text=True,timeout=10)
        self.assertEqual(again.returncode,0,again.stderr)
        self.assertIn('Unchanged',again.stdout)


if __name__=='__main__':
    unittest.main()
