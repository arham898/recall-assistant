"""Generate the golden question set from structured recall data.

No LLM calls — questions are built from templates and database fields.

Usage:
    uv run python scripts/make_generated_questions.py [--dry-run]
"""

from __future__ import annotations

import argparse
import random
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import NamedTuple

import psycopg

from recall_assistant.config import Settings
from recall_assistant.evals.gold import build_group_where
from recall_assistant.evals.golden import GoldenRow, ReferenceFact
from recall_assistant.evals.leakage import check_leakage
from recall_assistant.evals.mappings import COMPONENT_GROUPS

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SEED = 42
MAX_PER_MAKE = 5
MAX_GOLD = 10
EXCLUDED_MAKES = ("JAGUAR", "LAND ROVER")
# NHTSA uses 9999 for unknown model year; the loader converts it to NULL
# (see src/recall_assistant/ingest/parser.py lines 84-89) but we guard
# against raw values reaching the DB.
UNKNOWN_MODEL_YEAR = 9999
TOTAL_PER_TEMPLATE = 20
OUTPUT_PATH = Path("evals/generated.jsonl")

# Full-string overrides, checked before any splitting.
DISPLAY_OVERRIDES: dict[str, str] = {
    "4RUNNER": "4Runner",
    "E-ONE": "E-ONE",
}

# Part-level overrides: exact upper-case part -> display form.
# Applied after splitting on spaces/hyphens, before the vowel rule.
_PART_OVERRIDES: dict[str, str] = {
    "330I": "330i",
    "BEV": "BEV",
    "EDV": "EDV",
    "ETC": "ETC",
    "ETI": "ETI",
    "FLHTCUTG": "FLHTCUTG",
    "IN": "in",
    "MAC": "MAC",
    "RAM": "RAM",
}

COMPONENT_TEMPLATES = ("remedy", "consequence", "existence", "count")

# Minimum gold recalls for a candidate to be eligible for a template.
# Count and all_recalls require at least 2 so the question is non-trivial.
MIN_GOLD_FOR_TEMPLATE: dict[str, int] = {
    "remedy": 1,
    "consequence": 1,
    "existence": 1,
    "count": 2,
    "all_recalls": 2,
}

# Template -> (question pattern, ref_facts DB field)
TEMPLATE_CONFIG: dict[str, tuple[str, str]] = {
    "remedy": (
        (
            "What's the fix for the {modifier} {recall_word} on the"
            " {year} {make} {model}?"
        ),
        "corrective_action",
    ),
    "consequence": (
        (
            "What's the risk from the {modifier} {recall_word} on the"
            " {year} {make} {model}?"
        ),
        "consequence_summary",
    ),
    "existence": (
        "Are there any {modifier} recalls for the {year} {make} {model}?",
        "defect_summary",
    ),
    "count": (
        "How many {modifier} recalls apply to the {year} {make} {model}?",
        "defect_summary",
    ),
    "all_recalls": (
        "What recalls affect the {year} {make} {model}?",
        "defect_summary",
    ),
}


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


class Candidate(NamedTuple):
    """One (make, model, year) with its gold campaign IDs for a group."""

    make: str
    model: str
    year: int
    gold_ids: tuple[str, ...]


# ---------------------------------------------------------------------------
# Display name formatting
# ---------------------------------------------------------------------------

_PART_SPLIT = re.compile(r"(?<=[ -])(?=[^ -])|(?<=[^ -])(?=[ -])")


_VOWELS = set("aeiouAEIOU")


def display_name(raw: str) -> str:
    """Format an upper-cased make/model for display.

    Split on spaces and hyphens.  Title-case a part only if it is
    all-letters, 3+ characters, and contains a vowel.  Part-level
    overrides (brand names, model codes) are checked first.
    """
    if raw in DISPLAY_OVERRIDES:
        return DISPLAY_OVERRIDES[raw]
    parts = _PART_SPLIT.split(raw)
    result: list[str] = []
    for part in parts:
        if part in (" ", "-"):
            result.append(part)
        elif part in _PART_OVERRIDES:
            result.append(_PART_OVERRIDES[part])
        elif part.isalpha() and len(part) >= 3 and (set(part) & _VOWELS):
            result.append(part.capitalize())
        else:
            result.append(part)
    return "".join(result)


# ---------------------------------------------------------------------------
# Quota allocation
# ---------------------------------------------------------------------------


def allocate_quotas(
    rng: random.Random,
) -> dict[str, dict[str, int]]:
    """Allocate component-template quotas: 5 per group, 20 per template.

    Returns {group_key: {template: count}}.
    Every group gets exactly 1 question per template (= 4 base) plus
    exactly 1 extra.  Each template gets exactly 4 extras (16 / 4).
    """
    # Start with 1 per (group, template)
    quotas: dict[str, dict[str, int]] = {
        g.key: {t: 1 for t in COMPONENT_TEMPLATES} for g in COMPONENT_GROUPS
    }

    # Assign extras: each group gets 1, each template gets 4
    group_keys = [g.key for g in COMPONENT_GROUPS]
    templates = list(COMPONENT_TEMPLATES)

    # Build a bag of 16 template slots: 4 of each template
    extra_slots = templates * 4  # 16 slots
    rng.shuffle(extra_slots)

    # Shuffle group order too
    shuffled_groups = group_keys[:]
    rng.shuffle(shuffled_groups)

    # Assign one extra slot to each group
    for group_key, template in zip(shuffled_groups, extra_slots):
        quotas[group_key][template] += 1

    return quotas


# ---------------------------------------------------------------------------
# Database queries
# ---------------------------------------------------------------------------


def _fetch_group_candidates(
    conn: psycopg.Connection,
    group_key: str,
    categories: tuple[str, ...],
    excluded_subs: tuple = (),
) -> list[Candidate]:
    """Fetch (make, model, year, gold_ids) for a component group.

    Year filter and EXCLUDED_MAKES are applied in SQL only.
    Results are ORDER BY make, model, year, campaign_id for determinism.
    """
    group_where, group_params = build_group_where(categories, excluded_subs)
    excluded = ",".join(["%s"] * len(EXCLUDED_MAKES))
    sql = f"""
        SELECT rv.make, rv.model, rv.year, array_agg(DISTINCT rc.campaign_id ORDER BY rc.campaign_id)
        FROM recall_components rc
        JOIN recall_vehicles rv ON rc.campaign_id = rv.campaign_id
        WHERE {group_where}
          AND rv.year >= 2018
          AND rv.year != %s
          AND rv.make NOT IN ({excluded})
        GROUP BY rv.make, rv.model, rv.year
        ORDER BY rv.make, rv.model, rv.year
    """
    params = group_params + [UNKNOWN_MODEL_YEAR] + list(EXCLUDED_MAKES)
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return [
            Candidate(make, model, year, tuple(ids))
            for make, model, year, ids in cur.fetchall()
        ]


def _fetch_all_recalls_candidates(
    conn: psycopg.Connection,
) -> list[Candidate]:
    """Fetch (make, model, year, gold_ids) across all categories.

    Year filter and EXCLUDED_MAKES in SQL only.
    """
    excluded = ",".join(["%s"] * len(EXCLUDED_MAKES))
    sql = f"""
        SELECT rv.make, rv.model, rv.year, array_agg(DISTINCT rv.campaign_id ORDER BY rv.campaign_id)
        FROM recall_vehicles rv
        WHERE rv.year >= 2018
          AND rv.year != %s
          AND rv.make NOT IN ({excluded})
        GROUP BY rv.make, rv.model, rv.year
        ORDER BY rv.make, rv.model, rv.year
    """
    with conn.cursor() as cur:
        cur.execute(sql, [UNKNOWN_MODEL_YEAR] + list(EXCLUDED_MAKES))
        return [
            Candidate(make, model, year, tuple(ids))
            for make, model, year, ids in cur.fetchall()
        ]


def _fetch_recall_text(
    conn: psycopg.Connection,
    campaign_id: str,
    field: str,
) -> str:
    """Fetch a single text field from the recalls table."""
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {field} FROM recalls WHERE campaign_id = %s",
            (campaign_id,),
        )
        row = cur.fetchone()
        return row[0] if row else ""


def _fetch_recall_texts_for_leakage(
    conn: psycopg.Connection,
    campaign_ids: set[str],
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


def _fetch_group_recall_count(
    conn: psycopg.Connection,
    categories: tuple[str, ...],
    excluded_subs: tuple = (),
) -> int:
    """Count distinct recalls (campaigns) for a component group."""
    group_where, group_params = build_group_where(categories, excluded_subs)
    sql = f"""
        SELECT COUNT(DISTINCT rc.campaign_id)
        FROM recall_components rc
        WHERE {group_where}
    """
    with conn.cursor() as cur:
        cur.execute(sql, group_params)
        return cur.fetchone()[0]


def _fetch_third_level_components(
    conn: psycopg.Connection,
    second_level_filter: str,
    categories: tuple[str, ...],
) -> list[tuple[str, int]]:
    """Fetch third-level components for a specific second-level category."""
    placeholders = ",".join(["%s"] * len(categories))
    sql = f"""
        SELECT TRIM(SPLIT_PART(rc.component, ':', 3)) AS third,
               COUNT(DISTINCT rc.campaign_id) AS cnt
        FROM recall_components rc
        WHERE TRIM(SPLIT_PART(rc.component, ':', 1)) IN ({placeholders})
          AND TRIM(SPLIT_PART(rc.component, ':', 2)) = %s
        GROUP BY third
        ORDER BY cnt DESC
    """
    with conn.cursor() as cur:
        cur.execute(sql, list(categories) + [second_level_filter])
        return [(t, cnt) for t, cnt in cur.fetchall()]


def _fetch_second_level_components(
    conn: psycopg.Connection,
    categories: tuple[str, ...],
) -> list[tuple[str, int]]:
    """Fetch second-level components with recall counts for a group."""
    placeholders = ",".join(["%s"] * len(categories))
    sql = f"""
        SELECT TRIM(SPLIT_PART(rc.component, ':', 2)) AS sub_cat,
               COUNT(DISTINCT rc.campaign_id) AS cnt
        FROM recall_components rc
        WHERE TRIM(SPLIT_PART(rc.component, ':', 1)) IN ({placeholders})
        GROUP BY sub_cat
        ORDER BY cnt DESC
    """
    with conn.cursor() as cur:
        cur.execute(sql, list(categories))
        return [(sub, cnt) for sub, cnt in cur.fetchall()]


def _fetch_total_recalls_per_vehicle(
    conn: psycopg.Connection,
    vehicles: list[tuple[str, str, int]],
) -> dict[tuple[str, str, int], int]:
    """Fetch total recall count (any category) for each vehicle."""
    if not vehicles:
        return {}
    result: dict[tuple[str, str, int], int] = {}
    with conn.cursor() as cur:
        for make, model, year in vehicles:
            cur.execute(
                """SELECT COUNT(DISTINCT campaign_id) FROM recall_vehicles
                   WHERE make = %s AND model = %s AND year = %s""",
                (make, model, year),
            )
            row = cur.fetchone()
            result[(make, model, year)] = row[0] if row else 0
    return result


# ---------------------------------------------------------------------------
# Selection function (contains sort; ordering test calls this)
# ---------------------------------------------------------------------------


def select_candidates(
    candidates: list[Candidate],
    n: int,
    rng: random.Random,
    max_per_make: int = MAX_PER_MAKE,
    max_gold: int = MAX_GOLD,
    min_gold: int = 1,
    used_vehicles: set[tuple[str, str, int]] | None = None,
    used_gold_ids: set[str] | None = None,
) -> list[Candidate]:
    """Select up to n candidates with deterministic ordering.

    The sort is inside this function — the ordering test relies on it.
    Applies the 10-gold limit, min-gold floor, per-make cap, and
    no-shared-gold rule in Python only.
    """
    if used_vehicles is None:
        used_vehicles = set()
    if used_gold_ids is None:
        used_gold_ids = set()

    # Sort for determinism
    pool = sorted(candidates, key=lambda c: (c.make, c.model, c.year))

    # Filter: gold count in [min_gold, max_gold], skip used vehicles
    pool = [
        c
        for c in pool
        if min_gold <= len(c.gold_ids) <= max_gold
        and (c.make, c.model, c.year) not in used_vehicles
    ]

    # Per-make cap
    make_counts: Counter[str] = Counter()
    capped: list[Candidate] = []
    for c in pool:
        if make_counts[c.make] < max_per_make:
            capped.append(c)
            make_counts[c.make] += 1

    # Sample one at a time, enforcing no-shared-gold within the selection
    local_gold = set(used_gold_ids)
    selected: list[Candidate] = []
    available = capped[:]
    while len(selected) < n and available:
        # Filter out candidates whose gold overlaps with already-selected
        eligible = [c for c in available if not (set(c.gold_ids) & local_gold)]
        if not eligible:
            break
        pick = rng.sample(eligible, 1)[0]
        selected.append(pick)
        local_gold.update(pick.gold_ids)
        available.remove(pick)
    return selected


# ---------------------------------------------------------------------------
# Question building
# ---------------------------------------------------------------------------


def _normalise_question(q: str) -> str:
    """Lower-case and collapse whitespace for dedup."""
    return re.sub(r"\s+", " ", q.lower()).strip()


def _build_question(
    template_name: str,
    candidate: Candidate,
    modifier: str,
    conn: psycopg.Connection,
) -> GoldenRow | None:
    """Build a GoldenRow from a template and candidate."""
    pattern, ref_field = TEMPLATE_CONFIG[template_name]
    gold_count = len(candidate.gold_ids)

    # Plural logic for remedy/consequence
    if template_name in ("remedy", "consequence"):
        recall_word = "recall" if gold_count == 1 else "recalls"
    else:
        recall_word = "recalls"

    make_display = display_name(candidate.make)
    model_display = display_name(candidate.model)

    # Suppress make when the model starts with the make as whole words
    # (e.g. POLESTAR / POLESTAR 2 -> "Polestar 2", not "Polestar Polestar 2")
    # but ACME / ACMEVAN keeps its make (not a word boundary).
    make_words = candidate.make.split()
    model_words = candidate.model.split()
    if model_words[: len(make_words)] == make_words:
        make_in_question = ""
    else:
        make_in_question = make_display

    question = pattern.format(
        modifier=modifier,
        recall_word=recall_word,
        year=candidate.year,
        make=make_in_question,
        model=model_display,
    )
    # Collapse double spaces from empty make
    question = re.sub(r"  +", " ", question)

    # Build reference facts
    facts: list[ReferenceFact] = []
    for cid in sorted(candidate.gold_ids):
        text = _fetch_recall_text(conn, cid, ref_field)
        facts.append(ReferenceFact(campaign_id=cid, field=ref_field, text=text))

    # Reference answer
    if template_name == "count":
        ref_answer = str(gold_count)
    else:
        ref_answer = "; ".join(f"[{f.campaign_id}] {f.text}" for f in facts)

    row_id = (
        f"gen_{template_name}_{candidate.make}_{candidate.model}_{candidate.year}"
    ).replace(" ", "_")

    return GoldenRow(
        id=row_id,
        question=question,
        source="generated",
        template=template_name,
        component_group=None,
        gold_campaign_ids=list(candidate.gold_ids),
        reference_answer=ref_answer,
        reference_facts=facts,
        make=candidate.make,
        model=candidate.model,
        year=candidate.year,
        pass_rule="cite_all_gold",
        tags=[],
        notes=None,
    )


# ---------------------------------------------------------------------------
# Main generation
# ---------------------------------------------------------------------------


def generate(conn: psycopg.Connection, dry_run: bool = False) -> list[GoldenRow]:
    """Generate the full set of golden questions."""
    rng = random.Random(SEED)
    rows: list[GoldenRow] = []
    seen_questions: set[str] = set()
    used_vehicles: set[tuple[str, str, int]] = set()
    used_gold_ids: set[str] = set()

    # Quota allocation
    quotas = allocate_quotas(random.Random(SEED))

    # --- Dry-run: print group stats ---
    if dry_run:
        print("=" * 70)
        print("GROUP STATISTICS")
        print("=" * 70)
        for grp in COMPONENT_GROUPS:
            cands = _fetch_group_candidates(
                conn, grp.key, grp.categories, grp.excluded_subs
            )
            recall_cnt = _fetch_group_recall_count(
                conn, grp.categories, grp.excluded_subs
            )
            eligible = [c for c in cands if len(c.gold_ids) <= MAX_GOLD]
            count_eligible = [c for c in cands if 2 <= len(c.gold_ids) <= MAX_GOLD]
            over_limit = len(cands) - len(eligible)
            print(
                f"  {grp.key:<25} recalls={recall_cnt:>5}  "
                f"vehicles={len(cands):>5}  eligible={len(eligible):>5}  "
                f"count_eligible={len(count_eligible):>5}  over_limit={over_limit}"
            )

        all_cands = _fetch_all_recalls_candidates(conn)
        all_eligible = [c for c in all_cands if len(c.gold_ids) <= MAX_GOLD]
        all_count_eligible = [c for c in all_cands if 2 <= len(c.gold_ids) <= MAX_GOLD]
        print(
            f"\n  all_recalls              "
            f"vehicles={len(all_cands):>5}  eligible={len(all_eligible):>5}  "
            f"count_eligible={len(all_count_eligible):>5}  "
            f"over_limit={len(all_cands) - len(all_eligible)}"
        )

        # Gold-size distribution per group
        print("\n" + "=" * 70)
        print("GOLD-SIZE DISTRIBUTION PER GROUP")
        print("=" * 70)
        header = f"  {'group':<25} {'total':>6}"
        for i in range(1, 11):
            header += f" {i:>4}"
        header += f" {'>10':>4}"
        print(header)
        for grp in COMPONENT_GROUPS:
            cands = _fetch_group_candidates(
                conn, grp.key, grp.categories, grp.excluded_subs
            )
            dist: Counter[int] = Counter()
            for c in cands:
                dist[len(c.gold_ids)] += 1
            line = f"  {grp.key:<25} {len(cands):>6}"
            for i in range(1, 11):
                line += f" {dist.get(i, 0):>4}"
            over = sum(v for k, v in dist.items() if k > 10)
            line += f" {over:>4}"
            print(line)

        # all_recalls distribution
        dist_all: Counter[int] = Counter()
        for c in all_cands:
            dist_all[len(c.gold_ids)] += 1
        line = f"  {'all_recalls':<25} {len(all_cands):>6}"
        for i in range(1, 11):
            line += f" {dist_all.get(i, 0):>4}"
        over = sum(v for k, v in dist_all.items() if k > 10)
        line += f" {over:>4}"
        print(line)

        # Quota table
        print("\n" + "=" * 70)
        print("QUOTA ALLOCATION (group x template)")
        print("=" * 70)
        header = f"  {'group':<25}"
        for t in COMPONENT_TEMPLATES:
            header += f" {t:>12}"
        header += f" {'total':>6}"
        print(header)
        for grp in COMPONENT_GROUPS:
            q = quotas[grp.key]
            line = f"  {grp.key:<25}"
            for t in COMPONENT_TEMPLATES:
                line += f" {q[t]:>12}"
            line += f" {sum(q.values()):>6}"
            print(line)
        # Template totals
        line = f"  {'TOTAL':<25}"
        for t in COMPONENT_TEMPLATES:
            total = sum(quotas[g.key][t] for g in COMPONENT_GROUPS)
            line += f" {total:>12}"
        line += f" {sum(sum(q.values()) for q in quotas.values()):>6}"
        print(line)

    # --- Component template questions ---
    for grp in COMPONENT_GROUPS:
        candidates = _fetch_group_candidates(
            conn, grp.key, grp.categories, grp.excluded_subs
        )

        for template_name in COMPONENT_TEMPLATES:
            n = quotas[grp.key][template_name]
            selected = select_candidates(
                candidates,
                n,
                rng,
                min_gold=MIN_GOLD_FOR_TEMPLATE[template_name],
                used_vehicles=used_vehicles,
                used_gold_ids=used_gold_ids,
            )

            for cand in selected:
                row = _build_question(template_name, cand, grp.modifier, conn)
                if row is None:
                    continue
                # Set component_group
                row = row.model_copy(update={"component_group": grp.key})
                norm = _normalise_question(row.question)
                if norm in seen_questions:
                    continue
                seen_questions.add(norm)
                used_vehicles.add((cand.make, cand.model, cand.year))
                used_gold_ids.update(cand.gold_ids)
                rows.append(row)

    # --- all_recalls questions ---
    all_candidates = _fetch_all_recalls_candidates(conn)
    selected_all = select_candidates(
        all_candidates,
        TOTAL_PER_TEMPLATE,
        rng,
        min_gold=MIN_GOLD_FOR_TEMPLATE["all_recalls"],
        used_vehicles=used_vehicles,
        used_gold_ids=used_gold_ids,
    )
    for cand in selected_all:
        row = _build_question("all_recalls", cand, "", conn)
        if row is None:
            continue
        norm = _normalise_question(row.question)
        if norm in seen_questions:
            continue
        seen_questions.add(norm)
        used_vehicles.add((cand.make, cand.model, cand.year))
        used_gold_ids.update(cand.gold_ids)
        rows.append(row)

    # --- Dry-run: print examples and stats ---
    if dry_run:
        # All questions
        print("\n" + "=" * 70)
        print("ALL GENERATED QUESTIONS")
        print("=" * 70)
        by_template: dict[str, list[GoldenRow]] = defaultdict(list)
        for r in rows:
            if r.template:
                by_template[r.template].append(r)
        for t_name in list(COMPONENT_TEMPLATES) + ["all_recalls"]:
            print(f"\n  --- {t_name} ({len(by_template.get(t_name, []))}) ---")
            for r in by_template.get(t_name, []):
                gold_str = ",".join(r.gold_campaign_ids)
                print(f"    {r.question}")
                print(f"      gold=[{gold_str}]")

        # Counts by make
        print("\n" + "=" * 70)
        print("COUNTS BY MAKE")
        print("=" * 70)
        make_counter: Counter[str] = Counter()
        for r in rows:
            if r.make:
                make_counter[r.make] += 1
        for make, cnt in make_counter.most_common():
            print(f"  {make:<30} {cnt}")

        # Counts by model year
        print("\n" + "=" * 70)
        print("COUNTS BY MODEL YEAR")
        print("=" * 70)
        year_counter: Counter[int] = Counter()
        for r in rows:
            if r.year:
                year_counter[r.year] += 1
        for year in sorted(year_counter):
            print(f"  {year}  {year_counter[year]}")

        # Gold-size distribution per template
        print("\n" + "=" * 70)
        print("GOLD-SIZE DISTRIBUTION PER TEMPLATE (sampled questions)")
        print("=" * 70)
        for t_name in list(COMPONENT_TEMPLATES) + ["all_recalls"]:
            dist_t: Counter[int] = Counter()
            for r in by_template.get(t_name, []):
                dist_t[len(r.gold_campaign_ids)] += 1
            parts = [f"{k}:{v}" for k, v in sorted(dist_t.items())]
            print(f"  {t_name:<20} {', '.join(parts)}")

        # Total recalls per sampled vehicle
        print("\n" + "=" * 70)
        print("TOTAL RECALLS PER SAMPLED VEHICLE (distribution)")
        print("=" * 70)
        sampled_vehicles = [
            (r.make, r.model, r.year) for r in rows if r.make and r.year
        ]
        total_counts = _fetch_total_recalls_per_vehicle(conn, sampled_vehicles)
        total_dist: Counter[int] = Counter()
        for cnt in total_counts.values():
            total_dist[cnt] += 1
        for k in sorted(total_dist):
            print(f"  {k} recalls: {total_dist[k]} vehicles")

        # Second-level components per group
        print("\n" + "=" * 70)
        print("SECOND-LEVEL COMPONENTS PER GROUP")
        print("=" * 70)
        for grp in COMPONENT_GROUPS:
            sub_comps = _fetch_second_level_components(conn, grp.categories)
            print(f"\n  --- {grp.key} ---")
            for sub, cnt in sub_comps:
                label = sub if sub else "(none)"
                print(f"    {label:<50} {cnt:>4}")

        # Third-level breakdown for BACK OVER PREVENTION sub-categories
        print("\n" + "=" * 70)
        print("THIRD-LEVEL BREAKDOWN: BACK OVER PREVENTION")
        print("=" * 70)
        backup_cats = next(
            g.categories for g in COMPONENT_GROUPS if g.key == "backup_camera"
        )
        for sub2 in ("SENSING SYSTEM", "WARNINGS", "SOFTWARE"):
            thirds = _fetch_third_level_components(conn, sub2, backup_cats)
            print(f"\n  --- {sub2} ---")
            for t, cnt in thirds:
                label = t if t else "(none)"
                print(f"    {label:<50} {cnt:>4}")

        # Backup camera rows with gold IDs
        print("\n" + "=" * 70)
        print("BACKUP_CAMERA SAMPLED ROWS WITH GOLD IDS")
        print("=" * 70)
        for r in rows:
            if r.component_group == "backup_camera":
                gold_str = ", ".join(r.gold_campaign_ids)
                print(
                    f"  {r.template:<15} {r.make} {r.model} {r.year}  gold=[{gold_str}]"
                )

        # Leakage check
        print("\n" + "=" * 70)
        print("LEAKAGE CHECK")
        print("=" * 70)
        all_gold_ids_dry: set[str] = set()
        for r in rows:
            all_gold_ids_dry.update(r.gold_campaign_ids)
        recall_texts_dry = _fetch_recall_texts_for_leakage(conn, all_gold_ids_dry)
        flagged_dry = check_leakage(rows, recall_texts_dry)
        print(f"  Flagged: {len(flagged_dry)}")
        for row, field, gram in flagged_dry:
            print(f"    {row.id}: {field} -> {' '.join(gram)}")

        print(f"\nTotal generated: {len(rows)}")
        print("Dry run complete — no files written.")
        return rows

    # --- Leakage check ---
    all_gold_ids: set[str] = set()
    for r in rows:
        all_gold_ids.update(r.gold_campaign_ids)
    recall_texts = _fetch_recall_texts_for_leakage(conn, all_gold_ids)
    flagged = check_leakage(rows, recall_texts)
    if flagged:
        print(f"WARNING: {len(flagged)} leakage hits found:")
        for row, field, gram in flagged:
            print(f"  {row.id}: {field} -> {' '.join(gram)}")

    return rows


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description="Generate golden evaluation questions")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print stats and examples without writing files",
    )
    args = parser.parse_args()

    settings = Settings()
    conn = psycopg.connect(settings.database_url)

    try:
        rows = generate(conn, dry_run=args.dry_run)

        if not args.dry_run:
            OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
                f.writelines(row.model_dump_json() + "\n" for row in rows)
            print(f"Wrote {len(rows)} rows to {OUTPUT_PATH}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
