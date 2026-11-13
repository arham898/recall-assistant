"""Tests that structured log lines actually reach a handler.

Uses a real ``logging.Handler`` attached to the ``recall_assistant.api``
logger (not ``caplog``) to prove log output is emitted under uvicorn.
"""

from __future__ import annotations

import json
import logging
from io import StringIO

import httpx

from tests.api.conftest import TEST_API_KEY, create_test_app


def _parse_sse(text: str) -> list[tuple[str, dict]]:
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


class TestStructuredLogOutput:
    """Attach a real StreamHandler to a StringIO and assert the log line."""

    async def test_nonstreaming_log_line(self):
        app = create_test_app()
        buf = StringIO()
        handler = logging.StreamHandler(buf)
        handler.setLevel(logging.DEBUG)
        logger = logging.getLogger("recall_assistant.api")
        logger.addHandler(handler)

        try:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test"
            ) as c:
                await c.post(
                    "/ask",
                    json={"question": "test?"},
                    headers={"X-API-Key": TEST_API_KEY},
                )
        finally:
            logger.removeHandler(handler)

        output = buf.getvalue()
        # The _log_completion line from routes_ask.py
        assert "request_id=" in output
        assert "status=answered" in output
        assert "prompt_tokens=" in output
        assert "completion_tokens=" in output
        assert "latency_ms=" in output

    async def test_streaming_log_line(self):
        app = create_test_app()
        buf = StringIO()
        handler = logging.StreamHandler(buf)
        handler.setLevel(logging.DEBUG)
        logger = logging.getLogger("recall_assistant.api")
        logger.addHandler(handler)

        try:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test"
            ) as c:
                await c.post(
                    "/ask",
                    json={"question": "test?", "stream": True},
                    headers={"X-API-Key": TEST_API_KEY},
                )
        finally:
            logger.removeHandler(handler)

        output = buf.getvalue()
        assert "request_id=" in output
        assert "status=answered" in output
        assert "prompt_tokens=" in output
        assert "completion_tokens=" in output
        assert "latency_ms=" in output
