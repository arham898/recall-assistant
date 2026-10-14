"""Analyze row-level component-vehicle pairing in the raw NHTSA file.

For kept campaigns, reports:
  (a) campaigns with a top-level category on some but not all kept vehicles
  (b) campaigns with a top-level category only on vehicles the filter removed

Usage:
    uv run python scripts/component_vehicle_link.py
"""

import csv
import logging
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from recall_assistant.ingest.parser import COL, EXPECTED_COLUMNS, parse_date, parse_year

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RawRow:
    """One row from the raw file with fields needed for analysis."""

    campaign_id: str
    component: str
    make: str
    model: str
    year: int | None
    recall_type: str
    report_received_date: date | None


@dataclass(frozen=True)
class LinkExample:
    """One example for the report."""

    campaign_id: str
    category: str
    category_vehicles: list[tuple[str, str, int | None]]
    other_vehicles: list[tuple[str, str, int | None]]


@dataclass
class LinkResult:
    """Result of the component-vehicle link analysis."""

    kept_campaigns: int = 0
    kept_vehicle_entries: int = 0
    subset_count: int = 0
    removed_only_count: int = 0
    subset_examples: list[LinkExample] = field(default_factory=list)
    removed_only_examples: list[LinkExample] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Core analysis function
# ---------------------------------------------------------------------------


def analyze_component_vehicle_links(
    raw_rows: list[RawRow],
    min_model_year: int = 2018,
    recall_type: str = "V",
    max_examples: int = 3,
) -> LinkResult:
    """Analyze row-level component-vehicle pairing for kept campaigns.

    Applies the same filter as load_recalls.py: keep only recall_type
    campaigns, drop vehicles with known year < min_model_year, keep
    unknown-year vehicles only if the campaign qualifies by known year
    or report date.
    """
    # Group rows by campaign
    by_campaign: dict[str, list[RawRow]] = defaultdict(list)
    for row in raw_rows:
        by_campaign[row.campaign_id].append(row)

    report_date_cutoff = date(min_model_year, 1, 1)
    result = LinkResult()
    subset_hits: list[LinkExample] = []
    removed_only_hits: list[LinkExample] = []

    for cid in sorted(by_campaign):
        rows = by_campaign[cid]

        # Filter by recall type
        if rows[0].recall_type != recall_type:
            continue

        # Collect unique vehicles
        all_vehicles: set[tuple[str, str, int | None]] = set()
        for r in rows:
            all_vehicles.add((r.make, r.model, r.year))

        # Apply vehicle year filter
        filtered_vehicles = {
            v for v in all_vehicles if v[2] is None or v[2] >= min_model_year
        }
        if not filtered_vehicles:
            continue

        has_known_in_range = any(
            v[2] is not None and v[2] >= min_model_year for v in filtered_vehicles
        )
        if not has_known_in_range:
            rpt = rows[0].report_received_date
            if rpt is None or rpt < report_date_cutoff:
                continue

        # This campaign is kept
        result.kept_campaigns += 1
        result.kept_vehicle_entries += len(filtered_vehicles)

        # Group kept and removed vehicles by top-level category, using
        # the RAW ROW pairing (not a cross-product)
        cat_kept: dict[str, set[tuple[str, str, int | None]]] = defaultdict(set)
        cat_removed: dict[str, set[tuple[str, str, int | None]]] = defaultdict(set)

        for r in rows:
            cat = r.component.split(":", 1)[0] if ":" in r.component else r.component
            veh = (r.make, r.model, r.year)
            if veh in filtered_vehicles:
                cat_kept[cat].add(veh)
            else:
                cat_removed[cat].add(veh)

        # Check (a): category on some but not all kept vehicles
        for cat, cat_kv in cat_kept.items():
            if (
                cat_kv
                and cat_kv < filtered_vehicles
                and cid not in {e.campaign_id for e in subset_hits}
            ):
                result.subset_count += 1
                if len(subset_hits) < max_examples:
                    subset_hits.append(
                        LinkExample(
                            campaign_id=cid,
                            category=cat,
                            category_vehicles=sorted(cat_kv),
                            other_vehicles=sorted(filtered_vehicles - cat_kv),
                        )
                    )
                break

        # Check (b): category only on removed vehicles
        for cat, cat_rv in cat_removed.items():
            if cat not in cat_kept and cid not in {
                e.campaign_id for e in removed_only_hits
            }:
                result.removed_only_count += 1
                if len(removed_only_hits) < max_examples:
                    removed_only_hits.append(
                        LinkExample(
                            campaign_id=cid,
                            category=cat,
                            category_vehicles=sorted(cat_rv),
                            other_vehicles=sorted(filtered_vehicles),
                        )
                    )
                break

    result.subset_examples = subset_hits
    result.removed_only_examples = removed_only_hits
    return result


# ---------------------------------------------------------------------------
# File reading
# ---------------------------------------------------------------------------


def read_raw_rows(path: Path) -> list[RawRow]:
    """Read the raw NHTSA file into RawRow objects."""
    rows: list[RawRow] = []
    with open(path, encoding="latin-1", newline="") as f:
        for row in csv.reader(f, delimiter="\t"):
            if len(row) < EXPECTED_COLUMNS:
                continue
            cid = row[COL["CAMPNO"]].strip()
            if not cid:
                continue
            rows.append(
                RawRow(
                    campaign_id=cid,
                    component=row[COL["COMPNAME"]].strip(),
                    make=row[COL["MAKETXT"]].strip(),
                    model=row[COL["MODELTXT"]].strip(),
                    year=parse_year(row[COL["YEARTXT"]]),
                    recall_type=row[COL["RCLTYPECD"]].strip(),
                    report_received_date=parse_date(row[COL["RCDATE"]]),
                )
            )
    return rows


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    data_dir = Path(__file__).resolve().parent.parent / "data" / "raw"
    flat_files = list(data_dir.glob("FLAT_RCL*.txt"))
    if not flat_files:
        logger.error("No FLAT_RCL*.txt found in %s", data_dir)
        sys.exit(1)

    path = flat_files[0]
    logger.info("Reading %s ...", path.name)
    raw_rows = read_raw_rows(path)
    logger.info("Read %d raw rows", len(raw_rows))

    result = analyze_component_vehicle_links(raw_rows)

    print(f"Kept campaigns:        {result.kept_campaigns:,}")
    print(f"Kept vehicle entries:  {result.kept_vehicle_entries:,}")
    print()
    print(
        f"(a) Campaigns with a top-level category on some "
        f"but not all kept vehicles: {result.subset_count}"
    )
    for ex in result.subset_examples:
        print(f"  {ex.campaign_id}: category={ex.category!r}")
        print(f"    on: {ex.category_vehicles}")
        print(f"    not on: {ex.other_vehicles}")
    print()
    print(
        f"(b) Campaigns with a top-level category only on "
        f"removed vehicles: {result.removed_only_count}"
    )
    for ex in result.removed_only_examples:
        print(f"  {ex.campaign_id}: category={ex.category!r}")
        print(f"    removed vehicles: {ex.category_vehicles}")
        print(f"    kept vehicles: {ex.other_vehicles}")


if __name__ == "__main__":
    main()
