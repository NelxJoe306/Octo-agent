from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from octo_coding.memory_runtime import MemorySettings

from octo_agent.context_budget import (
    ContextBudgetExceeded, ContextBudgetPolicy, estimate_budget_text, evaluate_context_budget,
)
from octo_agent.events import ContextBudgetEvent, MessageEndEvent
from octo_agent.harness import AgentHarness, AgentHarnessConfig
from octo_agent.messages import (
    AssistantMessage, ImageContent, ThinkingContent, ToolCall, ToolResultMessage,
    Usage, UserMessage,
)
from octo_agent.provider_events import AssistantDoneEvent
from octo_agent.session import MessageEntry
from octo_agent.tools import AgentTool, AgentToolResult
from octo_ai.anthropic import AnthropicProvider
from octo_ai.env import AnthropicConfig, OpenAICompatibleConfig
from octo_ai.fake import FakeProvider
from octo_ai.google import GoogleGenerativeAIProvider
from octo_ai.mistral import MistralConversationsProvider
from octo_ai.model_limits import RequestTokenBudget, RuntimeModelLimits
from octo_ai.openai_compatible import OpenAICompatibleProvider
from octo_coding.paths import OctoPaths
from octo_coding.resources import OctoResourcePaths
from octo_coding.session import CodingSession, CodingSessionConfig
from octo_coding.session_manager import SessionManager


def response(text="done", calls=()):
    message = AssistantMessage(content=text, model="offline", stop_reason="stop")
    if calls:
        message.content.extend(calls)
        message.stop_reason = "toolUse"
    return [AssistantDoneEvent(reason=message.stop_reason, message=message)]


async def large_tool(call_id, arguments, signal=None, on_update=None):
    return AgentToolResult(content="evidence " * 2000)


def fixture_tool():
    return AgentTool(name="read", label="Read", description="Read file",
                     parameters={"type": "object", "properties": {}}, execute_fn=large_tool)


def gate(policy):
    async def check(*, model, system, messages, tools):
        return evaluate_context_budget(policy, system=system, messages=messages, tools=tools)
    return check


class BudgetTests(unittest.IsolatedAsyncioTestCase):
    def test_configuration_rejects_invalid_capacity(self):
        for values in (
            {"context_window": 0}, {"output_reserve": 0}, {"safety_margin": -1},
            {"soft_ratio": 0}, {"soft_ratio": float("nan")}, {"image_reserve": 0},
        ):
            with self.subTest(values=values), self.assertRaises(ValueError):
                ContextBudgetPolicy(**({"context_window": 10000, "output_reserve": 100} | values))

    async def test_exact_hard_boundary_allowed_and_one_over_blocked(self):
        prompt = UserMessage(content="continue")
        estimate = evaluate_context_budget(ContextBudgetPolicy(10000, 100), system="system",
                                           messages=[prompt], tools=[]).estimated_input
        for window, allowed in ((estimate + 100, True), (estimate + 99, False)):
            with self.subTest(window=window):
                provider = FakeProvider([response()])
                harness = AgentHarness(AgentHarnessConfig(
                    provider=provider, model="offline", system="system",
                    check_context=gate(ContextBudgetPolicy(window, 100, safety_margin=0)),
                ))
                events = []
                if allowed:
                    events = [event async for event in harness.prompt_message(prompt)]
                else:
                    with self.assertRaises(ContextBudgetExceeded) as failure:
                        async for event in harness.prompt_message(prompt):
                            events.append(event)
                    self.assertEqual(failure.exception.decision.remaining_input, -1)
                    self.assertEqual(harness.messages, (prompt,))
                self.assertEqual(len(provider.calls), int(allowed))
                decision = next(e.decision for e in events if isinstance(e, ContextBudgetEvent))
                self.assertEqual(decision.remaining_input, 0 if allowed else -1)
                self.assertEqual(decision.status, "NEEDS_COMPACTION" if allowed else "BLOCK")
                self.assertFalse(harness.is_running)

    async def test_soft_notice_allows_provider_without_summarization(self):
        provider = FakeProvider([response()])
        harness = AgentHarness(AgentHarnessConfig(
            provider=provider, model="offline", system="",
            check_context=gate(ContextBudgetPolicy(200, 20, safety_margin=0, soft_ratio=0.5)),
        ))
        events = [e async for e in harness.prompt("a" * 300)]
        self.assertEqual([e.decision.status for e in events if isinstance(e, ContextBudgetEvent)],
                         ["NEEDS_COMPACTION"])
        self.assertEqual(len(provider.calls), 1)

    def test_complete_input_accounting_ignores_old_provider_usage(self):
        policy = ContextBudgetPolicy(100000, 100)
        old = AssistantMessage(content=[ThinkingContent(thinking="reason", thinking_signature="sig")],
                               usage=Usage(input=999999, total_tokens=999999))
        base = evaluate_context_budget(policy, system="", messages=[], tools=[])
        full = evaluate_context_budget(policy, system="system", messages=[
            old, UserMessage(content="<system-reminder>当前分支</system-reminder>"),
            AssistantMessage(content=[ToolCall(id="c", name="read", arguments={"path": "a.txt"})]),
            ToolResultMessage(tool_call_id="c", tool_name="read", content="result"),
        ], tools=[fixture_tool()])
        self.assertGreater(full.system_tokens, base.system_tokens)
        self.assertGreater(full.message_tokens, 0)
        self.assertGreater(full.tool_tokens, 0)
        self.assertLess(full.estimated_input, 999999)
        self.assertEqual(full.estimated_input,
                         full.system_tokens + full.message_tokens + full.tool_tokens + full.image_tokens)
        self.assertGreater(estimate_budget_text("中文内容"), estimate_budget_text("abcd"))

    def test_image_content_has_explicit_reserve(self):
        decision = evaluate_context_budget(ContextBudgetPolicy(100000, 100, image_reserve=5000),
            system="", messages=[UserMessage(content=[ImageContent(data="base64", mime_type="image/png")])], tools=[])
        self.assertEqual(decision.image_tokens, 5000)

    async def test_projection_is_counted_after_preparation_without_altering_raw_history(self):
        raw = UserMessage(content="small")
        provider = FakeProvider([response()])
        async def prepare(messages, *, signal):
            return (*messages, UserMessage(content="<system-reminder>" + "topic " * 400 + "</system-reminder>"))
        harness = AgentHarness(AgentHarnessConfig(
            provider=provider, model="offline", system="", prepare_context=prepare,
            check_context=gate(ContextBudgetPolicy(500, 100, safety_margin=0)),
        ), messages=[raw])
        with self.assertRaises(ContextBudgetExceeded):
            _ = [e async for e in harness.continue_()]
        self.assertEqual(provider.calls, [])
        self.assertEqual(harness.messages, (raw,))

    async def test_large_tool_result_blocks_next_call_and_can_resume(self):
        provider = FakeProvider([
            response("inspect", [ToolCall(id="c", name="read", arguments={})]), response("recovered"),
        ])
        harness = AgentHarness(AgentHarnessConfig(
            provider=provider, model="offline", system="", tools=[fixture_tool()],
            check_context=gate(ContextBudgetPolicy(1000, 100, safety_margin=0)),
        ))
        with self.assertRaises(ContextBudgetExceeded):
            _ = [e async for e in harness.prompt("inspect")]
        self.assertEqual(len(provider.calls), 1)
        raw = [m.model_dump() for m in harness.messages]
        self.assertEqual(harness.messages[-1].text, "evidence " * 2000)
        harness.config.check_context = gate(ContextBudgetPolicy(100000, 100))
        _ = [e async for e in harness.continue_()]
        self.assertEqual(len(provider.calls), 2)
        self.assertEqual([m.model_dump() for m in harness.messages[:-1]], raw)
        self.assertEqual(harness.messages[-1].text, "recovered")

    async def test_queued_follow_up_is_rechecked(self):
        provider = FakeProvider([response("first"), response("must not send")])
        harness = AgentHarness(AgentHarnessConfig(
            provider=provider, model="offline", system="",
            check_context=gate(ContextBudgetPolicy(500, 100, safety_margin=0)),
        ))
        with self.assertRaises(ContextBudgetExceeded):
            async for event in harness.prompt("first"):
                if isinstance(event, MessageEndEvent) and isinstance(event.message, AssistantMessage):
                    harness.follow_up("x" * 2000)
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(harness.messages[-1].text, "x" * 2000)

    async def test_steering_is_rechecked_after_tool_batch(self):
        async def tiny_tool(call_id, arguments, signal=None, on_update=None):
            return AgentToolResult(content="ok")
        provider = FakeProvider([response("read", [ToolCall(id="c", name="read", arguments={})]), response()])
        tool = AgentTool(name="read", label="Read", description="", parameters={}, execute_fn=tiny_tool)
        harness = AgentHarness(AgentHarnessConfig(provider=provider, model="offline", system="", tools=[tool],
            check_context=gate(ContextBudgetPolicy(1000, 100, safety_margin=0))))
        with self.assertRaises(ContextBudgetExceeded):
            async for event in harness.prompt("first"):
                if isinstance(event, MessageEndEvent) and isinstance(event.message, ToolResultMessage):
                    harness.steer("x" * 4000)
        self.assertEqual(len(provider.calls), 1)

    async def test_cancellation_after_budget_event_never_calls_provider(self):
        provider = FakeProvider([response()])
        harness = AgentHarness(AgentHarnessConfig(provider=provider, model="offline", system="",
            check_context=gate(ContextBudgetPolicy(100000, 100))))
        with self.assertRaises(asyncio.CancelledError):
            async for event in harness.prompt("first"):
                if isinstance(event, ContextBudgetEvent):
                    harness.cancel()
        self.assertEqual(provider.calls, [])


class ProviderBudgetTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_wire_payload_matches_reserved_output_for_all_native_cap_parameters(self):
        cases = [
            (OpenAICompatibleProvider, OpenAICompatibleConfig(api_key="fake", infer_api_from_model=False), "max_completion_tokens", 16384),
            (OpenAICompatibleProvider, OpenAICompatibleConfig(api_key="fake", max_tokens=1234,
                 compat={"maxTokensField": "max_tokens"}, infer_api_from_model=False), "max_tokens", 1234),
            (OpenAICompatibleProvider, OpenAICompatibleConfig(api_key="fake", api="openai-responses", max_tokens=2345,
                 infer_api_from_model=False), "max_output_tokens", 2345),
            (GoogleGenerativeAIProvider, OpenAICompatibleConfig(api_key="fake", base_url="https://fixture.invalid",
                 api="google-generative-ai"), "maxOutputTokens", 16384),
            (MistralConversationsProvider, OpenAICompatibleConfig(api_key="fake", base_url="https://fixture.invalid",
                 api="mistral-conversations"), "max_tokens", 16384),
            (AnthropicProvider, AnthropicConfig(api_key="fake", max_tokens=1000, thinking_budget_tokens=5000,
                 oauth_system_prompt="oauth system"), "max_tokens", 6024),
            (AnthropicProvider, AnthropicConfig(api_key="fake"), "max_tokens", 4096),
        ]
        for provider_type, config, field, expected in cases:
            with self.subTest(provider=provider_type.__name__, field=field, expected=expected):
                payloads = []
                def receive(request):
                    payloads.append(json.loads(request.content))
                    return httpx.Response(400, json={"error": {"message": "offline fixture"}})
                async with httpx.AsyncClient(transport=httpx.MockTransport(receive)) as client:
                    provider = provider_type(config, client=client)
                    budget = provider.request_token_budget()
                    _ = [e async for e in provider.stream_response(model="offline", system="system",
                         messages=[UserMessage(content="hello")], tools=[])]
                self.assertEqual(len(payloads), 1)
                payload = payloads[0].get("generationConfig", payloads[0])
                self.assertEqual(payload[field], expected)
                self.assertEqual(budget.max_output_tokens, expected)
                if isinstance(config, AnthropicConfig):
                    self.assertEqual(budget.additional_system, config.oauth_system_prompt or "")


class SessionBudgetTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.env = patch.dict(os.environ, {"OCTO_HOME": str(self.base / "home"), "OCTO_OFFLINE": "1"})
        self.env.start()
        self.addCleanup(self.env.stop)

    async def make_session(self, **options):
        paths = OctoPaths(home=self.base / "home", agents_home=self.base / "agents")
        manager = SessionManager(paths)
        record = manager.create_session(cwd=self.base, model="offline", title="Budget fixture")
        provider = FakeProvider([response("answer")])
        config = CodingSessionConfig(memory_settings=MemorySettings(enabled=False), provider=provider, model="offline", storage=manager.storage_for(record),
            session_id=record.id, session_manager=manager, cwd=self.base, system="system", tools=[],
            resource_paths=OctoResourcePaths(root=paths.home, agents_root=paths.agents_home, cwd=self.base, paths=paths),
            auto_compact_enabled=False, extensions_enabled=False, skills_enabled=False, trust_override="approve", **options)
        session = await CodingSession.load(config)
        self.addAsyncCleanup(session.aclose)
        return session, provider, config

    async def test_soft_notice_and_live_effective_window_with_auto_compaction_disabled(self):
        session, provider, _ = await self.make_session(context_soft_ratio=0.001)
        session._runtime_model_limits = RuntimeModelLimits(context_window=40000, effective_context_window_percent=80)
        session._runtime_model_limits_key = (session.provider_name, session.model)
        events = [e async for e in session.prompt("question")]
        decisions = [e.decision for e in events if isinstance(e, ContextBudgetEvent)]
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(decisions[0].status, "NEEDS_COMPACTION")
        self.assertEqual(decisions[0].context_window, 32000)
        self.assertEqual(decisions[0].hard_input_limit, 32000 - 16384 - 1024)
        self.assertEqual(decisions[0], session.last_context_budget)
        status = session.handle_command("/session").message
        self.assertIn("Last main request: Context budget NEEDS_COMPACTION", status)
        self.assertIn("output=16384", status)
        self.assertIn("Last request estimate: system=", status)
        serialized = next(e for e in events if isinstance(e, ContextBudgetEvent)).model_dump(mode="json")
        self.assertEqual(serialized["decision"]["hardInputLimit"], decisions[0].hard_input_limit)
        self.assertNotIn("system-reminder", " ".join(m.text for m in session.messages))

    async def test_block_preserves_sqlite_raw_messages_and_branch_ownership_and_can_resume(self):
        session, provider, config = await self.make_session()
        _ = [e async for e in session.prompt("previous question")]
        branch = await session.edit_topic("create", session_id=session.session_id, title="Budget branch")
        await session.edit_topic("select", session_id=session.session_id, topic_id=branch)
        previous = [e.model_dump() for e in await session.session_entries()]
        provider.calls.clear()
        huge = "question " * 60000
        with self.assertRaises(ContextBudgetExceeded):
            _ = [e async for e in session.prompt(huge)]
        entries = await session.session_entries()
        self.assertEqual([e.model_dump() for e in entries[:len(previous)]], previous)
        self.assertEqual(provider.calls, [])
        self.assertEqual(session.last_context_budget.status, "BLOCK")
        self.assertIn("Context budget BLOCK", session.handle_command("/session").message)
        rows = (await session.topic_snapshot(branch))["messages"]
        self.assertEqual([m["text"] for m in rows], [huge])
        self.assertFalse(any(isinstance(e, MessageEntry) and e.message.role == "assistant"
                             for e in entries[len(previous):]))
        restored = await CodingSession.load(config)
        self.addAsyncCleanup(restored.aclose)
        self.assertEqual(restored.current_topic_id, branch)
        self.assertEqual(restored.messages[-1].text, huge)
        restored._runtime_model_limits_key = (restored.provider_name, restored.model)
        restored._runtime_model_limits = RuntimeModelLimits(context_window=1000000)
        _ = [e async for e in restored.continue_()]
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(restored.last_context_budget.status, "ALLOW")

    async def test_topic_reminder_contributes_to_gate(self):
        session, provider, _ = await self.make_session()
        branch = await session.edit_topic("create", session_id=session.session_id, title="A" * 120)
        await session.edit_topic("select", session_id=session.session_id, topic_id=branch)
        session._runtime_model_limits_key = (session.provider_name, session.model)
        session._runtime_model_limits = RuntimeModelLimits(context_window=16384 + 1024 + 100)
        with self.assertRaises(ContextBudgetExceeded):
            _ = [e async for e in session.prompt("small")]
        self.assertEqual(provider.calls, [])
        self.assertEqual(session.last_context_budget.status, "BLOCK")
        self.assertEqual([m.text for m in session.messages], ["small"])

    async def test_replacement_session_rebinds_guard_to_surviving_instance(self):
        session, provider, _ = await self.make_session(context_soft_ratio=0.001)
        await session.new_session()
        await session.set_session_name("Replacement budget fixture")
        _ = [e async for e in session.prompt("after replacement")]
        self.assertEqual(len(provider.calls), 1)
        self.assertIsNotNone(session.last_context_budget)
        self.assertEqual(session.last_context_budget.status, "NEEDS_COMPACTION")

    async def test_explicit_reserve_for_custom_provider(self):
        session, _, _ = await self.make_session(context_output_reserve_tokens=5000)
        class UnknownProvider:
            pass
        session._harness.config.provider = UnknownProvider()
        decision = await session._check_context_budget(model="offline", system="", messages=[], tools=[])
        self.assertEqual(decision.output_reserve, 5000)
        self.assertEqual(decision.output_source, "configured_reserve")

    async def test_unknown_provider_requires_explicit_reserve_or_model_maximum(self):
        session, _, _ = await self.make_session()
        class UnknownProvider:
            pass
        session._harness.config.provider = UnknownProvider()
        with self.assertRaisesRegex(ValueError, "Cannot resolve output reserve"):
            await session._check_context_budget(model="offline", system="", messages=[], tools=[])
        session._runtime_model_limits_key = (session.provider_name, session.model)
        session._runtime_model_limits = RuntimeModelLimits(context_window=100000, max_output_tokens=32768)
        decision = await session._check_context_budget(model="offline", system="", messages=[], tools=[])
        self.assertEqual(decision.output_reserve, 32768)
        self.assertEqual(decision.output_source, "model_maximum")


if __name__ == "__main__":
    unittest.main()
