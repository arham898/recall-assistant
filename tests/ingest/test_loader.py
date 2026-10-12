"""Integration tests for the recall loader.

These tests require a running Postgres instance.
Run with: uv run pytest -m integration
"""

from pathlib import Path

import psycopg
import pytest

from recall_assistant.ingest.filters import filter_recalls
from recall_assistant.ingest.loader import MassDeleteBlocked, upsert_recalls
from recall_assistant.ingest.parser import parse_recalls

pytestmark = pytest.mark.integration


def test_load_fixture_counts(
    db_conn: psycopg.Connection, sample_rcl_path: Path
) -> None:
    """Loading the fixture produces the expected row counts."""
    recalls, _ = parse_recalls(sample_rcl_path)
    filtered = filter_recalls(recalls, recall_type="V", min_model_year=2018)

    upserted, removed = upsert_recalls(db_conn, filtered)

    # Fixture has 3 V-type campaigns: 24V001, 24V002, 24V003
    # 24V002 loses the 2015 F-150, keeps the 2019 F-150
    assert upserted == 3
    assert removed == 0

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


def test_reload_with_tighter_filter_deletes_campaign(
    db_conn: psycopg.Connection, sample_rcl_path: Path
) -> None:
    """Reloading with a tighter filter removes campaigns, vehicles, and chunks."""
    recalls, _ = parse_recalls(sample_rcl_path)

    # First load: all 3 V-type campaigns
    filtered = filter_recalls(recalls, recall_type="V", min_model_year=2018)
    upsert_recalls(db_conn, filtered)

    # Create chunks for 24V001 so we can verify cascade
    with db_conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO chunks (campaign_id, chunker, field_name, body)
            VALUES ('24V001', 'record', 'all:0', 'test chunk for 24V001')
            """
        )
    db_conn.commit()

    # Verify chunks exist before reload
    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM chunks WHERE campaign_id = '24V001'")
        assert cur.fetchone()[0] == 1

    # Second load: tighter filter drops 24V001 (years 2020, 2021 < 2022)
    tighter = filter_recalls(recalls, recall_type="V", min_model_year=2022)
    assert not any(r.campaign_id == "24V001" for r in tighter)

    _, removed = upsert_recalls(db_conn, tighter, allow_mass_delete=True)
    assert removed > 0

    # 24V001 recall, vehicles, and chunks must all be gone
    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM recalls WHERE campaign_id = '24V001'")
        assert cur.fetchone()[0] == 0

        cur.execute("SELECT count(*) FROM recall_vehicles WHERE campaign_id = '24V001'")
        assert cur.fetchone()[0] == 0

        cur.execute("SELECT count(*) FROM chunks WHERE campaign_id = '24V001'")
        assert cur.fetchone()[0] == 0

    # 24V003 (year 2022) must still be intact
    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM recalls WHERE campaign_id = '24V003'")
        assert cur.fetchone()[0] == 1

        cur.execute("SELECT count(*) FROM recall_vehicles WHERE campaign_id = '24V003'")
        assert cur.fetchone()[0] == 1


def test_mass_delete_guard_blocks_over_10_percent(
    db_conn: psycopg.Connection, sample_rcl_path: Path
) -> None:
    """Deleting more than 10% of recalls is blocked without the flag."""
    recalls, _ = parse_recalls(sample_rcl_path)
    filtered = filter_recalls(recalls, recall_type="V", min_model_year=2018)
    upsert_recalls(db_conn, filtered)  # 3 campaigns loaded

    # Try to reload with only 1 campaign — would remove 2/3 (67%)
    single = [r for r in filtered if r.campaign_id == "24V003"]
    with pytest.raises(MassDeleteBlocked, match="safety threshold"):
        upsert_recalls(db_conn, single)

    # With the flag, it should succeed
    _, removed = upsert_recalls(db_conn, single, allow_mass_delete=True)
    assert removed == 2


def test_small_delete_proceeds_without_flag(
    db_conn: psycopg.Connection, sample_rcl_path: Path
) -> None:
    """A delete under 10% goes ahead without --allow-mass-delete.

    Load 3 campaigns, then reload with 2 → removes 1/3 = 33%.
    That's over 10%, so use a custom threshold of 50% to test that
    a 5%-equivalent scenario passes.  We also directly test: load 3,
    then reload dropping 1 with a threshold that allows it.
    """
    recalls, _ = parse_recalls(sample_rcl_path)
    filtered = filter_recalls(recalls, recall_type="V", min_model_year=2018)
    upsert_recalls(db_conn, filtered)  # 3 campaigns

    # Drop 24V002 only → remove 1/3 = 33%.  With threshold=0.50, allowed.
    two = [r for r in filtered if r.campaign_id != "24V002"]
    _, removed = upsert_recalls(db_conn, two, mass_delete_threshold=0.50)
    assert removed == 1
