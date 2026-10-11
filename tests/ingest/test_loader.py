"""Integration tests for the recall loader.

These tests require a running Postgres instance.
Run with: uv run pytest -m integration
"""

from pathlib import Path

import psycopg
import pytest

from recall_assistant.config import Settings
from recall_assistant.ingest.filters import filter_recalls
from recall_assistant.ingest.loader import upsert_recalls
from recall_assistant.ingest.migrator import run_migrations
from recall_assistant.ingest.parser import parse_recalls

pytestmark = pytest.mark.integration


@pytest.fixture
def db_conn():
    """Connect to the test database, run migrations, and clean up after."""
    settings = Settings()
    conn = psycopg.connect(settings.database_url)
    run_migrations(conn)

    yield conn

    # Clean up test data
    with conn.cursor() as cur:
        cur.execute("DELETE FROM recall_vehicles")
        cur.execute("DELETE FROM recalls")
    conn.commit()
    conn.close()


def test_load_fixture_counts(
    db_conn: psycopg.Connection, sample_rcl_path: Path
) -> None:
    """Loading the fixture produces the expected row counts."""
    recalls, _ = parse_recalls(sample_rcl_path)
    filtered = filter_recalls(recalls, recall_type="V", min_model_year=2018)

    count = upsert_recalls(db_conn, filtered)

    # Fixture has 3 V-type campaigns: 24V001, 24V002, 24V003
    # 24V002 loses the 2015 F-150, keeps the 2019 F-150
    assert count == 3

    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM recalls")
        assert cur.fetchone()[0] == 3

        cur.execute("SELECT count(*) FROM recall_vehicles")
        # 24V001: 3 vehicles, 24V002: 1 vehicle (2019 only), 24V003: 1 vehicle
        assert cur.fetchone()[0] == 5


def test_upsert_is_idempotent(
    db_conn: psycopg.Connection, sample_rcl_path: Path
) -> None:
    """Running the loader twice produces the same counts (no duplicates)."""
    recalls, _ = parse_recalls(sample_rcl_path)
    filtered = filter_recalls(recalls, recall_type="V", min_model_year=2018)

    upsert_recalls(db_conn, filtered)
    upsert_recalls(db_conn, filtered)  # second run

    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM recalls")
        assert cur.fetchone()[0] == 3

        cur.execute("SELECT count(*) FROM recall_vehicles")
        assert cur.fetchone()[0] == 5


def test_loaded_data_is_correct(
    db_conn: psycopg.Connection, sample_rcl_path: Path
) -> None:
    """Spot-check that loaded data matches the fixture."""
    recalls, _ = parse_recalls(sample_rcl_path)
    filtered = filter_recalls(recalls, recall_type="V", min_model_year=2018)
    upsert_recalls(db_conn, filtered)

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT manufacturer, component, do_not_drive FROM recalls WHERE campaign_id = %s",
            ("24V001",),
        )
        row = cur.fetchone()
        assert row[0] == "TOYOTA MOTOR"
        assert row[1] == "ENGINE"
        assert row[2] is True

        cur.execute(
            "SELECT make, model, year FROM recall_vehicles WHERE campaign_id = %s ORDER BY year",
            ("24V001",),
        )
        vehicles = cur.fetchall()
        assert len(vehicles) == 3
        assert vehicles[0] == ("TOYOTA", "CAMRY", 2020)
