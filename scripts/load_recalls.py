"""Parse, filter, migrate, and load NHTSA recalls into Postgres."""

import logging
import sys
from pathlib import Path

import psycopg

from recall_assistant.config import Settings
from recall_assistant.ingest.filters import filter_recalls
from recall_assistant.ingest.loader import upsert_recalls
from recall_assistant.ingest.migrator import run_migrations
from recall_assistant.ingest.parser import parse_recalls

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "raw"


def main() -> None:
    settings = Settings()

    # Find the flat file
    flat_files = list(DATA_DIR.glob("FLAT_RCL*.txt"))
    if not flat_files:
        logger.error(
            "No FLAT_RCL*.txt found in %s — run download_data.py first", DATA_DIR
        )
        sys.exit(1)
    flat_path = flat_files[0]

    # Parse
    logger.info("Parsing %s ...", flat_path.name)
    recalls, skipped = parse_recalls(flat_path)
    logger.info("Parsed %d campaigns (%d rows skipped)", len(recalls), skipped)

    # Filter
    filtered = filter_recalls(
        recalls,
        recall_type=settings.recall_type_filter,
        min_model_year=settings.min_model_year,
    )
    total_vehicles = sum(len(r.vehicles) for r in filtered)
    logger.info(
        "After filtering: %d campaigns, %d vehicle entries",
        len(filtered),
        total_vehicles,
    )

    # Migrate and load
    with psycopg.connect(settings.database_url) as conn:
        applied = run_migrations(conn)
        if applied:
            logger.info("Applied migrations: %s", applied)

        count = upsert_recalls(conn, filtered)
        logger.info("Done — %d campaigns loaded into database", count)


if __name__ == "__main__":
    main()
