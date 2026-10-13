"""Verify that partial HNSW indexes are usable for dense search.

With small test data the planner may prefer bitmap + sort over HNSW,
which is correct.  To prove the partial index CAN be used, we disable
seqscan and bitmapscan with SET LOCAL, forcing the planner onto the
HNSW path.  SET LOCAL scopes the change to the current transaction so
it cannot leak to other tests.

Marked as integration — needs Postgres with pgvector.
"""

from __future__ import annotations

import psycopg
import pytest

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _seed_chunks(db_conn: psycopg.Connection):
    """Insert enough dummy chunks with embeddings for the planner to
    have something to work with.

    ANALYZE afterwards so the planner has accurate statistics for the
    freshly-inserted rows — DELETE-based cleanup between tests does not
    reset pg_class.reltuples, and stale stats can make the planner
    prefer a btree+sort plan over HNSW.
    """
    with db_conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO recalls (campaign_id, manufacturer, component, recall_type)
            VALUES ('TEST_IDX', 'TEST', 'TEST', 'V')
            ON CONFLICT DO NOTHING
            """
        )
        for i in range(20):
            vec = [0.01 * (i + 1)] * 384
            cur.execute(
                """
                INSERT INTO chunks (campaign_id, chunker, field_name, body, embedding)
                VALUES ('TEST_IDX', %(chunker)s, %(field)s, %(body)s, %(vec)s::vector)
                ON CONFLICT (campaign_id, chunker, field_name) DO NOTHING
                """,
                {
                    "chunker": "record" if i < 10 else "field",
                    "field": f"test:{i}",
                    "body": f"test chunk {i}",
                    "vec": str(vec),
                },
            )
    db_conn.commit()
    with db_conn.cursor() as cur:
        cur.execute("ANALYZE chunks")


def _get_forced_explain_plan(conn: psycopg.Connection, chunker: str) -> str:
    """Run EXPLAIN with seqscan and bitmapscan disabled so the planner
    must use the HNSW index if it can."""
    query_vec = [0.05] * 384
    with conn.cursor() as cur:
        # SET LOCAL only lasts until end of transaction
        cur.execute("BEGIN")
        cur.execute("SET LOCAL enable_seqscan = off")
        cur.execute("SET LOCAL enable_bitmapscan = off")
        cur.execute("SET LOCAL enable_sort = off")
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
        cur.execute("ROLLBACK")
    return "\n".join(row[0] for row in rows)


class TestPartialIndexCanBeUsed:
    def test_record_partial_index_is_usable(self, db_conn):
        plan = _get_forced_explain_plan(db_conn, "record")
        assert "idx_chunks_embedding_record" in plan, (
            f"Expected idx_chunks_embedding_record in plan, got:\n{plan}"
        )

    def test_field_partial_index_is_usable(self, db_conn):
        plan = _get_forced_explain_plan(db_conn, "field")
        assert "idx_chunks_embedding_field" in plan, (
            f"Expected idx_chunks_embedding_field in plan, got:\n{plan}"
        )
