"""Check that dense, hybrid and filtered queries use the expected indexes.

Runs EXPLAIN (no planner overrides) against the production database
and appends the query plans to docs/results.md.

Usage:
    uv run python scripts/explain_check.py
"""

import logging
from datetime import date
from pathlib import Path

import psycopg

from recall_assistant.config import Settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

RESULTS_FILE = Path(__file__).resolve().parent.parent / "docs" / "results.md"


def _run_explain(
    conn: psycopg.Connection, label: str, sql_str: str, params: dict | tuple
) -> str:
    """Run EXPLAIN and return the plan as text."""
    from psycopg import sql as psql

    if isinstance(sql_str, str):
        query = psql.SQL("EXPLAIN ") + psql.SQL(sql_str)
    else:
        query = psql.SQL("EXPLAIN ") + sql_str
    with conn.cursor() as cur:
        cur.execute(query, params)
        rows = cur.fetchall()
    return "\n".join(row[0] for row in rows)


def get_explain_plan(conn: psycopg.Connection, chunker: str) -> str:
    """Run EXPLAIN on a dense search query and return the plan as text."""
    query_vec = [0.05] * 384
    return _run_explain(
        conn,
        f"dense {chunker}",
        "SELECT id, body, embedding <=> %s::vector AS distance "
        "FROM chunks "
        "WHERE chunker = %s AND embedding IS NOT NULL "
        "ORDER BY embedding <=> %s::vector "
        "LIMIT 10",
        (str(query_vec), chunker, str(query_vec)),
    )


def get_retrieval_explain_plans(conn: psycopg.Connection) -> list[tuple[str, str]]:
    """Return (label, plan) pairs for retrieval module queries."""
    from recall_assistant.retrieval import VehicleFilter
    from recall_assistant.retrieval._sql import (
        build_dense_query,
        build_keyword_query,
        build_tsquery_expr,
    )

    plans = []
    query_vec = [0.05] * 384

    for chunker in ("record", "field"):
        # Dense, no filter
        dense_sql, vf_params = build_dense_query(chunker, vehicle_filter=None)
        plan = _run_explain(
            conn,
            f"dense {chunker}",
            dense_sql,
            {"vec": str(query_vec), "fetch_limit": 10, **vf_params},
        )
        plans.append((f"Dense, chunker={chunker}, no filter", plan))

    # Dense with narrow filter: FORD, RANGER, 2021
    vf_narrow = VehicleFilter(make="FORD", model="RANGER", year=2021)
    dense_sql, vf_params = build_dense_query("record", vf_narrow)
    plan = _run_explain(
        conn,
        "dense narrow",
        dense_sql,
        {"vec": str(query_vec), "fetch_limit": 10, **vf_params},
    )
    plans.append(("Dense, record, FORD/RANGER/2021 (narrow)", plan))

    # Dense with broad filter: FORD
    vf_broad = VehicleFilter(make="FORD")
    dense_sql, vf_params = build_dense_query("record", vf_broad)
    plan = _run_explain(
        conn,
        "dense broad",
        dense_sql,
        {"vec": str(query_vec), "fetch_limit": 10, **vf_params},
    )
    plans.append(("Dense, record, FORD (broad)", plan))

    # Keyword for each chunker
    words = ["brake", "hose", "leaking"]
    tsquery_expr, word_params = build_tsquery_expr(words)
    for chunker in ("record", "field"):
        kw_sql, vf_params = build_keyword_query(chunker, tsquery_expr, None)
        plan = _run_explain(
            conn,
            f"keyword {chunker}",
            kw_sql,
            {"fetch_limit": 20, **word_params, **vf_params},
        )
        plans.append((f"Keyword, chunker={chunker}, no filter", plan))

    return plans


def main() -> None:
    settings = Settings()

    with psycopg.connect(settings.database_url) as conn:
        # Get chunk counts per chunker
        with conn.cursor() as cur:
            cur.execute(
                "SELECT chunker, count(*) FROM chunks "
                "WHERE embedding IS NOT NULL GROUP BY chunker ORDER BY chunker"
            )
            counts = cur.fetchall()

        if not counts:
            logger.error("No embedded chunks found. Run scripts/embed.py first.")
            return

        lines = [
            "",
            "## EXPLAIN plans for dense search (Module 1.2)",
            "",
            "```",
            "Command: uv run python scripts/explain_check.py",
            f"Date:    {date.today().isoformat()}",
            "```",
            "",
        ]

        for chunker_name, count in counts:
            logger.info(
                "Running EXPLAIN for chunker='%s' (%d embedded chunks) ...",
                chunker_name,
                count,
            )
            plan = get_explain_plan(conn, chunker_name)
            logger.info("Plan:\n%s", plan)

            uses_hnsw = "idx_chunks_embedding_" in plan
            lines.extend(
                [
                    f"### chunker = '{chunker_name}' ({count} embedded chunks)",
                    "",
                    "```",
                    plan,
                    "```",
                    "",
                    f"Uses partial HNSW index: **{'yes' if uses_hnsw else 'no'}**",
                    "",
                ]
            )

        # Retrieval module plans (no planner overrides)
        retrieval_plans = get_retrieval_explain_plans(conn)
        lines.append("")
        lines.append("## EXPLAIN plans for retrieval module (Module 1.3)")
        lines.append("")
        for label, plan in retrieval_plans:
            uses_hnsw = "idx_chunks_embedding_" in plan
            uses_gin = "idx_chunks_tsv" in plan
            notes = []
            if uses_hnsw:
                notes.append("HNSW")
            if uses_gin:
                notes.append("GIN")
            tag = ", ".join(notes) if notes else "seq/bitmap"
            lines.extend(
                [
                    f"### {label}",
                    "",
                    "```",
                    plan,
                    "```",
                    "",
                    f"Index used: **{tag}**",
                    "",
                ]
            )

    # Append to results.md
    with open(RESULTS_FILE, "a", encoding="utf-8") as f:
        f.write("\n".join(lines))
        f.write("\n")

    logger.info("Appended EXPLAIN plans to %s", RESULTS_FILE)


if __name__ == "__main__":
    main()
