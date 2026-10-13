"""Parse the NHTSA recall flat file into Recall records."""

import csv
import logging
import re
from collections import defaultdict
from datetime import date
from pathlib import Path

from recall_assistant.ingest.models import Recall, RecallVehicle

logger = logging.getLogger(__name__)

# Column indices (0-based) matching the RCL.txt field layout.
_COL = {
    "RECORD_ID": 0,
    "CAMPNO": 1,
    "MAKETXT": 2,
    "MODELTXT": 3,
    "YEARTXT": 4,
    "MFGCAMPNO": 5,
    "COMPNAME": 6,
    "MFGNAME": 7,
    "BGMAN": 8,
    "ENDMAN": 9,
    "RCLTYPECD": 10,
    "POTAFF": 11,
    "ODATE": 12,
    "INFLUENCED_BY": 13,
    "MFGTXT": 14,
    "RCDATE": 15,
    "DATEA": 16,
    "RPNO": 17,
    "FMVSS": 18,
    "DESC_DEFECT": 19,
    "CONEQUENCE_DEFECT": 20,
    "CORRECTIVE_ACTION": 21,
    "NOTES": 22,
    "RCL_CMPT_ID": 23,
    "MFR_COMP_NAME": 24,
    "MFR_COMP_DESC": 25,
    "MFR_COMP_PTNO": 26,
    "DO_NOT_DRIVE": 27,
    "PARK_OUTSIDE": 28,
}

EXPECTED_COLUMNS = 29


_ANCHOR_TAG_RE = re.compile(r"</?[aA]\b[^>]*>", re.IGNORECASE)


def strip_anchor_tags(text: str) -> str:
    """Remove <a ...> and </a> tags, keeping the visible text.

    Only targets anchor tags so that text like "SPEEDS <5 MPH" or
    "PUMP&REGULATOR" passes through unchanged.
    """
    return _ANCHOR_TAG_RE.sub("", text)


def _parse_date(value: str) -> date | None:
    """Parse an 8-digit YYYYMMDD string into a date, or None if empty/invalid."""
    value = value.strip()
    if not value or len(value) != 8:
        return None
    try:
        return date(int(value[:4]), int(value[4:6]), int(value[6:8]))
    except ValueError:
        return None


def _parse_int(value: str) -> int | None:
    """Parse a numeric string into an int, or None if empty/invalid."""
    value = value.strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _parse_year(value: str) -> int | None:
    """Parse model year; returns None for 9999 (unknown/N/A)."""
    year = _parse_int(value)
    if year == 9999:
        return None
    return year


def _parse_bool_flag(value: str) -> bool:
    """Parse Yes/No advisory flag."""
    return value.strip().upper() == "YES"


def _row_to_vehicle(row: list[str]) -> RecallVehicle:
    return RecallVehicle(
        make=row[_COL["MAKETXT"]].strip(),
        model=row[_COL["MODELTXT"]].strip(),
        year=_parse_year(row[_COL["YEARTXT"]]),
    )


def _row_to_recall_stub(row: list[str]) -> Recall:
    """Build a Recall from campaign-level fields (vehicles list starts empty)."""
    return Recall(
        campaign_id=row[_COL["CAMPNO"]].strip(),
        manufacturer=row[_COL["MFGNAME"]].strip(),
        manufacturer_campaign_number=row[_COL["MFGCAMPNO"]].strip(),
        component=row[_COL["COMPNAME"]].strip(),
        recall_type=row[_COL["RCLTYPECD"]].strip(),
        potential_units_affected=_parse_int(row[_COL["POTAFF"]]),
        defect_summary=strip_anchor_tags(row[_COL["DESC_DEFECT"]].strip()),
        consequence_summary=strip_anchor_tags(row[_COL["CONEQUENCE_DEFECT"]].strip()),
        corrective_action=strip_anchor_tags(row[_COL["CORRECTIVE_ACTION"]].strip()),
        notes=strip_anchor_tags(row[_COL["NOTES"]].strip()),
        report_received_date=_parse_date(row[_COL["RCDATE"]]),
        record_creation_date=_parse_date(row[_COL["DATEA"]]),
        manufacturing_begin_date=_parse_date(row[_COL["BGMAN"]]),
        manufacturing_end_date=_parse_date(row[_COL["ENDMAN"]]),
        owner_notification_date=_parse_date(row[_COL["ODATE"]]),
        influenced_by=row[_COL["INFLUENCED_BY"]].strip(),
        do_not_drive=_parse_bool_flag(row[_COL["DO_NOT_DRIVE"]]),
        park_outside=_parse_bool_flag(row[_COL["PARK_OUTSIDE"]]),
        vehicles=[],
    )


def parse_recalls(path: Path) -> tuple[list[Recall], int]:
    """Parse the NHTSA tab-delimited flat file and group rows by campaign.

    Returns (recalls, skipped_count).  Malformed rows are counted and
    logged rather than raising.
    """
    # First pass: group rows by campaign number.
    campaigns: dict[str, Recall] = {}
    seen_vehicles: dict[str, set[tuple[str, str, int | None]]] = defaultdict(set)
    skipped = 0

    with open(path, encoding="latin-1", newline="") as f:
        reader = csv.reader(f, delimiter="\t")
        for line_num, row in enumerate(reader, start=1):
            if len(row) < EXPECTED_COLUMNS:
                logger.warning(
                    "Row %d: expected %d columns, got %d — skipping",
                    line_num,
                    EXPECTED_COLUMNS,
                    len(row),
                )
                skipped += 1
                continue

            campaign_id = row[_COL["CAMPNO"]].strip()
            if not campaign_id:
                logger.warning("Row %d: empty campaign number — skipping", line_num)
                skipped += 1
                continue

            if campaign_id not in campaigns:
                try:
                    campaigns[campaign_id] = _row_to_recall_stub(row)
                except Exception:
                    logger.warning(
                        "Row %d: failed to parse campaign %s — skipping",
                        line_num,
                        campaign_id,
                        exc_info=True,
                    )
                    skipped += 1
                    continue

            # Add vehicle if not already seen for this campaign.
            vehicle = _row_to_vehicle(row)
            key = (vehicle.make, vehicle.model, vehicle.year)
            if key not in seen_vehicles[campaign_id]:
                seen_vehicles[campaign_id].add(key)
                campaigns[campaign_id].vehicles.append(vehicle)

    recalls = list(campaigns.values())
    logger.info(
        "Parsed %d campaigns from %d rows (%d skipped)",
        len(recalls),
        line_num,
        skipped,
    )
    return recalls, skipped
