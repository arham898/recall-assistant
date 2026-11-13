"""Tests for GET /health."""

from __future__ import annotations

from recall_assistant import __version__
from recall_assistant.rag import prompt_hash


class TestHealth:
    async def test_returns_200_without_auth(self, client):
        resp = await client.get("/health")
        assert resp.status_code == 200

    async def test_response_fields(self, client):
        body = (await client.get("/health")).json()
        assert body["version"] == __version__
        assert body["db_reachable"] is True
        assert body["embedding_model"] == "fake-embed"
        assert body["rerank_model"] == "fake-rerank"
        assert body["llm_model"] == "test-model"

    async def test_prompt_hash_matches(self, client):
        body = (await client.get("/health")).json()
        assert body["prompt_hash"] == prompt_hash()

    async def test_request_id_echoed(self, client):
        resp = await client.get("/health", headers={"X-Request-ID": "abc123"})
        assert resp.headers["X-Request-ID"] == "abc123"

    async def test_request_id_generated(self, client):
        resp = await client.get("/health")
        assert "X-Request-ID" in resp.headers
        assert len(resp.headers["X-Request-ID"]) > 0
