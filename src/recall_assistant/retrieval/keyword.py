"""Keyword retrieval via Postgres built-in full-text search.

Combines terms with OR (not AND) so a natural question like
"my car takes longer to stop" returns results even if not every
word appears in the same document.  Each word is parsed individually
by plainto_tsquery, which handles stop words and special characters
safely.
"""

from __future__ import annotations

import psycopg

from recall_assistant.retrieval import (
    Chunker,
    Hit,
    VehicleFilter,
    _extract_words,
    _validate_chunker,
)
from recall_assistant.retrieval._sql import (
    build_keyword_query,
    build_tsquery_expr,
)

# Same multipliers as dense — we want enough rows to fill k campaigns.
_FETCH_MULTIPLIER = {"record": 2, "field": 4}


def keyword_search(
    conn: psycopg.Connection,
    question: str,
    chunker: Chunker = "record",
    k: int = 10,
    pool_size: int = 50,
    vehicle_filter: VehicleFilter | None = None,
) -> list[Hit]:
    """Return up to *k* hits, one per campaign, ranked by ts_rank_cd.

    *pool_size* is the number of campaigns to consider in the candidate
    pool (never less than *k*).
    """
    _validate_chunker(chunker)
    words = _extract_words(question)
    if not words:
        return []

    tsquery_expr, word_params = build_tsquery_expr(words)
    query_sql, vf_params = build_keyword_query(
        chunker,
        tsquery_expr,
        vehicle_filter,
    )

    fetch_limit = max(pool_size, k) * _FETCH_MULTIPLIER[chunker]
    params = {
        "fetch_limit": fetch_limit,
        **word_params,
        **vf_params,
    }

    with conn.cursor() as cur:
        cur.execute(query_sql, params)
        rows = cur.fetchall()

    return _collapse(rows, k)


def _collapse(
    rows: list[tuple[int, str, str, float]],
    k: int,
) -> list[Hit]:
    """Collapse to best card per campaign, take top k, assign ranks."""
    # Rows are already ordered by score DESC, campaign_id, id from SQL.
    seen: dict[str, tuple[int, str, float]] = {}
    for chunk_id, campaign_id, body, score in rows:
        if campaign_id not in seen:
            seen[campaign_id] = (chunk_id, body, score)

    # Sort by score descending, then campaign_id for determinism.
    campaigns = sorted(seen.items(), key=lambda kv: (-kv[1][2], kv[0]))

    hits: list[Hit] = []
    for rank, (campaign_id, (chunk_id, body, score)) in enumerate(
        campaigns[:k], start=1
    ):
        hits.append(
            Hit(
                campaign_id=campaign_id,
                chunk_id=chunk_id,
                text=body,
                score=score,
                rank=rank,
            )
        )
    return hits
