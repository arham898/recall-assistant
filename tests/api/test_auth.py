"""Tests for API key authentication."""

from __future__ import annotations


class TestAuth:
    async def test_ask_without_key_401(self, client):
        resp = await client.post("/ask", json={"question": "test?"})
        assert resp.status_code == 401

    async def test_ask_wrong_key_401(self, client):
        resp = await client.post(
            "/ask",
            json={"question": "test?"},
            headers={"X-API-Key": "wrong"},
        )
        assert resp.status_code == 401

    async def test_ask_correct_key_200(self, client, auth_headers):
        resp = await client.post(
            "/ask", json={"question": "test?"}, headers=auth_headers
        )
        assert resp.status_code == 200

    async def test_recalls_without_key_401(self, client):
        resp = await client.get("/recalls/25V001")
        assert resp.status_code == 401

    async def test_health_open_without_key(self, client):
        resp = await client.get("/health")
        assert resp.status_code == 200
