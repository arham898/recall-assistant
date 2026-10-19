"""Retrieval evaluation logic: metrics aggregation, breakdowns, visibility.

Pure library — no CLI, no model loading, no file I/O except
``update_results_md``.  The script ``scripts/eval_retrieval.py`` wires
the CLI, database, models, timing and file output around these functions.
"""

from __future__ import annotations

import math
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import psycopg

from recall_assistant.config import PROJECT_ROOT
from recall_assistant.evals.gold import build_group_where
from recall_assistant.evals.golden import GoldenRow
from recall_assistant.evals.mappings import COMPONENT_GROUPS
from recall_assistant.evals.retrieval_metrics import (
    hit_at_k,
    recall_at_k,
    reciprocal_rank,
)
from recall_assistant.evals.run_metadata import update_results_section
from recall_assistant.retrieval import Chunker, Hit, VehicleFilter

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MODES = ("dense", "keyword", "hybrid", "rerank")
CHUNKERS: tuple[Chunker, ...] = ("record", "field")
GENERATED_PATH = PROJECT_ROOT / "evals" / "generated.jsonl"
HANDWRITTEN_PATH = PROJECT_ROOT / "evals" / "handwritten.jsonl"
RESULTS_MD = PROJECT_ROOT / "docs" / "results.md"

_GROUP_BY_KEY = {g.key: g for g in COMPONENT_GROUPS}


# ---------------------------------------------------------------------------
# Golden-set loading
# ---------------------------------------------------------------------------


def _load_jsonl(path: Path) -> list[GoldenRow]:
    rows: list[GoldenRow] = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(GoldenRow.model_validate_json(line))
    return rows


def load_golden_rows() -> list[GoldenRow]:
    """Load all golden rows from both JSONL files."""
    return _load_jsonl(GENERATED_PATH) + _load_jsonl(HANDWRITTEN_PATH)


# ---------------------------------------------------------------------------
# Duplicate guard
# ---------------------------------------------------------------------------


def check_no_duplicate_campaigns(hits: list[Hit], row_id: str, config: str) -> None:
    """Stop the run if any campaign_id appears more than once."""
    seen: set[str] = set()
    for h in hits:
        if h.campaign_id in seen:
            print(
                f"DUPLICATE campaign_id {h.campaign_id} in {config} for {row_id}",
                file=sys.stderr,
            )
            sys.exit(1)
        seen.add(h.campaign_id)


# ---------------------------------------------------------------------------
# Bucket helpers
# ---------------------------------------------------------------------------


def bucket_label(count: int) -> str:
    """Assign a vehicle-recall count to a bucket string."""
    if count == 1:
        return "1"
    elif count <= 3:
        return "2-3"
    elif count <= 10:
        return "4-10"
    else:
        return ">10"


# ---------------------------------------------------------------------------
# Percentile helper
# ---------------------------------------------------------------------------


def percentile(sorted_vals: list[float], p: float) -> float:
    """Nearest-rank (ceiling) percentile on a pre-sorted list."""
    n = len(sorted_vals)
    idx = math.ceil(n * p) - 1
    return sorted_vals[max(0, idx)]


# ---------------------------------------------------------------------------
# RowResult
# ---------------------------------------------------------------------------


@dataclass
class RowResult:
    """Retrieval outcome for one (row, config) pair."""

    row_id: str
    config_key: str
    retrieved_ids: list[str]
    top_score: float | None
    latency_s: float


# ---------------------------------------------------------------------------
# Vehicle completeness check
# ---------------------------------------------------------------------------


def has_complete_vehicle(row: GoldenRow) -> bool:
    """True when the row has a non-None make, model and year."""
    return row.make is not None and row.model is not None and row.year is not None


# ---------------------------------------------------------------------------
# Retrieval loop (extracted for testability)
# ---------------------------------------------------------------------------


def run_retrieval_loop(
    retrievers: dict[str, Any],
    rows: list[GoldenRow],
) -> tuple[dict[str, dict[str, RowResult]], int]:
    """Run every mode x chunker x filter on each row.

    Rows without a complete vehicle (make, model, year) run unfiltered
    only.  Returns ``({row_id: {config_key: RowResult}}, n_unfiltered_only)``
    where *n_unfiltered_only* is the count of rows that skipped filtered.
    """
    per_question: dict[str, dict[str, RowResult]] = defaultdict(dict)
    unfiltered_only = 0

    for row in rows:
        has_vehicle = has_complete_vehicle(row)
        if has_vehicle:
            vf = VehicleFilter(make=row.make, model=row.model, year=row.year)
        else:
            unfiltered_only += 1

        for chunker in CHUNKERS:
            ret = retrievers[chunker]
            for mode in MODES:
                method = getattr(ret, mode)

                # ── unfiltered ──
                t0 = time.perf_counter()
                hits_u: list[Hit] = method(question=row.question, k=10)
                elapsed_u = time.perf_counter() - t0
                check_no_duplicate_campaigns(
                    hits_u, row.id, f"{mode}_{chunker}_unfiltered"
                )
                key_u = f"{mode}_{chunker}_unfiltered"
                per_question[row.id][key_u] = RowResult(
                    row_id=row.id,
                    config_key=key_u,
                    retrieved_ids=[h.campaign_id for h in hits_u],
                    top_score=hits_u[0].score if hits_u else None,
                    latency_s=elapsed_u,
                )

                # ── filtered (only when vehicle is complete) ──
                if has_vehicle:
                    t0 = time.perf_counter()
                    hits_f: list[Hit] = method(
                        question=row.question, k=10, vehicle_filter=vf
                    )
                    elapsed_f = time.perf_counter() - t0
                    check_no_duplicate_campaigns(
                        hits_f, row.id, f"{mode}_{chunker}_filtered"
                    )
                    key_f = f"{mode}_{chunker}_filtered"
                    per_question[row.id][key_f] = RowResult(
                        row_id=row.id,
                        config_key=key_f,
                        retrieved_ids=[h.campaign_id for h in hits_f],
                        top_score=hits_f[0].score if hits_f else None,
                        latency_s=elapsed_f,
                    )

    return dict(per_question), unfiltered_only


# ---------------------------------------------------------------------------
# Metric aggregation
# ---------------------------------------------------------------------------


def aggregate_metrics(
    rows: list[GoldenRow],
    per_question: dict[str, dict[str, RowResult]],
    config_key: str,
) -> dict[str, Any]:
    """Compute aggregate metrics for one config over a set of rows."""
    hit5s: list[bool] = []
    hit10s: list[bool] = []
    rec5s: list[float] = []
    rec10s: list[float] = []
    rrs: list[float] = []
    latencies: list[float] = []

    for row in rows:
        if row.id not in per_question:
            continue
        if config_key not in per_question[row.id]:
            continue
        result = per_question[row.id][config_key]
        gold = set(row.gold_campaign_ids)
        hit5s.append(hit_at_k(result.retrieved_ids, gold, 5))
        hit10s.append(hit_at_k(result.retrieved_ids, gold, 10))
        rec5s.append(recall_at_k(result.retrieved_ids, gold, 5))
        rec10s.append(recall_at_k(result.retrieved_ids, gold, 10))
        rrs.append(reciprocal_rank(result.retrieved_ids, gold))
        latencies.append(result.latency_s)

    n = len(hit5s)
    if n == 0:
        return {"n": 0}

    sorted_lat = sorted(latencies)
    return {
        "n": n,
        "hit@5": sum(hit5s) / n,
        "hit@10": sum(hit10s) / n,
        "recall@5": sum(rec5s) / n,
        "recall@10": sum(rec10s) / n,
        "mrr": sum(rrs) / n,
        "latency_p50": percentile(sorted_lat, 0.5),
        "latency_p95": percentile(sorted_lat, 0.95),
    }


# ---------------------------------------------------------------------------
# Hidden-component classification
# ---------------------------------------------------------------------------


def classify_gold_visibility(
    conn: psycopg.Connection,
    row: GoldenRow,
) -> dict[str, bool]:
    """Return ``{campaign_id: is_visible}`` for each gold recall.

    Uses ``build_group_where`` with ``column='r.component'`` against
    the ``recalls`` table so the shared exclusion rules are applied.
    """
    grp = _GROUP_BY_KEY.get(row.component_group or "")
    if grp is None:
        return {cid: True for cid in row.gold_campaign_ids}

    where, params = build_group_where(
        grp.categories, grp.excluded_subs, column="r.component"
    )
    result: dict[str, bool] = {}
    for cid in row.gold_campaign_ids:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT EXISTS ("
                f"  SELECT 1 FROM recalls r"
                f"  WHERE r.campaign_id = %s AND {where}"
                f")",
                [cid] + params,
            )
            result[cid] = cur.fetchone()[0]
    return result


def compute_found_rates(
    rows: list[GoldenRow],
    per_question: dict[str, dict[str, RowResult]],
    config_key: str,
    visibility: dict[str, dict[str, bool]],
) -> dict[str, Any]:
    """Compute found-rate per gold recall, split by visibility.

    *visibility* maps ``{row_id: {campaign_id: is_visible}}``.
    """
    vis_found = 0
    vis_total = 0
    hid_found = 0
    hid_total = 0

    for row in rows:
        if row.id not in per_question:
            continue
        if config_key not in per_question[row.id]:
            continue
        vis_map = visibility.get(row.id, {})
        if not vis_map:
            continue
        result = per_question[row.id][config_key]
        top10 = set(result.retrieved_ids[:10])
        for cid in row.gold_campaign_ids:
            is_vis = vis_map.get(cid, True)
            if is_vis:
                vis_total += 1
                if cid in top10:
                    vis_found += 1
            else:
                hid_total += 1
                if cid in top10:
                    hid_found += 1

    return {
        "visible_found": vis_found,
        "visible_total": vis_total,
        "visible_rate": vis_found / vis_total if vis_total else None,
        "hidden_found": hid_found,
        "hidden_total": hid_total,
        "hidden_rate": hid_found / hid_total if hid_total else None,
    }


# ---------------------------------------------------------------------------
# Vehicle-recall count
# ---------------------------------------------------------------------------


def get_vehicle_recall_count(
    conn: psycopg.Connection, make: str, model: str, year: int
) -> int:
    """Count distinct campaigns for a (make, model, year)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(DISTINCT campaign_id) FROM recall_vehicles "
            "WHERE make = %s AND model = %s AND year = %s",
            (make.upper(), model.upper(), year),
        )
        return cur.fetchone()[0]


# ---------------------------------------------------------------------------
# Card seal
# ---------------------------------------------------------------------------


def card_seal(conn: psycopg.Connection) -> dict[str, dict[str, Any]]:
    """Chunk count and MD5 per chunker for reproducibility."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT chunker, count(*), "
            "md5(string_agg(body, '|' ORDER BY campaign_id, field_name, body)) "
            "FROM chunks GROUP BY chunker"
        )
        return {row[0]: {"count": row[1], "md5": row[2]} for row in cur.fetchall()}


# ---------------------------------------------------------------------------
# results.md writer
# ---------------------------------------------------------------------------


def update_results_md(label: str, content: str, *, path: Path | None = None) -> None:
    """Replace or append the section for *label* in docs/results.md."""
    header = f"## Retrieval evaluation: {label} (Module 2.2)"
    update_results_section(header, content, path=path)


# ---------------------------------------------------------------------------
# Summary formatting
# ---------------------------------------------------------------------------


def _fmt_pct(v: float | None) -> str:
    return f"{v:.3f}" if v is not None else "—"


def _fmt_ms(v: float | None) -> str:
    return f"{v * 1000:.0f}ms" if v is not None else "—"


def format_summary_table(summary: dict[str, Any], configs: list[str]) -> str:
    """Build a markdown table of metrics per config."""
    hdr = "| Config | n | hit@5 | hit@10 | recall@5 | recall@10 | MRR | p50 | p95 |"
    sep = "|--------|---|-------|--------|----------|-----------|-----|-----|-----|"
    lines = [hdr, sep]
    for cfg in configs:
        m = summary.get(cfg, {})
        if not m or m.get("n", 0) == 0:
            continue
        lines.append(
            f"| {cfg} | {m['n']} | {_fmt_pct(m['hit@5'])} "
            f"| {_fmt_pct(m['hit@10'])} | {_fmt_pct(m['recall@5'])} "
            f"| {_fmt_pct(m['recall@10'])} | {_fmt_pct(m['mrr'])} "
            f"| {_fmt_ms(m.get('latency_p50'))} "
            f"| {_fmt_ms(m.get('latency_p95'))} |"
        )
    return "\n".join(lines)


def format_markdown(results: dict[str, Any], command: str) -> str:
    """Format the full results section for docs/results.md."""
    configs = results["configs"]
    parts: list[str] = []

    parts.append(f"```\nCommand: {command}\nDate:    {date.today()}\n```")
    parts.append("")

    parts.append("### Overall")
    parts.append(format_summary_table(results["summary"]["overall"], configs))
    parts.append("")

    for breakdown_key, breakdown_label in [
        ("by_source", "By source"),
        ("by_template", "By template (generated only)"),
        ("by_tag", "By tag (handwritten only)"),
        ("by_vehicle_bucket", "By vehicle recall-count bucket"),
    ]:
        bd = results["summary"].get(breakdown_key, {})
        if not bd:
            continue
        parts.append(f"### {breakdown_label}")
        for group_val, group_data in sorted(bd.items()):
            parts.append(f"\n**{group_val}**")
            parts.append(format_summary_table(group_data, configs))
        parts.append("")

    hv = results["summary"].get("hidden_vs_visible", {})
    if hv:
        parts.append("### Hidden vs visible gold recalls (found rate)")
        for cfg in configs:
            cfg_data = hv.get(cfg, {})
            if not cfg_data:
                continue
            vr = cfg_data.get("visible_rate")
            hr = cfg_data.get("hidden_rate")
            parts.append(
                f"- **{cfg}**: visible {_fmt_pct(vr)} "
                f"(n={cfg_data.get('visible_total', 0)}), "
                f"hidden {_fmt_pct(hr)} "
                f"(n={cfg_data.get('hidden_total', 0)})"
            )
        parts.append("")

    rsd = results["summary"].get("rerank_score_distribution", {})
    if rsd:
        parts.append("### Rerank score distribution (top-1 cross-encoder)")
        for key, dist in sorted(rsd.items()):
            parts.append(f"\n**{key}**")
            for group_name in ("answerable", "unanswerable"):
                d = dist.get(group_name, {})
                if not d or d.get("n", 0) == 0:
                    continue
                parts.append(
                    f"- {group_name} (n={d['n']}): "
                    f"min={d.get('min', '—'):.3f}, "
                    f"p25={d.get('p25', '—'):.3f}, "
                    f"median={d.get('median', '—'):.3f}, "
                    f"p75={d.get('p75', '—'):.3f}, "
                    f"max={d.get('max', '—'):.3f}"
                )
        parts.append("")

    parts.append(f"Rows with >5 gold IDs: {results.get('gt5_gold_count', 0)}")

    return "\n".join(parts)
