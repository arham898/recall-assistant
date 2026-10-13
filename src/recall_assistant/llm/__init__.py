"""LLM client abstraction layer.

LLMClient is a protocol satisfied by any object with a generate() method.
LLMResult is the return type carrying text, token counts, cost and latency.
"""

from __future__ import annotations

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


class LLMClient(Protocol):
    """Generates text from a list of chat messages."""

    def generate(
        self,
        messages: list[dict],
        model: str,
        max_tokens: int,
    ) -> LLMResult: ...
