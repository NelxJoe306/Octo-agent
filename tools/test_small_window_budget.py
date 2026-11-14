from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from octo_agent.context_budget import ContextBudgetExceeded
from octo_ai.anthropic import AnthropicProvider
from octo_ai.env import AnthropicConfig, OpenAICompatibleConfig
from octo_ai.google import GoogleGenerativeAIProvider
from octo_ai.mistral import MistralConversationsProvider
from octo_ai.openai_compatible import OpenAICompatibleProvider
from octo_ai.model_limits import RuntimeModelLimits
from octo_coding.credentials import FileCredentialStore
from octo_coding.paths import OctoPaths
from octo_coding.provider_config import ProviderSettings, ProviderModelMetadata, load_provider_settings, openai_compatible_config_from_provider
from octo_coding.provider_runtime import create_model_provider, create_dynamic_model_provider
from octo_coding.extensions.providers import DynamicProvider, ProviderModel, NoAuth, OpenAICompatibleTransport
from octo_coding.resources import OctoResourcePaths
from octo_coding.session import CodingSession, CodingSessionConfig
from octo_coding.session_manager import SessionManager
from tools import test_context_budget as fixtures


class SmallWindowBudgetTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.SessionBudgetTests.setUp
    make_session = fixtures.SessionBudgetTests.make_session

    async def native_session(self, **options):
        paths = OctoPaths(home=self.base / 'native-home', agents_home=self.base / 'agents')
        settings = load_provider_settings(paths)
        provider_config = replace(settings.get_provider('openai'), models=('gpt-4',), default_model='gpt-4', context_windows={'gpt-4': 8192}, model_metadata={'gpt-4': ProviderModelMetadata(max_tokens=8192)}, max_retries=0)
        payloads = []
        def receive(request):
            payloads.append(json.loads(request.content))
            return httpx.Response(400, json={'error': {'message': 'offline fixture'}})
        client = httpx.AsyncClient(transport=httpx.MockTransport(receive))
        self.addAsyncCleanup(client.aclose)
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'offline-fixture'}):
            provider = create_model_provider(provider_config, model='gpt-4',
                credential_store=FileCredentialStore(self.base / 'credentials.json'))
        provider._client = client
        manager = SessionManager(paths)
        record = manager.create_session(cwd=self.base, model='gpt-4', title='Small window fixture')
        session = await CodingSession.load(CodingSessionConfig(
            provider=provider, model='gpt-4', provider_name='openai', owns_initial_provider=True,
            provider_settings=ProviderSettings(providers=(provider_config,)), runtime_provider_config=provider_config,
            storage=manager.storage_for(record), session_id=record.id, session_manager=manager,
            cwd=self.base, system='system', tools=[],
            resource_paths=OctoResourcePaths(root=paths.home, agents_root=paths.agents_home, cwd=self.base, paths=paths),
            auto_compact_enabled=False, extensions_enabled=False, skills_enabled=False, trust_override='approve', **options,
        ))
        self.addAsyncCleanup(session.aclose)
        return session, payloads, provider_config

    async def test_standard_factory_small_window_can_send_hello(self):
        session, payloads, provider_config = await self.native_session()
        self.assertEqual(provider_config.context_windows['gpt-4'], 8192)
        _ = [event async for event in session.prompt('hello')]
        self.assertEqual(len(payloads), 1)
        decision = session.last_context_budget
        self.assertEqual(decision.status, 'ALLOW')
        self.assertGreater(decision.hard_input_limit, 0)
        actual_output = payloads[0].get('max_output_tokens', payloads[0].get('max_completion_tokens', payloads[0].get('max_tokens')))
        self.assertEqual(actual_output, decision.output_reserve)
        self.assertLess(actual_output + decision.safety_margin, 8192)

    async def test_override_applies_to_native_provider_and_wire_payload(self):
        session, payloads, _ = await self.native_session(context_output_reserve_tokens=512)
        _ = [event async for event in session.prompt('hello')]
        self.assertEqual(len(payloads), 1)
        self.assertEqual(session.last_context_budget.output_reserve, 512)
        self.assertEqual(payloads[0].get('max_output_tokens', payloads[0].get('max_completion_tokens', payloads[0].get('max_tokens'))), 512)

    async def test_standard_factory_carries_catalog_output_maximum(self):
        paths = OctoPaths(home=self.base / 'catalog-home', agents_home=self.base / 'agents')
        provider = replace(load_provider_settings(paths).get_provider('openai'), models=('gpt-4',), default_model='gpt-4', context_windows={'gpt-4': 8192}, model_metadata={'gpt-4': ProviderModelMetadata(max_tokens=8192)})
        self.assertEqual(provider.model_metadata['gpt-4'].max_tokens, 8192)
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'offline-fixture'}):
            config = openai_compatible_config_from_provider(provider, model='gpt-4')
        self.assertEqual(config.model_max_output_tokens, 8192)
        self.assertEqual(config.context_window, 8192)

    async def test_impossible_capacity_is_blocked_with_visible_decision(self):
        session, provider, _ = await self.make_session()
        session._runtime_model_limits_key = (session.provider_name, session.model)
        session._runtime_model_limits = RuntimeModelLimits(context_window=500)
        with self.assertRaises(ContextBudgetExceeded):
            _ = [event async for event in session.prompt('hello')]
        self.assertEqual(provider.calls, [])
        self.assertEqual(session.last_context_budget.status, 'BLOCK')
        self.assertIn('Context budget BLOCK', session.handle_command('/session').message)

    async def test_invalid_override_remains_diagnosable_without_sending(self):
        session, payloads, _ = await self.native_session(context_output_reserve_tokens=9000)
        with self.assertRaisesRegex(ValueError, 'exceeds the model output maximum'):
            _ = [event async for event in session.prompt('hello')]
        self.assertEqual(payloads, [])
        self.assertIn('exceeds the model output maximum', session.last_context_budget_error)
        self.assertIn('Last context budget configuration error:', session.handle_command('/session').message)
        self.assertEqual(session.messages[-1].text, 'hello')

    async def test_existing_history_cannot_be_made_to_fit_by_reducing_only_reservation(self):
        session, payloads, _ = await self.native_session(context_output_reserve_tokens=512)
        with self.assertRaises(ContextBudgetExceeded):
            _ = [event async for event in session.prompt('x' * 40000)]
        self.assertEqual(payloads, [])
        self.assertEqual(session.last_context_budget.output_reserve, 512)
        self.assertEqual(session.last_context_budget.status, 'BLOCK')


class ProviderWindowWireTests(unittest.IsolatedAsyncioTestCase):
    async def test_small_window_defaults_and_overrides_match_wire_across_protocols(self):
        cases = (
            (OpenAICompatibleProvider, 'openai-completions', 'max_completion_tokens'),
            (OpenAICompatibleProvider, 'openai-responses', 'max_output_tokens'),
            (GoogleGenerativeAIProvider, 'google-generative-ai', 'maxOutputTokens'),
            (MistralConversationsProvider, 'mistral-conversations', 'max_tokens'),
        )
        for provider_type, api, field in cases:
            for window in (4096, 8000, 8192, 16384, 17408, 17409, 128000):
                for override in (None, 512):
                    with self.subTest(api=api, window=window, override=override):
                        payloads = []
                        def receive(request):
                            payloads.append(json.loads(request.content))
                            return httpx.Response(400, json={'error': {'message': 'offline fixture'}})
                        async with httpx.AsyncClient(transport=httpx.MockTransport(receive)) as client:
                            provider = provider_type(OpenAICompatibleConfig(
                                api_key='fixture', base_url='https://fixture.invalid', api=api,
                                context_window=window, model_max_output_tokens=8192,
                                max_retries=0, infer_api_from_model=False,
                            ), client=client)
                            budget = provider.configure_request_budget(
                                context_window=window, safety_margin=1024, output_override=override,
                            )
                            _ = [event async for event in provider.stream_response(
                                model='offline', system='system', messages=[fixtures.UserMessage(content='hello')], tools=[],
                            )]
                        expected = override if override is not None else min(16384, 8192, window // 4)
                        self.assertEqual(budget.max_output_tokens, expected)
                        self.assertLess(expected + 1024, window)
                        self.assertEqual(len(payloads), 1)
                        self.assertEqual(payloads[0].get('generationConfig', payloads[0])[field], expected)

    async def test_dynamic_factory_preserves_small_model_capacity(self):
        payloads = []
        def receive(request):
            payloads.append(json.loads(request.content))
            return httpx.Response(400, json={'error': {'message': 'offline fixture'}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(receive)) as client:
            dynamic = DynamicProvider(id='local', display_name='Local fixture',
                models=(ProviderModel(id='small', context_window=4096, max_tokens=4096),),
                default_model='small', transport=OpenAICompatibleTransport(
                    base_url='https://fixture.invalid', auth=NoAuth(), client=client, max_retries=0,
                ))
            provider = await create_dynamic_model_provider(dynamic, model='small', environment={})
            budget = provider.configure_request_budget(context_window=128000, safety_margin=1024)
            self.assertEqual(budget.context_window, 4096)
            self.assertEqual(budget.max_output_tokens, 1024)
            _ = [event async for event in provider.stream_response(model='small', system='system',
                 messages=[fixtures.UserMessage(content='hello')], tools=[])]
            self.assertEqual(payloads[0].get('max_completion_tokens', payloads[0].get('max_tokens')), 1024)
            await provider.aclose()

    async def test_runtime_window_shrink_recomputes_default_without_stale_cap(self):
        provider = OpenAICompatibleProvider(OpenAICompatibleConfig(api_key='fixture'))
        self.assertEqual(provider.configure_request_budget(context_window=128000, safety_margin=1024).max_output_tokens, 16384)
        self.assertEqual(provider.configure_request_budget(context_window=8192, safety_margin=1024).max_output_tokens, 2048)
        self.assertEqual(provider.configure_request_budget(context_window=4096, safety_margin=1024).max_output_tokens, 1024)
        self.assertEqual(provider.request_token_budget().max_output_tokens, 1024)
        await provider.aclose()

    async def test_anthropic_override_cannot_undercount_thinking_budget(self):
        provider = AnthropicProvider(AnthropicConfig(api_key='fixture', thinking_budget_tokens=5000))
        with self.assertRaisesRegex(ValueError, 'must cover the thinking budget'):
            provider.configure_request_budget(context_window=32000, safety_margin=1024, output_override=512)
        budget = provider.configure_request_budget(context_window=32000, safety_margin=1024, output_override=7000)
        self.assertEqual(budget.max_output_tokens, 7000)
        await provider.aclose()


if __name__ == '__main__':
    unittest.main()
