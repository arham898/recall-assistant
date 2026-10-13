"""Embedder interface, sentence-transformers implementation, and the
embed_missing function that wires embedding into the database.

The Embedder protocol lets tests swap in a fake without loading model
weights or calling the network.
"""

from __future__ import annotations

import logging
from typing import Protocol

import psycopg

logger = logging.getLogger(__name__)


class Embedder(Protocol):
    """Produces one normalised vector per input text."""

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Return one normalised vector per input text."""
        ...


class SentenceTransformerEmbedder:
    """Wraps sentence-transformers for BAAI/bge-small-en-v1.5.

    Passages are embedded without a prefix.  The BGE model card says to
    prepend a query instruction at *query* time only; that happens in the
    retrieval layer, not here.
    """

    def __init__(
        self,
        model_name: str = "BAAI/bge-small-en-v1.5",
        device: str = "cpu",
    ) -> None:
        from sentence_transformers import SentenceTransformer

        self._model = SentenceTransformer(model_name, device=device)

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Encode a batch of texts into normalised vectors."""
        # normalize_embeddings=True → unit L2 norm, ready for cosine.
        arr = self._model.encode(
            texts,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        return arr.tolist()


def embed_missing(
    conn: psycopg.Connection,
    chunker: str,
    embedder: Embedder,
    batch_size: int,
) -> int:
    """Embed chunks that have no embedding yet.  Returns count embedded.

    Selects all rows for the given chunker where embedding IS NULL,
    embeds them in batches via the provided Embedder, and writes the
    vectors back.  Rows that already have an embedding are untouched.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, body FROM chunks "
            "WHERE chunker = %s AND embedding IS NULL "
            "ORDER BY id",
            (chunker,),
        )
        rows = cur.fetchall()

    if not rows:
        logger.info("No chunks to embed")
        return 0

    total = len(rows)
    logger.info("Embedding %d chunks in batches of %d ...", total, batch_size)
    embedded = 0

    for start in range(0, total, batch_size):
        batch = rows[start : start + batch_size]
        ids = [r[0] for r in batch]
        texts = [r[1] for r in batch]

        vectors = embedder.embed(texts)

        with conn.cursor() as cur:
            for chunk_id, vec in zip(ids, vectors):
                cur.execute(
                    "UPDATE chunks SET embedding = %s WHERE id = %s",
                    (vec, chunk_id),
                )
        conn.commit()
        embedded += len(batch)

    return embedded
