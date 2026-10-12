"""Filter parsed recalls by type and model year."""

import logging
from dataclasses import replace
from datetime import date

from recall_assistant.ingest.models import Recall

logger = logging.getLogger(__name__)


def filter_recalls(
    recalls: list[Recall],
    recall_type: str = "V",
    min_model_year: int = 2018,
) -> list[Recall]:
    """Keep only recalls matching the type, with relevant vehicles.

    Returns new Recall objects — the input list is not mutated.

    Vehicle filtering:
      - Vehicles with a known year >= min_model_year are kept.
      - Vehicles with year=None (unknown) are kept only if the recall
        qualifies by at least one known-year vehicle or by report date.
      - Vehicles below min_model_year are dropped.

    Recall-level policy:
      - Keep if at least one vehicle has a known year >= min_model_year.
      - Otherwise, keep only if report_received_date is on or after
        1 January of min_model_year (report-date fallback).
      - Otherwise, drop the recall entirely.
    """
    report_date_cutoff = date(min_model_year, 1, 1)

    kept: list[Recall] = []
    for recall in recalls:
        if recall.recall_type != recall_type:
            continue

        filtered_vehicles = [
            v for v in recall.vehicles if v.year is None or v.year >= min_model_year
        ]
        if not filtered_vehicles:
            continue

        has_known_year_in_range = any(
            v.year is not None and v.year >= min_model_year for v in filtered_vehicles
        )
        if not has_known_year_in_range and (
            recall.report_received_date is None
            or recall.report_received_date < report_date_cutoff
        ):
            continue

        kept.append(replace(recall, vehicles=filtered_vehicles))

    logger.info(
        "Filtered %d → %d campaigns (type=%s, min_year=%d)",
        len(recalls),
        len(kept),
        recall_type,
        min_model_year,
    )
    return kept
