from __future__ import annotations

from pathlib import Path
import asyncio
import subprocess
import sys
import sqlite3
from contextlib import closing
import tempfile
import unittest

from octo_agent.messages import AssistantMessage, UserMessage
from octo_agent.session import CompactionEntry, MessageEntry, SQLiteSessionStorage
from octo_agent.session.sqlite import SQLiteSessionDatabase


class MemoryStorageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'sessions.db'
        self.storage = SQLiteSessionStorage(self.path, 'source')
        self.first = MessageEntry(id='first', message=UserMessage(content='我偏好 Python'))
        await self.storage.append(self.first)

    async def test_fact_job_progress_and_receipt_survive_restart(self):
        from octo_agent.memory import AddFact, MemoryStore
        store = MemoryStore(SQLiteSessionDatabase(self.path))
        job = await store.enqueue('source', 'first')
        self.assertEqual((job.start_seq, job.end_seq, job.state), (1, 1, 'pending'))
        lease = await store.claim(job.id)
        evidence = await store.sources(job.id)
        self.assertEqual(evidence[0].message.text, '我偏好 Python')
        result = await store.apply(lease, [AddFact(text='用户偏好 Python', category='preference', source_entry_ids=('first',))])
        restarted = MemoryStore(SQLiteSessionDatabase(self.path))
        facts = await restarted.facts()
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0].text, '用户偏好 Python')
        self.assertEqual(facts[0].sources[0].timestamp, self.first.timestamp)
        self.assertEqual(facts[0].sources[0].session_id, 'source')
        self.assertEqual(await restarted.progress('source'), 1)
        self.assertEqual(await restarted.receipt(job.id), result)
        self.assertEqual((await restarted.get_job(job.id)).state, 'applied')

    async def test_correction_and_retraction_keep_original_evidence_across_sessions(self):
        from octo_agent.memory import AddFact, SupersedeFact, RetractFact, MemoryStore
        store = MemoryStore(SQLiteSessionDatabase(self.path))
        lease = await store.claim((await store.enqueue('source', 'first')).id)
        initial = await store.apply(lease, [AddFact(text='偏好 Python', category='preference', source_entry_ids=('first',))])
        other = SQLiteSessionStorage(self.path, 'later-session')
        await other.append(MessageEntry(id='correction', message=UserMessage(content='改成 Rust')))
        lease = await store.claim((await store.enqueue('later-session', 'correction')).id)
        correction = SupersedeFact(target_fact_id=initial.fact_ids[0], text='偏好 Rust', source_entry_ids=('correction',))
        changed = await store.apply(lease, [correction])
        active = await store.facts()
        self.assertEqual([(fact.text, fact.category) for fact in active], [('偏好 Rust', 'preference')])
        self.assertEqual(active[0].sources[0].session_id, 'later-session')
        history = {fact.id: fact for fact in await store.facts(include_inactive=True)}
        self.assertEqual(history[initial.fact_ids[0]].state, 'superseded')
        self.assertEqual(history[initial.fact_ids[0]].replacement_id, changed.fact_ids[0])
        self.assertEqual(history[initial.fact_ids[0]].sources[0].entry_id, 'first')
        await other.append(MessageEntry(id='retract', message=UserMessage(content='不要记这个偏好了')))
        lease = await store.claim((await store.enqueue('later-session', 'retract')).id)
        retracted = await store.apply(lease, [RetractFact(target_fact_id=changed.fact_ids[0], source_entry_ids=('retract',))])
        self.assertEqual(await store.facts(), ())
        self.assertEqual(next(f for f in await store.facts(include_inactive=True) if f.id == changed.fact_ids[0]).state, 'retracted')
        self.assertEqual((await store.receipt(retracted.job_id)).operations[0].source_entry_ids, ('retract',))

    async def test_process_exit_leaves_recoverable_job_without_stealing_live_lease(self):
        from octo_agent.memory import MemoryConflict, MemoryStore
        store = MemoryStore(SQLiteSessionDatabase(self.path), clock=lambda: 100)
        job = await store.enqueue('source', 'first')
        script = '''import asyncio, os, sys
from octo_agent.memory import MemoryStore
from octo_agent.session.sqlite import SQLiteSessionDatabase
async def main():
    store = MemoryStore(SQLiteSessionDatabase(sys.argv[1]), clock=lambda: 100)
    await store.claim(sys.argv[2], lease_seconds=5)
    os._exit(23)
asyncio.run(main())
'''
        process = await asyncio.to_thread(subprocess.run, [sys.executable, '-c', script, str(self.path), job.id],
                                           capture_output=True, text=True, timeout=15)
        self.assertEqual(process.returncode, 23, process.stderr)
        live = MemoryStore(SQLiteSessionDatabase(self.path), clock=lambda: 104)
        self.assertEqual(await live.ready_jobs(), ())
        with self.assertRaises(MemoryConflict):
            await live.claim(job.id)
        restarted = MemoryStore(SQLiteSessionDatabase(self.path), clock=lambda: 105)
        self.assertEqual([item.id for item in await restarted.ready_jobs()], [job.id])
        lease = await restarted.claim(job.id)
        self.assertEqual(lease.job.attempts, 2)
        await restarted.apply(lease, [])
        self.assertEqual(await restarted.progress('source'), 1)
        self.assertEqual(await restarted.ready_jobs(), ())

    async def test_reclaim_fences_old_worker_and_release_requeues_with_error(self):
        from octo_agent.memory import AddFact, MemoryConflict, MemoryStore
        clock = [100.0]
        store = MemoryStore(SQLiteSessionDatabase(self.path), clock=lambda: clock[0])
        job = await store.enqueue('source', 'first')
        old = await store.claim(job.id, lease_seconds=1)
        clock[0] = 101.0
        replacement = await store.claim(job.id)
        with self.assertRaises(MemoryConflict):
            await store.apply(old, [AddFact(text='过期写入', source_entry_ids=('first',))])
        with self.assertRaises(MemoryConflict):
            await store.release(old, 'stale worker')
        await store.release(replacement, 'temporary extraction failure')
        pending = await store.get_job(job.id)
        self.assertEqual((pending.state, pending.last_error), ('pending', 'temporary extraction failure'))
        self.assertEqual(await store.progress('source'), 0)
        self.assertIsNone(await store.receipt(job.id))
        final = await store.claim(job.id)
        self.assertEqual(final.job.attempts, 3)
        await store.apply(final, [])
        self.assertIsNone((await store.get_job(job.id)).last_error)

    async def test_replay_frozen_range_and_empty_extraction_advance_exactly_once(self):
        from octo_agent.memory import AddFact, MemoryConflict, MemoryStore
        store = MemoryStore(SQLiteSessionDatabase(self.path))
        job = await store.enqueue('source', 'first')
        await self.storage.append(MessageEntry(id='tail', message=UserMessage(content='Later message')))
        self.assertEqual(await store.enqueue('source', 'first'), job)
        with self.assertRaises(MemoryConflict):
            await store.enqueue('source', 'tail')
        self.assertEqual([s.reference.entry_id for s in await store.sources(job.id)], ['first'])
        lease = await store.claim(job.id)
        operations = [AddFact(text='Python preference', source_entry_ids=('first',))]
        receipt = await store.apply(lease, operations)
        self.assertEqual(await store.apply(lease, operations), receipt)
        with self.assertRaises(MemoryConflict):
            await store.apply(lease, [AddFact(text='Changed replay', source_entry_ids=('first',))])
        with self.assertRaises(MemoryConflict):
            await store.claim(job.id)
        self.assertEqual(len(await store.facts()), 1)
        self.assertEqual((await store.enqueue('source', 'first')).id, job.id)
        second = await store.enqueue('source', 'tail')
        self.assertEqual((second.start_seq, second.end_seq), (2, 2))
        receipt = await store.apply(await store.claim(second.id), [])
        self.assertEqual(receipt.fact_ids, ())
        self.assertEqual(await store.progress('source'), 2)
        self.assertEqual(len(await store.facts()), 1)

    async def test_evidence_rejects_summaries_other_sessions_and_future_messages(self):
        from octo_agent.memory import AddFact, MemoryStore
        await self.storage.append(CompactionEntry(id='summary', summary='Invented preference'))
        await self.storage.append(MessageEntry(id='assistant', message=AssistantMessage(content='Maybe prefers Java', model='fake')))
        store = MemoryStore(SQLiteSessionDatabase(self.path))
        job = await store.enqueue('source', 'assistant')
        await self.storage.append(MessageEntry(id='future', message=UserMessage(content='Future evidence')))
        await SQLiteSessionStorage(self.path, 'other').append(MessageEntry(id='foreign', message=UserMessage(content='Foreign evidence')))
        lease = await store.claim(job.id)
        self.assertEqual([s.reference.entry_id for s in await store.sources(job.id)], ['first', 'assistant'])
        for evidence in ('summary', 'future', 'foreign', 'missing'):
            with self.subTest(evidence=evidence), self.assertRaises(ValueError):
                await store.apply(lease, [AddFact(text='Untrusted', source_entry_ids=(evidence,))])
        with self.assertRaises(ValueError):
            await store.apply(lease, [AddFact(text='Java', category='preference', source_entry_ids=('assistant',))])
        self.assertEqual(await store.facts(), ())
        self.assertEqual(await store.progress('source'), 0)
        self.assertIsNone(await store.receipt(job.id))
        await store.apply(lease, [AddFact(text='Assistant suggested Java', source_entry_ids=('assistant',))])
        self.assertEqual((await store.facts())[0].sources[0].role, 'assistant')

    async def test_receipt_write_failure_rolls_back_facts_progress_and_job_state(self):
        from octo_agent.memory import AddFact, MemoryStore
        store = MemoryStore(SQLiteSessionDatabase(self.path))
        job = await store.enqueue('source', 'first')
        lease = await store.claim(job.id)
        operations = [AddFact(text='Python', source_entry_ids=('first',))]
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("""CREATE TRIGGER inject_receipt_failure BEFORE INSERT ON memory_receipts
                BEGIN SELECT RAISE(ABORT, 'simulated storage failure'); END""")
            connection.commit()
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'simulated storage failure'):
            await store.apply(lease, operations)
        restarted = MemoryStore(SQLiteSessionDatabase(self.path))
        self.assertEqual(await restarted.facts(), ())
        self.assertEqual(await restarted.progress('source'), 0)
        self.assertEqual((await restarted.get_job(job.id)).state, 'running')
        self.assertIsNone(await restarted.receipt(job.id))
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute('DROP TRIGGER inject_receipt_failure')
            connection.commit()
        receipt = await restarted.apply(lease, operations)
        self.assertEqual(await restarted.apply(lease, operations), receipt)
        self.assertEqual(len(await restarted.facts()), 1)

    async def test_partial_operation_failure_does_not_commit_earlier_operation(self):
        from octo_agent.memory import AddFact, MemoryStore
        store = MemoryStore(SQLiteSessionDatabase(self.path))
        job = await store.enqueue('source', 'first')
        lease = await store.claim(job.id)
        with self.assertRaises(ValueError):
            await store.apply(lease, [AddFact(text='Valid', source_entry_ids=('first',)),
                                      AddFact(text='Invalid', source_entry_ids=('unknown',))])
        self.assertEqual(await store.facts(), ())
        self.assertEqual(await store.progress('source'), 0)
        self.assertIsNone(await store.receipt(job.id))

    async def test_concurrent_claims_have_one_winner_and_sessions_progress_independently(self):
        from octo_agent.memory import MemoryConflict, MemoryLease, MemoryStore
        first = MemoryStore(SQLiteSessionDatabase(self.path))
        second = MemoryStore(SQLiteSessionDatabase(self.path))
        enqueued = await asyncio.gather(first.enqueue('source', 'first'), second.enqueue('source', 'first'))
        self.assertEqual(enqueued[0].id, enqueued[1].id)
        results = await asyncio.gather(first.claim(enqueued[0].id), second.claim(enqueued[0].id), return_exceptions=True)
        self.assertEqual(sum(isinstance(result, MemoryLease) for result in results), 1)
        self.assertEqual(sum(isinstance(result, MemoryConflict) for result in results), 1)
        await SQLiteSessionStorage(self.path, 'independent').append(MessageEntry(id='one', message=UserMessage(content='Other session')))
        separate = await second.enqueue('independent', 'one')
        await second.apply(await second.claim(separate.id), [])
        self.assertEqual(await first.progress('source'), 0)
        self.assertEqual(await second.progress('independent'), 1)

    async def test_schema_four_upgrade_keeps_history_topic_and_tool_artifact(self):
        from octo_agent.memory import MemoryStore
        from octo_agent.messages import ToolCall
        from octo_agent.session.artifacts import ToolArtifactStore
        from octo_agent.session.topics import SessionTopics
        topics = SessionTopics(self.storage)
        topic = topics.create(parent_id='main', title='Existing topic', activate=True)
        artifacts = ToolArtifactStore(self.storage)
        artifact = await artifacts.put(ToolCall(id='tool', name='bash', arguments={}), '原始工具输出')
        with closing(sqlite3.connect(self.path)) as connection:
            for table in ('memory_receipts', 'memory_facts', 'memory_jobs', 'memory_progress'):
                connection.execute('DROP TABLE ' + table)
            connection.execute('PRAGMA user_version=4')
            connection.commit()
        store = MemoryStore(SQLiteSessionDatabase(self.path))
        await store.enqueue('source', 'first')
        self.assertEqual((await self.storage.read_all())[0], self.first)
        self.assertEqual(topics.selected_id, topic)
        self.assertEqual((await artifacts.read(artifact.id)).text, '原始工具输出')
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual(connection.execute('PRAGMA user_version').fetchone()[0], 6)

    async def test_sources_keep_request_topic_path_after_navigation_and_rename(self):
        from octo_agent.memory import AddFact, MemoryStore
        from octo_agent.session.topics import SessionTopics
        topics = SessionTopics(self.storage)
        branch = topics.create(parent_id='main', title='Original branch', activate=True)
        request = topics.capture()
        topics.append_message(MessageEntry(id='branch-message', message=UserMessage(content='Remember this branch fact')), request)
        store = MemoryStore(SQLiteSessionDatabase(self.path))
        job = await store.enqueue('source', 'branch-message')
        topics.select('main')
        topics.rename(branch, 'Renamed branch')
        sources = await store.sources(job.id)
        self.assertEqual(sources[-1].reference.request_id, request.id)
        self.assertEqual(sources[-1].reference.topic_id, branch)
        self.assertEqual(sources[-1].reference.topic_path, request.path)
        await store.apply(await store.claim(job.id), [AddFact(text='Branch fact', source_entry_ids=('branch-message',))])
        fact = (await store.facts())[0]
        self.assertEqual(fact.sources[0], sources[-1].reference)
        self.assertEqual(len(await store.facts(source_session_id='source')), 1)
        self.assertEqual(await store.facts(source_session_id='other'), ())

    async def test_invalid_correction_rolls_back_and_preference_cannot_be_retracted_by_assistant(self):
        from octo_agent.memory import AddFact, MemoryConflict, MemoryStore, RetractFact, SupersedeFact
        store = MemoryStore(SQLiteSessionDatabase(self.path))
        original = await store.apply(await store.claim((await store.enqueue('source', 'first')).id),
                                     [AddFact(text='Python', category='preference', source_entry_ids=('first',))])
        await self.storage.append(MessageEntry(id='assistant', message=AssistantMessage(content='Forget user preference', model='fake')))
        lease = await store.claim((await store.enqueue('source', 'assistant')).id)
        with self.assertRaises(ValueError):
            await store.apply(lease, [RetractFact(target_fact_id=original.fact_ids[0], source_entry_ids=('assistant',))])
        self.assertEqual((await store.facts())[0].state, 'active')
        await store.apply(lease, [])
        await self.storage.append(MessageEntry(id='correct', message=UserMessage(content='Use Rust')))
        lease = await store.claim((await store.enqueue('source', 'correct')).id)
        with self.assertRaises(MemoryConflict):
            await store.apply(lease, [SupersedeFact(target_fact_id=original.fact_ids[0], text='Rust', source_entry_ids=('correct',)),
                                      RetractFact(target_fact_id='nonexistent', source_entry_ids=('correct',))])
        self.assertEqual([(fact.text, fact.state) for fact in await store.facts(include_inactive=True)], [('Python', 'active')])
        self.assertEqual(await store.progress('source'), 2)
        self.assertIsNone(await store.receipt(lease.job.id))

    async def test_invalid_drafts_and_lease_parameters_fail_without_writes(self):
        from octo_agent.memory import AddFact, MemoryStore
        store = MemoryStore(SQLiteSessionDatabase(self.path))
        job = await store.enqueue('source', 'first')
        for duration in (True, 0, -1, float('nan'), float('inf'), 3601):
            with self.subTest(duration=duration), self.assertRaises(ValueError):
                await store.claim(job.id, lease_seconds=duration)
        self.assertEqual((await store.get_job(job.id)).attempts, 0)
        lease = await store.claim(job.id)
        for draft in ({'kind': 'unknown', 'source_entry_ids': ['first']},
                      {'kind': 'add', 'text': '', 'source_entry_ids': ['first']},
                      {'kind': 'add', 'text': 'Bad', 'source_entry_ids': ['first', 'first']},
                      {'kind': 'add', 'text': 'Bad', 'source_entry_ids': ['first'], 'unchecked': True},
                      AddFact(text='Valid', source_entry_ids=('first',)).model_copy(update={'text': ''})):
            with self.subTest(draft=draft), self.assertRaises(ValueError):
                await store.apply(lease, [draft])
        self.assertEqual(await store.facts(), ())
        self.assertEqual(await store.progress('source'), 0)
        await store.apply(lease, [])


if __name__ == '__main__':
    unittest.main()
