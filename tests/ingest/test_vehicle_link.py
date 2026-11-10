"""Tests for the component-vehicle link analysis.

Unit tests (no DB) — build RawRow objects matching the multi_component
fixture and verify that the analysis function detects the expected cases.
"""

import importlib.util
from datetime import date
from pathlib import Path

SCRIPTS_DIR = Path(__file__).parent.parent.parent / "scripts"


def _load_link_module():
    """Import scripts/component_vehicle_link.py as a module."""
    spec = importlib.util.spec_from_file_location(
        "component_vehicle_link",
        SCRIPTS_DIR / "component_vehicle_link.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_link_mod = _load_link_module()


def _make_row(
    campaign_id: str,
    component: str,
    make: str,
    model: str,
    year: int | None,
    report_date: date = date(2025, 2, 1),
):
    """Build a RawRow for testing."""
    return _link_mod.RawRow(
        campaign_id=campaign_id,
        component=component,
        make=make,
        model=model,
        year=year,
        recall_type="V",
        report_received_date=report_date,
    )


# ---------------------------------------------------------------------------
# Positive controls
# ---------------------------------------------------------------------------


def test_subset_detected() -> None:
    """25V001: SEAT BELTS on CAMRY only, AIR BAGS on both -> (a) hit."""
    rows = [
        _make_row("25V001", "SEAT BELTS:PRETENSIONER", "TOYOTA", "CAMRY", 2023),
        _make_row("25V001", "AIR BAGS:FRONTAL", "HONDA", "CIVIC", 2023),
        _make_row("25V001", "AIR BAGS:FRONTAL", "TOYOTA", "CAMRY", 2023),
    ]
    result = _link_mod.analyze_component_vehicle_links(rows)

    assert result.subset_count == 1
    assert any(ex.campaign_id == "25V001" for ex in result.subset_examples)


def test_removed_only_detected() -> None:
    """25V003: FUEL SYSTEM:LINES only on year 2015 (removed) -> (b) hit."""
    rows = [
        _make_row("25V003", "ENGINE", "TOYOTA", "RAV4", 2023),
        _make_row("25V003", "FUEL SYSTEM:LINES", "TOYOTA", "RAV4", 2015),
    ]
    result = _link_mod.analyze_component_vehicle_links(rows)

    assert result.removed_only_count == 1
    assert any(ex.campaign_id == "25V003" for ex in result.removed_only_examples)


# ---------------------------------------------------------------------------
# Negative control
# ---------------------------------------------------------------------------


def test_same_category_all_vehicles_no_hit() -> None:
    """25V002: both components share EXTERIOR LIGHTING, same vehicle -> no hit."""
    rows = [
        _make_row("25V002", "EXTERIOR LIGHTING", "FORD", "F-150", 2023),
        _make_row("25V002", "EXTERIOR LIGHTING:TAIL LIGHTS", "FORD", "F-150", 2023),
    ]
    result = _link_mod.analyze_component_vehicle_links(rows)

    assert result.subset_count == 0
    assert result.removed_only_count == 0
