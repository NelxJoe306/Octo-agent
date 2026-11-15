from __future__ import annotations

import asyncio
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from octo_memory import AddFact, MemoryStore
from octo_agent.messages import UserMessage
from octo_agent.session import MessageEntry, SQLiteSessionStorage
from octo_agent.session.sqlite import SQLiteSessionDatabase


class AsyncMemoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'sessions.db'
        self.storage = SQLiteSessionStorage(self.path, 'one')
        self.store = MemoryStore(SQLiteSessionDatabase(self.path))

    async def test_background_call_does_not_wait_for_model_and_next_session_recalls_preference(self):
        from octo_memory.runtime import MemoryRuntime, MemorySettings
        entered = asyncio.Event()
        finish = asyncio.Event()
        async def extract(job, sources, facts):
            self.assertIsNotNone(await self.store.get_job(job.id))
            entered.set()
            await finish.wait()
            return [AddFact(text='用户偏好 Python', category='preference', source_entry_ids=(sources[0].reference.entry_id,))]
        runtime = MemoryRuntime(self.store, 'one', extract, MemorySettings(message_threshold=1))
        self.addAsyncCleanup(runtime.aclose)
        await self.storage.append(MessageEntry(id='preference', message=UserMessage(content='记住我偏好 Python')))
        runtime.notify()
        await asyncio.wait_for(entered.wait(), 2)
        self.assertEqual(await self.store.progress('one'), 0)
        self.assertIsNone(await self.store.receipt((await self.store.pending_job('one')).id))
        finish.set()
        await runtime.wait_idle()
        other = MemoryRuntime(self.store, 'two', extract, MemorySettings())
        self.addAsyncCleanup(other.aclose)
        self.assertIn('用户偏好 Python', await other.recall(max_tokens=1000))
        self.assertEqual(await self.store.progress('one'), 1)

    async def test_extractor_uses_separate_tool_free_request_and_validates_model_draft(self):
        from octo_memory.extraction import extract_operations
        from octo_ai.fake import FakeProvider
        from octo_agent.messages import AssistantMessage
        from octo_agent.provider_events import AssistantDoneEvent
        await self.storage.append(MessageEntry(id='preference', message=UserMessage(content='我偏好 Python')))
        job = await self.store.enqueue('one', 'preference')
        message = AssistantMessage(content='[{"kind":"add","text":"用户偏好 Python","category":"preference","source_entry_ids":["preference"]}]', model='same-model')
        provider = FakeProvider([[AssistantDoneEvent(reason='stop', message=message)]])
        operations = await extract_operations(provider, model='same-model', window=8192, job=job,
                                               sources=await self.store.sources(job.id), facts=())
        self.assertEqual(operations[0].category, 'preference')
        self.assertEqual(provider.calls[0][0], 'same-model')
        self.assertEqual(provider.calls[0][3], [])
        self.assertEqual(provider.session_ids, ['memory:' + job.id])
        self.assertIn('preference', provider.calls[0][2][0].text)

    async def test_session_replies_before_memory_finishes_and_next_request_has_bounded_recall(self):
        from octo_coding.session import CodingSession, CodingSessionConfig
        from octo_memory.runtime import MemorySettings
        from octo_coding.paths import OctoPaths
        from octo_coding.resources import OctoResourcePaths
        from octo_ai.fake import FakeProvider
        from octo_agent.messages import AssistantMessage
        from octo_agent.provider_events import AssistantDoneEvent
        root = Path(self.temp.name)
        paths = OctoPaths(home=root/'home', agents_home=root/'agents')
        entered, finish = asyncio.Event(), asyncio.Event()
        async def extract(job, sources, facts):
            entered.set()
            await finish.wait()
            return [AddFact(text='用户偏好 Python', category='preference', source_entry_ids=(sources[0].reference.entry_id,))] if not facts else []
        provider = FakeProvider([[AssistantDoneEvent(reason='stop', message=AssistantMessage(content='收到', model='offline'))],
                                 [AssistantDoneEvent(reason='stop', message=AssistantMessage(content='继续', model='offline'))]])
        config = CodingSessionConfig(provider=provider, model='offline', storage=self.storage, cwd=root,
            system='Test assistant', tools=[], extensions_enabled=False, skills_enabled=False,
            resource_paths=OctoResourcePaths(root=paths.home, agents_root=paths.agents_home, cwd=root, paths=paths),
            memory_settings=MemorySettings(), memory_extractor=extract)
        with patch.dict(os.environ, {'OCTO_HOME':str(paths.home),'OCTO_OFFLINE':'1'}):
            session = await CodingSession.load(config)
            self.addAsyncCleanup(session.aclose)
            async def first_turn():
                return [event async for event in session.prompt('记住我偏好 Python')]
            await asyncio.wait_for(first_turn(), 3)
            await asyncio.wait_for(entered.wait(), 2)
            self.assertEqual(len(provider.calls), 1)
            self.assertNotIn('Stored user preferences', provider.calls[0][2][-1].text)
            finish.set()
            await session.flush_memory()
            _ = [event async for event in session.prompt('继续帮我做事')]
            self.assertIn('Stored user preferences', provider.calls[1][2][-1].text)
            self.assertIn('用户偏好 Python', provider.calls[1][2][-1].text)
            self.assertIsNotNone(session.last_context_budget)
            self.assertFalse(any('Stored user preferences' in entry.message.text for entry in await self.storage.read_all() if isinstance(entry, MessageEntry)))

    async def test_threshold_counts_original_messages_and_excludes_summaries(self):
        from octo_memory.runtime import MemoryRuntime, MemorySettings
        from octo_agent.session import CompactionEntry
        from octo_agent.messages import AssistantMessage, ToolResultMessage
        calls = []
        async def extract(job, sources, facts):
            calls.append(tuple(source.reference.role for source in sources))
            return []
        runtime = MemoryRuntime(self.store, 'one', extract, MemorySettings(message_threshold=3))
        self.addAsyncCleanup(runtime.aclose)
        await self.storage.append(CompactionEntry(id='summary', summary='Not original evidence'))
        await self.storage.append(MessageEntry(id='user', message=UserMessage(content='hello')))
        runtime.notify()
        await runtime.wait_idle()
        self.assertEqual(calls, [])
        await self.storage.append(MessageEntry(id='assistant', message=AssistantMessage(content='read', model='fake')))
        await self.storage.append(MessageEntry(id='tool', message=ToolResultMessage(tool_call_id='x', tool_name='read', content='result')))
        runtime.notify()
        await runtime.wait_idle()
        self.assertEqual(calls, [('user', 'assistant', 'toolResult')])
        self.assertEqual(await self.store.progress('one'), 4)

    async def test_small_model_batches_keep_every_original_source(self):
        from octo_memory.runtime import MemoryRuntime, MemorySettings
        seen = []
        async def extract(job, sources, facts):
            seen.append(tuple(source.reference.entry_id for source in sources))
            return []
        for number in range(3):
            await self.storage.append(MessageEntry(id=f'm{number}', message=UserMessage(content='message '*30)))
        runtime = MemoryRuntime(self.store, 'one', extract, MemorySettings(message_threshold=3), source_budget=lambda: 150)
        self.addAsyncCleanup(runtime.aclose)
        runtime.notify()
        await runtime.wait_idle()
        self.assertEqual(seen, [('m0',), ('m1',), ('m2',)])
        self.assertEqual(await self.store.progress('one'), 3)

    async def test_normal_close_persists_short_tail_and_cancels_slow_model_for_restart(self):
        from octo_memory.runtime import MemoryRuntime, MemorySettings
        entered = asyncio.Event()
        async def slow(job, sources, facts):
            entered.set()
            await asyncio.Event().wait()
        await self.storage.append(MessageEntry(id='tail', message=UserMessage(content='Small conversation')))
        runtime = MemoryRuntime(self.store, 'one', slow, MemorySettings(close_timeout=0.05))
        runtime.notify(force=True)
        await asyncio.wait_for(entered.wait(), 2)
        await asyncio.wait_for(runtime.aclose(), 2)
        self.assertTrue(entered.is_set())
        pending = await self.store.pending_job('one')
        self.assertIsNotNone(pending)
        self.assertEqual(await self.store.progress('one'), 0)
        self.assertIsNone(await self.store.receipt(pending.id))
        async def recovered(job, sources, facts):
            return []
        restarted = MemoryRuntime(self.store, 'two', recovered, MemorySettings())
        self.addAsyncCleanup(restarted.aclose)
        restarted.notify()
        await restarted.wait_idle()
        self.assertEqual(await self.store.progress('one'), 1)

    async def test_normal_close_extracts_tail_below_threshold(self):
        from octo_memory.runtime import MemoryRuntime, MemorySettings
        seen = []
        async def extract(job, sources, facts):
            seen.extend(source.reference.entry_id for source in sources)
            return []
        await self.storage.append(MessageEntry(id='short', message=UserMessage(content='short conversation')))
        runtime = MemoryRuntime(self.store, 'one', extract, MemorySettings(message_threshold=32))
        await runtime.aclose()
        self.assertEqual(seen, ['short'])
        self.assertEqual(await self.store.progress('one'), 1)

    async def test_close_keeps_later_tail_durable_behind_running_job_for_other_session_recovery(self):
        from octo_memory.runtime import MemoryRuntime, MemorySettings
        entered = asyncio.Event()
        async def slow(job, sources, facts):
            entered.set()
            await asyncio.Event().wait()
        await self.storage.append(MessageEntry(id='first', message=UserMessage(content='first turn')))
        runtime = MemoryRuntime(self.store, 'one', slow, MemorySettings(close_timeout=0.05))
        runtime.notify(force=True)
        await asyncio.wait_for(entered.wait(), 2)
        await self.storage.append(MessageEntry(id='later-tail', message=UserMessage(content='final preference in later tail')))
        await runtime.aclose()
        seen = []
        async def recovered(job, sources, facts):
            seen.extend(source.reference.entry_id for source in sources)
            return []
        restarted = MemoryRuntime(self.store, 'different-session', recovered, MemorySettings())
        self.addAsyncCleanup(restarted.aclose)
        restarted.notify()
        await restarted.wait_idle()
        self.assertEqual(await self.store.progress('one'), 2)
        self.assertEqual(seen, ['first', 'later-tail'])

    async def test_extraction_respects_native_output_ceiling(self):
        from octo_memory.extraction import extract_operations
        from octo_ai.fake import FakeProvider
        from octo_agent.messages import AssistantMessage
        from octo_agent.provider_events import AssistantDoneEvent
        class CappedProvider(FakeProvider):
            def __init__(self):
                super().__init__([[AssistantDoneEvent(reason='stop',message=AssistantMessage(content='[]',model='fake'))]])
                self._request_output_tokens = 1024
            def configure_request_budget(self, **kwargs):
                if kwargs['output_override'] > 1024:
                    raise ValueError('Native model output maximum is 1024')
                return super().configure_request_budget(**kwargs)
        await self.storage.append(MessageEntry(id='source',message=UserMessage(content='preference')))
        job = await self.store.enqueue('one','source')
        provider = CappedProvider()
        self.assertEqual(await extract_operations(provider,model='fake',window=8192,job=job,
            sources=await self.store.sources(job.id),facts=()), ())
        self.assertEqual(provider.request_token_budget().max_output_tokens,1024)
        from octo_ai.model_limits import RequestTokenBudget
        from octo_agent.context_budget import ContextBudgetExceeded
        class ExtraSystemProvider(CappedProvider):
            def request_token_budget(self):
                return RequestTokenBudget(self._request_output_tokens, additional_system='X'*6000, context_window=2048)
        restricted=ExtraSystemProvider()
        with self.assertRaises(ContextBudgetExceeded):
            await extract_operations(restricted,model='fake',window=8192,job=job,
                sources=await self.store.sources(job.id),facts=())
        self.assertEqual(restricted.calls,[])

    async def test_restart_detects_explicit_request_without_in_memory_trigger(self):
        from octo_memory.runtime import MemoryRuntime, MemorySettings
        seen = []
        async def extract(job, sources, facts):
            seen.extend(source.reference.entry_id for source in sources)
            return []
        await self.storage.append(MessageEntry(id='explicit', message=UserMessage(content='记住我喜欢简洁回答')))
        runtime = MemoryRuntime(self.store, 'one', extract, MemorySettings(message_threshold=32))
        self.addAsyncCleanup(runtime.aclose)
        runtime.notify()
        await runtime.wait_idle()
        self.assertEqual(seen, ['explicit'])
        self.assertEqual(await self.store.progress('one'), 1)

    async def test_bad_model_result_is_diagnosed_and_can_be_recovered_without_lost_progress(self):
        from octo_memory.runtime import MemoryRuntime, MemorySettings
        async def fail(job, sources, facts):
            raise ValueError('Invalid memory draft')
        runtime = MemoryRuntime(self.store, 'one', fail, MemorySettings(message_threshold=1))
        await self.storage.append(MessageEntry(id='message', message=UserMessage(content='one')))
        runtime.notify()
        await runtime.wait_idle()
        self.assertIn('Invalid memory draft', runtime.last_error)
        self.assertEqual((await self.store.pending_job('one')).state, 'pending')
        self.assertEqual(await self.store.progress('one'), 0)
        await runtime.aclose()
        async def success(job, sources, facts):
            return []
        restarted = MemoryRuntime(self.store, 'one', success, MemorySettings())
        self.addAsyncCleanup(restarted.aclose)
        restarted.notify()
        await restarted.wait_idle()
        self.assertEqual(await self.store.progress('one'), 1)

    async def test_recall_is_bounded_and_excludes_generic_facts(self):
        from octo_memory.runtime import MemoryRuntime, MemorySettings
        await self.storage.append(MessageEntry(id='source', message=UserMessage(content='I prefer Python; project runtime is 3.12')))
        lease = await self.store.claim((await self.store.enqueue('one', 'source')).id)
        await self.store.apply(lease, [AddFact(text='Personal Python preference', category='preference', source_entry_ids=('source',)),
                                       AddFact(text='Project runtime 3.12', source_entry_ids=('source',))])
        async def empty(job, sources, facts): return []
        runtime = MemoryRuntime(self.store, 'two', empty, MemorySettings())
        self.addAsyncCleanup(runtime.aclose)
        recalled = await runtime.recall(max_tokens=1000)
        self.assertIn('Personal Python preference', recalled)
        self.assertNotIn('Project runtime', recalled)
        self.assertEqual(await runtime.recall(max_tokens=1), '')

    async def test_local_settings_validate_and_can_disable_background_work(self):
        from octo_memory.runtime import MemoryRuntime, MemorySettings, load_memory_settings
        from octo_coding.paths import OctoPaths
        import json
        paths = OctoPaths(home=Path(self.temp.name))
        config = paths.home/'settings.json'
        with patch.dict(os.environ, {'OCTO_DISABLE_MEMORY':'0'}):
            self.assertTrue(load_memory_settings(paths.home/'settings.json').enabled)
        config.write_text(json.dumps({'memory': {'enabled': False, 'message_threshold': 8}, 'shellCommandPrefix': ''}), encoding='utf-8')
        settings = load_memory_settings(paths.home/'settings.json')
        self.assertFalse(settings.enabled)
        self.assertEqual(settings.message_threshold, 8)
        async def unexpected(job, sources, facts): self.fail('Disabled memory called model')
        runtime = MemoryRuntime(self.store, 'one', unexpected, settings)
        await self.storage.append(MessageEntry(id='one', message=UserMessage(content='记住')))
        runtime.notify(force=True)
        await runtime.aclose()
        self.assertIsNone(await self.store.pending_job('one'))
        config.write_text('{"memory":{"message_threshold":true}}', encoding='utf-8')
        with self.assertRaises(ValueError): load_memory_settings(paths.home/'settings.json')
        config.write_text('{"memory":{"enabled":true}}', encoding='utf-8')
        with patch.dict(os.environ, {'OCTO_DISABLE_MEMORY':'1'}):
            self.assertFalse(load_memory_settings(paths.home/'settings.json').enabled)

    async def test_model_draft_errors_and_oversized_input_do_not_advance_progress(self):
        from octo_memory.extraction import extract_operations
        from octo_ai.fake import FakeProvider
        from octo_agent.messages import AssistantMessage
        from octo_agent.provider_events import AssistantDoneEvent
        from octo_agent.context_budget import ContextBudgetExceeded
        await self.storage.append(MessageEntry(id='one', message=UserMessage(content='我偏好 Python')))
        job = await self.store.enqueue('one', 'one')
        sources = await self.store.sources(job.id)
        for content in ('not json', '{}', '[{"kind":"unknown"}]',
                        '[{"kind":"retract","target_fact_id":"not-provided","source_entry_ids":["one"]}]'):
            provider = FakeProvider([[AssistantDoneEvent(reason='stop', message=AssistantMessage(content=content, model='fake'))]])
            with self.subTest(content=content), self.assertRaises(ValueError):
                await extract_operations(provider, model='fake', window=8192, job=job, sources=sources, facts=())
        provider = FakeProvider([])
        with self.assertRaises(ContextBudgetExceeded):
            await extract_operations(provider, model='fake', window=512, job=job, sources=sources, facts=())
        self.assertEqual(provider.calls, [])
        self.assertEqual(await self.store.progress('one'), 0)


    async def test_new_and_resumed_sessions_keep_extractor_and_source_identity(self):
        from octo_coding.session import CodingSession, CodingSessionConfig
        from octo_memory.runtime import MemorySettings
        from octo_coding.paths import OctoPaths
        from octo_coding.resources import OctoResourcePaths
        from octo_coding.session_manager import SessionManager
        from octo_ai.fake import FakeProvider
        from octo_agent.messages import AssistantMessage
        from octo_agent.provider_events import AssistantDoneEvent
        root = Path(self.temp.name)
        paths = OctoPaths(home=root/'home', agents_home=root/'agents')
        manager = SessionManager(paths)
        record = manager.create_session(cwd=root, model='offline', title='Memory fixture')
        calls = []
        async def extract(job, sources, facts):
            calls.append((job.session_id, tuple(source.reference.session_id for source in sources)))
            return []
        provider = FakeProvider([[AssistantDoneEvent(reason='stop', message=AssistantMessage(content='done', model='offline'))] for _ in range(3)])
        config = CodingSessionConfig(provider=provider, model='offline', storage=manager.storage_for(record),
            session_id=record.id, session_manager=manager, cwd=root, system='Test', tools=[],
            resource_paths=OctoResourcePaths(root=paths.home, agents_root=paths.agents_home, cwd=root, paths=paths),
            extensions_enabled=False, skills_enabled=False, memory_settings=MemorySettings(), memory_extractor=extract)
        with patch.dict(os.environ, {'OCTO_HOME':str(paths.home),'OCTO_OFFLINE':'1'}):
            session = await CodingSession.load(config)
            self.addAsyncCleanup(session.aclose)
            _ = [event async for event in session.prompt('first short conversation')]
            await session.new_session()
            await session.set_session_name('Second memory fixture')
            new_id = session.session_id
            self.assertNotEqual(new_id, record.id)
            _ = [event async for event in session.prompt('second short conversation')]
            await session.resume(record.id)
            _ = [event async for event in session.prompt('记住第三次对话')]
            await session.flush_memory()
            self.assertTrue(any(identity == record.id for identity, _ in calls))
            self.assertTrue(any(identity == new_id for identity, _ in calls))
            self.assertTrue(all(identity == source for identity, sources in calls for source in sources))
            self.assertEqual(len(provider.calls), 3)

    async def test_aborted_prepared_session_does_not_enqueue_or_call_memory_model(self):
        from octo_coding.session import CodingSession, CodingSessionConfig
        from octo_memory.runtime import MemorySettings
        from octo_coding.paths import OctoPaths
        from octo_coding.resources import OctoResourcePaths
        from octo_ai.fake import FakeProvider
        root = Path(self.temp.name)
        paths = OctoPaths(home=root/'home', agents_home=root/'agents')
        await self.storage.append(MessageEntry(id='tail', message=UserMessage(content='existing tail')))
        async def unexpected(job, sources, facts): self.fail('Aborted preparation called extraction')
        config = CodingSessionConfig(provider=FakeProvider([]), model='offline', storage=self.storage, cwd=root,
            system='Test', tools=[], extensions_enabled=False, skills_enabled=False,
            resource_paths=OctoResourcePaths(root=paths.home, agents_root=paths.agents_home, cwd=root, paths=paths),
            defer_authoritative_writes=True, memory_settings=MemorySettings(), memory_extractor=unexpected)
        with patch.dict(os.environ, {'OCTO_HOME':str(paths.home),'OCTO_OFFLINE':'1'}):
            session = await CodingSession.load(config)
            await session.aclose()
        self.assertIsNone(await self.store.pending_job('one'))


    async def test_preference_snapshot_stays_frozen_during_tool_loop(self):
        from octo_coding.session import CodingSession, CodingSessionConfig
        from octo_memory.runtime import MemorySettings
        from octo_coding.paths import OctoPaths
        from octo_coding.resources import OctoResourcePaths
        from octo_ai.fake import FakeProvider
        from octo_agent.messages import AssistantMessage, ToolCall
        from octo_agent.provider_events import AssistantDoneEvent
        from octo_agent.tools import AgentTool, AgentToolResult
        root = Path(self.temp.name)
        paths = OctoPaths(home=root/'home', agents_home=root/'agents')
        async def extract(job, sources, facts):
            users = [s for s in sources if s.reference.role == 'user']
            return [AddFact(text='用户偏好 Python', category='preference', source_entry_ids=(users[0].reference.entry_id,))] if users and not facts else []
        async def tool(call_id, arguments, signal=None, on_update=None):
            await session.flush_memory()
            return AgentToolResult(content='done')
        provider = FakeProvider([
            [AssistantDoneEvent(reason='toolUse', message=AssistantMessage(content=[ToolCall(id='call',name='wait_memory',arguments={})],model='offline',stop_reason='toolUse'))],
            [AssistantDoneEvent(reason='stop', message=AssistantMessage(content='done',model='offline'))],
            [AssistantDoneEvent(reason='stop', message=AssistantMessage(content='next',model='offline'))]])
        config = CodingSessionConfig(provider=provider,model='offline',storage=self.storage,cwd=root,system='Test',
            tools=[AgentTool(name='wait_memory',label='Wait',description='Test synchronization',parameters={},execute_fn=tool)],
            resource_paths=OctoResourcePaths(root=paths.home,agents_root=paths.agents_home,cwd=root,paths=paths),
            extensions_enabled=False,skills_enabled=False,memory_settings=MemorySettings(),memory_extractor=extract)
        with patch.dict(os.environ, {'OCTO_HOME':str(paths.home),'OCTO_OFFLINE':'1'}):
            session = await CodingSession.load(config)
            self.addAsyncCleanup(session.aclose)
            _ = [event async for event in session.prompt('记住我偏好 Python')]
            self.assertNotIn('Stored user preferences',provider.calls[0][2][-1].text)
            self.assertNotIn('Stored user preferences',provider.calls[1][2][-1].text)
            _ = [event async for event in session.prompt('next request')]
            self.assertIn('Stored user preferences',provider.calls[2][2][-1].text)

    async def test_configured_relay_is_reused_by_real_independent_extraction_client(self):
        import json
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from threading import Thread
        from octo_coding.session import CodingSession, CodingSessionConfig
        from octo_memory.runtime import MemorySettings
        from octo_coding.paths import OctoPaths
        from octo_coding.resources import OctoResourcePaths
        from octo_coding.provider_config import OpenAICompatibleProviderConfig
        from octo_coding.provider_runtime import create_model_provider
        from octo_coding.credentials import FileCredentialStore
        captured = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                is_memory = 'extract durable memory' in payload['messages'][0]['content']
                captured.append((self.path, payload, is_memory))
                if is_memory:
                    sources = json.loads(payload['messages'][-1]['content'])['sources']
                    user = next((s for s in sources if s['role'] == 'user'), None)
                    content = json.dumps([{'kind':'add','text':'User prefers Python','category':'preference','source_entry_ids':[user['entry_id']]}] if user else [])
                else:
                    content = 'Received'
                chunks = [{'id':'local','model':'relay-model','choices':[{'index':0,'delta':{'role':'assistant','content':content},'finish_reason':None}]},
                          {'id':'local','model':'relay-model','choices':[{'index':0,'delta':{},'finish_reason':'stop'}]}]
                body = ''.join('data: '+json.dumps(chunk)+'\n\n' for chunk in chunks)+'data: [DONE]\n\n'
                data = body.encode()
                self.send_response(200)
                self.send_header('Content-Type','text/event-stream')
                self.send_header('Content-Length',str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        server = ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread = Thread(target=server.serve_forever,daemon=True)
        thread.start()
        root = Path(self.temp.name)
        paths = OctoPaths(home=root/'home',agents_home=root/'agents')
        cfg = OpenAICompatibleProviderConfig(name='relay-fixture',base_url=f'http://127.0.0.1:{server.server_port}/v1',
            api_key_env='OCTO_MEMORY_TEST_KEY',models=('relay-model',),default_model='relay-model',max_retries=0,timeout_seconds=5)
        try:
            with patch.dict(os.environ, {'OCTO_HOME':str(paths.home),'OCTO_OFFLINE':'1','OCTO_MEMORY_TEST_KEY':'local-test-only','NO_PROXY':'127.0.0.1,localhost'}):
                provider = create_model_provider(cfg,credential_store=FileCredentialStore(paths.home/'fixture-credentials.json'),model='relay-model')
                config = CodingSessionConfig(provider=provider,model='relay-model',provider_name='relay-fixture',
                    runtime_provider_config=cfg,storage=self.storage,cwd=root,system='Test assistant',tools=[],
                    resource_paths=OctoResourcePaths(root=paths.home,agents_root=paths.agents_home,cwd=root,paths=paths),
                    extensions_enabled=False,skills_enabled=False,memory_settings=MemorySettings(),owns_initial_provider=True)
                session = await CodingSession.load(config)
                try:
                    _ = [event async for event in session.prompt('Remember I prefer Python')]
                    await session.flush_memory()
                    self.assertEqual(session.memory_status,'enabled (background; preference recall only)')
                    self.assertIn('User prefers Python',(await self.store.facts(category='preference'))[0].text)
                finally:
                    await session.aclose()
            memory_calls = [payload for _,payload,kind in captured if kind]
            self.assertTrue(memory_calls)
            self.assertTrue(any(not kind for _,_,kind in captured))
            self.assertTrue(all(path=='/v1/chat/completions' for path,_,_ in captured))
            self.assertTrue(all(payload['model']=='relay-model' for _,payload,_ in captured))
            self.assertTrue(all(not payload.get('tools') for payload in memory_calls))
        finally:
            await asyncio.to_thread(server.shutdown)
            server.server_close()
            await asyncio.to_thread(thread.join, 2)


    async def test_schema_five_upgrade_preserves_applied_fact_progress_and_receipt(self):
        import sqlite3
        from contextlib import closing
        await self.storage.append(MessageEntry(id='source',message=UserMessage(content='Python preference')))
        job=await self.store.enqueue('one','source')
        receipt=await self.store.apply(await self.store.claim(job.id),[AddFact(text='Python',category='preference',source_entry_ids=('source',))])
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute('ALTER TABLE memory_progress DROP COLUMN flush_through_seq')
            connection.execute('PRAGMA user_version=5')
            connection.commit()
        restarted=MemoryStore(SQLiteSessionDatabase(self.path))
        self.assertEqual(await restarted.progress('one'),1)
        self.assertEqual(await restarted.receipt(job.id),receipt)
        self.assertEqual((await restarted.facts())[0].text,'Python')
        self.assertEqual(await restarted.flush_targets(),())

    async def test_cancelled_static_new_session_never_starts_prepared_worker(self):
        from octo_coding.session import CodingSession, CodingSessionConfig
        from octo_memory.runtime import MemorySettings
        from octo_coding.paths import OctoPaths
        from octo_coding.resources import OctoResourcePaths
        from octo_coding.session_manager import SessionManager
        from octo_ai.fake import FakeProvider
        root=Path(self.temp.name)
        paths=OctoPaths(home=root/'home',agents_home=root/'agents')
        manager=SessionManager(paths)
        record=manager.create_session(cwd=root,model='offline',title='Prepared fixture')
        marker=root/'shutdown-entered'
        extension=root/'hold_lifecycle.py'
        extension.write_text('import asyncio\nfrom pathlib import Path\ndef setup(octo):\n'
            '    @octo.on("session_shutdown")\n'
            '    async def hold(event,context):\n'
            '        if event.reason=="new":\n'
            f'            Path({str(marker)!r}).write_text("entered")\n'
            '            await asyncio.Event().wait()\n',encoding='utf-8')
        calls=[]
        async def extract(job,sources,facts):
            calls.append(job.id)
            return []
        config=CodingSessionConfig(provider=FakeProvider([]),model='offline',storage=manager.storage_for(record),
            session_id=record.id,session_manager=manager,cwd=root,system='Test',tools=[],
            resource_paths=OctoResourcePaths(root=paths.home,agents_root=paths.agents_home,cwd=root,paths=paths),
            extensions_enabled=False,extension_paths=(extension,),skills_enabled=False,
            memory_settings=MemorySettings(),memory_extractor=extract)
        with patch.dict(os.environ,{'OCTO_HOME':str(paths.home),'OCTO_OFFLINE':'1'}):
            session=await CodingSession.load(config)
            self.addAsyncCleanup(session.aclose)
            await session.flush_memory()
            other=SQLiteSessionStorage(paths.database_path,'other')
            await other.append(MessageEntry(id='old-job',message=UserMessage(content='pending work')))
            store=MemoryStore(SQLiteSessionDatabase(paths.database_path))
            job=await store.enqueue('other','old-job')
            pending=asyncio.create_task(session.new_session())
            try:
                async def wait_marker():
                    while not marker.exists(): await asyncio.sleep(0.01)
                await asyncio.wait_for(wait_marker(),5)
                await asyncio.sleep(0.05)
            finally:
                pending.cancel()
                try: await pending
                except asyncio.CancelledError: pass
            self.assertEqual(session.session_id,record.id)
            self.assertEqual(calls,[])
            self.assertEqual((await store.get_job(job.id)).state,'pending')


if __name__ == '__main__':
    unittest.main()
