"""Tests for POST /ask with stream=true (SSE)."""

from __future__ import annotations

import json
from collections.abc import Iterator

import httpx
import pytest

from recall_assistant.llm import StreamEvent
from recall_assistant.rag import DECLINE_MARKER
from tests.api.conftest import TEST_API_KEY, create_test_app

# ---------------------------------------------------------------------------
# SSE parser
# ---------------------------------------------------------------------------


def _parse_sse(text: str) -> list[tuple[str, dict]]:
    """Parse SSE text into ``(event_type, data_dict)`` pairs."""
    events: list[tuple[str, dict]] = []
    current_event: str | None = None
    current_data: str | None = None

    for line in text.split("\n"):
        if line.startswith("event: "):
            current_event = line[len("event: ") :]
        elif line.startswith("data: "):
            current_data = line[len("data: ") :]
        elif line == "" and current_event is not None and current_data is not None:
            events.append((current_event, json.loads(current_data)))
            current_event = None
            current_data = None

    return events


# ---------------------------------------------------------------------------
# Basic streaming tests
# ---------------------------------------------------------------------------


class TestAskStream:
    async def test_content_type(self, client, auth_headers):
        resp = await client.post(
            "/ask",
            json={"question": "test?", "stream": True},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]

    async def test_event_order(self, client, auth_headers):
        resp = await client.post(
            "/ask",
            json={"question": "test?", "stream": True},
            headers=auth_headers,
        )
        events = _parse_sse(resp.text)
        assert len(events) >= 3
        assert events[0][0] == "sources"
        assert events[-1][0] == "done"
        for ev_type, _ in events[1:-1]:
            assert ev_type == "token"

    async def test_sources_event(self, client, auth_headers):
        resp = await client.post(
            "/ask",
            json={"question": "test?", "stream": True},
            headers=auth_headers,
        )
        events = _parse_sse(resp.text)
        assert len(events[0][1]["sources"]) == 2

    async def test_done_event_fields(self, client, auth_headers):
        resp = await client.post(
            "/ask",
            json={"question": "test?", "stream": True},
            headers=auth_headers,
        )
        events = _parse_sse(resp.text)
        done = events[-1][1]
        assert done["status"] == "answered"
        assert "25V001" in done["cited_ids"]
        assert "prompt_tokens" in done
        assert "completion_tokens" in done
        assert "cost" in done
        assert done["cost"] == 0.001  # FakeLLMClient default
        assert "latency_ms" in done

    async def test_tokens_reconstruct_answer(self, client, auth_headers):
        resp = await client.post(
            "/ask",
            json={"question": "test?", "stream": True},
            headers=auth_headers,
        )
        events = _parse_sse(resp.text)
        tokens = [e[1]["token"] for e in events if e[0] == "token"]
        reconstructed = "".join(tokens)
        done_answer = events[-1][1]["answer"]
        assert reconstructed == done_answer

    async def test_stream_requires_auth(self, client):
        resp = await client.post("/ask", json={"question": "test?", "stream": True})
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# CreditLimitError → plain 503
# ---------------------------------------------------------------------------


class TestStreamCreditLimit:
    async def test_credit_limit_returns_503(self):
        from recall_assistant.llm.openrouter import CreditLimitError

        app = create_test_app()

        class ExplodingClient:
            def generate(self, *a, **kw):
                raise CreditLimitError("out")

            def stream(self, *a, **kw):
                raise CreditLimitError("out")

        app.state.llm_client = ExplodingClient()

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.post(
                "/ask",
                json={"question": "test?", "stream": True},
                headers={"X-API-Key": TEST_API_KEY},
            )
        assert resp.status_code == 503


# ---------------------------------------------------------------------------
# Broken stream → done with status="invalid"
# ---------------------------------------------------------------------------


class TestBrokenStream:
    async def test_broken_stream_sends_done_invalid(self):
        app = create_test_app()

        class BrokenStreamClient:
            def generate(self, *a, **kw):
                pass

            def stream(self, messages, model, max_tokens):
                return self._iter()

            def _iter(self) -> Iterator[StreamEvent]:
                yield StreamEvent(token="Hello ")
                yield StreamEvent(token="world")
                raise RuntimeError("connection reset")

        app.state.llm_client = BrokenStreamClient()

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.post(
                "/ask",
                json={"question": "test?", "stream": True},
                headers={"X-API-Key": TEST_API_KEY},
            )

        assert resp.status_code == 200
        events = _parse_sse(resp.text)

        # Should have: sources, 2 tokens, done
        done_events = [e for e in events if e[0] == "done"]
        assert len(done_events) == 1

        done = done_events[0][1]
        assert done["status"] == "invalid"
        assert done["error"] == "LLM stream interrupted"
        assert done["answer"] == "Hello world"
        # No upstream body leaked
        assert "connection reset" not in json.dumps(done)


# ---------------------------------------------------------------------------
# Parametrised streaming / non-streaming parity
# ---------------------------------------------------------------------------

PARITY_CASES = [
    pytest.param("Brake issue [25V001].", "stop", id="answered"),
    pytest.param(DECLINE_MARKER, "stop", id="declined"),
    pytest.param("", "stop", id="empty"),
    pytest.param("Brake issue [25V001].", "length", id="finish_reason_length"),
    pytest.param("reply [99V999].", "stop", id="invalid_id"),
]


@pytest.mark.parametrize("llm_text, finish_reason", PARITY_CASES)
async def test_streaming_parity(llm_text, finish_reason):
    """The streaming done event has the same status and cited_ids as JSON."""
    app = create_test_app(llm_text=llm_text, llm_finish_reason=finish_reason)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        headers = {"X-API-Key": TEST_API_KEY}

        # Non-streaming
        json_resp = await c.post("/ask", json={"question": "test?"}, headers=headers)
        json_body = json_resp.json()

        # Streaming
        stream_resp = await c.post(
            "/ask",
            json={"question": "test?", "stream": True},
            headers=headers,
        )
        events = _parse_sse(stream_resp.text)
        done = events[-1][1]

    assert done["status"] == json_body["status"]
    assert done["cited_ids"] == json_body["cited_ids"]
