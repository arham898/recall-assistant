"""Retriever facade wiring all four retrieval modes.

Takes a ConnectionPool and borrows a connection per search call.
The module-level functions keep taking a plain Connection.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from psycopg_pool import ConnectionPool

from recall_assistant.retrieval import (
    Chunker,
    Hit,
    Reranker,
    VehicleFilter,
)
from recall_assistant.retrieval.dense import dense_search
from recall_assistant.retrieval.hybrid import hybrid_search
from recall_assistant.retrieval.keyword import keyword_search
from recall_assistant.retrieval.rerank import rerank_search

if TYPE_CHECKING:
    from recall_assistant.ingest.embedder import Embedder


class Retriever:
    """Facade wiring all retrieval modes.

    Borrows a connection from the pool for each search call so the
    caller doesn't manage connections.
    """

    def __init__(
        self,
        pool: ConnectionPool,
        embedder: Embedder,
        reranker: Reranker | None = None,
        chunker: Chunker = "record",
    ) -> None:
        self._pool = pool
        self._embedder = embedder
        self._reranker = reranker
        self._chunker = chunker

    def dense(
        self,
        question: str,
        k: int = 10,
        pool_size: int = 50,
        ef_search: int = 40,
        vehicle_filter: VehicleFilter | None = None,
    ) -> list[Hit]:
        """Dense (vector) search."""
        with self._pool.connection() as conn:
            return dense_search(
                conn,
                self._embedder,
                question,
                chunker=self._chunker,
                k=k,
                pool_size=pool_size,
                ef_search=ef_search,
                vehicle_filter=vehicle_filter,
            )

    def keyword(
        self,
        question: str,
        k: int = 10,
        pool_size: int = 50,
        vehicle_filter: VehicleFilter | None = None,
    ) -> list[Hit]:
        """Keyword (full-text) search."""
        with self._pool.connection() as conn:
            return keyword_search(
                conn,
                question,
                chunker=self._chunker,
                k=k,
                pool_size=pool_size,
                vehicle_filter=vehicle_filter,
            )

    def hybrid(
        self,
        question: str,
        k: int = 10,
        pool_size: int = 50,
        rrf_k: int = 60,
        ef_search: int = 40,
        vehicle_filter: VehicleFilter | None = None,
    ) -> list[Hit]:
        """Hybrid (RRF fusion of dense + keyword) search."""
        with self._pool.connection() as conn:
            return hybrid_search(
                conn,
                self._embedder,
                question,
                chunker=self._chunker,
                k=k,
                pool_size=pool_size,
                rrf_k=rrf_k,
                ef_search=ef_search,
                vehicle_filter=vehicle_filter,
            )

    def rerank(
        self,
        question: str,
        k: int = 10,
        rerank_candidates: int = 30,
        pool_size: int = 50,
        rrf_k: int = 60,
        ef_search: int = 40,
        vehicle_filter: VehicleFilter | None = None,
    ) -> list[Hit]:
        """Reranked hybrid search."""
        if self._reranker is None:
            raise ValueError("Retriever was created without a reranker")
        with self._pool.connection() as conn:
            return rerank_search(
                conn,
                self._embedder,
                self._reranker,
                question,
                chunker=self._chunker,
                k=k,
                rerank_candidates=rerank_candidates,
                pool_size=pool_size,
                rrf_k=rrf_k,
                ef_search=ef_search,
                vehicle_filter=vehicle_filter,
            )
