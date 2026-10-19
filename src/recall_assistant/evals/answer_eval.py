"""Answer evaluation: scoring logic, scored-JSONL output, and results.md writer.

Pure library — no CLI, no model loading.  The script
``scripts/score_answers.py`` wires the CLI around these functions.
"""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

from recall_assistant.evals.answer_checks import (
    cites_all_gold,
    cites_gold,
    declined,
    invalid,
    invalid_reason,
    pass_rule_check,
    wrongly_declined,
)
from recall_assistant.evals.faithfulness import build_premise, strip_citations
from recall_assistant.evals.golden import GoldenRow
from recall_assistant.evals.retrieval_eval import percentile
from recall_assistant.evals.run_metadata import update_results_section

# ---------------------------------------------------------------------------
# JSONL I/O
# ---------------------------------------------------------------------------


def load_answers_jsonl(path: Path) -> tuple[dict, list[dict]]:
    """Load an answers JSONL file.

    Returns ``(metadata, rows)`` where *metadata* is the first line
    (``type: "metadata"``) and *rows* are the remaining answer dicts.
    """
    with open(path, encoding="utf-8") as f:
        lines = [line.strip() for line in f if line.strip()]
    metadata = json.loads(lines[0])
    rows = [json.loads(line) for line in lines[1:]]
    return metadata, rows


# ---------------------------------------------------------------------------
# Per-row check results
# ---------------------------------------------------------------------------


def _dedup(ids: list[str]) -> list[str]:
    """Remove duplicates from *ids*, preserving first-occurrence order."""
    seen: set[str] = set()
    result: list[str] = []
    for x in ids:
        if x not in seen:
            seen.add(x)
            result.append(x)
    return result


def check_one_row(
    row: dict,
    golden: GoldenRow,
    scorer: Callable[[str, str], float] | None = None,
) -> dict:
    """Apply all code checks to one answer row.

    If *scorer* is provided and status is ``"answered"``, compute a
    faithfulness score (premise = joined retrieved texts, hypothesis =
    answer text with citations stripped).  Otherwise ``"faithfulness"``
    is ``None``.

    Returns a dict suitable for writing to the scored JSONL.
    """
    status = row["status"]
    cited = _dedup(row["cited_ids"])
    invalid_ids = row.get("invalid_ids", [])
    gold = golden.gold_campaign_ids
    retrieved = row.get("retrieved_ids", [])

    is_declined = declined(status)
    is_invalid = invalid(status)

    reason = ""
    if is_invalid:
        reason = invalid_reason(
            row.get("answer_text", ""),
            row.get("finish_reason", "stop"),
            cited,
            invalid_ids,
        )

    faith: float | None = None
    if scorer is not None and status == "answered":
        premise = build_premise(row.get("retrieved_texts", []))
        hypothesis = strip_citations(row.get("answer_text", ""))
        faith = scorer(premise, hypothesis)

    return {
        "question_id": row["question_id"],
        "source": row["source"],
        "pass_rule": golden.pass_rule,
        "template": golden.template,
        "status": status,
        "declined": is_declined,
        "invalid": is_invalid,
        "invalid_reason": reason,
        "wrongly_declined": wrongly_declined(status, retrieved, gold),
        "cites_gold": cites_gold(cited, gold),
        "cites_all_gold": cites_all_gold(cited, gold),
        "pass_rule_passed": pass_rule_check(golden.pass_rule, status, cited, gold),
        "cited_ids": cited,
        "invalid_ids": invalid_ids,
        "gold_campaign_ids": gold,
        "faithfulness": faith,
        "cost": row.get("cost"),
        "generation_latency_s": row.get("generation_latency_s"),
        "total_latency_s": _total_latency(row),
    }


def _total_latency(row: dict) -> float | None:
    """Sum retrieval + generation latency, or None if either is missing."""
    ret = row.get("retrieval_latency_s")
    gen = row.get("generation_latency_s")
    if ret is not None and gen is not None:
        return ret + gen
    return gen


# ---------------------------------------------------------------------------
# Aggregate scoring
# ---------------------------------------------------------------------------


def _group_stats(checked: list[dict]) -> dict[str, Any]:
    """Compute pass/fail/invalid counts for a list of checked rows."""
    n = len(checked)
    passed = sum(1 for r in checked if r["pass_rule_passed"])
    wd = sum(1 for r in checked if r["wrongly_declined"])
    inv = sum(1 for r in checked if r["invalid"])
    return {
        "n": n,
        "passed": passed,
        "pass_rate": passed / n if n else 0.0,
        "wrongly_declined": wd,
        "invalid_count": inv,
    }


def score_answers(
    rows: list[dict],
    golden_by_id: dict[str, GoldenRow],
    scorer: Callable[[str, str], float] | None = None,
) -> tuple[dict[str, Any], list[dict]]:
    """Apply all code checks and aggregate.

    Returns ``(scores_dict, checked_rows)`` where *checked_rows* is
    the per-row scored list (for the scored JSONL) and *scores_dict*
    contains the aggregated metrics.

    If *scorer* is provided it is called for every ``status == "answered"``
    row.  Progress is printed every 20 rows when a scorer is active.
    """
    checked: list[dict] = []
    faith_secs = 0.0
    for i, row in enumerate(rows, 1):
        golden = golden_by_id.get(row["question_id"])
        if golden is None:
            continue
        if scorer is not None and row["status"] == "answered":
            t0 = time.perf_counter()
            checked.append(check_one_row(row, golden, scorer=scorer))
            faith_secs += time.perf_counter() - t0
        else:
            checked.append(check_one_row(row, golden, scorer=scorer))
        if scorer is not None and i % 20 == 0:
            print(f"  scored {i}/{len(rows)}...", file=sys.stderr)

    # Split by source.
    generated = [r for r in checked if r["source"] == "generated"]
    handwritten = [r for r in checked if r["source"] == "handwritten"]
    unanswerable = [r for r in checked if r["source"] == "unanswerable"]

    # By-template breakdown for generated rows.
    templates = sorted({r["template"] for r in generated if r["template"]})
    by_template: dict[str, dict] = {}
    for tmpl in templates:
        subset = [r for r in generated if r["template"] == tmpl]
        by_template[tmpl] = _group_stats(subset)

    # Unanswerable breakdown.
    n_unans = len(unanswerable)
    unans_declined = sum(1 for r in unanswerable if r["declined"])
    unans_invalid = sum(1 for r in unanswerable if r["invalid"])
    unans_answered = n_unans - unans_declined - unans_invalid

    # Unanswerable invalid breakdown by reason.
    unans_invalid_reasons: dict[str, int] = {}
    for r in unanswerable:
        if r["invalid"]:
            reason = r.get("invalid_reason", "")
            unans_invalid_reasons[reason] = unans_invalid_reasons.get(reason, 0) + 1

    # Cost.
    costs = [r["cost"] for r in checked if r["cost"] is not None]
    total_cost = sum(costs)
    cost_per_q = total_cost / len(costs) if costs else 0.0

    # Latency.
    gen_lats = sorted(
        r["generation_latency_s"]
        for r in checked
        if r["generation_latency_s"] is not None
    )
    total_lats = sorted(
        r["total_latency_s"] for r in checked if r["total_latency_s"] is not None
    )

    # Faithfulness.
    faith_scores = sorted(
        r["faithfulness"] for r in checked if r["faithfulness"] is not None
    )
    faith_dict: dict[str, Any] = {"n": 0}
    if faith_scores:
        n_f = len(faith_scores)
        flagged = sum(1 for s in faith_scores if s < 0.5)
        lowest_5 = sorted(
            (
                (r["faithfulness"], r["question_id"])
                for r in checked
                if r["faithfulness"] is not None
            ),
        )[:5]
        faith_dict = {
            "n": n_f,
            "mean": sum(faith_scores) / n_f,
            "median": percentile(faith_scores, 0.5),
            "p25": percentile(faith_scores, 0.25),
            "min": faith_scores[0],
            "flagged_below_0_5": flagged,
            "lowest_5": [(qid, score) for score, qid in lowest_5],
            "seconds": faith_secs,
        }

    scores: dict[str, Any] = {
        "generated": {**_group_stats(generated), "by_template": by_template},
        "handwritten": _group_stats(handwritten),
        "unanswerable": {
            "n": n_unans,
            "declined": unans_declined,
            "invalid": unans_invalid,
            "invalid_reasons": unans_invalid_reasons,
            "answered": unans_answered,
        },
        "faithfulness": faith_dict,
        "cost": {"total": total_cost, "per_question": cost_per_q},
        "latency_generation": {
            "p50": percentile(gen_lats, 0.5) if gen_lats else None,
            "p95": percentile(gen_lats, 0.95) if gen_lats else None,
        },
        "latency_total": {
            "p50": percentile(total_lats, 0.5) if total_lats else None,
            "p95": percentile(total_lats, 0.95) if total_lats else None,
        },
    }

    return scores, checked


# ---------------------------------------------------------------------------
# Markdown formatting
# ---------------------------------------------------------------------------


def _fmt_pct(v: float | None) -> str:
    return f"{v:.3f}" if v is not None else "—"


def _fmt_s(v: float | None) -> str:
    return f"{v:.2f}s" if v is not None else "—"


def _fmt_cost(v: float | None) -> str:
    return f"${v:.5f}" if v is not None else "—"


def format_answer_markdown(scores: dict[str, Any], metadata: dict, command: str) -> str:
    """Build the markdown section body for docs/results.md."""
    parts: list[str] = []

    parts.append(f"```\nCommand: {command}\nDate:    {date.today()}\n```")
    parts.append("")

    # Code-check pass rates.
    parts.append("### Code-check pass rates")
    hdr = "| Source | n | passed | rate | wrongly_declined | invalid |"
    sep = "|--------|---|--------|------|------------------|---------|"
    parts.append(hdr)
    parts.append(sep)
    for src in ("generated", "handwritten"):
        s = scores.get(src, {})
        if not s or s.get("n", 0) == 0:
            continue
        parts.append(
            f"| {src} | {s['n']} | {s['passed']} | {_fmt_pct(s['pass_rate'])} "
            f"| {s['wrongly_declined']} | {s['invalid_count']} |"
        )
    parts.append("")

    # By template (generated only).
    by_tmpl = scores.get("generated", {}).get("by_template", {})
    if by_tmpl:
        parts.append("### By template (generated only)")
        parts.append("| Template | n | passed | rate | wrongly_declined | invalid |")
        parts.append("|----------|---|--------|------|------------------|---------|")
        for tmpl in sorted(by_tmpl):
            t = by_tmpl[tmpl]
            parts.append(
                f"| {tmpl} | {t['n']} | {t['passed']} "
                f"| {_fmt_pct(t['pass_rate'])} | {t['wrongly_declined']} "
                f"| {t['invalid_count']} |"
            )
        parts.append("")

    # Unanswerable.
    u = scores.get("unanswerable", {})
    if u and u.get("n", 0) > 0:
        parts.append(f"### Unanswerable (n={u['n']})")
        parts.append("| declined (pass) | invalid | answered (fail) |")
        parts.append("|-----------------|---------|-----------------|")
        parts.append(f"| {u['declined']} | {u['invalid']} | {u['answered']} |")
        inv_reasons = u.get("invalid_reasons", {})
        if inv_reasons:
            parts.append("")
            parts.append(
                "Invalid breakdown: "
                + ", ".join(
                    f"{reason}={count}" for reason, count in sorted(inv_reasons.items())
                )
            )
        parts.append("")

    # Faithfulness.
    f = scores.get("faithfulness", {})
    if f and f.get("n", 0) > 0:
        model_name = metadata.get("faithfulness_model", "HHEM-2.1-Open")
        model_rev = metadata.get("faithfulness_revision", "")
        parts.append(f"### Faithfulness ({model_name})")
        parts.append("")
        parts.append(
            "| n | mean | median | p25 | min | flagged (below 0.5) | seconds |"
        )
        parts.append(
            "|---|------|--------|-----|-----|---------------------|---------|"
        )
        parts.append(
            f"| {f['n']} | {_fmt_pct(f.get('mean'))} "
            f"| {_fmt_pct(f.get('median'))} | {_fmt_pct(f.get('p25'))} "
            f"| {_fmt_pct(f.get('min'))} | {f.get('flagged_below_0_5', 0)} "
            f"| {_fmt_s(f.get('seconds'))} |"
        )
        lowest = f.get("lowest_5", [])
        if lowest:
            parts.append("")
            parts.append("Lowest 5:")
            for qid, score in lowest:
                parts.append(f"- {qid}: {score:.3f}")
        if model_rev:
            parts.append("")
            parts.append(f"Model revision: `{model_rev}`")
        parts.append("")

    # Cost.
    c = scores.get("cost", {})
    parts.append("### Cost")
    parts.append("| total | per_question |")
    parts.append("|-------|-------------|")
    parts.append(
        f"| {_fmt_cost(c.get('total'))} | {_fmt_cost(c.get('per_question'))} |"
    )
    parts.append("")

    # Latency.
    gen_lat = scores.get("latency_generation", {})
    tot_lat = scores.get("latency_total", {})
    parts.append("### Latency")
    parts.append("| metric | generation p50 | generation p95 | total p50 | total p95 |")
    parts.append("|--------|----------------|----------------|-----------|-----------|")
    parts.append(
        f"| value | {_fmt_s(gen_lat.get('p50'))} "
        f"| {_fmt_s(gen_lat.get('p95'))} "
        f"| {_fmt_s(tot_lat.get('p50'))} "
        f"| {_fmt_s(tot_lat.get('p95'))} |"
    )

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# results.md writer
# ---------------------------------------------------------------------------


def update_answer_results_md(
    label: str, content: str, *, path: Path | None = None
) -> None:
    """Replace or append the section for *label* in docs/results.md."""
    header = f"## Answer evaluation: {label} (Module 2.3)"
    update_results_section(header, content, path=path)
