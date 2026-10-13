"""Shared SQL fragments for the retrieval module.

Every dense query, keyword query and hybrid query composes from these
building blocks so a fix in one place applies everywhere.

The chunker value is always inserted as a SQL literal (not a parameter)
so the planner sees ``WHERE chunker = 'record'`` as a constant and can
match the partial HNSW index condition.
"""

from __future__ import annotations

from psycopg import sql

from recall_assistant.retrieval import VehicleFilter, _validate_chunker

# ------------------------------------------------------------------
# Vehicle filter
# ------------------------------------------------------------------


def vehicle_filter_clause(
    vf: VehicleFilter | None,
) -> tuple[sql.Composable, dict[str, str | int | None]]:
    """Return (SQL fragment, params) for an optional vehicle filter.

    Uses EXISTS so a campaign with multiple matching vehicles does not
    produce duplicate rows.
    """
    if vf is None:
        return sql.SQL(""), {}

    conditions: list[sql.Composable] = []
    params: dict[str, str | int | None] = {}

    if vf.make is not None:
        conditions.append(sql.SQL("rv.make = %(vf_make)s"))
        params["vf_make"] = vf.make
    if vf.model is not None:
        conditions.append(sql.SQL("rv.model = %(vf_model)s"))
        params["vf_model"] = vf.model
    if vf.year is not None:
        conditions.append(sql.SQL("rv.year = %(vf_year)s"))
        params["vf_year"] = vf.year

    if not conditions:
        return sql.SQL(""), {}

    where = sql.SQL(" AND ").join(conditions)
    fragment = sql.SQL(
        " AND EXISTS ("
        "SELECT 1 FROM recall_vehicles rv "
        "WHERE rv.campaign_id = c.campaign_id AND {where}"
        ")"
    ).format(where=where)
    return fragment, params


# ------------------------------------------------------------------
# Dense query
# ------------------------------------------------------------------


def build_dense_query(
    chunker: str,
    vehicle_filter: VehicleFilter | None,
) -> tuple[sql.Composable, dict[str, str | int | None]]:
    """Build the full dense SELECT with vehicle filter composed in.

    Caller supplies %(vec)s and %(fetch_limit)s params.
    """
    _validate_chunker(chunker)
    vf_sql, vf_params = vehicle_filter_clause(vehicle_filter)
    query = sql.SQL(
        "SELECT c.id, c.campaign_id, c.body, "
        "c.embedding <=> %(vec)s::vector AS distance "
        "FROM chunks c "
        "WHERE c.chunker = {chunker} "
        "AND c.embedding IS NOT NULL"
        "{vf} "
        "ORDER BY c.embedding <=> %(vec)s::vector, c.campaign_id, c.id "
        "LIMIT %(fetch_limit)s"
    ).format(chunker=sql.Literal(chunker), vf=vf_sql)
    return query, vf_params


# ------------------------------------------------------------------
# Keyword helpers
# ------------------------------------------------------------------


def build_tsquery_expr(words: list[str]) -> tuple[str, dict[str, str]]:
    """Build an OR-combined tsquery expression from extracted words.

    Returns (sql_expression_string, params_dict).  Each word is parsed
    by plainto_tsquery individually so stop words and special chars are
    handled safely by Postgres.
    """
    parts: list[str] = []
    params: dict[str, str] = {}
    for i, word in enumerate(words):
        key = f"w{i}"
        parts.append(f"plainto_tsquery('english', %({key})s)")
        params[key] = word
    expr = " || ".join(parts)
    return expr, params


def build_keyword_query(
    chunker: str,
    tsquery_expr: str,
    vehicle_filter: VehicleFilter | None,
) -> tuple[sql.Composable, dict[str, str | int | None]]:
    """Build the full keyword SELECT with vehicle filter composed in.

    Caller supplies the word params from build_tsquery_expr and
    %(fetch_limit)s.
    """
    _validate_chunker(chunker)
    vf_sql, vf_params = vehicle_filter_clause(vehicle_filter)
    query = sql.SQL(
        "SELECT c.id, c.campaign_id, c.body, "
        "ts_rank_cd(c.tsv, ({tsquery})) AS score "
        "FROM chunks c "
        "WHERE c.chunker = {chunker} "
        "AND c.tsv @@ ({tsquery})"
        "{vf} "
        "ORDER BY score DESC, c.campaign_id, c.id "
        "LIMIT %(fetch_limit)s"
    ).format(
        chunker=sql.Literal(chunker),
        tsquery=sql.SQL(tsquery_expr),
        vf=vf_sql,
    )
    return query, vf_params


# ------------------------------------------------------------------
# RRF fusion — the exact SQL used by both hybrid_search and fuse_ranks
# ------------------------------------------------------------------


def build_fusion_query(
    dense_input: sql.Composable,
    kw_input: sql.Composable,
) -> sql.Composable:
    """Compose the RRF fusion query with concrete dense/keyword inputs.

    The dense_input and kw_input must each produce columns:
    campaign_id, id, body, rank.

    Caller supplies %(rrf_k)s and %(k)s params.
    """
    return sql.SQL(
        "WITH "
        "dense_ranked AS ("
        "SELECT campaign_id, id AS chunk_id, body AS text, rank "
        "FROM ({dense_input}) AS _d"
        "), "
        "kw_ranked AS ("
        "SELECT campaign_id, id AS chunk_id, body AS text, rank "
        "FROM ({kw_input}) AS _kw"
        "), "
        "fused AS ("
        "SELECT "
        "COALESCE(d.campaign_id, kw.campaign_id) AS campaign_id, "
        "COALESCE(d.chunk_id, kw.chunk_id) AS chunk_id, "
        "COALESCE(d.text, kw.text) AS text, "
        "COALESCE(1.0 / (%(rrf_k)s + d.rank), 0) "
        "+ COALESCE(1.0 / (%(rrf_k)s + kw.rank), 0) AS rrf_score, "
        "d.rank AS dense_rank "
        "FROM dense_ranked d "
        "FULL OUTER JOIN kw_ranked kw ON d.campaign_id = kw.campaign_id"
        ") "
        "SELECT campaign_id, chunk_id, text, rrf_score "
        "FROM fused "
        "ORDER BY rrf_score DESC, "
        "dense_rank ASC NULLS LAST, "
        "campaign_id ASC "
        "LIMIT %(k)s"
    ).format(dense_input=dense_input, kw_input=kw_input)
