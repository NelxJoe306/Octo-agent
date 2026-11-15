from __future__ import annotations

import asyncio
from contextlib import closing
import sqlite3
from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import patch

from octo_coding.memory_runtime import MemorySettings

from octo_agent.events import MessageEndEvent
from octo_agent.context_budget import ContextBudgetExceeded
from octo_agent.context_recovery import overflow_token_deficit
from octo_coding.compaction import context_blocks, select_context_blocks
from octo_coding.tui.adapter import TuiEventAdapter
from octo_coding.tui.state import TuiState
from octo_agent.messages import AssistantMessage, ToolCall, ToolResultMessage, UserMessage
from octo_agent.provider_events import AssistantDoneEvent, AssistantErrorEvent
from octo_agent.session import CompactionEntry, MessageEntry, CustomEntry
from octo_agent.tools import AgentTool, AgentToolResult
from octo_ai.fake import FakeProvider
from octo_ai.model_limits import RuntimeModelLimits
from octo_coding.context_window import SUMMARIZATION_SYSTEM_PROMPT
from octo_coding.paths import OctoPaths
from octo_coding.resources import OctoResourcePaths
from octo_coding.session import CodingSession, CodingSessionConfig
from octo_coding.session_manager import SessionManager


def response(text, calls=()):
    message = AssistantMessage(content=text, model='offline', stop_reason='toolUse' if calls else 'stop')
    message.content.extend(calls)
    return [AssistantDoneEvent(reason=message.stop_reason, message=message)]


def failure(text):
    return [AssistantErrorEvent(reason='error', error=AssistantMessage(
        content='', model='offline', stop_reason='error', error_message=text,
    ))]


class CompactionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.env = patch.dict(os.environ, {'OCTO_HOME': str(self.base/'home'), 'OCTO_OFFLINE': '1'})
        self.env.start()
        self.addCleanup(self.env.stop)

    async def make_session(self, streams, *, seed=(), tools=(), window=4096, ratio=0.35, storage_wrapper=None):
        paths = OctoPaths(home=self.base/'home', agents_home=self.base/'agents')
        manager = SessionManager(paths)
        record = manager.create_session(cwd=self.base, model='offline', title='Compaction fixture')
        storage = manager.storage_for(record)
        parent = None
        seeded = []
        for message in seed:
            entry = MessageEntry(parent_id=parent, message=message)
            seeded.append(entry)
            parent = entry.id
        await storage.append_batch(seeded)
        if storage_wrapper:
            storage = storage_wrapper(storage)
        provider = FakeProvider(streams)
        config = CodingSessionConfig(memory_settings=MemorySettings(enabled=False), provider=provider, model='offline', storage=storage,
            session_id=record.id, session_manager=manager, cwd=self.base, system='system', tools=list(tools),
            resource_paths=OctoResourcePaths(root=paths.home, agents_root=paths.agents_home, cwd=self.base, paths=paths),
            auto_compact_enabled=True, context_soft_ratio=ratio, context_output_reserve_tokens=512,
            context_safety_margin=128, extensions_enabled=False, skills_enabled=False, trust_override='approve')
        session = await CodingSession.load(config)
        session._runtime_model_limits = RuntimeModelLimits(context_window=window)
        session._runtime_model_limits_key = (session.provider_name, session.model)
        self.addAsyncCleanup(session.aclose)
        return session, provider, config, seeded

    async def test_tool_batch_triggers_compaction_and_next_request_uses_committed_projection(self):
        async def read(call_id, arguments, signal=None, on_update=None):
            return AgentToolResult(content='evidence ' * 500)
        tool = AgentTool(name='read', label='Read', description='Read evidence', parameters={}, execute_fn=read)
        session, provider, config, seeded = await self.make_session([
            response('inspect', [ToolCall(id='read-1', name='read', arguments={})]),
            response('Completed read: evidence verified. Current task remains to answer the user.'),
            response('final answer'),
        ], seed=(UserMessage(content='old ' * 120), AssistantMessage(content='old answer ' * 80)), tools=[tool])
        events = [event async for event in session.prompt('Inspect the evidence and answer')]
        summary_calls = [call for call in provider.calls if call[1] == SUMMARIZATION_SYSTEM_PROMPT]
        self.assertEqual(len(summary_calls), 1)
        entries = await session.session_entries()
        compactions = [entry for entry in entries if isinstance(entry, CompactionEntry)]
        self.assertEqual(len(compactions), 1)
        self.assertTrue(compactions[0].replaces_entry_ids)
        last_input = provider.calls[-1][2]
        self.assertTrue(any('Previous conversation summary:' in message.text for message in last_input))
        self.assertTrue(any(message.text == 'Inspect the evidence and answer' for message in last_input))
        self.assertTrue(any(isinstance(message, ToolResultMessage) and message.text == 'evidence ' * 500 for message in last_input))
        self.assertTrue(any(isinstance(message, AssistantMessage) and any(call.id == 'read-1' for call in message.tool_calls) for message in last_input))
        self.assertEqual([message.model_dump() for message in session.messages],
                         [message.model_dump() for message in session.state.messages])
        self.assertEqual(session.messages[-1].text, 'final answer')
        originals = [entry for entry in entries if isinstance(entry, MessageEntry)]
        self.assertEqual([entry.model_dump() for entry in originals[:2]], [entry.model_dump() for entry in seeded])
        self.assertTrue(any(isinstance(entry.message, ToolResultMessage) and entry.message.text == 'evidence ' * 500 for entry in originals))
        restored = await CodingSession.load(config)
        self.addAsyncCleanup(restored.aclose)
        self.assertTrue(any('Previous conversation summary:' in message.text for message in restored.messages))
        self.assertTrue(any(event.type == 'context_recovery_end' for event in events))

    async def test_three_summary_failures_disable_summarization_and_trim_survives_restart(self):
        session, provider, config, seeded = await self.make_session([
            failure('summary unavailable'), response('answer 1'),
            failure('summary unavailable'), response('answer 2'),
            failure('summary unavailable'), response('answer 3'), response('after restart'),
        ], seed=(UserMessage(content='old ' * 600), AssistantMessage(content='old answer ' * 500)))
        for index in range(3):
            _ = [event async for event in session.prompt(f'question {index}')]
        self.assertEqual(sum(call[1] == SUMMARIZATION_SYSTEM_PROMPT for call in provider.calls), 3)
        entries = await session.session_entries()
        receipt = [entry for entry in entries if isinstance(entry, CustomEntry) and entry.namespace == 'octo.context-compaction'][-1]
        self.assertEqual(receipt.data['failures'], 3)
        self.assertTrue(receipt.data['summary_disabled'])
        self.assertTrue(any(isinstance(entry, CompactionEntry) and 'omitted without summarization' in entry.summary for entry in entries))
        restored = await CodingSession.load(config)
        self.addAsyncCleanup(restored.aclose)
        restored._runtime_model_limits = RuntimeModelLimits(context_window=4096)
        restored._runtime_model_limits_key = (restored.provider_name, restored.model)
        _ = [event async for event in restored.prompt('extra ' * 700)]
        self.assertEqual(sum(call[1] == SUMMARIZATION_SYSTEM_PROMPT for call in provider.calls), 3)
        self.assertEqual(restored.messages[-1].text, 'after restart')
        original = [entry for entry in await restored.session_entries() if isinstance(entry, MessageEntry)]
        self.assertEqual([entry.model_dump() for entry in original[:2]], [entry.model_dump() for entry in seeded])

    async def test_provider_overflow_trims_complete_blocks_and_retries_once(self):
        for repeat_error in (False, True):
            with self.subTest(repeat_error=repeat_error):
                overflow = 'Maximum context length is 1,000 tokens. However, you requested 1,400 tokens.'
                session, provider, _, seeded = await self.make_session([
                    failure(overflow), failure(overflow) if repeat_error else response('recovered'), response('must not retry twice'),
                ], ratio=0.95, seed=(UserMessage(content='old ' * 200), AssistantMessage(content='old answer ' * 150)))
                events = []
                retry_flags = []
                async for event in session.prompt('current question'):
                    events.append(event)
                    if isinstance(event, MessageEndEvent) and isinstance(event.message, AssistantMessage) and event.message.stop_reason == 'error':
                        retry_flags.append(session.will_auto_retry(event.message))
                self.assertEqual(retry_flags, [True, False] if repeat_error else [True])
                self.assertEqual(len(provider.calls), 2)
                self.assertFalse(any(call[1] == SUMMARIZATION_SYSTEM_PROMPT for call in provider.calls))
                self.assertTrue(any(message.text == 'current question' for message in provider.calls[-1][2]))
                self.assertFalse(any(isinstance(message, AssistantMessage) and message.stop_reason == 'error'
                                     for message in provider.calls[-1][2]))
                recoveries = [event for event in events if event.type == 'context_recovery_end' and event.reason == 'overflow']
                self.assertEqual(len(recoveries), 1)
                self.assertEqual(recoveries[0].result.strategy, 'trim')
                errors = [entry for entry in await session.session_entries() if isinstance(entry, MessageEntry)
                          and isinstance(entry.message, AssistantMessage) and entry.message.stop_reason == 'error']
                self.assertEqual(len(errors), 2 if repeat_error else 1)

    async def test_compaction_receipt_failure_rolls_back_summary_and_coverage(self):
        session, provider, config, seeded = await self.make_session([
            response('small summary'), response('must not send'),
        ], seed=(UserMessage(content='old ' * 600), AssistantMessage(content='old answer ' * 500)))
        with closing(sqlite3.connect(config.storage.path)) as connection:
            connection.execute("""CREATE TRIGGER fail_compaction_receipt BEFORE INSERT ON session_entries
                WHEN json_extract(NEW.body_json, '$.namespace') = 'octo.context-compaction'
                BEGIN SELECT RAISE(ABORT, 'fixture receipt failure'); END""")
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'fixture receipt failure'):
            _ = [event async for event in session.prompt('current question')]
        entries = await session.session_entries()
        self.assertFalse(any(isinstance(entry, CompactionEntry) for entry in entries))
        self.assertEqual(len(provider.calls), 1)
        restored = await CodingSession.load(config)
        self.addAsyncCleanup(restored.aclose)
        self.assertTrue(any(message.text == 'old ' * 600 for message in restored.messages))

    async def test_oversized_current_input_is_not_summarized_or_dropped(self):
        session, provider, _, _ = await self.make_session([
            response('must not summarize'), response('must not send'),
        ], seed=(UserMessage(content='old question'), AssistantMessage(content='old answer')))
        with self.assertRaises(ContextBudgetExceeded):
            _ = [event async for event in session.prompt('x' * 20000)]
        self.assertEqual(provider.calls, [])
        self.assertFalse(any(isinstance(entry, CompactionEntry) for entry in await session.session_entries()))
        self.assertEqual(session.messages[-1].text, 'x' * 20000)

    async def test_cancellation_after_commit_recovers_same_live_session(self):
        session, provider, config, _ = await self.make_session([
            response('small summary'), response('resumed answer'),
        ], seed=(UserMessage(content='old ' * 600), AssistantMessage(content='old answer ' * 500)))
        committed = asyncio.Event()
        release = asyncio.Event()
        original = config.storage.append_batch_if_head
        async def delayed_after_commit(records, *, expected_head):
            await original(records, expected_head=expected_head)
            committed.set()
            await release.wait()
        async def collect():
            return [event async for event in session.prompt('first pending question')]
        with patch.object(config.storage, 'append_batch_if_head', delayed_after_commit):
            task = asyncio.create_task(collect())
            await asyncio.wait_for(committed.wait(), 5)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        _ = [event async for event in session.prompt('resume in same session')]
        self.assertTrue(any('Previous conversation summary:' in message.text for message in provider.calls[-1][2]))
        self.assertEqual(session.messages[-1].text, 'resumed answer')

    async def test_cancel_during_summary_never_commits_or_counts_failure(self):
        session, provider, _, _ = await self.make_session([
            response('summary should be discarded'), response('must not send'),
        ], seed=(UserMessage(content='old ' * 600), AssistantMessage(content='old answer ' * 500)))
        original = provider.stream_response
        def cancel_in_stream(**arguments):
            source = original(**arguments)
            async def iterate():
                async for event in source:
                    if arguments['system'] == SUMMARIZATION_SYSTEM_PROMPT:
                        session.cancel()
                    yield event
            return iterate()
        with patch.object(provider, 'stream_response', cancel_in_stream):
            with self.assertRaises(asyncio.CancelledError):
                _ = [event async for event in session.prompt('current question')]
        entries = await session.session_entries()
        self.assertFalse(any(isinstance(entry, CompactionEntry) for entry in entries))
        self.assertEqual(session.context_summary_failures, 0)

    async def test_continue_has_same_bounded_overflow_recovery(self):
        session, provider, _, _ = await self.make_session([
            failure('Context window exceeded'), response('continue recovered'),
        ], ratio=0.95, seed=(UserMessage(content='old ' * 200), AssistantMessage(content='old answer ' * 150),
                            UserMessage(content='latest question')))
        events = [event async for event in session.continue_()]
        self.assertEqual(len(provider.calls), 2)
        state = TuiState()
        adapter = TuiEventAdapter(state)
        for event in events:
            adapter.apply(event)
        self.assertFalse(state.running)
        self.assertIsNone(state.error)
        self.assertFalse(any(item.role == 'error' for item in state.items))
        self.assertTrue(any(item.role == 'assistant' and 'continue recovered' in item.text for item in state.items))

    def test_tool_batches_are_indivisible_and_unsettled_batches_rejected(self):
        assistant = AssistantMessage(content=[ToolCall(id='a', name='read', arguments={}),
                                               ToolCall(id='b', name='read', arguments={})], stop_reason='toolUse')
        rows = [('user', UserMessage(content='question')), ('call', assistant),
                ('ra', ToolResultMessage(tool_call_id='a', tool_name='read', content='A')),
                ('rb', ToolResultMessage(tool_call_id='b', tool_name='read', content='B'))]
        blocks = context_blocks(rows, protected_ids={'user'}, image_reserve=4096)
        self.assertEqual([block.entry_ids for block in blocks], [('user',), ('call', 'ra', 'rb')])
        selected = select_context_blocks(rows, remove_tokens=1,
            protected_ids={entry_id for entry_id, _ in rows}, image_reserve=4096)
        self.assertEqual(selected, ())
        with self.assertRaisesRegex(ValueError, 'tool history'):
            context_blocks(rows[:-1], protected_ids={'user'}, image_reserve=4096)

    def test_overflow_deficits_are_parsed_without_guessing_unrelated_numbers(self):
        self.assertEqual(overflow_token_deficit('Maximum context length is 4,096 tokens. However, you requested 4,500 tokens.'), 404)
        self.assertEqual(overflow_token_deficit('Context window exceeded by 300 tokens'), 300)
        self.assertIsNone(overflow_token_deficit('Context window exceeded; status 400, request id 987654'))

    async def test_rate_or_quota_errors_do_not_trigger_context_trimming(self):
        session, provider, _, _ = await self.make_session([
            failure('Rate limit: token limit exceeded, try again later'), response('must not retry'),
        ], ratio=0.95, seed=(UserMessage(content='old question'), AssistantMessage(content='old answer')))
        events = [event async for event in session.prompt('current question')]
        self.assertEqual(len(provider.calls), 1)
        self.assertFalse(any(event.type == 'context_recovery_start' for event in events))
        self.assertFalse(any(isinstance(entry, CompactionEntry) for entry in await session.session_entries()))

    async def test_summary_that_expands_input_is_rejected_without_rewriting_context(self):
        session, provider, _, _ = await self.make_session([
            response('inflated ' * 1500), response('main answer'),
        ], seed=(UserMessage(content='old ' * 600), AssistantMessage(content='old answer ' * 500)))
        _ = [event async for event in session.prompt('current question')]
        self.assertEqual(session.context_summary_failures, 1)
        self.assertFalse(any(isinstance(entry, CompactionEntry) for entry in await session.session_entries()))
        self.assertFalse(any('inflated' in message.text for message in provider.calls[-1][2]))
        self.assertEqual(session.messages[-1].text, 'main answer')

    async def test_summary_input_too_large_uses_trim_without_sending_oversized_summary(self):
        session, provider, _, seeded = await self.make_session([
            response('after trim'),
        ], seed=(UserMessage(content='old ' * 6000), AssistantMessage(content='old answer ' * 1000)))
        _ = [event async for event in session.prompt('current question')]
        self.assertEqual(len(provider.calls), 1)
        self.assertNotEqual(provider.calls[0][1], SUMMARIZATION_SYSTEM_PROMPT)
        self.assertEqual(session.messages[-1].text, 'after trim')
        self.assertTrue(any('omitted without summarization' in message.text for message in provider.calls[-1][2]))
        originals = [entry for entry in await session.session_entries() if isinstance(entry, MessageEntry)]
        self.assertEqual([entry.model_dump() for entry in originals[:2]], [entry.model_dump() for entry in seeded])

    async def test_stale_summary_cannot_commit_over_a_new_storage_head(self):
        session, provider, config, _ = await self.make_session([
            response('stale summary'), response('must not send'),
        ], seed=(UserMessage(content='old ' * 600), AssistantMessage(content='old answer ' * 500)))
        original = provider.stream_response
        def advance_head(**arguments):
            source = original(**arguments)
            async def iterate():
                async for event in source:
                    if arguments['system'] == SUMMARIZATION_SYSTEM_PROMPT:
                        entries = await config.storage.read_all()
                        await config.storage.append(CustomEntry(parent_id=entries[-1].id, namespace='fixture.concurrent', data={}))
                    yield event
            return iterate()
        with patch.object(provider, 'stream_response', advance_head):
            with self.assertRaisesRegex(ValueError, 'head changed'):
                _ = [event async for event in session.prompt('current question')]
        self.assertEqual(len(provider.calls), 1)
        self.assertFalse(any(isinstance(entry, CompactionEntry) for entry in await session.session_entries()))

    async def test_manual_compaction_uses_complete_blocks_and_preserves_latest_round(self):
        session, provider, config, seeded = await self.make_session([
            response('manual summary'),
        ], seed=(UserMessage(content='old ' * 100),
                 AssistantMessage(content=[ToolCall(id='a', name='read', arguments={}), ToolCall(id='b', name='read', arguments={})], stop_reason='toolUse'),
                 ToolResultMessage(tool_call_id='a', tool_name='read', content='A'),
                 ToolResultMessage(tool_call_id='b', tool_name='read', content='B'),
                 AssistantMessage(content='old final'), UserMessage(content='latest question'), AssistantMessage(content='latest answer')))
        result = await session.compact_detailed()
        self.assertEqual(result.replaced_entry_count, 5)
        self.assertEqual([message.text for message in session.messages[-2:]], ['latest question', 'latest answer'])
        record = [entry for entry in await session.session_entries() if isinstance(entry, CompactionEntry)][-1]
        self.assertEqual(set(record.replaces_entry_ids), {entry.id for entry in seeded[:5]})

    async def test_compaction_preserves_queued_topic_attribution_and_source_labels(self):
        session, provider, _, seeded = await self.make_session([
            response('Main history summary'), response('answer A'),
            response('Topic A evidence summary'), response('answer B'),
        ], seed=(UserMessage(content='old ' * 600), AssistantMessage(content='old answer ' * 500)))
        a = await session.edit_topic('create', session_id=session.session_id, title='Topic A')
        b = await session.edit_topic('create', session_id=session.session_id, title='Topic B')
        await session.edit_topic('select', session_id=session.session_id, topic_id=a)
        question_a = 'Question A ' + 'A' * 2000
        question_b = 'Question B ' + 'B' * 3000
        async for event in session.prompt(question_a):
            if isinstance(event, MessageEndEvent) and isinstance(event.message, AssistantMessage) and event.message.text == 'answer A':
                await session.edit_topic('select', session_id=session.session_id, topic_id=b)
                _ = [queued async for queued in session.prompt(question_b, streaming_behavior='follow_up')]
        self.assertEqual([message['text'] for message in (await session.topic_snapshot(a))['messages']], [question_a, 'answer A'])
        self.assertEqual([message['text'] for message in (await session.topic_snapshot(b))['messages']], [question_b, 'answer B'])
        summaries = [call for call in provider.calls if call[1] == SUMMARIZATION_SYSTEM_PROMPT]
        self.assertEqual(len(summaries), 2)
        self.assertIn(a, summaries[-1][2][0].text)
        self.assertIn('shared_summary', summaries[-1][2][0].text)
        self.assertIn(b, provider.calls[-1][2][-1].text)
        originals = [entry for entry in await session.session_entries() if isinstance(entry, MessageEntry)]
        self.assertEqual([entry.model_dump() for entry in originals[:2]], [entry.model_dump() for entry in seeded])

    async def test_successful_automatic_summary_resets_consecutive_failure_count(self):
        session, provider, _, _ = await self.make_session([
            failure('summary unavailable'), response('answer 1'), response('compact summary'), response('answer 2'),
        ], seed=(UserMessage(content='old ' * 600), AssistantMessage(content='old answer ' * 500)))
        _ = [event async for event in session.prompt('question 1')]
        self.assertEqual(session.context_summary_failures, 1)
        _ = [event async for event in session.prompt('question 2')]
        self.assertEqual(session.context_summary_failures, 0)
        receipt = [entry for entry in await session.session_entries() if isinstance(entry, CustomEntry)
                   and entry.namespace == 'octo.context-compaction'][-1]
        self.assertEqual(receipt.data['failures'], 0)

    async def test_new_session_does_not_reuse_previous_compaction_projection(self):
        session, provider, _, _ = await self.make_session([
            response('old session summary'), response('old session answer'), response('new session answer'),
        ], seed=(UserMessage(content='old ' * 600), AssistantMessage(content='old answer ' * 500)))
        _ = [event async for event in session.prompt('old task')]
        await session.new_session()
        await session.set_session_name('New compaction fixture')
        _ = [event async for event in session.prompt('new task')]
        self.assertEqual(session.context_summary_failures, 0)
        self.assertEqual(session.messages[-1].text, 'new session answer')
        self.assertFalse(any('old session summary' in message.text for message in provider.calls[-1][2]))

    async def test_reopen_after_partial_failed_tool_call_can_continue(self):
        partial_error = AssistantMessage(content=[ToolCall(id='partial', name='read', arguments={})],
            model='offline', stop_reason='error', error_message='Connection interrupted')
        session, provider, config, _ = await self.make_session([
            [AssistantErrorEvent(reason='error', error=partial_error)], response('resumed safely'),
        ], ratio=0.95)
        _ = [event async for event in session.prompt('current question')]
        restored = await CodingSession.load(config)
        self.addAsyncCleanup(restored.aclose)
        _ = [event async for event in restored.continue_()]
        self.assertEqual(restored.messages[-1].text, 'resumed safely')
        self.assertFalse(any(isinstance(message, ToolResultMessage) and message.tool_call_id == 'partial'
                             for message in provider.calls[-1][2]))

    async def test_queued_steering_overflow_really_sends_its_retry(self):
        session, provider, _, _ = await self.make_session([
            response('first answer'), failure('Context window exceeded'), response('queued retry answer'),
        ], ratio=0.95, seed=(UserMessage(content='old ' * 200), AssistantMessage(content='old answer ' * 150)))
        async for event in session.prompt('first question'):
            if isinstance(event, MessageEndEvent) and isinstance(event.message, AssistantMessage) and event.message.text == 'first answer':
                _ = [queued async for queued in session.prompt('follow-up question', streaming_behavior='steer')]
        self.assertEqual(len(provider.calls), 3)
        self.assertEqual(session.messages[-1].text, 'queued retry answer')
        self.assertTrue(any(message.text == 'follow-up question' for message in provider.calls[-1][2]))

    async def test_current_round_tool_result_that_cannot_fit_is_never_summarized_or_trimmed(self):
        async def read(call_id, arguments, signal=None, on_update=None):
            return AgentToolResult(content='关键证据' * 600)
        tool = AgentTool(name='read', label='Read', description='Read evidence', parameters={}, execute_fn=read)
        session, provider, _, _ = await self.make_session([
            response('inspect', [ToolCall(id='current-read', name='read', arguments={})]),
            response('must not summarize current evidence'), response('must not send'),
        ], seed=(UserMessage(content='short old question'), AssistantMessage(content='short old answer')), tools=[tool])
        with self.assertRaises(ContextBudgetExceeded):
            _ = [event async for event in session.prompt('Use the current tool evidence')]
        self.assertEqual(len(provider.calls), 1)
        self.assertFalse(any(isinstance(entry, CompactionEntry) for entry in await session.session_entries()))
        self.assertTrue(any(isinstance(message, ToolResultMessage) and message.text == '关键证据' * 600
                            for message in session.messages))
        self.assertEqual(session.context_summary_failures, 0)
        self.assertEqual(session.last_context_budget.status, 'BLOCK')

    async def test_overflow_without_old_context_does_not_claim_a_retry(self):
        session, provider, _, _ = await self.make_session([failure('Context window exceeded'), response('must not retry')])
        flags = []
        async for event in session.prompt('current question'):
            if isinstance(event, MessageEndEvent) and isinstance(event.message, AssistantMessage) and event.message.stop_reason == 'error':
                flags.append(session.will_auto_retry(event.message))
        self.assertEqual(flags, [False])
        self.assertEqual(len(provider.calls), 1)

    async def test_disabled_recovery_does_not_claim_a_retry(self):
        session, provider, _, _ = await self.make_session([
            failure('Context window exceeded'), response('must not retry'),
        ], ratio=0.95, seed=(UserMessage(content='old question'), AssistantMessage(content='old answer')))
        session.set_auto_compaction_enabled(False)
        flags = []
        async for event in session.prompt('current question'):
            if isinstance(event, MessageEndEvent) and isinstance(event.message, AssistantMessage) and event.message.stop_reason == 'error':
                flags.append(session.will_auto_retry(event.message))
        self.assertEqual(flags, [False])
        self.assertEqual(len(provider.calls), 1)


if __name__ == '__main__':
    unittest.main()
