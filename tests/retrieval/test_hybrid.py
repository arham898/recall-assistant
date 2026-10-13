"""Tests for hybrid (RRF) retrieval.

The hand-calculated RRF test calls fuse_ranks() directly with VALUES
lists so the fusion SQL is exercised on controlled inputs.  This is
the same SQL fragment that hybrid_search uses.
"""

from __future__ import annotations

import math

import pytest

from recall_assistant.retrieval import BGE_QUERY_PREFIX, VehicleFilter
from recall_assistant.retrieval.hybrid import fuse_ranks, hybrid_search
from tests.retrieval.conftest import LookupEmbedder, RecordingEmbedder

pytestmark = pytest.mark.integration


class TestFuseRanks:
    """Test the RRF fusion SQL on hand-written rank lists."""

    def test_hand_calculated_rrf(self, db_conn):
        """Exact example from the plan:

        dense:   24V-123 (1), 23V-456 (2), 22V-789 (3)
        keyword: 24V-123 (1), 21V-555 (2), 22V-789 (3)

        expected:
          24V-123  1/(60+1) + 1/(60+1) = 2/61 ≈ 0.03279
          22V-789  1/(60+3) + 1/(60+3) = 2/63 ≈ 0.03175
          23V-456  1/(60+2) + 0         = 1/62 ≈ 0.01613  (in dense)
          21V-555  0        + 1/(60+2)  = 1/62 ≈ 0.01613  (NOT in dense)
          → tie at ≈0.01613 broken by dense_rank ASC NULLS LAST:
            23V-456 first (dense_rank=2), then 21V-555 (NULL)
        """
        dense = [
            ("24V-123", 1, "dense text A"),
            ("23V-456", 2, "dense text B"),
            ("22V-789", 3, "dense text C"),
        ]
        kw = [
            ("24V-123", 11, "kw text A"),
            ("21V-555", 12, "kw text D"),
            ("22V-789", 13, "kw text C"),
        ]
        hits = fuse_ranks(db_conn, dense, kw, rrf_k=60, k=10)

        assert len(hits) == 4

        # Order
        assert hits[0].campaign_id == "24V-123"
        assert hits[1].campaign_id == "22V-789"
        assert hits[2].campaign_id == "23V-456"
        assert hits[3].campaign_id == "21V-555"

        # Scores
        assert math.isclose(hits[0].score, 2.0 / 61, rel_tol=1e-6)
        assert math.isclose(hits[1].score, 2.0 / 63, rel_tol=1e-6)
        assert math.isclose(hits[2].score, 1.0 / 62, rel_tol=1e-6)
        assert math.isclose(hits[3].score, 1.0 / 62, rel_tol=1e-6)

        # Ranks
        assert [h.rank for h in hits] == [1, 2, 3, 4]

        # Tie-breaker: 23V-456 (in dense, rank=2) before 21V-555 (not in dense)
        assert hits[2].campaign_id == "23V-456"
        assert hits[3].campaign_id == "21V-555"

        # 24V-123 uses dense chunk (COALESCE prefers dense)
        assert hits[0].chunk_id == 1

    def test_campaign_missing_from_dense(self, db_conn):
        """A campaign only in keyword should still appear, but sort
        after equal-RRF-score peers that are in dense."""
        dense = [("A", 1, "text")]
        kw = [("B", 2, "text")]
        hits = fuse_ranks(db_conn, dense, kw, rrf_k=60, k=10)

        # Both have score 1/61 ≈ 0.01639
        assert len(hits) == 2
        assert math.isclose(hits[0].score, hits[1].score, rel_tol=1e-6)
        # A is in dense → sorts first
        assert hits[0].campaign_id == "A"
        assert hits[1].campaign_id == "B"

    def test_empty_keyword(self, db_conn):
        """Empty keyword list → only dense results."""
        dense = [("X", 1, "text X"), ("Y", 2, "text Y")]
        hits = fuse_ranks(db_conn, dense, [], rrf_k=60, k=10)
        assert len(hits) == 2
        assert hits[0].campaign_id == "X"

    def test_empty_dense(self, db_conn):
        """Empty dense list → only keyword results."""
        kw = [("X", 1, "text X"), ("Y", 2, "text Y")]
        hits = fuse_ranks(db_conn, [], kw, rrf_k=60, k=10)
        assert len(hits) == 2


class TestFieldChunkerCampaignRanks:
    """Prove that RRF uses campaign ranks, not card ranks.

    Campaign A has 3 field cards (distances 0.1, 0.2, 0.3).
    Campaign B has 1 field card (distance 0.35).

    After collapse: A rank 1, B rank 2.
    A's three cards occupying raw positions 1-3 must not give it
    triple the RRF contribution.
    """

    def test_field_collapse_uses_campaign_ranks(self, db_conn):
        # Make a query vector — just a unit vector
        qvec = [1.0] + [0.0] * 383

        # Campaign A: 3 cards at distances 0.1, 0.2, 0.3 from qvec
        # Campaign B: 1 card at distance 0.35
        # We create vectors such that cosine distance = desired value.
        # For unit vectors: distance = 1 - cos(θ), so cos(θ) = 1 - d.
        # A unit vector with cos similarity s to [1,0,...,0] is [s, sqrt(1-s²), 0, ...].
        def _vec_at_distance(d: float) -> list[float]:
            s = 1.0 - d
            return [s, math.sqrt(max(0, 1.0 - s * s))] + [0.0] * 382

        vec_a1 = _vec_at_distance(0.1)
        vec_a2 = _vec_at_distance(0.2)
        vec_a3 = _vec_at_distance(0.3)
        vec_b1 = _vec_at_distance(0.35)

        # Build lookup so embed_query returns our query vector
        from recall_assistant.retrieval import BGE_QUERY_PREFIX

        lookup = LookupEmbedder(
            {
                BGE_QUERY_PREFIX + "test query": qvec,
            }
        )

        with db_conn.cursor() as cur:
            # Insert recalls
            for cid in ("FIELD_A", "FIELD_B"):
                cur.execute(
                    "INSERT INTO recalls "
                    "(campaign_id, manufacturer, component, recall_type) "
                    "VALUES (%s, 'TEST', 'TEST', 'V')",
                    (cid,),
                )
                cur.execute(
                    "INSERT INTO recall_vehicles "
                    "(campaign_id, make, model, year) "
                    "VALUES (%s, 'TEST', 'TEST', 2020)",
                    (cid,),
                )

            # Campaign A: 3 field cards
            for i, vec in enumerate([vec_a1, vec_a2, vec_a3]):
                cur.execute(
                    "INSERT INTO chunks "
                    "(campaign_id, chunker, field_name, body, embedding) "
                    "VALUES ('FIELD_A', 'field', %s, %s, %s::vector)",
                    (f"f:{i}", f"A card {i}", str(vec)),
                )

            # Campaign B: 1 field card
            cur.execute(
                "INSERT INTO chunks "
                "(campaign_id, chunker, field_name, body, embedding) "
                "VALUES ('FIELD_B', 'field', 'f:0', 'B card 0', "
                "%s::vector)",
                (str(vec_b1),),
            )
        db_conn.commit()

        hits = hybrid_search(
            db_conn,
            lookup,
            "test query",
            chunker="field",
            k=10,
            rrf_k=60,
        )

        # Both campaigns should appear
        cids = [h.campaign_id for h in hits]
        assert "FIELD_A" in cids
        assert "FIELD_B" in cids

        # A should rank higher than B (its best card is closer)
        rank_a = next(h.rank for h in hits if h.campaign_id == "FIELD_A")
        rank_b = next(h.rank for h in hits if h.campaign_id == "FIELD_B")
        assert rank_a < rank_b

        # Each campaign appears exactly once
        assert cids.count("FIELD_A") == 1
        assert cids.count("FIELD_B") == 1


class TestPoolSizeIndependentOfK:
    """The candidate pool for each retrieval list must be independent of
    k so the top-k results don't change depending on how many you ask for.

    30 campaigns. POOL_Z is ranked last by dense (distance 0.40) but
    1st by keyword ("brake hose leak" appears only in its text).  With
    pool_size >= 30, POOL_Z is in both lists and its RRF score pushes
    it into the top 3.  With a pool derived from k=3, the dense CTE
    retrieves only ~12 cards / ~12 campaigns — POOL_Z is excluded and
    gets only a keyword contribution.
    """

    def test_pool_size_does_not_change_top_k(self, db_conn):
        from recall_assistant.retrieval import BGE_QUERY_PREFIX

        qvec = [1.0] + [0.0] * 383

        def _vec_at_distance(d: float) -> list[float]:
            s = 1.0 - d
            return [s, math.sqrt(max(0, 1.0 - s * s))] + [0.0] * 382

        lookup = LookupEmbedder({BGE_QUERY_PREFIX + "brake hose leak": qvec})

        with db_conn.cursor() as cur:
            # 29 filler campaigns at distances 0.01..0.29
            for i in range(29):
                cid = f"POOL_{i:03d}"
                dist = 0.01 + i * 0.01
                body = f"filler campaign {i} unrelated text"
                vec = _vec_at_distance(dist)
                cur.execute(
                    "INSERT INTO recalls "
                    "(campaign_id, manufacturer, component, recall_type) "
                    "VALUES (%s, 'TEST', 'TEST', 'V')",
                    (cid,),
                )
                cur.execute(
                    "INSERT INTO recall_vehicles "
                    "(campaign_id, make, model, year) "
                    "VALUES (%s, 'TEST', 'TEST', 2020)",
                    (cid,),
                )
                cur.execute(
                    "INSERT INTO chunks "
                    "(campaign_id, chunker, field_name, body, embedding) "
                    "VALUES (%s, 'record', 'all:0', %s, %s::vector)",
                    (cid, body, str(vec)),
                )

            # Campaign Z: furthest in dense (0.40) but has the keyword hit
            vec = _vec_at_distance(0.40)
            cur.execute(
                "INSERT INTO recalls "
                "(campaign_id, manufacturer, component, recall_type) "
                "VALUES ('POOL_Z', 'TEST', 'TEST', 'V')",
            )
            cur.execute(
                "INSERT INTO recall_vehicles "
                "(campaign_id, make, model, year) "
                "VALUES ('POOL_Z', 'TEST', 'TEST', 2020)",
            )
            cur.execute(
                "INSERT INTO chunks "
                "(campaign_id, chunker, field_name, body, embedding) "
                "VALUES ('POOL_Z', 'record', 'all:0', "
                "'brake hose leaking fluid stop', %s::vector)",
                (str(vec),),
            )
        db_conn.commit()

        # k=3 vs k=30: the top-3 must be identical.
        hits_k3 = hybrid_search(db_conn, lookup, "brake hose leak", k=3)
        hits_k30 = hybrid_search(db_conn, lookup, "brake hose leak", k=30)

        top3_from_k3 = [h.campaign_id for h in hits_k3]
        top3_from_k30 = [h.campaign_id for h in hits_k30[:3]]

        assert top3_from_k3 == top3_from_k30, (
            f"Top-3 changed with k: k=3 gave {top3_from_k3}, k=30 gave {top3_from_k30}"
        )


class TestHybridSearch:
    def test_embed_query_uses_bge_prefix(self, db_conn, seed_retrieval_data):
        rec = RecordingEmbedder()
        hybrid_search(db_conn, rec, "brake problem", k=3)
        assert len(rec.calls) == 1
        assert rec.calls[0] == [BGE_QUERY_PREFIX + "brake problem"]

    def test_stop_words_only_returns_dense_only(self, db_conn, seed_retrieval_data):
        """All stop words → keyword finds nothing, but dense results
        still come through."""
        emb = seed_retrieval_data
        hits = hybrid_search(db_conn, emb, "my the a is", k=5)
        assert len(hits) > 0

    def test_filter_propagates(self, db_conn, seed_retrieval_data):
        emb = seed_retrieval_data
        hits = hybrid_search(
            db_conn,
            emb,
            "brake problem",
            k=10,
            vehicle_filter=VehicleFilter(make="FORD"),
        )
        # Only FORD campaigns: RET_002 and RET_005
        assert all(h.campaign_id in ("RET_002", "RET_005") for h in hits)

    def test_ranks_are_sequential(self, db_conn, seed_retrieval_data):
        emb = seed_retrieval_data
        hits = hybrid_search(db_conn, emb, "brake problem", k=5)
        assert [h.rank for h in hits] == list(range(1, len(hits) + 1))
