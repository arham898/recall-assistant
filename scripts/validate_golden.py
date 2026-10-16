"""Validate the golden evaluation set against the database.

Reads evals/generated.jsonl and evals/handwritten.jsonl, checks them
against the real database through Settings.

Usage:
    uv run python scripts/validate_golden.py
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path

import psycopg

from recall_assistant.config import Settings
from recall_assistant.evals.gold import build_group_where
from recall_assistant.evals.golden import GoldenRow
from recall_assistant.evals.leakage import check_leakage
from recall_assistant.evals.mappings import COMPONENT_GROUPS

GENERATED_PATH = Path("evals/generated.jsonl")
HANDWRITTEN_PATH = Path("evals/handwritten.jsonl")

TARGET_GENERATED = 100
TARGET_HANDWRITTEN = 30
TARGET_UNANSWERABLE = 20


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def _load_jsonl(path: Path) -> list[GoldenRow]:
    """Load and parse a JSONL file into GoldenRow objects."""
    if not path.exists():
        return []
    rows: list[GoldenRow] = []
    with open(path, encoding="utf-8") as f:
        for line_no, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            data = json.loads(line)
            row = GoldenRow.model_validate(data)
            rows.append(row)
    return rows


def check_generated_file(path: Path) -> list[str]:
    """Check that generated.jsonl exists and is non-empty."""
    errors: list[str] = []
    if not path.exists():
        errors.append(f"{path} not found")
    else:
        rows = _load_jsonl(path)
        if not rows:
            errors.append(f"{path} is empty")
    return errors


# ---------------------------------------------------------------------------
# Check functions (each takes a connection argument)
# ---------------------------------------------------------------------------


def check_db_not_empty(conn: psycopg.Connection) -> list[str]:
    """Fail loudly if the database has no recalls."""
    errors: list[str] = []
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM recalls")
        count = cur.fetchone()[0]
        if count == 0:
            errors.append("Database is empty: recalls table has 0 rows")
        cur.execute("SELECT COUNT(*) FROM recall_vehicles")
        count = cur.fetchone()[0]
        if count == 0:
            errors.append("Database is empty: recall_vehicles table has 0 rows")
    return errors


def check_unique_ids(rows: list[GoldenRow]) -> list[str]:
    """Check that all IDs are unique."""
    errors: list[str] = []
    seen: set[str] = set()
    for r in rows:
        if r.id in seen:
            errors.append(f"Duplicate ID: {r.id}")
        seen.add(r.id)
    return errors


def _normalise(q: str) -> str:
    """Lower-case and collapse whitespace."""
    return re.sub(r"\s+", " ", q.lower()).strip()


def check_unique_questions(rows: list[GoldenRow]) -> list[str]:
    """Check for duplicate questions after normalisation."""
    errors: list[str] = []
    seen: dict[str, str] = {}
    for r in rows:
        norm = _normalise(r.question)
        if norm in seen:
            errors.append(f"Duplicate question: {r.id} matches {seen[norm]}")
        seen[norm] = r.id
    return errors


def check_no_shared_gold(rows: list[GoldenRow]) -> list[str]:
    """Check that no campaign is gold in more than one generated row."""
    errors: list[str] = []
    seen: dict[str, str] = {}
    for r in rows:
        if r.source != "generated":
            continue
        for cid in r.gold_campaign_ids:
            if cid in seen:
                errors.append(f"Campaign {cid} is gold in both {seen[cid]} and {r.id}")
            else:
                seen[cid] = r.id
    return errors


def check_gold_ids_exist(conn: psycopg.Connection, rows: list[GoldenRow]) -> list[str]:
    """Check that every gold campaign ID exists in recalls."""
    errors: list[str] = []
    all_ids: set[str] = set()
    for r in rows:
        all_ids.update(r.gold_campaign_ids)
    if not all_ids:
        return errors
    placeholders = ",".join(["%s"] * len(all_ids))
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT campaign_id FROM recalls WHERE campaign_id IN ({placeholders})",
            list(all_ids),
        )
        found = {row[0] for row in cur.fetchall()}
    missing = all_ids - found
    for cid in sorted(missing):
        owners = [r.id for r in rows if cid in r.gold_campaign_ids]
        errors.append(f"Gold campaign {cid} not in recalls (used by {owners})")
    return errors


def check_gold_drift(conn: psycopg.Connection, rows: list[GoldenRow]) -> list[str]:
    """For generated rows, recompute gold set and compare."""
    errors: list[str] = []
    group_lookup = {g.key: g for g in COMPONENT_GROUPS}

    for r in rows:
        if r.source != "generated":
            continue
        if r.make is None or r.model is None or r.year is None:
            continue

        if r.template == "all_recalls":
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT DISTINCT campaign_id FROM recall_vehicles
                       WHERE make = %s AND model = %s AND year = %s
                       ORDER BY campaign_id""",
                    (r.make, r.model, r.year),
                )
                expected = {row[0] for row in cur.fetchall()}
        elif r.component_group and r.component_group in group_lookup:
            grp = group_lookup[r.component_group]
            group_where, group_params = build_group_where(
                grp.categories, grp.excluded_subs
            )
            with conn.cursor() as cur:
                cur.execute(
                    f"""SELECT DISTINCT rc.campaign_id
                        FROM recall_components rc
                        JOIN recall_vehicles rv ON rc.campaign_id = rv.campaign_id
                        WHERE {group_where}
                          AND rv.make = %s AND rv.model = %s AND rv.year = %s
                        ORDER BY rc.campaign_id""",
                    group_params + [r.make, r.model, r.year],
                )
                expected = {row[0] for row in cur.fetchall()}
        else:
            continue

        stored = set(r.gold_campaign_ids)
        if stored != expected:
            errors.append(
                f"Gold drift for {r.id}: stored={sorted(stored)} "
                f"expected={sorted(expected)}"
            )

    return errors


def check_vehicles_exist(
    conn: psycopg.Connection, rows: list[GoldenRow]
) -> tuple[list[str], list[str]]:
    """Check that make/model/year vehicles exist in recall_vehicles.

    Returns (errors, warnings). For unanswerable rows, missing vehicle
    is a warning listing the row, not an error.
    """
    errors: list[str] = []
    warnings: list[str] = []
    for r in rows:
        if r.make is None or r.model is None or r.year is None:
            continue
        with conn.cursor() as cur:
            cur.execute(
                """SELECT 1 FROM recall_vehicles
                   WHERE make = %s AND model = %s AND year = %s LIMIT 1""",
                (r.make, r.model, r.year),
            )
            exists = cur.fetchone() is not None

        if not exists:
            msg = (
                f"Vehicle ({r.make}, {r.model}, {r.year}) not in "
                f"recall_vehicles (row {r.id})"
            )
            if r.source == "unanswerable":
                warnings.append(msg)
            else:
                errors.append(msg)

    return errors, warnings


def check_unanswerable(rows: list[GoldenRow]) -> list[str]:
    """Unanswerable rows must have empty gold IDs and decline pass rule."""
    errors: list[str] = []
    for r in rows:
        if r.source != "unanswerable":
            continue
        if r.gold_campaign_ids:
            errors.append(
                f"Unanswerable row {r.id} has gold_campaign_ids: {r.gold_campaign_ids}"
            )
        if r.pass_rule != "decline":
            errors.append(
                f"Unanswerable row {r.id} has pass_rule={r.pass_rule}, "
                f"expected 'decline'"
            )
    return errors


def check_leakage_rows(
    conn: psycopg.Connection, rows: list[GoldenRow]
) -> tuple[list[str], list[str]]:
    """Run leakage check. Generated rows are errors; others are warnings."""
    all_ids: set[str] = set()
    for r in rows:
        all_ids.update(r.gold_campaign_ids)
    if not all_ids:
        return [], []

    recall_texts = _fetch_recall_texts(conn, all_ids)
    flagged = check_leakage(rows, recall_texts)

    errors: list[str] = []
    warnings: list[str] = []
    for row, field, gram in flagged:
        msg = f"Leakage in {row.id}: field={field} gram={' '.join(gram)}"
        if row.source == "generated":
            errors.append(msg)
        else:
            warnings.append(msg)
    return errors, warnings


def _fetch_recall_texts(
    conn: psycopg.Connection, campaign_ids: set[str]
) -> dict[str, dict[str, str]]:
    """Fetch text fields for leakage checking."""
    if not campaign_ids:
        return {}
    result: dict[str, dict[str, str]] = {}
    placeholders = ",".join(["%s"] * len(campaign_ids))

    with conn.cursor() as cur:
        cur.execute(
            f"""SELECT campaign_id, defect_summary, consequence_summary,
                       corrective_action
                FROM recalls WHERE campaign_id IN ({placeholders})""",
            list(campaign_ids),
        )
        for cid, defect, consequence, corrective in cur.fetchall():
            result[cid] = {
                "defect_summary": defect,
                "consequence_summary": consequence,
                "corrective_action": corrective,
            }

        cur.execute(
            f"""SELECT campaign_id, component
                FROM recall_components WHERE campaign_id IN ({placeholders})""",
            list(campaign_ids),
        )
        for cid, comp in cur.fetchall():
            existing = result.get(cid, {})
            if "component" in existing:
                existing["component"] += " " + comp
            else:
                existing["component"] = comp
            result[cid] = existing

    return result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")

    settings = Settings()
    conn = psycopg.connect(settings.database_url)

    all_errors: list[str] = []
    all_warnings: list[str] = []

    try:
        # Check DB not empty
        db_errors = check_db_not_empty(conn)
        if db_errors:
            for e in db_errors:
                print(f"FATAL: {e}")
            sys.exit(1)

        # Load files — missing or empty generated.jsonl is an error
        gen_errors = check_generated_file(GENERATED_PATH)
        if gen_errors:
            for e in gen_errors:
                print(f"FATAL: {e}")
            sys.exit(1)
        generated = _load_jsonl(GENERATED_PATH)
        handwritten = _load_jsonl(HANDWRITTEN_PATH)
        all_rows = generated + handwritten

        # Run checks
        all_errors.extend(check_unique_ids(all_rows))
        all_errors.extend(check_unique_questions(all_rows))
        all_errors.extend(check_no_shared_gold(all_rows))
        all_errors.extend(check_gold_ids_exist(conn, all_rows))
        all_errors.extend(check_gold_drift(conn, all_rows))

        vehicle_errors, vehicle_warnings = check_vehicles_exist(conn, all_rows)
        all_errors.extend(vehicle_errors)
        all_warnings.extend(vehicle_warnings)

        all_errors.extend(check_unanswerable(all_rows))

        leakage_errors, leakage_warnings = check_leakage_rows(conn, all_rows)
        all_errors.extend(leakage_errors)
        all_warnings.extend(leakage_warnings)

        # --- Print summary ---
        print("=" * 60)
        print("GOLDEN SET VALIDATION SUMMARY")
        print("=" * 60)

        # Counts by source
        source_counts: Counter[str] = Counter()
        for r in all_rows:
            source_counts[r.source] += 1
        print("\nCounts by source:")
        for src in ("generated", "handwritten", "unanswerable"):
            print(f"  {src}: {source_counts.get(src, 0)}")

        # Counts by template
        template_counts: Counter[str | None] = Counter()
        for r in all_rows:
            template_counts[r.template] += 1
        print("\nCounts by template:")
        for t, cnt in template_counts.most_common():
            print(f"  {t or '(none)'}: {cnt}")

        # Counts by group
        group_counts: Counter[str | None] = Counter()
        for r in all_rows:
            group_counts[r.component_group] += 1
        print("\nCounts by component group:")
        for g, cnt in group_counts.most_common():
            print(f"  {g or '(none)'}: {cnt}")

        # Counts by make
        make_counts: Counter[str | None] = Counter()
        for r in all_rows:
            make_counts[r.make] += 1
        print("\nCounts by make (top 20):")
        for m, cnt in make_counts.most_common(20):
            print(f"  {m or '(none)'}: {cnt}")

        # Counts by model year
        year_counts: Counter[int | None] = Counter()
        for r in all_rows:
            year_counts[r.year] += 1
        print("\nCounts by model year:")
        for y in sorted(k for k in year_counts if k is not None):
            print(f"  {y}: {year_counts[y]}")
        if None in year_counts:
            print(f"  (none): {year_counts[None]}")

        # Multi-gold rows
        multi_gold = sum(1 for r in all_rows if len(r.gold_campaign_ids) > 1)
        print(f"\nRows with >1 gold ID: {multi_gold}")

        # Shortfall warnings
        n_gen = source_counts.get("generated", 0)
        n_hand = source_counts.get("handwritten", 0)
        n_unanswerable = source_counts.get("unanswerable", 0)
        if n_gen < TARGET_GENERATED:
            all_warnings.append(f"Shortfall: {n_gen}/{TARGET_GENERATED} generated")
        if n_hand < TARGET_HANDWRITTEN:
            all_warnings.append(f"Shortfall: {n_hand}/{TARGET_HANDWRITTEN} handwritten")
        if n_unanswerable < TARGET_UNANSWERABLE:
            all_warnings.append(
                f"Shortfall: {n_unanswerable}/{TARGET_UNANSWERABLE} unanswerable"
            )

        # Print results
        if all_warnings:
            print(f"\n--- {len(all_warnings)} warnings ---")
            for w in all_warnings:
                print(f"  WARNING: {w}")

        if all_errors:
            print(f"\n--- {len(all_errors)} errors ---")
            for e in all_errors:
                print(f"  ERROR: {e}")
            sys.exit(1)
        else:
            print("\nAll checks passed.")

    finally:
        conn.close()


if __name__ == "__main__":
    main()
