"""Shared fixtures for API tests."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import contextmanager

import httpx
import pytest

from recall_assistant.api.app import create_app
from recall_assistant.config import Settings
from recall_assistant.retrieval import Hit
from tests.fakes import FakeEmbedder, FakeLLMClient, FakeReranker, FakeRetriever

FAKE_HITS = [
    Hit(campaign_id="25V001", chunk_id=1, text="fuel pump failure", score=0.9, rank=1),
    Hit(campaign_id="25V002", chunk_id=2, text="brake line issue", score=0.8, rank=2),
]

TEST_API_KEY = "test-secret-key"


# ---------------------------------------------------------------------------
# Minimal fake pool (health endpoint calls pool.connection() → SELECT 1)
# ---------------------------------------------------------------------------


class FakePool:
    """Minimal pool supporting ``with pool.connection() as conn``."""

    class _Conn:
        def execute(self, sql: str, *args: object) -> None:
            pass

    @contextmanager
    def connection(self):
        yield self._Conn()

    def close(self) -> None:
        pass


# ---------------------------------------------------------------------------
# App builder
# ---------------------------------------------------------------------------


def create_test_app(
    *,
    llm_text: str = "Brake issue [25V001].",
    llm_finish_reason: str = "stop",
    hits: list[Hit] | None = None,
    api_key: str = TEST_API_KEY,
    pool: object | None = None,
):
    """Build a FastAPI app wired to fakes — no real models or DB."""
    settings = Settings(
        api_key=api_key,
        llm_model="test-model",
        embedding_model="fake-embed",
        rerank_model="fake-rerank",
    )

    used_hits = hits if hits is not None else FAKE_HITS

    return create_app(
        settings=settings,
        pool=pool or FakePool(),
        embedder=FakeEmbedder(),
        reranker=FakeReranker([0.5] * 10),
        llm_client=FakeLLMClient(text=llm_text, finish_reason=llm_finish_reason),
        retriever_factory=lambda chunker: FakeRetriever(used_hits),
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def app():
    return create_test_app()


@pytest.fixture
async def client(app) -> AsyncGenerator[httpx.AsyncClient, None]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def auth_headers():
    return {"X-API-Key": TEST_API_KEY}
