"""LLM client abstraction layer.

LLMClient is a protocol satisfied by any object with generate() and
stream() methods.  LLMResult is the return type for non-streaming calls;
StreamEvent is the element type for streaming calls.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class LLMResult:
    """Result of a single LLM generation call."""

    text: str
    prompt_tokens: int
    completion_tokens: int
    finish_reason: str
    latency_s: float
    cost: float | None
    retries: int


@dataclass(frozen=True)
class StreamUsage:
    """Token counts from a streaming response."""

    prompt_tokens: int
    completion_tokens: int
    cost: float | None


@dataclass(frozen=True)
class StreamEvent:
    """One piece of a streaming LLM response.

    Most events carry a *token* (a fragment of generated text).
    The final event has ``token=""``, *finish_reason* set, and
    *usage* with the total token counts for the request.
    """

    token: str
    finish_reason: str | None = None
    usage: StreamUsage | None = None


class LLMClient(Protocol):
    """Generates text from a list of chat messages."""

    def generate(
        self,
        messages: list[dict],
        model: str,
        max_tokens: int,
    ) -> LLMResult: ...

    def stream(
        self,
        messages: list[dict],
        model: str,
        max_tokens: int,
    ) -> Iterator[StreamEvent]:
        """Open a streaming completion and return a token iterator.

        Performs the HTTP request eagerly — errors (CreditLimitError,
        timeout, etc.) raise from this call, not from ``next()``.
        """
        ...
