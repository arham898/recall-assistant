"""Unit tests for OpenRouterClient.stream() and _iter_chunks.

Tests _iter_chunks with fake chunk objects matching the observed shape
from OpenRouter / Google AI Studio (Gemini 2.5 Flash Lite).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from recall_assistant.llm import StreamEvent
from recall_assistant.llm.openrouter import CreditLimitError, OpenRouterClient

# ---------------------------------------------------------------------------
# Helpers: fake chunk objects matching the real SDK shape
# ---------------------------------------------------------------------------


def _delta(content: str | None = None):
    return SimpleNamespace(content=content)


def _choice(delta_content: str | None = None, finish_reason: str | None = None):
    return SimpleNamespace(delta=_delta(delta_content), finish_reason=finish_reason)


def _usage(prompt_tokens: int, completion_tokens: int, cost: float | None = None):
    ns = SimpleNamespace(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
    )
    if cost is not None:
        ns.cost = cost
    return ns


def _chunk(
    *,
    content: str | None = None,
    finish_reason: str | None = None,
    usage: object | None = None,
):
    """Build a fake ChatCompletionChunk."""
    choices = (
        [_choice(content, finish_reason)]
        if content is not None or finish_reason is not None
        else []
    )
    return SimpleNamespace(choices=choices, usage=usage)


# ---------------------------------------------------------------------------
# _iter_chunks tests
# ---------------------------------------------------------------------------


class TestIterChunks:
    """Test _iter_chunks with the four-chunk shape from the probe."""

    PROBE_CHUNKS: list = [  # noqa: RUF012
        # chunk 0: token "Hello, "
        _chunk(content="Hello, "),
        # chunk 1: token "nice to meet you!"
        _chunk(content="nice to meet you!"),
        # chunk 2: empty content, finish_reason='stop', no usage
        _chunk(content="", finish_reason="stop"),
        # chunk 3: empty content, finish_reason='stop', usage set
        _chunk(
            content="",
            finish_reason="stop",
            usage=_usage(7, 10, cost=4.7e-06),
        ),
    ]

    def test_four_chunk_stream(self):
        events = list(OpenRouterClient._iter_chunks(self.PROBE_CHUNKS))

        assert len(events) == 3  # two tokens + one final

        # Token events
        assert events[0] == StreamEvent(token="Hello, ")
        assert events[1] == StreamEvent(token="nice to meet you!")

        # Final event
        final = events[2]
        assert final.token == ""
        assert final.finish_reason == "stop"
        assert final.usage is not None
        assert final.usage.prompt_tokens == 7
        assert final.usage.completion_tokens == 10
        assert final.usage.cost == pytest.approx(4.7e-06)

    def test_no_usage_chunk(self):
        """Stream ends with finish_reason but no usage chunk."""
        chunks = [
            _chunk(content="Hello"),
            _chunk(content="", finish_reason="stop"),
            # no usage chunk
        ]
        events = list(OpenRouterClient._iter_chunks(chunks))

        assert len(events) == 2  # one token + one final
        assert events[0] == StreamEvent(token="Hello")

        final = events[1]
        assert final.token == ""
        assert final.finish_reason == "stop"
        assert final.usage is None

    def test_no_cost_attribute(self):
        """Usage chunk without a cost attribute (non-OpenRouter provider)."""
        chunks = [
            _chunk(content="Hi"),
            _chunk(
                content="",
                finish_reason="stop",
                usage=SimpleNamespace(prompt_tokens=5, completion_tokens=3),
            ),
        ]
        events = list(OpenRouterClient._iter_chunks(chunks))
        final = events[-1]
        assert final.usage is not None
        assert final.usage.cost is None


# ---------------------------------------------------------------------------
# stream() raises at call time, not during iteration
# ---------------------------------------------------------------------------


class TestStreamRaisesEagerly:
    def test_credit_limit_raises_at_call(self):
        """CreditLimitError propagates from stream(), not from next()."""
        from unittest.mock import MagicMock

        import openai

        # Build a mock openai client whose create() raises 402
        mock_openai_client = MagicMock()
        resp = MagicMock()
        resp.status_code = 402
        resp.headers = {}
        mock_openai_client.chat.completions.create.side_effect = openai.APIStatusError(
            message="credit limit",
            response=resp,
            body=None,
        )

        client = OpenRouterClient.__new__(OpenRouterClient)
        client._client = mock_openai_client
        client._temperature = 0.0
        client._max_retries = 0
        client._max_retry_wait = 30.0
        client._sleep = lambda _: None

        with pytest.raises(CreditLimitError):
            client.stream(
                [{"role": "user", "content": "hi"}],
                model="test",
                max_tokens=10,
            )
