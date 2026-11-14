from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from dataclasses import replace
from contextlib import closing
from functools import partial
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from octo_agent.events import MessageEndEvent, ToolExecutionEndEvent
from octo_agent.messages import AssistantMessage, UserMessage, ToolCall
from octo_agent.provider_events import AssistantDoneEvent
from octo_agent.session import MessageEntry, SQLiteSessionStorage
from octo_agent.session.topics import SessionTopics, TopicRequest
from octo_agent.session.jsonl import entry_to_json_line
from octo_agent.tools import AgentTool, AgentToolResult
from octo_ai.fake import FakeProvider
from octo_coding.paths import OctoPaths
from octo_coding.resources import OctoResourcePaths
from octo_coding.session import CodingSession, CodingSessionConfig
from octo_coding.session_manager import SessionManager
from octo_coding.topic_web import TopicWeb


class TopicTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.env = patch.dict(os.environ, {'OCTO_HOME': str(self.base / 'home'), 'OCTO_OFFLINE': '1'})
        self.env.start()
        self.addCleanup(self.env.stop)

    async def make_session(self, streams=None):
        paths = OctoPaths(home=self.base/'home', agents_home=self.base/'agents')
        manager = SessionManager(paths)
        record = manager.create_session(cwd=self.base, model='offline', title='Offline topic fixture')
        provider = FakeProvider(streams or [[AssistantDoneEvent(reason='stop', message=AssistantMessage(content='answer', model='offline'))]])
        config = CodingSessionConfig(provider=provider, model='offline', storage=manager.storage_for(record),
            session_id=record.id, session_manager=manager, cwd=self.base,
            resource_paths=OctoResourcePaths(root=paths.home, agents_root=paths.agents_home, cwd=self.base, paths=paths),
            auto_compact_enabled=False, extensions_enabled=False, skills_enabled=False, trust_override='approve')
        session = await CodingSession.load(config)
        self.addAsyncCleanup(session.aclose)
        return session, provider, config

    async def test_switch_during_answer_freezes_attribution_and_context(self):
        session, provider, config = await self.make_session()
        a = await session.edit_topic('create', session_id=session.session_id, title='Topic A')
        b = await session.edit_topic('create', session_id=session.session_id, title='Topic B')
        await session.edit_topic('select', session_id=session.session_id, topic_id=a)
        stream = session.prompt('question A')
        async for event in stream:
            if isinstance(event, MessageEndEvent) and isinstance(event.message, UserMessage):
                await session.edit_topic('select', session_id=session.session_id, topic_id=b)
        data = await session.topic_snapshot(a)
        self.assertEqual([m['text'] for m in data['messages']], ['question A', 'answer'])
        self.assertEqual(data['selected_topic_id'], b)
        self.assertEqual((await session.topic_snapshot(b))['messages'], [])
        self.assertIn(a, provider.calls[0][2][-1].text)
        self.assertNotIn(b, provider.calls[0][2][-1].text)
        self.assertEqual([m.text for m in session.messages], ['question A', 'answer'])
        restored = await CodingSession.load(config)
        self.addAsyncCleanup(restored.aclose)
        self.assertEqual(restored.current_topic_id, b)
        self.assertEqual((await restored.topic_snapshot(a))['messages'], data['messages'])

    async def test_queued_follow_up_keeps_enqueue_topic_and_shared_history(self):
        answer = lambda text: [AssistantDoneEvent(reason='stop', message=AssistantMessage(content=text, model='offline'))]
        session, provider, _ = await self.make_session([answer('answer A'), answer('answer B')])
        b = await session.edit_topic('create', session_id=session.session_id, title='Topic B')
        stream = session.prompt('question A')
        queued = False
        async for event in stream:
            if isinstance(event, MessageEndEvent) and isinstance(event.message, UserMessage) and not queued:
                queued = True
                await session.edit_topic('select', session_id=session.session_id, topic_id=b)
                _ = [e async for e in session.prompt('question B', streaming_behavior='follow_up')]
                await session.edit_topic('select', session_id=session.session_id, topic_id='main')
        self.assertEqual([m['text'] for m in (await session.topic_snapshot(b))['messages']], ['question B','answer B'])
        self.assertIn('question A', [m.text for m in provider.calls[1][2]])
        self.assertIn('answer A', [m.text for m in provider.calls[1][2]])
        self.assertIn(b, provider.calls[1][2][-1].text)

    async def test_atomic_conflicts_anchor_and_legacy_main(self):
        storage = SQLiteSessionStorage(self.base/'db', 'session')
        old = MessageEntry(id='old', message=UserMessage(content='legacy'))
        await storage.append(old)
        topics = SessionTopics(storage)
        child = topics.create(parent_id='main', title='child', anchor_entry_id='old')
        self.assertEqual(topics.selected_id, 'main')
        self.assertEqual(topics.snapshot()['messages'][0]['text'], 'legacy')
        with self.assertRaises(ValueError): topics.create(parent_id='main', title='x', anchor_entry_id='missing')
        first = topics.capture(child)
        entry = MessageEntry(id='new', parent_id=old.id, message=UserMessage(content='new'))
        topics.append_message(entry, first)
        topics.append_message(entry, first)
        with self.assertRaises(ValueError): topics.append_message(entry, topics.capture('main'))
        self.assertEqual(len(await storage.read_all()), 2)
        with storage.database.connection() as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM topic_requests').fetchone()[0], 1)
        topics.select(child)
        self.assertEqual(SessionTopics(SQLiteSessionStorage(storage.path, 'session')).selected_id, child)
        self.assertEqual(SessionTopics(SQLiteSessionStorage(storage.path, 'other')).snapshot()['messages'], [])

    async def test_web_attaches_same_session_authenticates_and_never_prompts(self):
        session, provider, _ = await self.make_session()
        directory = self.base/'static'; directory.mkdir(); (directory/'index.html').write_text('map')
        server = TopicWeb(session, directory=directory)
        self.addCleanup(server.close)
        async with httpx.AsyncClient(trust_env=False) as client:
            self.assertEqual((await client.get(server.origin+'/api/snapshot')).status_code, 403)
            headers = {'X-Octo-Token': server.token}
            self.assertEqual((await client.get(server.origin+'/api/snapshot', headers={**headers,'Origin':'https://outside.example'})).status_code,403)
            payload = {'action':'create','session_id':session.session_id,'topic_id':'main','title':'from map'}
            response = await client.post(server.origin+'/api/topics', headers=headers, json=payload)
            self.assertEqual(response.status_code,200,response.text)
            topic = response.json()['topic_id']
            await client.post(server.origin+'/api/topics', headers=headers, json={**payload,'action':'select','topic_id':topic})
            self.assertEqual(session.current_topic_id, topic)
            data = (await client.get(server.origin+'/api/snapshot',headers=headers)).json()
            self.assertEqual(data['selected_topic_id'], topic)
            self.assertEqual(provider.calls, [])
            response = await client.post(server.origin+'/api/topics',headers=headers,json={**payload,'session_id':'stale'})
            self.assertEqual(response.status_code,400)
            async with client.stream('GET', server.origin+'/api/events?token='+server.token) as events:
                self.assertEqual(events.status_code, 200)
                line = await anext(events.aiter_lines())
                self.assertTrue(line.startswith('data:'))

    async def test_topic_commands_and_new_session_do_not_reuse_old_callback(self):
        session, provider, _ = await self.make_session()
        self.assertIsNotNone(session.handle_command('/topic-new example').topic_arguments)
        self.assertTrue(session.handle_command('/map').map_requested)
        await session.topic_command('new example')
        old_id = session.session_id
        topic = (await session.topic_snapshot())['topics'][-1]['id']
        await session.topic_command('use '+topic)
        await session.new_session()
        self.assertNotEqual(session.session_id, old_id)
        self.assertEqual(session.current_topic_id, 'main')
        _ = [event async for event in session.prompt('fresh')]
        self.assertIn('main', provider.calls[-1][2][-1].text)
        self.assertNotIn(topic, provider.calls[-1][2][-1].text)

    async def test_stream_close_clears_active_topic_and_can_continue(self):
        session, provider, _ = await self.make_session()
        child = await session.edit_topic('create', session_id=session.session_id, title='child')
        await session.edit_topic('select', session_id=session.session_id, topic_id=child)
        stream = session.prompt('paused question')
        async for event in stream:
            if isinstance(event, MessageEndEvent) and isinstance(event.message, UserMessage):
                break
        await stream.aclose()
        data = await session.topic_snapshot(child)
        self.assertFalse(data['running'])
        self.assertIsNone(data['active_topic_id'])
        await session.edit_topic('select', session_id=session.session_id, topic_id='main')
        _ = [event async for event in session.continue_()]
        self.assertIn(child, provider.calls[-1][2][-1].text)
        self.assertEqual([m['text'] for m in (await session.topic_snapshot(child))['messages']], ['paused question','answer'])

    async def test_terminal_topic_commands_and_map_attach_without_model_calls(self):
        from octo_coding.tui.app import OctoTuiApp, PromptInput
        session, provider, _ = await self.make_session()
        app = OctoTuiApp(session)
        directory = self.base/'map-assets'
        directory.mkdir()
        (directory/'index.html').write_text('offline map fixture', encoding='utf-8')
        with patch('webbrowser.open', return_value=True), patch(
            'octo_coding.tui.app.TopicWeb', partial(TopicWeb, directory=directory)
        ):
            async with app.run_test(size=(110,35)) as pilot:
                await pilot.pause()
                prompt = app.query_one('#prompt', PromptInput)
                prompt.text = '/topic-new terminal child'
                await app.action_submit_prompt()
                await pilot.pause()
                child = (await session.topic_snapshot())['topics'][-1]['id']
                prompt.text = '/topic-use '+child
                await app.action_submit_prompt()
                await pilot.pause()
                self.assertIn('terminal child', str(app.query_one('#topic-indicator').render()))
                prompt.text = '/map'
                await app.action_submit_prompt()
                await pilot.pause()
                self.assertIsNotNone(app._topic_web)
                self.assertIs(app._topic_web.session, session)
                web = app._topic_web
                async with httpx.AsyncClient(trust_env=False) as client:
                    response = await client.get(web.origin)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.text, 'offline map fixture')
                self.assertEqual(provider.calls, [])
                await pilot.pause()
        self.assertTrue(web.closed)

    async def test_tool_round_keeps_frozen_topic_and_static_prefix(self):
        first = AssistantMessage(content=[ToolCall(id='tool-1', name='fixture', arguments={})], model='offline', stop_reason='toolUse')
        final = AssistantMessage(content='tool answer', model='offline')
        session, provider, config = await self.make_session([
            [AssistantDoneEvent(reason='toolUse', message=first)],
            [AssistantDoneEvent(reason='stop', message=final)],
        ])
        async def execute(call_id, arguments, signal=None, on_update=None):
            return AgentToolResult(content='fixture evidence')
        tool = AgentTool(name='fixture', label='Fixture', description='Offline fixture', parameters={'type':'object','properties':{}}, execute_fn=execute)
        await session.aclose()
        session = await CodingSession.load(replace(config, tools=[tool]))
        self.addAsyncCleanup(session.aclose)
        a = await session.edit_topic('create', session_id=session.session_id, title='original title')
        b = await session.edit_topic('create', session_id=session.session_id, title='different topic')
        await session.edit_topic('select', session_id=session.session_id, topic_id=a)
        async for event in session.prompt('use fixture'):
            if isinstance(event, ToolExecutionEndEvent):
                await session.edit_topic('select', session_id=session.session_id, topic_id=b)
                await session.edit_topic('rename', session_id=session.session_id, topic_id=a, title='changed title')
        self.assertEqual(len(provider.calls), 2)
        self.assertEqual(provider.calls[0][1], provider.calls[1][1])
        self.assertEqual(provider.calls[0][3], provider.calls[1][3])
        first = json.loads(provider.calls[0][2][-1].text.split('\n', 1)[1].rsplit('\n', 1)[0])
        second = json.loads(provider.calls[1][2][-1].text.split('\n', 1)[1].rsplit('\n', 1)[0])
        self.assertEqual(first['current_topic'], second['current_topic'])
        self.assertEqual([source['topic_id'] for source in second['history_sources']], [a, a, a])
        self.assertIn('original title', provider.calls[1][2][-1].text)
        data = await session.topic_snapshot(a)
        self.assertIn('fixture evidence', [message['text'] for message in data['messages']])
        self.assertEqual((await session.topic_snapshot(b))['messages'], [])

    async def test_schema_one_upgrade_preserves_message_body(self):
        path = self.base/'v1.db'
        original = MessageEntry(id='old-entry', message=UserMessage(content='before upgrade'))
        with closing(sqlite3.connect(path)) as connection:
            connection.execute('CREATE TABLE sessions(session_id TEXT PRIMARY KEY, record_json TEXT)')
            connection.execute('CREATE TABLE session_entries(session_id TEXT, id TEXT, seq INTEGER, body_json TEXT, PRIMARY KEY(session_id,id), UNIQUE(session_id,seq))')
            connection.execute('INSERT INTO sessions VALUES (?, NULL)', ('old-session',))
            connection.execute('INSERT INTO session_entries VALUES (?, ?, ?, ?)', ('old-session', original.id, 1, entry_to_json_line(original)))
            connection.execute('PRAGMA user_version=1')
            connection.commit()
        storage = SQLiteSessionStorage(path, 'old-session')
        self.assertEqual((await storage.read_all())[0], original)
        topics = SessionTopics(storage)
        child = topics.create(parent_id='main', title='after upgrade', anchor_entry_id=original.id)
        topics.select(child)
        with storage.database.connection() as connection:
            self.assertEqual(connection.execute('PRAGMA user_version').fetchone()[0],3)
            self.assertEqual(connection.execute('SELECT body_json FROM session_entries').fetchone()[0], entry_to_json_line(original))

    async def test_continuation_uses_own_mainline_and_root_is_distinct(self):
        storage = SQLiteSessionStorage(self.base/'continuation.db', 'session')
        main = MessageEntry(id='root-latest', message=UserMessage(content='main'))
        await storage.append(main)
        topics = SessionTopics(storage)
        a = topics.create(parent_id='main', title='A')
        b = topics.create(parent_id=a, title='B', anchor_entry_id=main.id)
        c = topics.create(parent_id=a, title='C', anchor_entry_id=main.id)
        entry = MessageEntry(id='a-latest', parent_id=main.id, message=UserMessage(content='A latest'))
        topics.append_message(entry, topics.capture(a))
        topics.append_message(MessageEntry(id='b-latest', parent_id=entry.id, message=UserMessage(content='B latest')), topics.capture(b))
        self.assertEqual(topics.capture('main').continuation_entry_id, 'root-latest')
        self.assertEqual(topics.capture(a).continuation_entry_id, 'a-latest')
        self.assertEqual(topics.capture(b).continuation_entry_id, 'b-latest')
        self.assertEqual(topics.capture(c).continuation_entry_id, 'root-latest')
        labels = {topic['id']: topic for topic in topics.topics()}
        self.assertEqual(labels[a]['anchor_entry_id'], main.id)
        self.assertEqual(labels['main']['kind'], 'trunk')
        self.assertEqual(labels[b]['branch_label'], '分支 1.1')
        self.assertEqual(labels[c]['parent_label'], '分支 1')
        with self.assertRaises(ValueError): topics.rename('main', 'ordinary')
        topics.rename(a, 'renamed A')
        self.assertEqual({topic['id']:topic for topic in topics.topics()}[b]['branch_label'], '分支 1.1')

    async def test_request_persists_latest_position_without_following_child(self):
        session, provider, _ = await self.make_session()
        _ = [event async for event in session.prompt('main question')]
        main_latest = (await session.topic_snapshot())['messages'][-1]['id']
        a = await session.edit_topic('create', session_id=session.session_id, title='child', anchor_entry_id=main_latest)
        await session.edit_topic('select', session_id=session.session_id, topic_id=a)
        _ = [event async for event in session.prompt('child question')]
        await session.edit_topic('select', session_id=session.session_id, topic_id='main')
        _ = [event async for event in session.prompt('continue main')]
        self.assertIn(main_latest, provider.calls[-1][2][-1].text)
        self.assertIn('"kind": "trunk"', provider.calls[-1][2][-1].text)
        with session.storage.database.connection() as connection:
            row = connection.execute("SELECT continuation_entry_id FROM topic_requests WHERE topic_id='main' ORDER BY rowid DESC LIMIT 1").fetchone()
        self.assertEqual(row[0], main_latest)

    async def test_rounds_group_tools_and_connect_original_fork(self):
        storage = SQLiteSessionStorage(self.base/'rounds.db', 'session')
        topics = SessionTopics(storage)
        first = topics.capture()
        for entry_id, message in [('u1',UserMessage(content='first question')), ('a1',AssistantMessage(content='first answer'))]:
            topics.append_message(MessageEntry(id=entry_id, message=message), first)
        branch = topics.create(parent_id='main', title='fork', anchor_entry_id='u1')
        second = topics.capture('main')
        topics.append_message(MessageEntry(id='u2',message=UserMessage(content='second question')), second)
        fork = topics.capture(branch)
        topics.append_message(MessageEntry(id='u3',message=UserMessage(content='fork question')), fork)
        topics.append_message(MessageEntry(id='a3',message=AssistantMessage(content='tool phase')), fork)
        topics.append_message(MessageEntry(id='a4',message=AssistantMessage(content='final answer')), fork)
        turns = topics.rounds()
        self.assertEqual(len(turns),3)
        self.assertEqual(turns[-1]['message_count'],3)
        self.assertEqual(turns[-1]['parent_turn_id'],first.id)
        self.assertEqual(turns[1]['parent_turn_id'],first.id)
        self.assertEqual(turns[-1]['number'],1)
        self.assertEqual(turns[1]['number'],2)



    async def test_hyphen_commands_create_and_select_and_accept_unique_title(self):
        session, provider, _ = await self.make_session()
        command = session.handle_command('/topic-new memory')
        self.assertTrue(command.handled)
        await session.topic_command(command.topic_arguments)
        child = session.current_topic_id
        self.assertNotEqual(child, 'main')
        await session.topic_command(session.handle_command('/topic-use main').topic_arguments)
        await session.topic_command(session.handle_command('/topic-use memory').topic_arguments)
        self.assertEqual(session.current_topic_id, child)
        await session.edit_topic('create', session_id=session.session_id, title='memory')
        with self.assertRaisesRegex(ValueError, '重名'):
            await session.topic_command('use memory')
        self.assertEqual(provider.calls, [])

    async def test_web_create_can_select_atomically_without_model_calls(self):
        session, provider, _ = await self.make_session()
        directory = self.base/'static'; directory.mkdir(); (directory/'index.html').write_text('map')
        server = TopicWeb(session, directory=directory)
        self.addCleanup(server.close)
        async with httpx.AsyncClient(trust_env=False) as client:
            response = await client.post(server.origin+'/api/topics',
                headers={'X-Octo-Token': server.token}, json={'action':'create',
                'session_id':session.session_id, 'topic_id':'main', 'title':'web child', 'activate':True})
        self.assertEqual(response.status_code, 200, response.text)
        child = response.json()['topic_id']
        self.assertEqual(session.current_topic_id, child)
        with self.assertRaises(ValueError):
            await session.edit_topic('create', session_id=session.session_id, topic_id='main',
                title='invalid', anchor_entry_id='missing', activate=True)
        self.assertEqual(session.current_topic_id, child)
        self.assertEqual(len((await session.topic_snapshot())['topics']), 2)
        self.assertEqual(provider.calls, [])

    async def test_model_input_identifies_runtime_and_each_history_topic(self):
        answer = lambda text: [AssistantDoneEvent(reason='stop', message=AssistantMessage(content=text, model='offline'))]
        session, provider, _ = await self.make_session([answer('main answer'), answer('child answer')])
        _ = [event async for event in session.prompt('main question')]
        await session.topic_command('new child')
        child = session.current_topic_id
        _ = [event async for event in session.prompt('which topic?')]
        reminder = provider.calls[-1][2][-1].text
        self.assertIn('Octo', reminder)
        self.assertIn('offline', reminder)
        self.assertIn('history_sources', reminder)
        payload = json.loads(reminder.split('\n', 1)[1].rsplit('\n', 1)[0])
        self.assertEqual(payload['runtime']['model'], 'offline')
        sources = payload['history_sources']
        self.assertEqual([s['topic_id'] for s in sources], ['main', 'main', child])
        self.assertEqual([s['message_index'] for s in sources], [0, 1, 2])
        self.assertEqual([m.text for m in session.messages], ['main question', 'main answer', 'which topic?', 'child answer'])

    async def test_nonzero_native_shell_result_is_reported_as_error(self):
        from octo_agent.loop import _run_tool
        from octo_coding.tools import create_bash_tool
        tool = create_bash_tool(cwd=self.base)
        result, is_error, _ = await _run_tool(tool, ToolCall(id='exit', name='bash',
            arguments={'command':'exit 7', 'description':'Testing exit status'}), None)
        self.assertEqual(result.details['exit_code'], 7)
        self.assertTrue(is_error)
        self.assertIn('cmd.exe' if os.name == 'nt' else '/bin/sh', tool.description)
        call = AssistantMessage(model='offline', content=[ToolCall(id='shell-error', name='bash',
            arguments={'command':'exit 7', 'description':'Testing exit status'})], stop_reason='toolUse')
        session, provider, config = await self.make_session([
            [AssistantDoneEvent(reason='toolUse', message=call)],
            [AssistantDoneEvent(reason='stop', message=AssistantMessage(content='reported failure', model='offline'))],
        ])
        _ = [event async for event in session.prompt('run the harmless failure fixture')]
        from octo_agent.messages import ToolResultMessage
        evidence = [message for message in provider.calls[1][2] if isinstance(message, ToolResultMessage)]
        self.assertEqual(len(evidence), 1)
        self.assertTrue(evidence[0].is_error)
        stored = [entry.message for entry in await config.storage.read_all()
                  if isinstance(entry, MessageEntry) and isinstance(entry.message, ToolResultMessage)]
        self.assertTrue(stored[0].is_error)


if __name__ == '__main__':
    unittest.main()
