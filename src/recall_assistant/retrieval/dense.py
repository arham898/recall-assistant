"""Dense retrieval via pgvector cosine distance on partial HNSW indexes.

Uses iterative scan (pgvector >= 0.8.0) to fetch enough cards to fill
k distinct campaigns, then collapses to one hit per campaign.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import psycopg

from recall_assistant.retrieval import (
    Chunker,
    Hit,
    VehicleFilter,
    _validate_chunker,
    embed_query,
)
from recall_assistant.retrieval._sql import build_dense_query

if TYPE_CHECKING:
    from recall_assistant.ingest.embedder import Embedder

# Record chunker: max 2 cards per campaign (400-tok windows).
# Field chunker: max 4 cards per campaign (3 fields × possible windows).
# Verified by measure_tokens.py on the real data.
_FETCH_MULTIPLIER = {"record": 2, "field": 4}


def dense_search(
    conn: psycopg.Connection,
    embedder: Embedder,
    question: str,
    chunker: Chunker = "record",
    k: int = 10,
    pool_size: int = 50,
    ef_search: int = 40,
    vehicle_filter: VehicleFilter | None = None,
) -> list[Hit]:
    """Return up to *k* hits, one per campaign, ranked by cosine similarity.

    *pool_size* is the number of campaigns to consider in the candidate
    pool (never less than *k*).  The fetch limit is
    ``max(pool_size, k) * cards-per-campaign``.
    """
    _validate_chunker(chunker)
    query_vec = embed_query(embedder, question)

    query_sql, vf_params = build_dense_query(chunker, vehicle_filter)
    fetch_limit = max(pool_size, k) * _FETCH_MULTIPLIER[chunker]

    params = {
        "vec": str(query_vec),
        "fetch_limit": fetch_limit,
        **vf_params,
    }

    with conn.transaction(), conn.cursor() as cur:
        cur.execute("SELECT set_config('hnsw.iterative_scan', 'relaxed_order', true)")
        cur.execute(
            "SELECT set_config('hnsw.ef_search', %s, true)",
            (str(ef_search),),
        )
        cur.execute(query_sql, params)
        rows = cur.fetchall()

    return _collapse(rows, k)


def _collapse(
    rows: list[tuple[int, str, str, float]],
    k: int,
) -> list[Hit]:
    """Re-sort by exact distance, collapse to best card per campaign,
    take top k, assign ranks."""
    # Re-sort: distance ASC, campaign_id ASC, chunk_id ASC
    rows.sort(key=lambda r: (r[3], r[1], r[0]))

    seen: dict[str, tuple[int, str, float]] = {}
    for chunk_id, campaign_id, body, distance in rows:
        if campaign_id not in seen:
            seen[campaign_id] = (chunk_id, body, distance)

    # The insertion order of seen preserves the first-seen (best) per campaign.
    # Sort again by distance for the campaign-level ranking.
    campaigns = sorted(seen.items(), key=lambda kv: (kv[1][2], kv[0]))

    hits: list[Hit] = []
    for rank, (campaign_id, (chunk_id, body, distance)) in enumerate(
        campaigns[:k], start=1
    ):
        hits.append(
            Hit(
                campaign_id=campaign_id,
                chunk_id=chunk_id,
                text=body,
                score=1.0 - distance,
                rank=rank,
            )
        )
    return hits
