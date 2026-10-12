"""Check that dense queries use the partial HNSW indexes on real data.

Runs EXPLAIN (no SET LOCAL overrides) against the production database
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


def get_explain_plan(conn: psycopg.Connection, chunker: str) -> str:
    """Run EXPLAIN on a dense search query and return the plan as text."""
    query_vec = [0.05] * 384
    with conn.cursor() as cur:
        cur.execute(
            """
            EXPLAIN SELECT id, body, embedding <=> %s::vector AS distance
            FROM chunks
            WHERE chunker = %s AND embedding IS NOT NULL
            ORDER BY embedding <=> %s::vector
            LIMIT 10
            """,
            (str(query_vec), chunker, str(query_vec)),
        )
        rows = cur.fetchall()
    return "\n".join(row[0] for row in rows)


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

    # Append to results.md
    with open(RESULTS_FILE, "a", encoding="utf-8") as f:
        f.write("\n".join(lines))
        f.write("\n")

    logger.info("Appended EXPLAIN plans to %s", RESULTS_FILE)


if __name__ == "__main__":
    main()
