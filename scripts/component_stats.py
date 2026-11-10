"""Print component statistics from the recall_components table.

Usage:
    uv run python scripts/component_stats.py

Each query is a standalone function taking a psycopg Connection,
so it can be tested on a fixture database.
"""

import logging

import psycopg

from recall_assistant.config import Settings
from recall_assistant.ingest.migrator import run_migrations

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Query functions
# ---------------------------------------------------------------------------


def total_campaigns(conn: psycopg.Connection) -> int:
    """Total number of campaigns in the recalls table."""
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM recalls")
        return cur.fetchone()[0]


def total_component_rows(conn: psycopg.Connection) -> int:
    """Total rows in recall_components."""
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM recall_components")
        return cur.fetchone()[0]


def campaigns_with_zero_components(conn: psycopg.Connection) -> int:
    """Campaigns with no rows in recall_components."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM recalls r "
            "WHERE NOT EXISTS ("
            "  SELECT 1 FROM recall_components rc "
            "  WHERE rc.campaign_id = r.campaign_id"
            ")"
        )
        return cur.fetchone()[0]


def multi_component_campaigns(conn: psycopg.Connection) -> int:
    """Campaigns with more than one distinct component."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM ("
            "  SELECT campaign_id FROM recall_components "
            "  GROUP BY campaign_id HAVING count(*) > 1"
            ") t"
        )
        return cur.fetchone()[0]


def multi_category_campaigns(conn: psycopg.Connection) -> int:
    """Campaigns with more than one distinct top-level category."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM ("
            "  SELECT campaign_id FROM recall_components "
            "  GROUP BY campaign_id "
            "  HAVING count(DISTINCT SPLIT_PART(component, ':', 1)) > 1"
            ") t"
        )
        return cur.fetchone()[0]


def hidden_categories(conn: psycopg.Connection) -> list[tuple[str, int]]:
    """Top-level categories in recall_components but not in recalls.component.

    Returns (category, campaign_count) ordered by count DESC, category ASC.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT SPLIT_PART(rc.component, ':', 1) AS cat, "
            "       count(DISTINCT rc.campaign_id) "
            "FROM recall_components rc "
            "WHERE NOT EXISTS ("
            "  SELECT 1 FROM recalls r "
            "  WHERE r.campaign_id = rc.campaign_id "
            "    AND SPLIT_PART(r.component, ':', 1) = SPLIT_PART(rc.component, ':', 1)"
            ") "
            "GROUP BY cat "
            "ORDER BY count DESC, cat"
        )
        return cur.fetchall()


def examples(conn: psycopg.Connection, n: int = 3) -> list[tuple[str, str, list[str]]]:
    """Multi-component campaigns with their recalls.component and full list.

    Returns (campaign_id, recalls_component, [all components]).
    Deterministic: ORDER BY campaign_id.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT rc.campaign_id "
            "FROM recall_components rc "
            "GROUP BY rc.campaign_id HAVING count(*) > 1 "
            "ORDER BY rc.campaign_id "
            "LIMIT %s",
            (n,),
        )
        campaign_ids = [row[0] for row in cur.fetchall()]

    results: list[tuple[str, str, list[str]]] = []
    for cid in campaign_ids:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT component FROM recalls WHERE campaign_id = %s",
                (cid,),
            )
            recalls_comp = cur.fetchone()[0]

            cur.execute(
                "SELECT component FROM recall_components "
                "WHERE campaign_id = %s ORDER BY component",
                (cid,),
            )
            all_comps = [row[0] for row in cur.fetchall()]

        results.append((cid, recalls_comp, all_comps))
    return results


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    settings = Settings()

    with psycopg.connect(settings.database_url) as conn:
        applied = run_migrations(conn)
        if applied:
            logger.info("Applied migrations: %s", applied)

        tc = total_campaigns(conn)
        tr = total_component_rows(conn)
        zc = campaigns_with_zero_components(conn)
        mc = multi_component_campaigns(conn)
        mcat = multi_category_campaigns(conn)
        hc = hidden_categories(conn)
        ex = examples(conn, n=3)

    print(f"Total campaigns:                    {tc:,}")
    print(f"Total component rows:               {tr:,}")
    print(f"Campaigns with 0 components:        {zc:,}")
    print(f"Campaigns with >1 component:        {mc:,}")
    print(f"Campaigns with >1 top-level category: {mcat:,}")
    print()
    print("Hidden categories (in recall_components but not recalls.component):")
    for cat, count in hc:
        print(f"  {cat}: {count:,} campaigns")
    print()
    print("Examples (first 3 multi-component campaigns):")
    for cid, rc, all_comps in ex:
        print(f"  {cid}: recalls.component = {rc!r}")
        print(f"    recall_components = {all_comps}")


if __name__ == "__main__":
    main()
