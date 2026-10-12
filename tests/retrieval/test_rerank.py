"""Tests for rerank retrieval.

FakeReranker returns caller-supplied scores so ordering is fully
controlled.  The slow test uses the real CrossEncoderReranker.
"""

from __future__ import annotations

import pytest

from recall_assistant.retrieval import BGE_QUERY_PREFIX, Reranker
from recall_assistant.retrieval.rerank import rerank_search
from tests.retrieval.conftest import FakeReranker, RecordingEmbedder

pytestmark = pytest.mark.integration


class TestFakeRerankerProtocol:
    def test_satisfies_protocol(self):
        r = FakeReranker([1.0, 0.5])
        result = r.score("query", ["a", "b"])
        assert isinstance(result, list)
        assert len(result) == 2
        assert isinstance(result[0], float)

    def test_protocol_compliance(self):
        """FakeReranker is assignable to Reranker."""
        r: Reranker = FakeReranker([1.0])
        assert r.score("q", ["p"]) == [1.0]


class TestRerankSearch:
    def test_controlled_scores_exact_order(self, db_conn, seed_retrieval_data):
        """Supply known scores and verify the output follows them
        exactly in descending order."""
        emb = seed_retrieval_data

        # We have 5 seed campaigns.  Rerank candidates = 5 so all
        # come through from hybrid.  Assign scores in a non-trivial
        # order so we can verify re-sorting.
        scores = [0.1, 0.9, 0.5, 0.3, 0.7]
        reranker = FakeReranker(scores)

        hits = rerank_search(
            db_conn,
            emb,
            reranker,
            "brake problem",
            k=5,
            rerank_candidates=5,
        )

        # Scores should be sorted descending
        assert [h.score for h in hits] == sorted(scores, reverse=True)
        assert [h.rank for h in hits] == [1, 2, 3, 4, 5]

    def test_k_limits_results(self, db_conn, seed_retrieval_data):
        emb = seed_retrieval_data
        reranker = FakeReranker([0.5, 0.4, 0.3, 0.2, 0.1])
        hits = rerank_search(
            db_conn,
            emb,
            reranker,
            "brake problem",
            k=3,
            rerank_candidates=5,
        )
        assert len(hits) == 3
        assert [h.rank for h in hits] == [1, 2, 3]

    def test_embed_query_uses_bge_prefix(self, db_conn, seed_retrieval_data):
        """rerank_search → hybrid_search → embed_query must send the
        prefixed question."""
        rec = RecordingEmbedder()
        reranker = FakeReranker([0.5] * 5)
        rerank_search(db_conn, rec, reranker, "brake problem", k=3)
        assert len(rec.calls) == 1
        assert rec.calls[0] == [BGE_QUERY_PREFIX + "brake problem"]


@pytest.mark.slow
class TestRerankWithRealModel:
    def test_fuel_pump_ranked_first(self, db_conn, seed_retrieval_data):
        """With the real cross-encoder, a fuel pump query should rank
        the fuel pump campaign (RET_001) first."""
        from recall_assistant.ingest.embedder import SentenceTransformerEmbedder
        from recall_assistant.retrieval.rerank import CrossEncoderReranker

        emb = SentenceTransformerEmbedder()
        reranker = CrossEncoderReranker()

        hits = rerank_search(
            db_conn,
            emb,
            reranker,
            "fuel pump failure causing engine stall",
            k=5,
            rerank_candidates=5,
        )

        assert len(hits) > 0
        assert hits[0].campaign_id == "RET_001"
