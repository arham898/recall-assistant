"""Unit tests for recall filtering."""

from datetime import date
from pathlib import Path

from recall_assistant.ingest.filters import filter_recalls
from recall_assistant.ingest.models import Recall, RecallVehicle
from recall_assistant.ingest.parser import parse_recalls


def _make_recall(
    campaign_id: str = "TEST001",
    recall_type: str = "V",
    report_date: date | None = None,
    vehicles: list[RecallVehicle] | None = None,
) -> Recall:
    if vehicles is None:
        vehicles = [RecallVehicle(make="TEST", model="CAR", year=2021)]
    return Recall(
        campaign_id=campaign_id,
        manufacturer="TEST",
        manufacturer_campaign_number="",
        component="ENGINE",
        recall_type=recall_type,
        potential_units_affected=None,
        defect_summary="test",
        consequence_summary="test",
        corrective_action="test",
        notes="",
        report_received_date=report_date,
        record_creation_date=None,
        manufacturing_begin_date=None,
        manufacturing_end_date=None,
        owner_notification_date=None,
        influenced_by="",
        do_not_drive=False,
        park_outside=False,
        vehicles=vehicles,
    )


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


def test_filter_keeps_unknown_year_with_recent_report(sample_rcl_path: Path) -> None:
    """Unknown-year vehicle kept when report date >= min_model_year cutoff.

    24E001 has year=None and report_received_date=2024-03-01.
    With min_model_year=2024, report date (2024-03-01) >= 2024-01-01 → kept.
    """
    recalls, _ = parse_recalls(sample_rcl_path)
    filtered = filter_recalls(recalls, recall_type="E", min_model_year=2024)

    assert len(filtered) == 1
    assert filtered[0].campaign_id == "24E001"
    assert filtered[0].vehicles[0].year is None


# ---------------------------------------------------------------------------
# Report-date fallback policy tests
# ---------------------------------------------------------------------------


def test_known_year_2021_reported_2024_kept():
    """Known model year >= min_model_year → keep regardless of report date."""
    r = _make_recall(
        vehicles=[RecallVehicle(make="T", model="C", year=2021)],
        report_date=date(2024, 6, 1),
    )
    filtered = filter_recalls([r], recall_type="V", min_model_year=2018)
    assert len(filtered) == 1


def test_unknown_year_reported_2010_dropped():
    """Unknown year + old report date → drop."""
    r = _make_recall(
        vehicles=[RecallVehicle(make="W", model="S", year=None)],
        report_date=date(2010, 12, 10),
    )
    filtered = filter_recalls([r], recall_type="V", min_model_year=2018)
    assert len(filtered) == 0


def test_unknown_year_reported_2027_kept():
    """Unknown year + recent report date → keep."""
    r = _make_recall(
        vehicles=[RecallVehicle(make="W", model="S", year=None)],
        report_date=date(2027, 3, 15),
    )
    filtered = filter_recalls([r], recall_type="V", min_model_year=2018)
    assert len(filtered) == 1


def test_known_year_2015_only_reported_2015_dropped():
    """All known years below min + old report date → drop."""
    r = _make_recall(
        vehicles=[RecallVehicle(make="F", model="T", year=2015)],
        report_date=date(2015, 6, 1),
    )
    filtered = filter_recalls([r], recall_type="V", min_model_year=2018)
    assert len(filtered) == 0
