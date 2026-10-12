"""Hybrid retrieval: reciprocal rank fusion of dense and keyword search.

Both retrieval lists, campaign-level collapse, ranking, fusion,
tie-breaking and final ordering happen in a single CTE-based SQL
query built from the shared fragments in _sql.py.

fuse_ranks() is the testable entry point for the fusion step: it
accepts hand-written rank lists (as Python tuples) and runs the
exact same SQL that hybrid_search uses.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import psycopg
from psycopg import sql

from recall_assistant.retrieval import (
    Chunker,
    Hit,
    VehicleFilter,
    _extract_words,
    _validate_chunker,
    embed_query,
)
from recall_assistant.retrieval._sql import (
    build_dense_query,
    build_fusion_query,
    build_keyword_query,
    build_tsquery_expr,
)

if TYPE_CHECKING:
    from recall_assistant.ingest.embedder import Embedder

_FETCH_MULTIPLIER = {"record": 2, "field": 4}


# ------------------------------------------------------------------
# Public API
# ------------------------------------------------------------------


def hybrid_search(
    conn: psycopg.Connection,
    embedder: Embedder,
    question: str,
    chunker: Chunker = "record",
    k: int = 10,
    pool_size: int = 50,
    rrf_k: int = 60,
    ef_search: int = 40,
    vehicle_filter: VehicleFilter | None = None,
) -> list[Hit]:
    """Return up to *k* hits fused from dense and keyword search.

    *pool_size* is the number of campaigns each list considers (never
    less than *k*).  Collapses each list to one hit per campaign before
    fusing so RRF works on campaign ranks.  Ties are broken by dense
    rank (NULLS LAST), then campaign ID.
    """
    _validate_chunker(chunker)
    query_vec = embed_query(embedder, question)
    words = _extract_words(question)

    # --- build the combined query from shared fragments ---
    pool = max(pool_size, k)
    dense_limit = pool * _FETCH_MULTIPLIER[chunker]
    kw_limit = pool * _FETCH_MULTIPLIER[chunker]

    dense_sql, vf_params_d = build_dense_query(chunker, vehicle_filter)

    # Wrap dense in collapse + rank
    dense_collapsed = sql.SQL(
        "SELECT id, campaign_id, body, "
        "ROW_NUMBER() OVER (ORDER BY distance, campaign_id) AS rank "
        "FROM ("
        "SELECT DISTINCT ON (campaign_id) id, campaign_id, body, distance "
        "FROM ({inner}) AS _raw "
        "ORDER BY campaign_id, distance, id"
        ") AS _best"
    ).format(inner=dense_sql)

    # Keyword branch — may be empty if all words are stop words
    if words:
        tsquery_expr, word_params = build_tsquery_expr(words)
        kw_sql, _vf_params_kw = build_keyword_query(
            chunker,
            tsquery_expr,
            vehicle_filter,
        )
        kw_collapsed = sql.SQL(
            "SELECT id, campaign_id, body, "
            "ROW_NUMBER() OVER (ORDER BY score DESC, campaign_id) AS rank "
            "FROM ("
            "SELECT DISTINCT ON (campaign_id) "
            "id, campaign_id, body, score "
            "FROM ({inner}) AS _raw "
            "ORDER BY campaign_id, score DESC, id"
            ") AS _best"
        ).format(inner=kw_sql)
    else:
        # No searchable words → empty keyword set
        word_params = {}
        kw_collapsed = sql.SQL(
            "SELECT NULL::text AS campaign_id, NULL::int AS id, "
            "NULL::text AS body, NULL::int AS rank "
            "WHERE false"
        )

    fusion_sql = build_fusion_query(dense_collapsed, kw_collapsed)

    params: dict = {
        "vec": str(query_vec),
        "fetch_limit": dense_limit,
        "rrf_k": rrf_k,
        "k": k,
        **vf_params_d,
        **word_params,
    }
    # keyword query also needs fetch_limit — but it uses the same
    # param name, so we need a separate one.  Since both dense and
    # keyword queries use %(fetch_limit)s, we set it to the max.
    params["fetch_limit"] = max(dense_limit, kw_limit)

    with conn.transaction(), conn.cursor() as cur:
        cur.execute("SELECT set_config('hnsw.iterative_scan', 'relaxed_order', true)")
        cur.execute(
            "SELECT set_config('hnsw.ef_search', %s, true)",
            (str(ef_search),),
        )
        cur.execute(fusion_sql, params)
        rows = cur.fetchall()

    return [
        Hit(
            campaign_id=row[0],
            chunk_id=row[1],
            text=row[2],
            score=float(row[3]),
            rank=rank,
        )
        for rank, row in enumerate(rows, start=1)
    ]


def fuse_ranks(
    conn: psycopg.Connection,
    dense_ranks: list[tuple[str, int, str]],
    kw_ranks: list[tuple[str, int, str]],
    rrf_k: int = 60,
    k: int = 10,
) -> list[Hit]:
    """Run the RRF fusion on hand-written rank lists.

    Each entry is (campaign_id, chunk_id, body).  Ranks are assigned
    by list position (1-based).

    Uses the exact same build_fusion_query SQL that hybrid_search
    uses, fed with VALUES instead of live CTEs.
    """
    # Build VALUES lists for dense and keyword
    if dense_ranks:
        dense_values = sql.SQL(", ").join(
            sql.SQL("({cid}, {chid}, {body}, {rank})").format(
                cid=sql.Literal(cid),
                chid=sql.Literal(chid),
                body=sql.Literal(body),
                rank=sql.Literal(rank),
            )
            for rank, (cid, chid, body) in enumerate(dense_ranks, start=1)
        )
        dense_input = sql.SQL(
            "SELECT campaign_id, id, body, rank FROM "
            "(VALUES {vals}) AS t(campaign_id, id, body, rank)"
        ).format(vals=dense_values)
    else:
        dense_input = sql.SQL(
            "SELECT NULL::text AS campaign_id, NULL::int AS id, "
            "NULL::text AS body, NULL::int AS rank "
            "WHERE false"
        )

    if kw_ranks:
        kw_values = sql.SQL(", ").join(
            sql.SQL("({cid}, {chid}, {body}, {rank})").format(
                cid=sql.Literal(cid),
                chid=sql.Literal(chid),
                body=sql.Literal(body),
                rank=sql.Literal(rank),
            )
            for rank, (cid, chid, body) in enumerate(kw_ranks, start=1)
        )
        kw_input = sql.SQL(
            "SELECT campaign_id, id, body, rank FROM "
            "(VALUES {vals}) AS t(campaign_id, id, body, rank)"
        ).format(vals=kw_values)
    else:
        kw_input = sql.SQL(
            "SELECT NULL::text AS campaign_id, NULL::int AS id, "
            "NULL::text AS body, NULL::int AS rank "
            "WHERE false"
        )

    fusion_sql = build_fusion_query(dense_input, kw_input)

    with conn.cursor() as cur:
        cur.execute(fusion_sql, {"rrf_k": rrf_k, "k": k})
        rows = cur.fetchall()

    return [
        Hit(
            campaign_id=row[0],
            chunk_id=row[1],
            text=row[2],
            score=float(row[3]),
            rank=rank,
        )
        for rank, row in enumerate(rows, start=1)
    ]
