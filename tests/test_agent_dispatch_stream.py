from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import sdk.agent_dispatch as agent_dispatch


class FakeAssistantMessage:
    def __init__(self, text: str) -> None:
        self.content = [FakeTextBlock(text)]


class FakeTextBlock:
    def __init__(self, text: str) -> None:
        self.text = text


class FakeResultMessage:
    def __init__(self, result: str) -> None:
        self.result = result
        self.is_error = False
        self.usage = {"input_tokens": 10, "output_tokens": 20}


async def fake_receive_messages():
    yield FakeAssistantMessage("working")
    yield FakeResultMessage("done")
    await asyncio.sleep(3600)


class FakeClient:
    def __init__(self) -> None:
        self.disconnected = False

    async def connect(self, prompt: str) -> None:
        self.prompt = prompt

    async def receive_messages(self):
        async for item in fake_receive_messages():
            yield item

    def disconnect(self) -> None:
        self.disconnected = True


class FakeAsyncClient(FakeClient):
    async def disconnect(self) -> None:
        self.disconnected = True


class StubBus:
    def __init__(self) -> None:
        self.events = []

    async def emit(self, event) -> None:
        self.events.append(event)


@unittest.skipUnless(agent_dispatch.HAS_SDK, "claude-agent-sdk is only available inside the project venv")
class AgentDispatchStreamTests(unittest.TestCase):
    def test_run_client_stops_after_result_message(self) -> None:
        dispatcher = agent_dispatch.AgentDispatcher.__new__(agent_dispatch.AgentDispatcher)
        dispatcher.bus = StubBus()

        client = FakeClient()
        with patch.object(agent_dispatch, "AssistantMessage", FakeAssistantMessage), \
             patch.object(agent_dispatch, "TextBlock", FakeTextBlock), \
             patch.object(agent_dispatch, "ResultMessage", FakeResultMessage):
            result = asyncio.run(dispatcher._run_client(client, "implementer", "prompt", "claude-sonnet-4-6"))

        self.assertEqual(result, "done")
        self.assertTrue(client.disconnected)
        token_events = [event for event in dispatcher.bus.events if getattr(event, "type", "") == "agent.tokens"]
        self.assertEqual(len(token_events), 1)

    def test_run_client_awaits_async_disconnect(self) -> None:
        dispatcher = agent_dispatch.AgentDispatcher.__new__(agent_dispatch.AgentDispatcher)
        dispatcher.bus = StubBus()

        client = FakeAsyncClient()
        with patch.object(agent_dispatch, "AssistantMessage", FakeAssistantMessage), \
             patch.object(agent_dispatch, "TextBlock", FakeTextBlock), \
             patch.object(agent_dispatch, "ResultMessage", FakeResultMessage):
            result = asyncio.run(dispatcher._run_client(client, "implementer", "prompt", "claude-sonnet-4-6"))

        self.assertEqual(result, "done")
        self.assertTrue(client.disconnected)


if __name__ == "__main__":
    unittest.main()
