"""Tests for keyword retrieval.

All tests use the seed_retrieval_data fixture which inserts five
campaigns with known text so keyword matching is predictable.
"""

from __future__ import annotations

import pytest

from recall_assistant.retrieval import VehicleFilter
from recall_assistant.retrieval.keyword import keyword_search

pytestmark = pytest.mark.integration


class TestKeywordSearch:
    def test_natural_language_returns_results(self, db_conn, seed_retrieval_data):
        """'my car takes longer to stop' should match brake-related
        recalls via stemmed 'stop' → 'stop' matching 'stopping'."""
        hits = keyword_search(db_conn, "my car takes longer to stop", k=5)
        assert len(hits) > 0

    def test_punctuation_mixed_with_words(self, db_conn, seed_retrieval_data):
        """Words mixed with special chars must reach Postgres and
        return results, not short-circuit to []."""
        hits = keyword_search(
            db_conn,
            'Ranger: brake (hose) & "leak"! stop',
            k=5,
        )
        assert len(hits) > 0

    def test_pure_special_chars_returns_empty(self, db_conn, seed_retrieval_data):
        hits = keyword_search(db_conn, ': ! & ( ) "', k=5)
        assert hits == []

    def test_all_stop_words_returns_empty(self, db_conn, seed_retrieval_data):
        hits = keyword_search(db_conn, "my the a is to", k=5)
        assert hits == []

    def test_one_hit_per_campaign(self, db_conn, seed_retrieval_data):
        hits = keyword_search(db_conn, "brake", chunker="field", k=10)
        campaign_ids = [h.campaign_id for h in hits]
        assert len(campaign_ids) == len(set(campaign_ids))

    def test_scores_are_positive(self, db_conn, seed_retrieval_data):
        hits = keyword_search(db_conn, "fuel pump", k=5)
        assert len(hits) > 0
        for h in hits:
            assert h.score > 0

    def test_filter_by_make(self, db_conn, seed_retrieval_data):
        hits = keyword_search(
            db_conn,
            "brake",
            k=10,
            vehicle_filter=VehicleFilter(make="ford"),
        )
        assert len(hits) > 0
        assert all(h.campaign_id == "RET_002" for h in hits)

    def test_ranks_are_sequential(self, db_conn, seed_retrieval_data):
        hits = keyword_search(db_conn, "brake", k=5)
        assert [h.rank for h in hits] == list(range(1, len(hits) + 1))
