"""Tests for FakeLLMClient protocol conformance."""

from __future__ import annotations

from recall_assistant.llm import LLMClient, LLMResult
from tests.fakes import FakeLLMClient


class TestFakeLLMClientProtocol:
    def test_satisfies_protocol(self):
        """FakeLLMClient is assignable to LLMClient."""
        client: LLMClient = FakeLLMClient(text="hello")
        result = client.generate(
            messages=[{"role": "user", "content": "test"}],
            model="test-model",
            max_tokens=100,
        )
        assert isinstance(result, LLMResult)
        assert result.text == "hello"
        assert result.finish_reason == "stop"

    def test_records_calls(self):
        client = FakeLLMClient(text="reply")
        messages = [{"role": "user", "content": "q"}]
        client.generate(messages, model="m", max_tokens=10)
        assert len(client.calls) == 1
        recorded_messages, recorded_model, recorded_max_tokens = client.calls[0]
        assert recorded_messages == messages
        assert recorded_model == "m"
        assert recorded_max_tokens == 10

    def test_result_fields(self):
        client = FakeLLMClient(
            text="answer",
            prompt_tokens=200,
            completion_tokens=80,
            cost=0.005,
        )
        result = client.generate([], model="m", max_tokens=10)
        assert result.prompt_tokens == 200
        assert result.completion_tokens == 80
        assert result.cost == 0.005
        assert result.latency_s > 0
        assert result.retries == 0
