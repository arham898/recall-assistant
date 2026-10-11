"""Unit tests for recall filtering."""

from pathlib import Path

from recall_assistant.ingest.filters import filter_recalls
from recall_assistant.ingest.parser import parse_recalls


def test_filter_keeps_only_vehicle_type(sample_rcl_path: Path) -> None:
    """Equipment recalls (type E) are excluded when filtering for V."""
    recalls, _ = parse_recalls(sample_rcl_path)
    filtered = filter_recalls(recalls, recall_type="V", min_model_year=2000)

    campaign_ids = {r.campaign_id for r in filtered}
    assert "24E001" not in campaign_ids
    assert "24V001" in campaign_ids
    assert "24V002" in campaign_ids
    assert "24V003" in campaign_ids


def test_filter_drops_old_model_years(sample_rcl_path: Path) -> None:
    """Vehicles below min_model_year are removed from campaigns."""
    recalls, _ = parse_recalls(sample_rcl_path)
    filtered = filter_recalls(recalls, recall_type="V", min_model_year=2018)

    # 24V002 has F-150 2015 and F-150 2019.
    # After filtering, only the 2019 vehicle should remain.
    ford = next(r for r in filtered if r.campaign_id == "24V002")
    assert len(ford.vehicles) == 1
    assert ford.vehicles[0].year == 2019


def test_filter_drops_campaign_with_no_qualifying_vehicles(
    sample_rcl_path: Path,
) -> None:
    """A campaign is dropped entirely if no vehicles meet the year threshold."""
    recalls, _ = parse_recalls(sample_rcl_path)
    # Set min year very high so only 24V003 (2022) survives
    filtered = filter_recalls(recalls, recall_type="V", min_model_year=2022)

    campaign_ids = {r.campaign_id for r in filtered}
    assert "24V003" in campaign_ids
    # 24V001 has 2020, 2021 — both below 2022
    assert "24V001" not in campaign_ids


def test_filter_keeps_unknown_year(sample_rcl_path: Path) -> None:
    """Vehicles with year=None (9999 in source) are kept regardless of min year."""
    recalls, _ = parse_recalls(sample_rcl_path)
    # Change filter to E type so we can test the equipment recall with year=None
    filtered = filter_recalls(recalls, recall_type="E", min_model_year=2025)

    assert len(filtered) == 1
    assert filtered[0].campaign_id == "24E001"
    assert filtered[0].vehicles[0].year is None
