"""Tests for OpenRouterClient retry logic using httpx2.MockTransport.

Each test creates a real openai.OpenAI client backed by a mock HTTP
transport, so the tests exercise the actual SDK exception mapping
without making network calls.
"""

from __future__ import annotations

import httpx2
import openai
import pytest

from recall_assistant.llm.openrouter import (
    CreditLimitError,
    MaxRetriesExceeded,
    OpenRouterClient,
    RetryAfterTooLong,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _success_body(
    text: str = "Hello!",
    finish_reason: str = "stop",
    cost: float | None = None,
) -> dict:
    """A valid chat completion response body."""
    usage: dict = {
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "total_tokens": 15,
    }
    if cost is not None:
        usage["cost"] = cost
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 0,
        "model": "test-model",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": finish_reason,
            }
        ],
        "usage": usage,
    }


def _error_body(message: str = "error") -> dict:
    return {"error": {"message": message, "type": "error", "code": None}}


def _make_client(
    handler,
    max_retries: int = 3,
    max_retry_wait: float = 30.0,
) -> tuple[OpenRouterClient, list[float]]:
    """Build an OpenRouterClient with a mock transport.

    Returns (client, sleep_durations) where sleep_durations is populated
    by the injected sleep_fn.
    """
    sleeps: list[float] = []
    client = OpenRouterClient(
        api_key="test-key",
        max_retries=max_retries,
        max_retry_wait=max_retry_wait,
        sleep_fn=sleeps.append,
        http_client=httpx2.Client(transport=httpx2.MockTransport(handler)),
    )
    return client, sleeps


def _generate(client: OpenRouterClient):
    """Call generate with minimal arguments."""
    return client.generate(
        messages=[{"role": "user", "content": "hi"}],
        model="test-model",
        max_tokens=10,
    )


# ---------------------------------------------------------------------------
# 429 tests
# ---------------------------------------------------------------------------


class TestRetryOn429:
    def test_retries_without_retry_after(self):
        requests_made = 0

        def handler(request: httpx2.Request) -> httpx2.Response:
            nonlocal requests_made
            requests_made += 1
            if requests_made == 1:
                return httpx2.Response(
                    429,
                    json=_error_body("rate limited"),
                    request=request,
                )
            return httpx2.Response(200, json=_success_body(), request=request)

        client, sleeps = _make_client(handler)
        result = _generate(client)

        assert requests_made == 2
        assert len(sleeps) == 1
        assert sleeps[0] >= 1.0  # backoff(1) = 1.0
        assert result.text == "Hello!"
        assert result.retries == 1

    def test_retries_with_retry_after(self):
        requests_made = 0

        def handler(request: httpx2.Request) -> httpx2.Response:
            nonlocal requests_made
            requests_made += 1
            if requests_made == 1:
                return httpx2.Response(
                    429,
                    headers={"retry-after": "2"},
                    json=_error_body("rate limited"),
                    request=request,
                )
            return httpx2.Response(200, json=_success_body(), request=request)

        client, sleeps = _make_client(handler)
        result = _generate(client)

        assert requests_made == 2
        assert len(sleeps) == 1
        assert sleeps[0] == 2.0
        assert result.text == "Hello!"

    def test_retry_after_exceeds_max_raises(self):
        requests_made = 0

        def handler(request: httpx2.Request) -> httpx2.Response:
            nonlocal requests_made
            requests_made += 1
            return httpx2.Response(
                429,
                headers={"retry-after": "999"},
                json=_error_body("rate limited"),
                request=request,
            )

        client, sleeps = _make_client(handler, max_retry_wait=30.0)
        with pytest.raises(RetryAfterTooLong) as exc_info:
            _generate(client)

        assert requests_made == 1
        assert len(sleeps) == 0
        assert "999" in str(exc_info.value)

    def test_retry_after_zero(self):
        requests_made = 0

        def handler(request: httpx2.Request) -> httpx2.Response:
            nonlocal requests_made
            requests_made += 1
            if requests_made == 1:
                return httpx2.Response(
                    429,
                    headers={"retry-after": "0"},
                    json=_error_body("rate limited"),
                    request=request,
                )
            return httpx2.Response(200, json=_success_body(), request=request)

        client, sleeps = _make_client(handler)
        result = _generate(client)

        assert requests_made == 2
        assert len(sleeps) == 1
        assert sleeps[0] == 0.0
        assert result.text == "Hello!"

    def test_retry_after_http_date_falls_back_to_backoff(self):
        requests_made = 0

        def handler(request: httpx2.Request) -> httpx2.Response:
            nonlocal requests_made
            requests_made += 1
            if requests_made == 1:
                return httpx2.Response(
                    429,
                    headers={"retry-after": "Thu, 01 Jan 2026 00:00:00 GMT"},
                    json=_error_body("rate limited"),
                    request=request,
                )
            return httpx2.Response(200, json=_success_body(), request=request)

        client, sleeps = _make_client(handler)
        result = _generate(client)

        assert requests_made == 2
        assert len(sleeps) == 1
        # Non-numeric Retry-After → falls back to backoff(1) = 1.0
        assert sleeps[0] == 1.0
        assert result.text == "Hello!"


# ---------------------------------------------------------------------------
# 5xx tests
# ---------------------------------------------------------------------------


class TestRetryOnServerError:
    def test_retries_on_500(self):
        requests_made = 0

        def handler(request: httpx2.Request) -> httpx2.Response:
            nonlocal requests_made
            requests_made += 1
            if requests_made == 1:
                return httpx2.Response(
                    500, json=_error_body("server error"), request=request
                )
            return httpx2.Response(200, json=_success_body(), request=request)

        client, sleeps = _make_client(handler)
        result = _generate(client)

        assert requests_made == 2
        assert len(sleeps) == 1
        assert result.text == "Hello!"

    def test_503_with_retry_after(self):
        requests_made = 0

        def handler(request: httpx2.Request) -> httpx2.Response:
            nonlocal requests_made
            requests_made += 1
            if requests_made == 1:
                return httpx2.Response(
                    503,
                    headers={"retry-after": "1"},
                    json=_error_body("service unavailable"),
                    request=request,
                )
            return httpx2.Response(200, json=_success_body(), request=request)

        client, sleeps = _make_client(handler)
        result = _generate(client)

        assert requests_made == 2
        assert len(sleeps) == 1
        assert sleeps[0] == 1.0
        assert result.text == "Hello!"

    def test_503_retry_after_exceeds_max_raises(self):
        requests_made = 0

        def handler(request: httpx2.Request) -> httpx2.Response:
            nonlocal requests_made
            requests_made += 1
            return httpx2.Response(
                503,
                headers={"retry-after": "999"},
                json=_error_body("service unavailable"),
                request=request,
            )

        client, sleeps = _make_client(handler, max_retry_wait=30.0)
        with pytest.raises(RetryAfterTooLong) as exc_info:
            _generate(client)

        assert requests_made == 1
        assert len(sleeps) == 0
        assert "999" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Non-retryable errors
# ---------------------------------------------------------------------------


class TestNoRetryOn4xx:
    def test_402_raises_credit_limit(self):
        requests_made = 0

        def handler(request: httpx2.Request) -> httpx2.Response:
            nonlocal requests_made
            requests_made += 1
            return httpx2.Response(
                402, json=_error_body("credit limit"), request=request
            )

        client, sleeps = _make_client(handler)
        with pytest.raises(CreditLimitError):
            _generate(client)

        assert requests_made == 1
        assert len(sleeps) == 0

    def test_400_raises_immediately(self):
        requests_made = 0

        def handler(request: httpx2.Request) -> httpx2.Response:
            nonlocal requests_made
            requests_made += 1
            return httpx2.Response(
                400, json=_error_body("bad request"), request=request
            )

        client, sleeps = _make_client(handler)
        with pytest.raises(openai.BadRequestError):
            _generate(client)

        assert requests_made == 1
        assert len(sleeps) == 0


# ---------------------------------------------------------------------------
# Timeout and connection errors
# ---------------------------------------------------------------------------


class TestTimeoutNotRetried:
    def test_timeout_reraises(self):
        requests_made = 0

        def handler(request: httpx2.Request) -> httpx2.Response:
            nonlocal requests_made
            requests_made += 1
            raise httpx2.ReadTimeout("read timed out")

        client, sleeps = _make_client(handler)
        with pytest.raises(openai.APITimeoutError):
            _generate(client)

        assert requests_made == 1
        assert len(sleeps) == 0


class TestConnectionErrorRetried:
    def test_retries_on_connection_error(self):
        requests_made = 0

        def handler(request: httpx2.Request) -> httpx2.Response:
            nonlocal requests_made
            requests_made += 1
            if requests_made == 1:
                raise httpx2.ConnectError("connection refused")
            return httpx2.Response(200, json=_success_body(), request=request)

        client, sleeps = _make_client(handler)
        result = _generate(client)

        assert requests_made == 2
        assert len(sleeps) == 1
        assert result.text == "Hello!"


# ---------------------------------------------------------------------------
# Max retries exceeded
# ---------------------------------------------------------------------------


class TestMaxRetriesExceeded:
    def test_fails_after_all_attempts(self):
        requests_made = 0

        def handler(request: httpx2.Request) -> httpx2.Response:
            nonlocal requests_made
            requests_made += 1
            return httpx2.Response(
                429, json=_error_body("rate limited"), request=request
            )

        client, sleeps = _make_client(handler, max_retries=3)
        with pytest.raises(MaxRetriesExceeded):
            _generate(client)

        assert requests_made == 4  # 1 initial + 3 retries
        assert len(sleeps) == 3


# ---------------------------------------------------------------------------
# Cost and finish_reason extraction
# ---------------------------------------------------------------------------


class TestResponseParsing:
    def test_cost_extracted_from_usage(self):
        def handler(request: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(
                200,
                json=_success_body(cost=0.00042),
                request=request,
            )

        client, _ = _make_client(handler)
        result = _generate(client)
        assert result.cost == 0.00042

    def test_cost_none_when_missing(self):
        def handler(request: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(200, json=_success_body(cost=None), request=request)

        client, _ = _make_client(handler)
        result = _generate(client)
        assert result.cost is None

    def test_content_filter_finish_reason(self):
        def handler(request: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(
                200,
                json=_success_body(finish_reason="content_filter"),
                request=request,
            )

        client, _ = _make_client(handler)
        result = _generate(client)
        assert result.finish_reason == "content_filter"

    def test_retries_count_reported(self):
        requests_made = 0

        def handler(request: httpx2.Request) -> httpx2.Response:
            nonlocal requests_made
            requests_made += 1
            if requests_made <= 2:
                return httpx2.Response(
                    429,
                    json=_error_body("rate limited"),
                    request=request,
                )
            return httpx2.Response(200, json=_success_body(), request=request)

        client, sleeps = _make_client(handler)
        result = _generate(client)

        assert requests_made == 3
        assert len(sleeps) == 2
        assert result.retries == 2
