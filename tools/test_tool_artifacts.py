from __future__ import annotations

from pathlib import Path
import tempfile
import os
import re
from unittest.mock import patch
import unittest

from octo_agent.messages import ToolCall
from octo_agent.session import SQLiteSessionStorage
from octo_agent.session.topics import TopicRequest


class ToolArtifactStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_full_output_survives_restart_with_frozen_source_and_middle_read(self):
        from octo_agent.session.artifacts import ToolArtifactStore
        with tempfile.TemporaryDirectory() as temporary:
            path=Path(temporary)/'sessions.db'
            text='first\n'+'data '*10000+'MIDDLE_EVIDENCE\n'+'尾部🙂'*4000
            call=ToolCall(id='call-1',name='bash',arguments={})
            request=TopicRequest(id='request-1',topic_id='branch-1',path=('主线','分支'),continuation_entry_id=None)
            store=ToolArtifactStore(SQLiteSessionStorage(path,'session-1'))
            reference=await store.put(call,text,request=request)
            restarted=ToolArtifactStore(SQLiteSessionStorage(path,'session-1'))
            middle=await restarted.read(reference.id,offset=text.index('MIDDLE_EVIDENCE'),limit=16)
            self.assertEqual(middle.text,'MIDDLE_EVIDENCE\n')
            self.assertEqual(middle.reference.id,reference.id)
            self.assertEqual(middle.reference.request_id,'request-1')
            self.assertEqual(middle.reference.topic_id,'branch-1')
            self.assertEqual(middle.reference.topic_path,('主线','分支'))
            self.assertEqual(middle.reference.byte_count,len(text.encode('utf-8')))




    async def test_utf8_nul_bounded_reads_and_session_isolation(self):
        from octo_agent.session.artifacts import ToolArtifactStore
        with tempfile.TemporaryDirectory() as temporary:
            path=Path(temporary)/'sessions.db'
            store=ToolArtifactStore(SQLiteSessionStorage(path,'one'))
            text='零🙂\0abc\n汉字🙂END'
            reference=await store.put(ToolCall(id='unicode',name='fixture',arguments={}),text)
            offset=0;parts=[]
            while True:
                chunk=await store.read(reference.id,offset=offset,limit=4)
                self.assertLessEqual(len(chunk.text.encode('utf-8')),4)
                parts.append(chunk.text)
                if chunk.next_offset is None: break
                self.assertGreater(chunk.next_offset,offset)
                offset=chunk.next_offset
            self.assertEqual(''.join(parts),text)
            aligned=await store.read(reference.id,offset=1,limit=4)
            self.assertEqual((aligned.offset,aligned.text),(3,'🙂'))
            self.assertIsNone((await store.read(reference.id,offset=reference.byte_count)).next_offset)
            with self.assertRaises(ValueError):
                await ToolArtifactStore(SQLiteSessionStorage(path,'two')).read(reference.id)
            for arguments in [{'offset':-1},{'offset':True},{'offset':reference.byte_count+1},{'offset':10**100},{'limit':3},{'limit':4097}]:
                with self.assertRaises(ValueError): await store.read(reference.id,**arguments)
            with self.assertRaises(ValueError): await store.read('../secrets')

    async def test_repeated_save_is_idempotent_without_misreporting_error_status(self):
        from octo_agent.session.artifacts import ToolArtifactStore
        with tempfile.TemporaryDirectory() as temporary:
            store=ToolArtifactStore(SQLiteSessionStorage(Path(temporary)/'sessions.db','one'))
            call=ToolCall(id='same-call',name='fixture',arguments={})
            first=await store.put(call,'identical output')
            repeated=await store.put(call,'identical output')
            self.assertEqual(first,repeated)
            failed=await store.put(call,'identical output',is_error=True)
            self.assertNotEqual(first.id,failed.id)
            self.assertTrue((await store.read(failed.id)).reference.is_error)
            self.assertFalse((await store.read(first.id)).reference.is_error)



    async def test_schema_three_upgrade_keeps_history_and_topic_selection(self):
        import sqlite3
        from contextlib import closing
        from octo_agent.messages import UserMessage
        from octo_agent.session import MessageEntry
        from octo_agent.session.topics import SessionTopics
        from octo_agent.session.artifacts import ToolArtifactStore
        with tempfile.TemporaryDirectory() as temporary:
            path=Path(temporary)/'sessions.db'
            storage=SQLiteSessionStorage(path,'old-session')
            entry=MessageEntry(message=UserMessage(content='original history'))
            await storage.append(entry)
            topics=SessionTopics(storage)
            child=topics.create(parent_id='main',title='original branch',activate=True)
            with closing(sqlite3.connect(path)) as connection:
                connection.execute('DROP TABLE tool_artifacts')
                connection.execute('PRAGMA user_version=3')
                connection.commit()
            upgraded=SQLiteSessionStorage(path,'old-session')
            store=ToolArtifactStore(upgraded)
            saved=await store.put(ToolCall(id='after-upgrade',name='fixture',arguments={}), 'persistent text')
            self.assertEqual((await store.read(saved.id)).text,'persistent text')
            self.assertEqual(await upgraded.read_all(),[entry])
            self.assertEqual(SessionTopics(upgraded).selected_id,child)


class ToolArtifactSessionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name)
        self.env=patch.dict(os.environ,{'OCTO_HOME':str(self.base/'home'),'OCTO_OFFLINE':'1'})
        self.env.start();self.addCleanup(self.env.stop)

    async def make_session(self, streams, tools=None, provider=None, extension_paths=()):
        from octo_ai.fake import FakeProvider
        from octo_ai.model_limits import RuntimeModelLimits
        from octo_coding.paths import OctoPaths
        from octo_coding.resources import OctoResourcePaths
        from octo_coding.session import CodingSession, CodingSessionConfig
        from octo_coding.session_manager import SessionManager
        class LimitedProvider(FakeProvider):
            def configure_request_budget(self,*,context_window,safety_margin,output_override=None,model_max_output_tokens=None):
                return super().configure_request_budget(context_window=context_window,safety_margin=safety_margin,
                    output_override=512 if output_override is None else output_override,model_max_output_tokens=model_max_output_tokens)
            async def discover_model_limits(self, model):
                return RuntimeModelLimits(context_window=6000,max_output_tokens=512)
        paths=OctoPaths(home=self.base/'home',agents_home=self.base/'agents')
        manager=SessionManager(paths)
        record=manager.create_session(cwd=self.base,model='offline',title='Artifact fixture')
        provider=provider or LimitedProvider(streams)
        config=CodingSessionConfig(provider=provider,model='offline',storage=manager.storage_for(record),
            session_id=record.id,session_manager=manager,cwd=self.base,system='Artifact test assistant',tools=tools,
            resource_paths=OctoResourcePaths(root=paths.home,agents_root=paths.agents_home,cwd=self.base,paths=paths),
            context_output_reserve_tokens=512,context_safety_margin=128,auto_compact_enabled=False,
            extensions_enabled=False,extension_paths=extension_paths,skills_enabled=False,trust_override='approve')
        session=await CodingSession.load(config)
        self.addAsyncCleanup(session.aclose)
        return session,provider,config

    async def test_large_current_tool_result_does_not_block_next_model_call(self):
        from octo_agent.messages import AssistantMessage, ToolResultMessage
        from octo_agent.provider_events import AssistantDoneEvent
        from octo_agent.tools import AgentTool, AgentToolResult
        from octo_agent.session.artifacts import ToolArtifactStore
        text='start\n'+'x'*40000+'MIDDLE_EVIDENCE\n'+'y'*40000+'\nend'
        async def execute(call_id,arguments,signal=None,on_update=None):
            return AgentToolResult(content=text)
        tool=AgentTool(name='large',label='Large',description='Return large text',parameters={},execute_fn=execute)
        call=AssistantMessage(model='offline',stop_reason='toolUse',content=[ToolCall(id='large-1',name='large',arguments={})])
        done=AssistantMessage(model='offline',content='Finished despite the large current result')
        session,provider,config=await self.make_session([
            [AssistantDoneEvent(reason='toolUse',message=call)],[AssistantDoneEvent(reason='stop',message=done)]],[tool])
        _=[event async for event in session.prompt('Inspect the full tool output')]
        self.assertEqual(len(provider.calls),2)
        evidence=next(message for message in provider.calls[1][2] if isinstance(message,ToolResultMessage))
        self.assertLess(len(evidence.text.encode('utf-8')),2048)
        self.assertEqual(evidence.tool_call_id,'large-1')
        self.assertFalse(evidence.is_error)
        self.assertEqual(session.last_context_budget.status,'ALLOW')
        reference=re.search(r'art_[0-9a-f]{64}',evidence.text).group()
        middle=await ToolArtifactStore(config.storage).read(reference,offset=text.index('MIDDLE_EVIDENCE'),limit=16)
        self.assertEqual(middle.text,'MIDDLE_EVIDENCE\n')
        self.assertIn('read_artifact',[tool.name for tool in provider.calls[0][3]])
        self.assertEqual([tool.name for tool in provider.calls[0][3]],[tool.name for tool in provider.calls[1][3]])



    async def test_native_bash_persists_pretruncation_text_and_model_reads_middle(self):
        import sys
        from octo_ai.fake import FakeProvider
        from octo_ai.model_limits import RuntimeModelLimits
        from octo_agent.messages import AssistantMessage, ToolResultMessage
        from octo_agent.provider_events import AssistantDoneEvent
        command=f'"{sys.executable}" -c "print(\'A\'*70000+\'MIDDLE_EVIDENCE\'+\'B\'*70000)"'
        class ReadingProvider(FakeProvider):
            async def discover_model_limits(self,model): return RuntimeModelLimits(context_window=6000,max_output_tokens=512)
            def stream_response(self,*,model,system,messages,tools,signal=None,session_id=None):
                self.calls.append((model,system,list(messages),list(tools)))
                if len(self.calls)==1:
                    call=ToolCall(id='bash-big',name='bash',arguments={'command':command,'description':'Testing persistent output'})
                    message=AssistantMessage(model=model,stop_reason='toolUse',content=[call])
                elif len(self.calls)==2:
                    output=next(message for message in messages if isinstance(message,ToolResultMessage) and message.tool_name=='bash')
                    artifact=re.search(r'art_[0-9a-f]{64}',output.text).group()
                    call=ToolCall(id='read-middle',name='read_artifact',arguments={'artifact_id':artifact,'offset':70000,'limit':32})
                    message=AssistantMessage(model=model,stop_reason='toolUse',content=[call])
                else: message=AssistantMessage(model=model,content='Verified the middle evidence')
                async def events(): yield AssistantDoneEvent(reason=message.stop_reason,message=message)
                return events()
        provider=ReadingProvider([])
        session,provider,config=await self.make_session([],provider=provider)
        _=[event async for event in session.prompt('Run the long local output fixture and inspect its middle')]
        self.assertEqual(len(provider.calls),3)
        results=[message for message in provider.calls[2][2] if isinstance(message,ToolResultMessage)]
        self.assertFalse(results[-1].is_error,results[-1].text)
        self.assertIn('MIDDLE_EVIDENCE',results[-1].text)
        self.assertGreater(results[0].details['artifact']['byte_count'],140000)
        self.assertIsNone(results[0].details['full_output_path'])
        self.assertLess(len(results[0].model_dump_json().encode('utf-8')),8192)
        self.assertLess(len(results[0].text.encode('utf-8')),2048)
        self.assertLess(len(results[-1].text.encode('utf-8')),4500)
        self.assertFalse(results[-1].details.get('artifact'))



    async def test_reload_new_session_resume_and_frozen_request_keep_artifacts_scoped(self):
        import sys
        from octo_agent.events import ToolExecutionStartEvent
        from octo_agent.messages import AssistantMessage,ToolResultMessage
        from octo_agent.provider_events import AssistantDoneEvent
        from octo_agent.session.artifacts import ToolArtifactStore
        command=f'"{sys.executable}" -c "print(\'Z\'*12000)"'
        streams=[]
        for number in range(3):
            streams.extend([[AssistantDoneEvent(reason='toolUse',message=AssistantMessage(model='offline',stop_reason='toolUse',
                content=[ToolCall(id='reused-call',name='bash',arguments={'command':command,'description':'Checking session storage'})]))],
                [AssistantDoneEvent(reason='stop',message=AssistantMessage(model='offline',content='done'))]])
        session,provider,config=await self.make_session(streams)
        old_session=session.session_id
        a=await session.edit_topic('create',session_id=old_session,title='A',activate=True)
        b=await session.edit_topic('create',session_id=old_session,title='B')
        async for event in session.prompt('first artifact'):
            if isinstance(event,ToolExecutionStartEvent): await session.edit_topic('select',session_id=old_session,topic_id=b)
        first=next(message for message in session.messages if isinstance(message,ToolResultMessage))
        first_id=first.details['artifact']['id']
        self.assertEqual((await ToolArtifactStore(config.storage).read(first_id)).reference.topic_id,a)
        await session.reload()
        self.assertEqual(sum(tool.name=='read_artifact' for tool in session.tools),1)
        read_tool=next(tool for tool in session.tools if tool.name=='read_artifact')
        self.assertIn('ZZZZ',(await read_tool.execute('reload-read',{'artifact_id':first_id,'limit':4})).text)
        await session.new_session()
        await session.set_session_name('New artifact fixture')
        self.assertNotEqual(session.session_id,old_session)
        read_tool=next(tool for tool in session.tools if tool.name=='read_artifact')
        with self.assertRaises(ValueError): await read_tool.execute('isolated',{'artifact_id':first_id})
        _=[event async for event in session.prompt('second artifact in a new session')]
        second=next(message for message in session.messages if isinstance(message,ToolResultMessage))
        with self.assertRaises(ValueError): await ToolArtifactStore(config.storage).read(second.details['artifact']['id'])
        await session.resume(old_session)
        read_tool=next(tool for tool in session.tools if tool.name=='read_artifact')
        self.assertIn('ZZZZ',(await read_tool.execute('resume-read',{'artifact_id':first_id,'limit':4})).text)
        c=await session.edit_topic('create',session_id=old_session,title='C',activate=True)
        _=[event async for event in session.prompt('third artifact after resume')]
        third=[message for message in session.messages if isinstance(message,ToolResultMessage)][-1]
        reference=(await ToolArtifactStore(config.storage).read(third.details['artifact']['id'])).reference
        self.assertEqual(reference.topic_id,c)
        self.assertIsNotNone(reference.request_id)

    async def test_compaction_preserves_artifact_readback(self):
        from octo_agent.messages import AssistantMessage
        from octo_agent.provider_events import AssistantDoneEvent
        from octo_agent.tools import AgentTool,AgentToolResult
        text='retained original '*3000
        async def execute(*args,**kwargs): return AgentToolResult(content=text)
        tool=AgentTool(name='large',label='Large',description='Large',parameters={},execute_fn=execute)
        streams=[[AssistantDoneEvent(reason='toolUse',message=AssistantMessage(model='offline',stop_reason='toolUse',
            content=[ToolCall(id='before-compact',name='large',arguments={})]))]]
        for answer in ['completed old task','current answer','The old inspection completed; original logs remain stored.']:
            streams.append([AssistantDoneEvent(reason='stop',message=AssistantMessage(model='offline',content=answer))])
        session,provider,config=await self.make_session(streams,[tool])
        _=[event async for event in session.prompt('old inspection')]
        artifact=next(message.details['artifact']['id'] for message in session.messages if getattr(message,'tool_name',None)=='large')
        _=[event async for event in session.prompt('new question')]
        await session.compact()
        read_tool=next(tool for tool in session.tools if tool.name=='read_artifact')
        result=await read_tool.execute('after-compact',{'artifact_id':artifact,'offset':0,'limit':64})
        self.assertIn('retained original',result.text)
        from octo_agent.session import CompactionEntry
        self.assertTrue(any(isinstance(entry,CompactionEntry) for entry in await session.session_entries()))




    async def test_extension_cannot_replace_reader_but_read_hooks_still_run(self):
        from octo_agent.session.artifacts import ToolArtifactStore
        extension=self.base/'reader_override.py'
        extension.write_text(
            'from octo_agent.tools import AgentTool, AgentToolResult\n'
            'from octo_coding.extensions import ToolResultHookResult\n'
            'async def replacement(*args, **kwargs):\n'
            '    return AgentToolResult(content="WRONG_EXTENSION_READER")\n'
            'def setup(octo):\n'
            '    octo.register_tool(AgentTool(name="read_artifact",label="Wrong",description="Wrong reader",parameters={},execute_fn=replacement))\n'
            '    @octo.on("tool_result")\n'
            '    def mark(event,context):\n'
            '        if event.tool_name=="read_artifact":\n'
            '            return ToolResultHookResult(content="HOOK:"+event.result.text)\n',encoding='utf-8')
        session,provider,config=await self.make_session([],[],extension_paths=(extension,))
        reference=await ToolArtifactStore(config.storage).put(ToolCall(id='protected',name='fixture',arguments={}), 'expected original')
        reader=next(tool for tool in session.tools if tool.name=='read_artifact')
        result=await reader.execute('read',{'artifact_id':reference.id})
        self.assertIn('expected original',result.text)
        self.assertTrue(result.text.startswith('HOOK:'))
        self.assertNotIn('WRONG_EXTENSION_READER',result.text)
        self.assertEqual(sum(tool.name=='read_artifact' for tool in session.tools),1)


class ArtifactProcessingTests(unittest.IsolatedAsyncioTestCase):
    async def test_persistence_failure_does_not_emit_a_fake_reference(self):
        from octo_coding.artifacts import ArtifactTools
        from octo_agent.session.artifacts import ToolArtifactStore
        from octo_agent.tools import AgentToolResult
        with tempfile.TemporaryDirectory() as temporary:
            blocked=Path(temporary)/'blocked';blocked.write_text('not a directory')
            artifacts=ArtifactTools(ToolArtifactStore(SQLiteSessionStorage(blocked/'sessions.db','one')))
            original=AgentToolResult(content='long '*5000)
            result,error=await artifacts.externalize(ToolCall(id='failed-save',name='bash',arguments={}),original,False)
            self.assertTrue(error);self.assertTrue(result.is_error)
            self.assertIn('already executed',result.text)
            self.assertNotRegex(result.text,r'art_[0-9a-f]{64}')
            self.assertNotIn('artifact',result.details)
            self.assertEqual(original.text,'long '*5000)

    async def test_externalization_preserves_images_error_and_original_result(self):
        from octo_coding.artifacts import ArtifactTools
        from octo_agent.session.artifacts import ToolArtifactStore
        from octo_agent.messages import TextContent,ImageContent
        from octo_agent.tools import AgentToolResult
        with tempfile.TemporaryDirectory() as temporary:
            store=ToolArtifactStore(SQLiteSessionStorage(Path(temporary)/'sessions.db','one'))
            artifacts=ArtifactTools(store)
            text='正文🙂'*4000
            image=ImageContent(data='AA==',mime_type='image/png')
            original=AgentToolResult(content=[TextContent(text=text),image],is_error=True,details={'exit_code':7})
            result,error=await artifacts.externalize(ToolCall(id='image-result',name='fixture',arguments={}),original,True)
            self.assertTrue(error);self.assertTrue(result.is_error)
            self.assertEqual(result.content[-1],image)
            self.assertEqual(result.details['exit_code'],7)
            self.assertEqual(original.text,text)
            reference=(await store.read(result.details['artifact']['id'])).reference
            self.assertTrue(reference.is_error)
            self.assertLess(len(result.text.encode('utf-8')),2048)
            self.assertNotIn('unabridgedText',result.model_dump())

    async def test_read_tool_limits_and_validation_are_visible_through_execution(self):
        from octo_coding.artifacts import ArtifactTools
        from octo_agent.session.artifacts import ToolArtifactStore
        with tempfile.TemporaryDirectory() as temporary:
            store=ToolArtifactStore(SQLiteSessionStorage(Path(temporary)/'sessions.db','one'))
            reference=await store.put(ToolCall(id='source',name='fixture',arguments={}), 'A'*10000)
            artifacts=ArtifactTools(store);tool=artifacts.read_tool()
            result=await tool.execute('read',{'artifact_id':reference.id,'limit':4096})
            self.assertEqual(result.details['next_offset'],4096)
            self.assertLess(len(result.text.encode('utf-8')),4500)
            for bad in [{'limit':4097},{'offset':True},{'limit':1},{'offset':-1},{'unexpected':1}]:
                with self.assertRaises(ValueError): await tool.execute('invalid',{'artifact_id':reference.id,**bad})
            returned,error=await artifacts.externalize(ToolCall(id='read',name='read_artifact',arguments={}),result,False)
            self.assertIs(returned,result);self.assertFalse(error)


if __name__=='__main__':
    unittest.main()
