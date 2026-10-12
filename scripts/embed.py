"""Chunk and embed recall records.

Usage:
    uv run python scripts/embed.py --chunker record
    uv run python scripts/embed.py --chunker field

Idempotent: re-running skips already-embedded chunks.  When a recall's
text changes, the upsert replaces body and clears embedding so the
embedding phase picks it up.  Stale windows (from a recall that now
produces fewer chunks) are deleted.
"""

import argparse
import logging
import time

import psycopg
from transformers import AutoTokenizer

from recall_assistant.config import Settings
from recall_assistant.ingest.chunkers import Chunk, chunk_fields, chunk_record
from recall_assistant.ingest.embedder import SentenceTransformerEmbedder, embed_missing
from recall_assistant.ingest.migrator import run_migrations
from recall_assistant.ingest.models import Recall, RecallVehicle

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------


def _load_recalls(conn: psycopg.Connection) -> list[Recall]:
    """Read all recalls with their vehicles from the database."""
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM recalls ORDER BY campaign_id")
        recall_cols = [desc.name for desc in cur.description]
        recall_rows = cur.fetchall()

        cur.execute(
            "SELECT campaign_id, make, model, year "
            "FROM recall_vehicles ORDER BY campaign_id"
        )
        vehicle_rows = cur.fetchall()

    # Group vehicles by campaign
    vehicles_by_campaign: dict[str, list[RecallVehicle]] = {}
    for cid, make, model, year in vehicle_rows:
        vehicles_by_campaign.setdefault(cid, []).append(
            RecallVehicle(make=make, model=model, year=year)
        )

    recalls = []
    for row in recall_rows:
        data = dict(zip(recall_cols, row))
        cid = data["campaign_id"]
        recalls.append(
            Recall(
                campaign_id=cid,
                manufacturer=data["manufacturer"],
                manufacturer_campaign_number=data["manufacturer_campaign_number"],
                component=data["component"],
                recall_type=data["recall_type"],
                potential_units_affected=data["potential_units_affected"],
                defect_summary=data["defect_summary"],
                consequence_summary=data["consequence_summary"],
                corrective_action=data["corrective_action"],
                notes=data["notes"],
                report_received_date=data["report_received_date"],
                record_creation_date=data["record_creation_date"],
                manufacturing_begin_date=data["manufacturing_begin_date"],
                manufacturing_end_date=data["manufacturing_end_date"],
                owner_notification_date=data["owner_notification_date"],
                influenced_by=data["influenced_by"],
                do_not_drive=data["do_not_drive"],
                park_outside=data["park_outside"],
                vehicles=vehicles_by_campaign.get(cid, []),
            )
        )
    return recalls


def _upsert_chunks(
    conn: psycopg.Connection,
    chunks: list[Chunk],
    chunker: str,
) -> int:
    """Insert or update chunks, delete stale windows, return count upserted."""
    upserted = 0
    # Group chunks by campaign for stale-window cleanup
    by_campaign: dict[str, list[Chunk]] = {}
    for c in chunks:
        by_campaign.setdefault(c.campaign_id, []).append(c)

    with conn.cursor() as cur:
        for campaign_id, campaign_chunks in by_campaign.items():
            # Upsert each chunk
            for chunk in campaign_chunks:
                cur.execute(
                    """
                    INSERT INTO chunks (campaign_id, chunker, field_name, body)
                    VALUES (%(cid)s, %(chunker)s, %(field)s, %(body)s)
                    ON CONFLICT (campaign_id, chunker, field_name) DO UPDATE
                        SET body = EXCLUDED.body,
                            embedding = NULL
                        WHERE chunks.body IS DISTINCT FROM EXCLUDED.body
                    """,
                    {
                        "cid": chunk.campaign_id,
                        "chunker": chunk.chunker,
                        "field": chunk.field_name,
                        "body": chunk.body,
                    },
                )
                upserted += 1

            # Delete stale windows for this campaign+chunker
            new_fields = [c.field_name for c in campaign_chunks]
            cur.execute(
                """
                DELETE FROM chunks
                WHERE campaign_id = %s
                  AND chunker = %s
                  AND field_name != ALL(%s)
                """,
                (campaign_id, chunker, new_fields),
            )

    conn.commit()
    return upserted


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="Chunk and embed recalls")
    parser.add_argument(
        "--chunker",
        choices=["record", "field"],
        required=True,
        help="Chunking strategy to use",
    )
    args = parser.parse_args()

    settings = Settings()
    chunker_name: str = args.chunker

    # Load tokenizer for chunking
    logger.info("Loading tokenizer for %s ...", settings.embedding_model)
    tokenizer = AutoTokenizer.from_pretrained(settings.embedding_model)

    # Choose chunker function
    if chunker_name == "record":
        chunker_fn = chunk_record
    else:
        chunker_fn = chunk_fields

    with psycopg.connect(settings.database_url) as conn:
        # Run migrations
        applied = run_migrations(conn)
        if applied:
            logger.info("Applied migrations: %s", applied)

        # Load recalls from DB
        recalls = _load_recalls(conn)
        logger.info("Loaded %d recalls from database", len(recalls))

        # Chunk
        logger.info("Chunking with '%s' strategy ...", chunker_name)
        all_chunks: list[Chunk] = []
        for recall in recalls:
            all_chunks.extend(
                chunker_fn(
                    recall,
                    tokenizer,
                    max_tokens=settings.chunk_max_tokens,
                    overlap=settings.chunk_overlap_tokens,
                )
            )
        logger.info("Generated %d chunks", len(all_chunks))

        # Upsert chunks (with stale-window cleanup)
        upserted = _upsert_chunks(conn, all_chunks, chunker_name)
        logger.info("Upserted %d chunks", upserted)

        # Embed
        logger.info("Loading embedding model %s ...", settings.embedding_model)
        embedder = SentenceTransformerEmbedder(
            model_name=settings.embedding_model,
            device="cpu",
        )

        t0 = time.perf_counter()
        embedded = embed_missing(
            conn, chunker_name, embedder, settings.embedding_batch_size
        )
        elapsed = time.perf_counter() - t0

        logger.info(
            "Embedded %d chunks in %.1f seconds (%.0f chunks/s)",
            embedded,
            elapsed,
            embedded / elapsed if elapsed > 0 else 0,
        )

    logger.info("Done.")


if __name__ == "__main__":
    main()
