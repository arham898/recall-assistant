"""Tests for POST /ask (non-streaming)."""

from __future__ import annotations

import httpx

from recall_assistant.rag import DECLINE_MARKER
from tests.api.conftest import TEST_API_KEY, create_test_app


class TestAsk:
    async def test_answered_status(self, client, auth_headers):
        resp = await client.post(
            "/ask", json={"question": "brake problems?"}, headers=auth_headers
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "answered"
        assert "25V001" in body["cited_ids"]
        assert body["answer"] == "Brake issue [25V001]."
        assert len(body["sources"]) == 2

    async def test_declined_status(self):
        app = create_test_app(llm_text=DECLINE_MARKER)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.post(
                "/ask",
                json={"question": "unrelated?"},
                headers={"X-API-Key": TEST_API_KEY},
            )
        assert resp.json()["status"] == "declined"

    async def test_sources_in_response(self, client, auth_headers):
        resp = await client.post(
            "/ask", json={"question": "test?"}, headers=auth_headers
        )
        sources = resp.json()["sources"]
        assert len(sources) == 2
        assert sources[0]["campaign_id"] == "25V001"
        assert sources[0]["rank"] == 1

    async def test_vehicle_filter(self, client, auth_headers):
        resp = await client.post(
            "/ask",
            json={
                "question": "test?",
                "make": "Toyota",
                "model": "Camry",
                "year": 2020,
            },
            headers=auth_headers,
        )
        assert resp.status_code == 200

    async def test_mode_parameter(self, client, auth_headers):
        resp = await client.post(
            "/ask",
            json={"question": "test?", "mode": "dense"},
            headers=auth_headers,
        )
        assert resp.status_code == 200

    async def test_chunker_parameter(self, client, auth_headers):
        resp = await client.post(
            "/ask",
            json={"question": "test?", "chunker": "field"},
            headers=auth_headers,
        )
        assert resp.status_code == 200

    async def test_empty_question_422(self, client, auth_headers):
        resp = await client.post("/ask", json={"question": ""}, headers=auth_headers)
        assert resp.status_code == 422

    async def test_credit_limit_503(self):
        from recall_assistant.llm.openrouter import CreditLimitError

        app = create_test_app()

        class ExplodingClient:
            def generate(self, *a, **kw):
                raise CreditLimitError("out of credits")

            def stream(self, *a, **kw):
                raise CreditLimitError("out of credits")

        app.state.llm_client = ExplodingClient()

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.post(
                "/ask",
                json={"question": "test?"},
                headers={"X-API-Key": TEST_API_KEY},
            )
        assert resp.status_code == 503
        assert "temporarily unavailable" in resp.json()["detail"].lower()

    async def test_request_id_in_response(self, client, auth_headers):
        headers = {**auth_headers, "X-Request-ID": "my-req-id"}
        resp = await client.post("/ask", json={"question": "test?"}, headers=headers)
        assert resp.headers["X-Request-ID"] == "my-req-id"
