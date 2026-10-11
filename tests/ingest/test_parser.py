"""Unit tests for the NHTSA flat-file parser."""

from datetime import date
from pathlib import Path

from recall_assistant.ingest.parser import parse_recalls


def test_parse_groups_by_campaign(sample_rcl_path: Path) -> None:
    """Rows sharing a campaign number are grouped into one Recall."""
    recalls, _ = parse_recalls(sample_rcl_path)
    campaign_ids = [r.campaign_id for r in recalls]

    # Fixture has 4 campaigns: 24V001, 24V002, 24E001, 24V003
    assert len(recalls) == 4
    assert "24V001" in campaign_ids
    assert "24V002" in campaign_ids
    assert "24E001" in campaign_ids
    assert "24V003" in campaign_ids


def test_parse_vehicles_deduped(sample_rcl_path: Path) -> None:
    """Vehicles within a campaign are de-duplicated by (make, model, year)."""
    recalls, _ = parse_recalls(sample_rcl_path)
    toyota = next(r for r in recalls if r.campaign_id == "24V001")

    # 3 rows: (CAMRY, 2020), (COROLLA, 2021), (CAMRY, 2021) — all distinct
    assert len(toyota.vehicles) == 3
    makes_models = {(v.make, v.model, v.year) for v in toyota.vehicles}
    assert ("TOYOTA", "CAMRY", 2020) in makes_models
    assert ("TOYOTA", "COROLLA", 2021) in makes_models
    assert ("TOYOTA", "CAMRY", 2021) in makes_models


def test_parse_campaign_fields(sample_rcl_path: Path) -> None:
    """Campaign-level fields are correctly extracted."""
    recalls, _ = parse_recalls(sample_rcl_path)
    toyota = next(r for r in recalls if r.campaign_id == "24V001")

    assert toyota.manufacturer == "TOYOTA MOTOR"
    assert toyota.component == "ENGINE"
    assert toyota.recall_type == "V"
    assert toyota.potential_units_affected == 15000
    assert toyota.defect_summary.startswith("Defect in fuel pump")
    assert toyota.consequence_summary.startswith("Engine stall")
    assert toyota.report_received_date == date(2024, 1, 1)
    assert toyota.do_not_drive is True
    assert toyota.park_outside is False


def test_parse_year_9999_becomes_none(sample_rcl_path: Path) -> None:
    """Model year 9999 is parsed as None (unknown)."""
    recalls, _ = parse_recalls(sample_rcl_path)
    graco = next(r for r in recalls if r.campaign_id == "24E001")
    assert graco.vehicles[0].year is None


def test_parse_skips_malformed_rows(sample_rcl_path: Path) -> None:
    """Rows with fewer than 29 columns are skipped and counted."""
    _, skipped = parse_recalls(sample_rcl_path)
    assert skipped == 1  # The "BAD_ROW" line


def test_parse_bool_flags(sample_rcl_path: Path) -> None:
    """do_not_drive and park_outside advisory flags are parsed correctly."""
    recalls, _ = parse_recalls(sample_rcl_path)
    honda = next(r for r in recalls if r.campaign_id == "24V003")
    assert honda.do_not_drive is False
    assert honda.park_outside is True


def test_parse_dates(sample_rcl_path: Path) -> None:
    """Date fields are parsed into date objects."""
    recalls, _ = parse_recalls(sample_rcl_path)
    ford = next(r for r in recalls if r.campaign_id == "24V002")
    assert ford.manufacturing_begin_date == date(2014, 6, 1)
    assert ford.manufacturing_end_date == date(2015, 3, 1)
    assert ford.owner_notification_date == date(2024, 2, 1)
