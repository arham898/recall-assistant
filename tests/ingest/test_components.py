"""Tests for the recall_components feature.

Unit tests verify the parser collects all distinct components.
Integration tests verify the full pipeline (parse, filter, load)
stores and replaces components correctly in the database.
Stats tests verify component_stats.py query functions on the fixture.
"""

from pathlib import Path

import psycopg
import pytest

from recall_assistant.ingest.filters import filter_recalls
from recall_assistant.ingest.loader import upsert_recalls
from recall_assistant.ingest.parser import parse_recalls

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def multi_component_path() -> Path:
    return FIXTURES_DIR / "multi_component.tsv"


# ---------------------------------------------------------------------------
# Unit tests (no DB)
# ---------------------------------------------------------------------------


def test_parser_collects_all_components_two_categories(
    multi_component_path: Path,
) -> None:
    """25V001 has SEAT BELTS:PRETENSIONER and AIR BAGS:FRONTAL across 3 rows."""
    recalls, _ = parse_recalls(multi_component_path)
    r = next(r for r in recalls if r.campaign_id == "25V001")

    assert r.component == "SEAT BELTS:PRETENSIONER"
    assert r.components == ["AIR BAGS:FRONTAL", "SEAT BELTS:PRETENSIONER"]


def test_parser_collects_all_components_same_category(
    multi_component_path: Path,
) -> None:
    """25V002 has EXTERIOR LIGHTING and EXTERIOR LIGHTING:TAIL LIGHTS."""
    recalls, _ = parse_recalls(multi_component_path)
    r = next(r for r in recalls if r.campaign_id == "25V002")

    assert r.component == "EXTERIOR LIGHTING"
    assert r.components == ["EXTERIOR LIGHTING", "EXTERIOR LIGHTING:TAIL LIGHTS"]


def test_parser_collects_components_with_old_vehicle(
    multi_component_path: Path,
) -> None:
    """25V003 has ENGINE on year 2023 and FUEL SYSTEM:LINES on year 2015."""
    recalls, _ = parse_recalls(multi_component_path)
    r = next(r for r in recalls if r.campaign_id == "25V003")

    assert r.component == "ENGINE"
    assert r.components == ["ENGINE", "FUEL SYSTEM:LINES"]
    assert len(r.vehicles) == 2


# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_full_pipeline_stores_components(
    db_conn: psycopg.Connection, multi_component_path: Path
) -> None:
    """Full pipeline: parse -> filter -> load stores recall_components."""
    recalls, _ = parse_recalls(multi_component_path)
    filtered = filter_recalls(recalls, recall_type="V", min_model_year=2018)
    upsert_recalls(db_conn, filtered)

    with db_conn.cursor() as cur:
        # 25V001: 2 components
        cur.execute(
            "SELECT component FROM recall_components "
            "WHERE campaign_id = '25V001' ORDER BY component"
        )
        comps = [row[0] for row in cur.fetchall()]
        assert comps == ["AIR BAGS:FRONTAL", "SEAT BELTS:PRETENSIONER"]

        # recalls.component is the first-row value
        cur.execute("SELECT component FROM recalls WHERE campaign_id = '25V001'")
        assert cur.fetchone()[0] == "SEAT BELTS:PRETENSIONER"

        # 25V003: both components stored even though RAV4 2015 was filtered
        cur.execute(
            "SELECT component FROM recall_components "
            "WHERE campaign_id = '25V003' ORDER BY component"
        )
        comps = [row[0] for row in cur.fetchall()]
        assert comps == ["ENGINE", "FUEL SYSTEM:LINES"]

        # 25V003: only 1 vehicle survives filtering (RAV4 2023)
        cur.execute("SELECT count(*) FROM recall_vehicles WHERE campaign_id = '25V003'")
        assert cur.fetchone()[0] == 1


@pytest.mark.integration
def test_reload_removes_disappeared_component(
    db_conn: psycopg.Connection, multi_component_path: Path
) -> None:
    """Re-loading with fewer components removes the disappeared one."""
    recalls, _ = parse_recalls(multi_component_path)
    filtered = filter_recalls(recalls, recall_type="V", min_model_year=2018)
    upsert_recalls(db_conn, filtered)

    # Verify 2 components for 25V001
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM recall_components WHERE campaign_id = '25V001'"
        )
        assert cur.fetchone()[0] == 2

    # Re-load with only one component
    r = next(r for r in filtered if r.campaign_id == "25V001")
    from dataclasses import replace

    modified = replace(r, components=["AIR BAGS:FRONTAL"])
    others = [r for r in filtered if r.campaign_id != "25V001"]
    upsert_recalls(db_conn, [modified] + others)

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT component FROM recall_components "
            "WHERE campaign_id = '25V001' ORDER BY component"
        )
        comps = [row[0] for row in cur.fetchall()]
        assert comps == ["AIR BAGS:FRONTAL"]


@pytest.mark.integration
def test_existing_fixture_component_unchanged(
    db_conn: psycopg.Connection, sample_rcl_path: Path
) -> None:
    """Loading sample_rcl.tsv: recalls.component and recall_components agree."""
    recalls, _ = parse_recalls(sample_rcl_path)
    filtered = filter_recalls(recalls, recall_type="V", min_model_year=2018)
    upsert_recalls(db_conn, filtered)

    with db_conn.cursor() as cur:
        # 24V001: all 3 rows have component "ENGINE"
        cur.execute("SELECT component FROM recalls WHERE campaign_id = '24V001'")
        assert cur.fetchone()[0] == "ENGINE"

        cur.execute(
            "SELECT component FROM recall_components "
            "WHERE campaign_id = '24V001' ORDER BY component"
        )
        comps = [row[0] for row in cur.fetchall()]
        assert comps == ["ENGINE"]


# ---------------------------------------------------------------------------
# Stats query tests (integration)
# ---------------------------------------------------------------------------


def _load_stats_module():
    """Import scripts/component_stats.py as a module (cached)."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "component_stats",
        Path(__file__).parent.parent.parent / "scripts" / "component_stats.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_stats_mod = _load_stats_module()


@pytest.mark.integration
def test_stats_on_fixture(
    db_conn: psycopg.Connection, multi_component_path: Path
) -> None:
    """Verify component_stats query functions against the fixture data."""
    stats = _stats_mod

    recalls, _ = parse_recalls(multi_component_path)
    filtered = filter_recalls(recalls, recall_type="V", min_model_year=2018)
    upsert_recalls(db_conn, filtered)

    assert stats.total_campaigns(db_conn) == 3
    assert stats.total_component_rows(db_conn) == 6
    assert stats.campaigns_with_zero_components(db_conn) == 0
    assert stats.multi_component_campaigns(db_conn) == 3
    assert stats.multi_category_campaigns(db_conn) == 2

    hc = stats.hidden_categories(db_conn)
    assert hc == [("AIR BAGS", 1), ("FUEL SYSTEM", 1)]

    ex = stats.examples(db_conn, n=3)
    assert len(ex) == 3
    assert ex[0][0] == "25V001"
    assert ex[1][0] == "25V002"
    assert ex[2][0] == "25V003"
