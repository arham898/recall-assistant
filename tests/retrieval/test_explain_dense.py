"""Verify that dense_search uses the partial HNSW index for each chunker.

Mirrors tests/ingest/test_partial_index.py: disables seqscan and
bitmapscan with SET LOCAL to force the planner onto the HNSW path,
then checks the EXPLAIN output for the expected index name.

Marked as integration — needs Postgres with pgvector.
"""

from __future__ import annotations

import psycopg
import pytest

from recall_assistant.retrieval._sql import build_dense_query

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _seed_chunks(db_conn: psycopg.Connection):
    """Insert dummy chunks with embeddings for the planner."""
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO recalls "
            "(campaign_id, manufacturer, component, recall_type) "
            "VALUES ('EXPL_IDX', 'TEST', 'TEST', 'V') "
            "ON CONFLICT DO NOTHING"
        )
        for i in range(20):
            vec = [0.01 * (i + 1)] * 384
            chunker = "record" if i < 10 else "field"
            cur.execute(
                "INSERT INTO chunks "
                "(campaign_id, chunker, field_name, body, embedding) "
                "VALUES ('EXPL_IDX', %(chunker)s, %(fn)s, %(body)s, "
                "%(vec)s::vector) "
                "ON CONFLICT (campaign_id, chunker, field_name) DO NOTHING",
                {
                    "chunker": chunker,
                    "fn": f"test:{i}",
                    "body": f"test chunk {i}",
                    "vec": str(vec),
                },
            )
    db_conn.commit()
    with db_conn.cursor() as cur:
        cur.execute("ANALYZE chunks")


def _get_forced_explain(conn: psycopg.Connection, chunker: str) -> str:
    """Run EXPLAIN with seqscan/bitmapscan/indexscan/sort disabled."""
    query_vec = [0.05] * 384
    dense_sql, _ = build_dense_query(chunker, vehicle_filter=None)

    with conn.transaction(), conn.cursor() as cur:
        cur.execute("SELECT set_config('enable_seqscan', 'off', true)")
        cur.execute("SELECT set_config('enable_bitmapscan', 'off', true)")
        # Disable btree indexscan so the planner can't use
        # idx_chunks_chunker.  HNSW uses a custom scan type that
        # remains available.
        cur.execute("SELECT set_config('enable_indexscan', 'off', true)")
        # Disable sort so the planner can't combine any scan with a
        # sort node — the only ordered path left is the HNSW scan.
        cur.execute("SELECT set_config('enable_sort', 'off', true)")
        # Wrap the dense query in EXPLAIN
        from psycopg import sql

        explain_sql = sql.SQL("EXPLAIN ") + dense_sql
        cur.execute(
            explain_sql,
            {"vec": str(query_vec), "fetch_limit": 10},
        )
        rows = cur.fetchall()
    return "\n".join(row[0] for row in rows)


class TestDenseUsesPartialHnswIndex:
    def test_record_index(self, db_conn):
        plan = _get_forced_explain(db_conn, "record")
        assert "idx_chunks_embedding_record" in plan, (
            f"Expected idx_chunks_embedding_record in plan:\n{plan}"
        )

    def test_field_index(self, db_conn):
        plan = _get_forced_explain(db_conn, "field")
        assert "idx_chunks_embedding_field" in plan, (
            f"Expected idx_chunks_embedding_field in plan:\n{plan}"
        )
