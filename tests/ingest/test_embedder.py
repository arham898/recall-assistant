"""Tests for recall_assistant.ingest.embedder."""

from __future__ import annotations

import hashlib
import math
import random

import pytest

# ---------------------------------------------------------------------------
# FakeEmbedder — hashlib-seeded, normalised, deterministic
# ---------------------------------------------------------------------------

EMBEDDING_DIM = 384


class FakeEmbedder:
    """Deterministic fake embedder for tests.

    Uses hashlib.sha256 to seed a random.Random per text, producing a
    384-dim vector that is then L2-normalised.  Properties:
      - Deterministic across runs (hashlib, not hash()).
      - Different texts → different directions (cosine < 1).
      - Never produces an all-zero vector.
    """

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
# Tests
# ---------------------------------------------------------------------------


class TestFakeEmbedder:
    def test_correct_shape(self):
        emb = FakeEmbedder()
        vecs = emb.embed(["hello", "world", "foo", "bar", "baz"])
        assert len(vecs) == 5
        for v in vecs:
            assert len(v) == EMBEDDING_DIM

    def test_deterministic(self):
        emb = FakeEmbedder()
        v1 = emb.embed(["the engine may stall"])[0]
        v2 = emb.embed(["the engine may stall"])[0]
        assert v1 == v2

    def test_different_texts_different_directions(self):
        emb = FakeEmbedder()
        v1 = emb.embed(["brakes may fail"])[0]
        v2 = emb.embed(["airbag inflator rupture"])[0]
        cos_sim = sum(a * b for a, b in zip(v1, v2))
        # Two random unit vectors in 384-d have expected |cos| ≈ 0.05.
        # Certainly < 0.99.
        assert cos_sim < 0.99, f"cosine similarity too high: {cos_sim}"

    def test_unit_norm(self):
        emb = FakeEmbedder()
        vec = emb.embed(["test normalization"])[0]
        norm = math.sqrt(sum(x * x for x in vec))
        assert abs(norm - 1.0) < 1e-6

    def test_empty_input(self):
        emb = FakeEmbedder()
        assert emb.embed([]) == []


class TestEmbedderProtocol:
    """Verify FakeEmbedder satisfies the Embedder protocol at runtime."""

    def test_protocol_compliance(self):
        emb = FakeEmbedder()
        result = emb.embed(["test"])
        assert isinstance(result, list)
        assert isinstance(result[0], list)
        assert isinstance(result[0][0], float)


@pytest.mark.integration
class TestEmbedPipeline:
    """Integration tests that call embed_missing from the library with
    a FakeEmbedder against the test database."""

    def test_skips_already_embedded(self, db_conn):
        """embed_missing only touches rows with NULL embedding."""
        from recall_assistant.ingest.embedder import embed_missing

        original_vec = [0.1] * 384

        with db_conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO recalls (campaign_id, manufacturer, component, recall_type)
                VALUES ('EMBED_TEST', 'TEST', 'TEST', 'V')
                """
            )
            cur.execute(
                """
                INSERT INTO chunks (campaign_id, chunker, field_name, body, embedding)
                VALUES
                    ('EMBED_TEST', 'record', 'all:0', 'already embedded',
                     %s::vector),
                    ('EMBED_TEST', 'record', 'all:1', 'needs embedding', NULL)
                """,
                (str(original_vec),),
            )
        db_conn.commit()

        # Run the same function that scripts/embed.py calls
        count = embed_missing(db_conn, "record", FakeEmbedder(), batch_size=64)

        # Only the NULL-embedding row should have been embedded
        assert count == 1

        # The pre-existing embedding must be unchanged
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT embedding::text FROM chunks "
                "WHERE campaign_id = 'EMBED_TEST' AND field_name = 'all:0'"
            )
            vec_str = cur.fetchone()[0]
            assert "0.1" in vec_str

        # The previously-NULL row must now have an embedding
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT embedding IS NOT NULL FROM chunks "
                "WHERE campaign_id = 'EMBED_TEST' AND field_name = 'all:1'"
            )
            assert cur.fetchone()[0] is True
