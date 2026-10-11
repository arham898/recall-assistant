"""Filter parsed recalls by type and model year."""

import logging
from dataclasses import replace

from recall_assistant.ingest.models import Recall

logger = logging.getLogger(__name__)


def filter_recalls(
    recalls: list[Recall],
    recall_type: str = "V",
    min_model_year: int = 2018,
) -> list[Recall]:
    """Keep only recalls matching the type, with at least one vehicle in range.

    Returns new Recall objects — the input list is not mutated.
    Vehicles with year=None (unknown/9999) are kept — they may still be relevant.
    Vehicles below min_model_year are dropped from each recall's vehicle list.
    Recalls with no remaining vehicles after filtering are dropped entirely.
    """
    kept: list[Recall] = []
    for recall in recalls:
        if recall.recall_type != recall_type:
            continue

        filtered_vehicles = [
            v for v in recall.vehicles if v.year is None or v.year >= min_model_year
        ]
        if not filtered_vehicles:
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
