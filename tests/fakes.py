"""Shared test fakes for all test modules.

FakeEmbedder and FakeReranker moved here unchanged from
tests/retrieval/conftest.py.  FakeLLMClient and FakeRetriever are new.
"""

from __future__ import annotations

import hashlib
import math
import random

from recall_assistant.llm import LLMResult
from recall_assistant.retrieval import Hit

# ---------------------------------------------------------------------------
# Fake embedder (moved unchanged from tests/retrieval/conftest.py)
# ---------------------------------------------------------------------------

EMBEDDING_DIM = 384


class FakeEmbedder:
    """Deterministic fake: SHA256-seeded Gaussian, L2-normalised."""

    def __init__(self, dim: int = EMBEDDING_DIM) -> None:
        self.dim = dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._embed_one(t) for t in texts]

    def _embed_one(self, text: str) -> list[float]:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        rng = random.Random(digest)
        raw = [rng.gauss(0, 1) for _ in range(self.dim)]
        norm = math.sqrt(sum(x * x for x in raw))
        return [x / norm for x in raw]


# ---------------------------------------------------------------------------
# Fake reranker (moved unchanged from tests/retrieval/conftest.py)
# ---------------------------------------------------------------------------


class FakeReranker:
    """Returns caller-supplied scores so tests are deterministic."""

    def __init__(self, scores: list[float]) -> None:
        self._scores = scores

    def score(self, query: str, passages: list[str]) -> list[float]:
        return self._scores[: len(passages)]


# ---------------------------------------------------------------------------
# Fake LLM client
# ---------------------------------------------------------------------------


class FakeLLMClient:
    """Returns canned responses and records every call."""

    def __init__(
        self,
        text: str = "",
        finish_reason: str = "stop",
        prompt_tokens: int = 100,
        completion_tokens: int = 50,
        cost: float | None = 0.001,
    ) -> None:
        self.text = text
        self.finish_reason = finish_reason
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.cost = cost
        self.calls: list[tuple[list[dict], str, int]] = []

    def generate(
        self,
        messages: list[dict],
        model: str,
        max_tokens: int,
    ) -> LLMResult:
        self.calls.append((messages, model, max_tokens))
        return LLMResult(
            text=self.text,
            prompt_tokens=self.prompt_tokens,
            completion_tokens=self.completion_tokens,
            finish_reason=self.finish_reason,
            latency_s=0.1,
            cost=self.cost,
            retries=0,
        )


# ---------------------------------------------------------------------------
# Fake retriever
# ---------------------------------------------------------------------------


class FakeRetriever:
    """Duck-typed Retriever returning canned hits.  Records every call."""

    def __init__(self, hits: list[Hit]) -> None:
        self._hits = hits
        self.calls: list[tuple[str, str, dict]] = []

    def _record(self, method: str, question: str, **kwargs: object) -> list[Hit]:
        self.calls.append((method, question, kwargs))
        return self._hits

    def dense(self, question: str, **kwargs: object) -> list[Hit]:
        return self._record("dense", question, **kwargs)

    def keyword(self, question: str, **kwargs: object) -> list[Hit]:
        return self._record("keyword", question, **kwargs)

    def hybrid(self, question: str, **kwargs: object) -> list[Hit]:
        return self._record("hybrid", question, **kwargs)

    def rerank(self, question: str, **kwargs: object) -> list[Hit]:
        return self._record("rerank", question, **kwargs)
