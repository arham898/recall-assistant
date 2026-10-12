"""Tests for dense retrieval.

Uses seed_retrieval_data (FakeEmbedder) for structural tests and
RecordingEmbedder to verify the BGE prefix is applied.
"""

from __future__ import annotations

import pytest

from recall_assistant.retrieval import BGE_QUERY_PREFIX, VehicleFilter
from recall_assistant.retrieval.dense import dense_search
from tests.retrieval.conftest import RecordingEmbedder

pytestmark = pytest.mark.integration


class TestDenseSearch:
    def test_returns_k_results(self, db_conn, seed_retrieval_data):
        emb = seed_retrieval_data
        hits = dense_search(db_conn, emb, "brake problem", k=3)
        assert len(hits) == 3

    def test_ranks_are_sequential(self, db_conn, seed_retrieval_data):
        emb = seed_retrieval_data
        hits = dense_search(db_conn, emb, "brake problem", k=5)
        assert [h.rank for h in hits] == [1, 2, 3, 4, 5]

    def test_scores_are_decreasing(self, db_conn, seed_retrieval_data):
        emb = seed_retrieval_data
        hits = dense_search(db_conn, emb, "brake problem", k=5)
        for i in range(len(hits) - 1):
            assert hits[i].score >= hits[i + 1].score

    def test_one_hit_per_campaign_field_chunker(self, db_conn, seed_retrieval_data):
        """Each campaign has 3 field cards but only one hit should
        appear per campaign."""
        emb = seed_retrieval_data
        hits = dense_search(db_conn, emb, "brake problem", chunker="field", k=5)
        campaign_ids = [h.campaign_id for h in hits]
        assert len(campaign_ids) == len(set(campaign_ids))

    def test_filter_by_make(self, db_conn, seed_retrieval_data):
        emb = seed_retrieval_data
        hits = dense_search(
            db_conn,
            emb,
            "problem",
            k=10,
            vehicle_filter=VehicleFilter(make="toyota"),
        )
        # RET_001 and RET_004 are TOYOTA
        assert len(hits) > 0
        assert all(h.campaign_id in ("RET_001", "RET_004") for h in hits)

    def test_filter_by_model(self, db_conn, seed_retrieval_data):
        emb = seed_retrieval_data
        hits = dense_search(
            db_conn,
            emb,
            "problem",
            k=10,
            vehicle_filter=VehicleFilter(model="F-150"),
        )
        assert len(hits) == 1
        assert hits[0].campaign_id == "RET_002"

    def test_filter_by_year(self, db_conn, seed_retrieval_data):
        emb = seed_retrieval_data
        hits = dense_search(
            db_conn,
            emb,
            "problem",
            k=10,
            vehicle_filter=VehicleFilter(year=2023),
        )
        assert len(hits) == 1
        assert hits[0].campaign_id == "RET_005"

    def test_filter_no_results(self, db_conn, seed_retrieval_data):
        emb = seed_retrieval_data
        hits = dense_search(
            db_conn,
            emb,
            "problem",
            k=10,
            vehicle_filter=VehicleFilter(make="NONEXISTENT"),
        )
        assert hits == []

    def test_embed_query_uses_bge_prefix(self, db_conn, seed_retrieval_data):
        """dense_search must embed the question with the BGE prefix."""
        rec = RecordingEmbedder()
        dense_search(db_conn, rec, "brake problem", k=3)
        assert len(rec.calls) == 1
        assert rec.calls[0] == [BGE_QUERY_PREFIX + "brake problem"]
