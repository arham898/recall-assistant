"""Simple SQL migration runner.

Applies .sql files from the migrations/ directory in sorted order,
tracking which have already run in a schema_migrations table.
"""

import logging
from pathlib import Path

import psycopg

logger = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent.parent.parent / "migrations"


def _ensure_migrations_table(conn: psycopg.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            filename TEXT PRIMARY KEY,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)


def run_migrations(
    conn: psycopg.Connection, migrations_dir: Path = MIGRATIONS_DIR
) -> list[str]:
    """Apply pending .sql migrations and return the list of newly applied filenames."""
    _ensure_migrations_table(conn)

    applied_rows = conn.execute(
        "SELECT filename FROM schema_migrations ORDER BY filename"
    ).fetchall()
    already_applied = {row[0] for row in applied_rows}

    sql_files = sorted(migrations_dir.glob("*.sql"))
    newly_applied: list[str] = []

    for sql_file in sql_files:
        if sql_file.name in already_applied:
            logger.debug("Migration %s already applied — skipping", sql_file.name)
            continue

        logger.info("Applying migration %s ...", sql_file.name)
        sql = sql_file.read_text(encoding="utf-8")
        conn.execute(sql)
        conn.execute(
            "INSERT INTO schema_migrations (filename) VALUES (%s)",
            (sql_file.name,),
        )
        newly_applied.append(sql_file.name)

    conn.commit()
    logger.info(
        "Migrations complete: %d applied, %d already present",
        len(newly_applied),
        len(already_applied),
    )
    return newly_applied
