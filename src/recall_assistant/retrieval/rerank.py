"""Rerank retrieval: hybrid search followed by cross-encoder re-scoring.

Takes hybrid's top candidates and re-scores each (question, passage)
pair with a cross-encoder.  No new dependency — sentence-transformers
already includes CrossEncoder.

The reranker reads only the best card per campaign (the one returned
by hybrid).  For field-chunker campaigns this is the chunk closest by
embedding distance when available, else the highest-ranked keyword
chunk.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import psycopg

from recall_assistant.retrieval import (
    Chunker,
    Hit,
    Reranker,
    VehicleFilter,
    _validate_chunker,
)
from recall_assistant.retrieval.hybrid import hybrid_search

if TYPE_CHECKING:
    from recall_assistant.ingest.embedder import Embedder


class CrossEncoderReranker:
    """Wraps sentence-transformers CrossEncoder for reranking.

    Default model is cross-encoder/ms-marco-MiniLM-L-6-v2.
    BAAI/bge-reranker-v2-m3 is selectable for comparison.
    """

    def __init__(
        self,
        model_name: str = "cross-encoder/ms-marco-MiniLM-L-6-v2",
        device: str = "cpu",
    ) -> None:
        from sentence_transformers import CrossEncoder

        self._model = CrossEncoder(model_name, device=device)

    def score(self, query: str, passages: list[str]) -> list[float]:
        """Return one relevance score per passage.  Higher is better."""
        pairs = [[query, p] for p in passages]
        return self._model.predict(pairs).tolist()


def rerank_search(
    conn: psycopg.Connection,
    embedder: Embedder,
    reranker: Reranker,
    question: str,
    chunker: Chunker = "record",
    k: int = 10,
    rerank_candidates: int = 30,
    pool_size: int = 50,
    rrf_k: int = 60,
    ef_search: int = 40,
    vehicle_filter: VehicleFilter | None = None,
) -> list[Hit]:
    """Return up to *k* hits re-scored by a cross-encoder.

    Calls hybrid_search to get *rerank_candidates* campaigns, then
    re-scores each with the reranker and returns the top *k*.
    """
    _validate_chunker(chunker)
    candidates = hybrid_search(
        conn,
        embedder,
        question,
        chunker=chunker,
        k=rerank_candidates,
        pool_size=pool_size,
        rrf_k=rrf_k,
        ef_search=ef_search,
        vehicle_filter=vehicle_filter,
    )

    if not candidates:
        return []

    passages = [h.text for h in candidates]
    scores = reranker.score(question, passages)

    # Pair each candidate with its reranker score, sort descending
    scored = sorted(
        zip(candidates, scores), key=lambda pair: (-pair[1], pair[0].campaign_id)
    )

    hits: list[Hit] = []
    for rank, (original, rerank_score) in enumerate(scored[:k], start=1):
        hits.append(
            Hit(
                campaign_id=original.campaign_id,
                chunk_id=original.chunk_id,
                text=original.text,
                score=rerank_score,
                rank=rank,
            )
        )
    return hits
