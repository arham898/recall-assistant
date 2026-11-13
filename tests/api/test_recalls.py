"""Tests for GET /recalls/{campaign_id}."""

from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest

from tests.api.conftest import TEST_API_KEY, create_test_app


class TestRecallsMocked:
    @patch("recall_assistant.api.routes_recalls.load_recall_rows_by_ids")
    async def test_found(self, mock_loader, client, auth_headers):
        mock_loader.return_value = (
            {
                "25V001": {
                    "manufacturer": "TOYOTA",
                    "component": "FUEL PUMP",
                    "defect_summary": "fuel pump failure",
                    "consequence_summary": "engine stall",
                    "corrective_action": "replace pump",
                }
            },
            {"25V001": [("TOYOTA", "CAMRY", 2020)]},
            {"25V001": ["FUEL PUMP"]},
        )
        resp = await client.get("/recalls/25V001", headers=auth_headers)
        assert resp.status_code == 200
        body = resp.json()
        assert body["campaign_id"] == "25V001"
        assert body["manufacturer"] == "TOYOTA"
        assert len(body["vehicles"]) == 1
        assert body["vehicles"][0]["make"] == "TOYOTA"
        assert body["components"] == ["FUEL PUMP"]

    @patch("recall_assistant.api.routes_recalls.load_recall_rows_by_ids")
    async def test_not_found_404(self, mock_loader, client, auth_headers):
        mock_loader.return_value = ({}, {}, {})
        resp = await client.get("/recalls/NONEXISTENT", headers=auth_headers)
        assert resp.status_code == 404

    async def test_requires_auth(self, client):
        resp = await client.get("/recalls/25V001")
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Integration test with seed data
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestRecallsIntegration:
    async def test_recall_from_seed(self, _test_db, seed_retrieval_data):
        """GET /recalls/RET_001 against the real test database."""
        from psycopg_pool import ConnectionPool

        pool = ConnectionPool(_test_db, min_size=1, max_size=2, open=True)
        try:
            app = create_test_app(pool=pool)
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test"
            ) as c:
                resp = await c.get(
                    "/recalls/RET_001",
                    headers={"X-API-Key": TEST_API_KEY},
                )

            assert resp.status_code == 200
            body = resp.json()
            assert body["campaign_id"] == "RET_001"
            assert body["manufacturer"] == "TOYOTA MOTOR"
            assert body["component"] == "FUEL PUMP"
            makes = [v["make"] for v in body["vehicles"]]
            assert "TOYOTA" in makes
        finally:
            pool.close()
