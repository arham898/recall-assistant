"""OpenRouter LLM client using the openai SDK.

Retry logic handles 429 and 5xx with backoff, honours Retry-After on
429 and 503, and fails immediately on 402 (credit limit) and other 4xx.
Timeouts are not retried.  Connection errors are retried.

The SDK's built-in retries are disabled (max_retries=0) so all retry
behaviour lives here.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

import openai

if TYPE_CHECKING:
    import httpx2

from recall_assistant.llm import LLMResult

log = logging.getLogger(__name__)

_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class CreditLimitError(Exception):
    """OpenRouter returned 402 — credit limit reached."""


class MaxRetriesExceeded(Exception):
    """All retry attempts exhausted."""


class RetryAfterTooLong(Exception):
    """Server requested a wait longer than max_retry_wait."""

    def __init__(self, requested_wait: float, max_wait: float) -> None:
        super().__init__(
            f"Server requested {requested_wait}s retry wait, "
            f"exceeding max_retry_wait ({max_wait}s)"
        )
        self.requested_wait = requested_wait


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _backoff(attempt: int) -> float:
    """Exponential backoff: 1, 2, 4, 8, ... seconds."""
    return float(2 ** (attempt - 1))


def _parse_retry_after(
    exc: openai.RateLimitError | openai.InternalServerError,
) -> float | None:
    """Extract a numeric Retry-After value from the response headers.

    Returns None if the header is absent or not a parseable number
    (e.g. an HTTP-date string).
    """
    headers = exc.response.headers if exc.response is not None else {}
    raw = headers.get("retry-after")
    if raw is None:
        return None
    try:
        return float(raw)
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class OpenRouterClient:
    """Calls OpenRouter via the openai SDK with custom retry logic."""

    def __init__(
        self,
        api_key: str,
        temperature: float = 0.0,
        request_timeout: float = 60.0,
        max_retries: int = 3,
        max_retry_wait: float = 30.0,
        sleep_fn: Callable[[float], None] = time.sleep,
        http_client: httpx2.Client | None = None,
    ) -> None:
        self._client = openai.OpenAI(
            api_key=api_key,
            base_url=_OPENROUTER_BASE_URL,
            max_retries=0,
            timeout=request_timeout,
            http_client=http_client,
        )
        self._temperature = temperature
        self._max_retries = max_retries
        self._max_retry_wait = max_retry_wait
        self._sleep = sleep_fn

    def generate(
        self,
        messages: list[dict],
        model: str,
        max_tokens: int,
    ) -> LLMResult:
        """Send a chat completion request with retry logic."""
        last_exc: Exception | None = None
        total_attempts = self._max_retries + 1

        for attempt in range(1, total_attempts + 1):
            try:
                start = time.perf_counter()
                response = self._client.chat.completions.create(
                    model=model,
                    messages=messages,
                    max_tokens=max_tokens,
                    temperature=self._temperature,
                )
                latency = time.perf_counter() - start

                choice = response.choices[0]
                usage = response.usage
                cost = getattr(usage, "cost", None)

                return LLMResult(
                    text=choice.message.content or "",
                    prompt_tokens=usage.prompt_tokens,
                    completion_tokens=usage.completion_tokens,
                    finish_reason=choice.finish_reason or "unknown",
                    latency_s=latency,
                    cost=cost,
                    retries=attempt - 1,
                )

            except openai.APITimeoutError:
                # Not retried: 4 attempts x 60s is too long to wait,
                # and a request that timed out here may still have
                # completed and been billed.
                raise

            except openai.RateLimitError as exc:
                last_exc = exc
                wait = _parse_retry_after(exc)
                if wait is not None and wait > self._max_retry_wait:
                    raise RetryAfterTooLong(wait, self._max_retry_wait) from exc
                if wait is None:
                    wait = _backoff(attempt)
                wait = min(wait, self._max_retry_wait)

            except openai.InternalServerError as exc:
                last_exc = exc
                wait = _parse_retry_after(exc)
                if wait is not None and wait > self._max_retry_wait:
                    raise RetryAfterTooLong(wait, self._max_retry_wait) from exc
                if wait is None:
                    wait = _backoff(attempt)
                wait = min(wait, self._max_retry_wait)

            except openai.APIStatusError as exc:
                if exc.status_code == 402:
                    raise CreditLimitError(
                        "OpenRouter credit limit reached (HTTP 402). "
                        "Add credits at https://openrouter.ai/credits"
                    ) from exc
                raise

            except openai.APIConnectionError as exc:
                last_exc = exc
                wait = _backoff(attempt)
                wait = min(wait, self._max_retry_wait)

            if attempt >= total_attempts:
                break

            log.info(
                "Retry %d/%d after %.1fs wait (attempt %d failed)",
                attempt,
                self._max_retries,
                wait,
                attempt,
            )
            self._sleep(wait)

        raise MaxRetriesExceeded(
            f"Failed after {self._max_retries} retries "
            f"({total_attempts} total attempts)"
        ) from last_exc
