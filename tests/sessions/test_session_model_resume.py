from __future__ import annotations

import io
import os
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from octo_memory.runtime import MemorySettings

from octo_agent.messages import AssistantMessage, UserMessage
from octo_agent.provider_events import AssistantDoneEvent
from octo_agent.session import SessionInfoEntry, ModelChangeEntry, MessageEntry
from octo_ai.fake import FakeProvider
from octo_coding.paths import OctoPaths
from octo_coding.provider_config import OpenAICompatibleProviderConfig, ProviderSettings, ProviderConfigError
from octo_coding.resources import OctoResourcePaths
from octo_coding.session import CodingSession, CodingSessionConfig
from octo_coding.session_manager import SessionManager


class ResumeProvider(FakeProvider):
    async def aclose(self): pass


class SessionModelResumeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.paths = OctoPaths(home=self.base/'home', agents_home=self.base/'agents')
        self.paths.home.mkdir(parents=True, exist_ok=True)
        (self.paths.home/'settings.json').write_text('{"memory":{"enabled":false}}', encoding='utf-8')
        self.manager = SessionManager(self.paths)
        self.settings = ProviderSettings(default_provider='deepseek', providers=(
            OpenAICompatibleProviderConfig(name='deepseek', models=('deepseek-flash',), default_model='deepseek-flash',
                context_windows={'deepseek-flash':128000}),))
        self.env = patch.dict(os.environ, {'OCTO_HOME':str(self.paths.home), 'OCTO_OFFLINE':'1'})
        self.env.start(); self.addCleanup(self.env.stop)

    def provider(self):
        return ResumeProvider([[AssistantDoneEvent(reason='stop', message=AssistantMessage(content='continued', model='deepseek-flash'))]])

    async def historical_session(self, provider='deepseek', model='deepseek-v4-flash'):
        record = self.manager.create_session(cwd=self.base, model=model, provider_name=provider, title='Historical fixture')
        info = SessionInfoEntry(cwd=str(self.base))
        change = ModelChangeEntry(parent_id=info.id, provider=provider, model=model)
        question = MessageEntry(parent_id=change.id, message=UserMessage(content='remember this history'))
        answer = MessageEntry(parent_id=question.id, message=AssistantMessage(content='historical answer', model=model))
        entries = [info, change, question, answer]
        storage = self.manager.storage_for(record)
        await storage.append_batch(entries)
        return record, storage, entries

    def config(self, record, storage, **options):
        return CodingSessionConfig(memory_settings=MemorySettings(enabled=False), provider=options.pop('provider', None), model='deepseek-flash', provider_name='deepseek', provider_settings=self.settings,
            storage=storage, session_id=record.id, session_manager=self.manager, cwd=self.base,
            resource_paths=OctoResourcePaths(root=self.paths.home, agents_root=self.paths.agents_home, cwd=self.base, paths=self.paths),
            system='offline fixture', tools=[], extensions_enabled=False, skills_enabled=False,
            auto_compact_enabled=False, trust_override='approve', **options)

    async def test_loading_retired_model_restores_history_and_uses_current_configuration(self):
        record, storage, original = await self.historical_session()
        provider = self.provider()
        with patch('octo_coding.session.create_model_provider', return_value=provider):
            session = await CodingSession.load(self.config(record, storage))
            self.addAsyncCleanup(session.aclose)
            self.assertEqual(session.model, 'deepseek-flash')
            self.assertEqual([message.text for message in session.messages], ['remember this history', 'historical answer'])
            self.assertTrue(any(item.kind == 'session-model' for item in session.resource_diagnostics))
            _ = [event async for event in session.prompt('continue')]
        self.assertEqual(provider.calls[0][0], 'deepseek-flash')
        self.assertIn('remember this history', [message.text for message in provider.calls[0][2]])
        self.assertEqual((await storage.read_all())[:len(original)], original)

    async def test_in_app_resume_does_not_require_old_model(self):
        record, storage, original = await self.historical_session()
        current = self.manager.create_session(cwd=self.base, model='deepseek-flash', provider_name='deepseek')
        provider = self.provider()
        with patch('octo_coding.session.create_model_provider', return_value=provider):
            session = await CodingSession.load(self.config(current, self.manager.storage_for(current),
                provider=provider, runtime_provider_config=self.settings.get_provider('deepseek')))
            self.addAsyncCleanup(session.aclose)
            result = await session.resume(record.id)
            self.assertEqual(session.session_id, record.id)
            self.assertEqual(session.model, 'deepseek-flash')
            self.assertIn('deepseek-flash', result)
            self.assertEqual([message.text for message in session.messages], ['remember this history', 'historical answer'])
        self.assertEqual((await storage.read_all())[:len(original)], original)

    async def test_print_resume_works_with_removed_provider(self):
        from octo_coding.cli import run_openai_print_mode
        record, storage, original = await self.historical_session(provider='removed-provider')
        provider = self.provider()
        with patch('octo_coding.cli.load_provider_settings', return_value=self.settings), \
             patch('octo_coding.cli.create_model_provider', return_value=provider), \
             patch('octo_coding.session.create_model_provider', return_value=provider), redirect_stdout(io.StringIO()):
            await run_openai_print_mode('continue', None, self.base, resume_session_id=record.id,
                session_manager=self.manager, extensions_enabled=False, trust_override='approve')
        self.assertEqual(provider.calls[0][0], 'deepseek-flash')
        self.assertIn('historical answer', [message.text for message in provider.calls[0][2]])
        self.assertEqual((await storage.read_all())[:len(original)], original)

    async def test_tui_startup_uses_resolved_model_even_if_record_provider_is_removed(self):
        from octo_coding.tui.app import run_tui_app
        record, storage, original = await self.historical_session(provider='removed-provider')
        provider = self.provider()
        observed = []
        class OfflineApp:
            def __init__(self, session, **options): self.session = session
            async def run_async(self):
                observed.append((self.session.provider_name, self.session.model, [message.text for message in self.session.messages]))
        with patch('octo_coding.tui.app.load_provider_settings', return_value=self.settings), \
             patch('octo_coding.tui.app.provider_has_usable_credentials', return_value=True), \
             patch('octo_coding.tui.app.create_model_provider', return_value=provider), \
             patch('octo_coding.session.create_model_provider', return_value=provider), \
             patch('octo_coding.tui.app.OctoTuiApp', OfflineApp):
            restored = await run_tui_app(model=None, cwd=self.base, session_id=record.id, session_manager=self.manager,
                extensions_enabled=False, trust_override='approve')
        self.assertEqual(restored, record.id)
        self.assertEqual(observed, [('deepseek','deepseek-flash',['remember this history','historical answer'])])
        self.assertEqual((await storage.read_all())[:len(original)], original)

    async def test_explicit_invalid_model_remains_an_error_and_does_not_write(self):
        record, storage, original = await self.historical_session()
        with self.assertRaises(ProviderConfigError):
            await CodingSession.load(self.config(record, storage, requested_provider='deepseek', requested_model='typo'))
        self.assertEqual(await storage.read_all(), original)



    async def test_still_available_historical_model_is_preserved(self):
        record, storage, original = await self.historical_session(model='deepseek-flash')
        provider = self.provider()
        with patch('octo_coding.session.create_model_provider', return_value=provider):
            session = await CodingSession.load(self.config(record, storage))
            self.addAsyncCleanup(session.aclose)
        self.assertEqual(session.model, 'deepseek-flash')
        self.assertFalse(any(item.kind == 'session-model' for item in session.resource_diagnostics))
        self.assertEqual(await storage.read_all(), original)

    async def test_aborted_preparation_does_not_persist_fallback_and_adoption_is_idempotent(self):
        from octo_coding.session_preparation import prepare_coding_session
        record, storage, original = await self.historical_session()
        with patch('octo_coding.session.create_model_provider', side_effect=lambda *a, **k: self.provider()):
            prepared = await prepare_coding_session(self.config(record, storage))
            self.assertEqual(prepared.session.model, 'deepseek-flash')
            self.assertEqual(await storage.read_all(), original)
            await prepared.abort()
            self.assertEqual(await storage.read_all(), original)
            prepared = await prepare_coding_session(self.config(record, storage))
            session = await prepared.adopt()
            await session.aclose()
            committed = await storage.read_all()
            self.assertEqual(committed[:len(original)], original)
            self.assertEqual(len(committed), len(original)+1)
            session = await CodingSession.load(self.config(record, storage))
            self.addAsyncCleanup(session.aclose)
            self.assertEqual(session.model, 'deepseek-flash')
            self.assertFalse(any(item.kind == 'session-model' for item in session.resource_diagnostics))
            self.assertEqual(await storage.read_all(), committed)

    async def test_explicit_provider_without_model_uses_its_default(self):
        from octo_coding.cli import run_openai_print_mode
        record, storage, original = await self.historical_session()
        provider = self.provider()
        with patch('octo_coding.cli.load_provider_settings', return_value=self.settings), \
             patch('octo_coding.cli.create_model_provider', return_value=provider), \
             patch('octo_coding.session.create_model_provider', return_value=provider), redirect_stdout(io.StringIO()):
            await run_openai_print_mode('continue', None, self.base, provider_name='deepseek', resume_session_id=record.id,
                session_manager=self.manager, extensions_enabled=False, trust_override='approve')
        self.assertEqual(provider.calls[0][0], 'deepseek-flash')
        self.assertEqual((await storage.read_all())[:len(original)], original)

    async def test_fallback_does_not_reuse_same_id_from_a_removed_provider(self):
        record, storage, original = await self.historical_session(provider='removed-provider', model='common-id')
        configured=replace(self.settings.get_provider('deepseek'), models=('deepseek-flash','common-id'), context_windows={})
        settings=replace(self.settings, providers=(configured,))
        provider=self.provider()
        with patch('octo_coding.session.create_model_provider', return_value=provider):
            session=await CodingSession.load(replace(self.config(record,storage),provider_settings=settings))
            self.addAsyncCleanup(session.aclose)
        self.assertEqual(session.provider_name,'deepseek')
        self.assertEqual(session.model,'deepseek-flash')
        self.assertEqual(await storage.read_all(),original)

    async def test_rpc_resume_resolves_model_without_rewriting_history(self):
        from octo_coding.cli import run_openai_rpc_mode
        record, storage, original = await self.historical_session()
        provider=self.provider()
        observed=[]
        class OfflineRpc:
            def __init__(self,session): self.session=session
            async def run(self): observed.append((self.session.model,[message.text for message in self.session.messages]))
        with patch('octo_coding.cli.load_provider_settings',return_value=self.settings), \
             patch('octo_coding.cli.SessionManager',return_value=self.manager), \
             patch('octo_coding.cli.create_model_provider',return_value=provider), \
             patch('octo_coding.session.create_model_provider',return_value=provider), \
             patch('octo_coding.cli.RpcServer',OfflineRpc):
            await run_openai_rpc_mode(None,self.base,resume_session_id=record.id,extensions_enabled=False,trust_override='approve')
        self.assertEqual(observed,[('deepseek-flash',['remember this history','historical answer'])])
        self.assertEqual((await storage.read_all())[:len(original)],original)



    async def test_model_fallback_preserves_selected_topic_and_message_attribution(self):
        from octo_agent.session.topics import SessionTopics
        from octo_coding.session_preparation import prepare_coding_session
        record, storage, original = await self.historical_session()
        topics=SessionTopics(storage)
        child=topics.create(parent_id='main',title='keep this branch',activate=True)
        request=topics.capture(child)
        question=MessageEntry(parent_id=original[-1].id,message=UserMessage(content='branch question'))
        topics.append_message(question,request)
        answer=MessageEntry(parent_id=question.id,message=AssistantMessage(content='branch answer',model='deepseek-v4-flash'))
        topics.append_message(answer,request)
        before=topics.snapshot(child)
        raw=await storage.read_all()
        with patch('octo_coding.session.create_model_provider',return_value=self.provider()):
            prepared=await prepare_coding_session(self.config(record,storage))
            session=await prepared.adopt()
            self.addAsyncCleanup(session.aclose)
            self.assertEqual(session.current_topic_id,child)
            after=await session.topic_snapshot(child)
            self.assertEqual(after['messages'],before['messages'])
            self.assertEqual(after['topics'],before['topics'])
        self.assertEqual((await storage.read_all())[:len(raw)],raw)



    async def test_dynamic_provider_can_restore_retired_model_without_an_alias(self):
        from unittest.mock import AsyncMock
        from octo_agent.session import SessionState
        from octo_coding.extensions.providers import DynamicProvider, ProviderModel
        from octo_coding.extensions.provider_registry import DynamicProviderRegistry
        from octo_coding.session import _prepare_provider_selection
        record, storage, original = await self.historical_session()
        provider=self.provider()
        dynamic=DynamicProvider(id='deepseek',display_name='DeepSeek fixture',
            models=(ProviderModel(id='deepseek-flash'),),default_model='deepseek-flash',runtime_factory=lambda context:provider)
        registry=DynamicProviderRegistry()
        registry.register('fixture',dynamic)
        with patch('octo_coding.session.create_dynamic_model_provider',new=AsyncMock(return_value=provider)) as factory:
            selected=await _prepare_provider_selection(self.config(record,storage),
                state=SessionState.from_entries(original),provider_registry=registry)
            self.assertEqual(selected.model,'deepseek-flash')
            self.assertEqual(factory.await_args.kwargs['model'],'deepseek-flash')
            with self.assertRaises(ProviderConfigError):
                await _prepare_provider_selection(self.config(record,storage,requested_model='typo'),
                    state=SessionState.from_entries(original),provider_registry=registry)
        self.assertEqual(await storage.read_all(),original)

    async def test_explicit_valid_model_overrides_a_valid_saved_model(self):
        record, storage, original = await self.historical_session(model='deepseek-flash')
        configured=replace(self.settings.get_provider('deepseek'),models=('deepseek-flash','deepseek-v4-pro'))
        settings=replace(self.settings,providers=(configured,))
        with patch('octo_coding.session.create_model_provider',return_value=self.provider()):
            session=await CodingSession.load(replace(self.config(record,storage,requested_model='deepseek-v4-pro'),provider_settings=settings))
            self.addAsyncCleanup(session.aclose)
        self.assertEqual(session.model,'deepseek-v4-pro')
        self.assertEqual(await storage.read_all(),original)


if __name__ == '__main__':
    unittest.main()
