from __future__ import annotations

import asyncio
import unittest

from agent_core.harness import AgentHarness, AgentHarnessConfig
from agent_core.messages import AssistantMessage, TextContent, ToolCall, UserMessage
from agent_core.provider_events import AssistantDoneEvent
from agent_core.tools import AgentTool, AgentToolResult
from model_adapters.fake import FakeProvider


def response(text, calls=()):
    message = AssistantMessage(
        content=[TextContent(text=text), *calls], model="offline",
        stop_reason="toolUse" if calls else "stop",
    )
    return [AssistantDoneEvent(reason=message.stop_reason, message=message)]


async def read_fixture(call_id, arguments, signal=None, on_update=None):
    return AgentToolResult(content="fixture evidence")


def read_tool():
    return AgentTool(
        name="read", label="Read", description="Read fixture",
        parameters={"type": "object", "properties": {}}, execute_fn=read_fixture,
    )


class ContextHarnessTests(unittest.IsolatedAsyncioTestCase):
    async def test_each_model_call_uses_projection_without_rewriting_history(self):
        provider = FakeProvider([
            response("inspect", [ToolCall(id="call-1", name="read", arguments={})]),
            response("finished"),
        ])
        original = UserMessage(content="old verbose history")

        async def prepare(messages, *, signal):
            messages[0].content = UserMessage(content="compact history").content
            return messages

        harness = AgentHarness(
            AgentHarnessConfig(
                provider=provider, model="offline", system="stable system",
                tools=[read_tool()], prepare_context=prepare,
            ),
            messages=[original],
        )
        _ = [event async for event in harness.prompt("continue")]

        self.assertEqual(len(provider.calls), 2)
        for _, system, messages, tools in provider.calls:
            self.assertEqual(messages[0].text, "compact history")
            self.assertEqual(system, "stable system")
            self.assertEqual([tool.name for tool in tools], ["read"])
        self.assertEqual(provider.calls[1][2][-1].text, "fixture evidence")
        self.assertEqual(original.text, "old verbose history")
        self.assertEqual(harness.messages[0].text, "old verbose history")
        self.assertEqual(harness.messages[-1].text, "finished")

    async def test_cancellation_during_preparation_never_calls_provider(self):
        provider = FakeProvider([response("must not generate")])

        async def prepare(messages, *, signal):
            harness.cancel()
            return messages

        harness = AgentHarness(AgentHarnessConfig(
            provider=provider, model="offline", system="stable system",
            prepare_context=prepare,
        ))
        with self.assertRaises(asyncio.CancelledError):
            _ = [event async for event in harness.prompt("continue")]
        self.assertEqual(provider.calls, [])
        self.assertFalse(harness.is_running)

    async def test_broken_projection_is_rejected_before_next_model_call(self):
        provider = FakeProvider([
            response("inspect", [ToolCall(id="call-1", name="read", arguments={})]),
            response("must not generate"),
        ])

        async def prepare(messages, *, signal):
            return messages if len(messages) < 3 else (messages[0], messages[-1])

        harness = AgentHarness(AgentHarnessConfig(
            provider=provider, model="offline", system="stable system",
            tools=[read_tool()], prepare_context=prepare,
        ))
        with self.assertRaisesRegex(ValueError, "tool history"):
            _ = [event async for event in harness.prompt("inspect")]
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(harness.messages[-1].text, "fixture evidence")
        self.assertFalse(harness.is_running)

    async def test_projection_cannot_change_tool_result_to_another_tool(self):
        provider = FakeProvider([
            response("inspect", [ToolCall(id="call-1", name="read", arguments={})]),
            response("must not generate"),
        ])

        async def prepare(messages, *, signal):
            if len(messages) >= 3:
                messages[-1].tool_name = "different_tool"
            return messages

        harness = AgentHarness(AgentHarnessConfig(
            provider=provider, model="offline", system="stable system",
            tools=[read_tool()], prepare_context=prepare,
        ))
        with self.assertRaisesRegex(ValueError, "tool history"):
            _ = [event async for event in harness.prompt("inspect")]
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(harness.messages[-1].tool_name, "read")

    async def test_preparation_error_propagates_and_harness_can_resume(self):
        provider = FakeProvider([response("recovered")])
        fail = True

        async def prepare(messages, *, signal):
            if fail:
                raise RuntimeError("preparation failed")
            return messages

        harness = AgentHarness(AgentHarnessConfig(
            provider=provider, model="offline", system="stable system",
            prepare_context=prepare,
        ))
        with self.assertRaisesRegex(RuntimeError, "preparation failed"):
            _ = [event async for event in harness.prompt("continue")]
        self.assertFalse(harness.is_running)
        self.assertEqual(provider.calls, [])
        fail = False
        _ = [event async for event in harness.continue_()]
        self.assertEqual(harness.messages[-1].text, "recovered")


if __name__ == "__main__":
    unittest.main()
