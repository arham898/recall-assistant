"""Pure helpers for comparing a prompt-fix answer run against a baseline.

No CLI, no side-effects.  The script ``scripts/compare_prompt_fix.py``
wires the CLI around these functions.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Any

from recall_assistant.evals.answer_eval import load_answers_jsonl, read_jsonl
from recall_assistant.evals.retrieval_eval import percentile

# Example IDs that appeared in the old system prompt.
_EXAMPLE_IDS = ("24V123000", "25V456000")


# ---------------------------------------------------------------------------
# Loaders (thin wrappers around existing functions)
# ---------------------------------------------------------------------------


def load_scored(path: Path) -> list[dict]:
    """Load a scored JSONL file (no metadata row)."""
    return read_jsonl(path)


def load_answer_texts(path: Path) -> dict[str, str]:
    """Load answer texts keyed by question_id, skipping the metadata row."""
    _metadata, rows = load_answers_jsonl(path)
    return {r["question_id"]: r["answer_text"] for r in rows}


# ---------------------------------------------------------------------------
# Aggregation helpers
# ---------------------------------------------------------------------------


def pass_rate_by_source_and_template(scored: list[dict]) -> dict[str, dict[str, Any]]:
    """Group scored rows by source/template, return n / passed / rate.

    Generated rows are keyed by their template name.
    Handwritten and unanswerable rows are keyed by source.
    """
    buckets: dict[str, list[dict]] = {}
    for r in scored:
        source = r.get("source", "generated")
        if source == "generated":
            key = r.get("template") or "(none)"
        else:
            key = source
        buckets.setdefault(key, []).append(r)
    result: dict[str, dict[str, Any]] = {}
    for key in sorted(buckets):
        rows = buckets[key]
        n = len(rows)
        passed = sum(1 for r in rows if r["pass_rule_passed"])
        result[key] = {"n": n, "passed": passed, "rate": passed / n if n else 0.0}
    return result


def invalid_reason_counts(scored: list[dict]) -> dict[str, int]:
    """Count each invalid_reason among rows where invalid is True."""
    counts: dict[str, int] = {}
    for r in scored:
        if r["invalid"]:
            reason = r["invalid_reason"]
            counts[reason] = counts.get(reason, 0) + 1
    return counts


def count_example_ids(texts: dict[str, str]) -> int:
    """Count answers whose text contains a prompt example ID (no brackets)."""
    return sum(1 for t in texts.values() if any(eid in t for eid in _EXAMPLE_IDS))


_LEAK_PATTERNS = ("campaign_id", "<", "two digits", "NNV")


def count_placeholder_leak(texts: dict[str, str]) -> int:
    """Count answers whose text contains a prompt-leaking pattern."""
    return sum(1 for t in texts.values() if any(p in t for p in _LEAK_PATTERNS))


_BARE_BRACKET_RE = re.compile(r"^\[[^\]]*\]$")


def count_bare_bracket_answers(texts: dict[str, str]) -> int:
    """Count answer_text values that are entirely a bracket of IDs."""
    return sum(1 for t in texts.values() if _BARE_BRACKET_RE.match(t.strip()))


def faithfulness_summary(scored: list[dict]) -> dict[str, Any]:
    """Summarise faithfulness scores, excluding rows with no value."""
    vals = sorted(r["faithfulness"] for r in scored if r["faithfulness"] is not None)
    if not vals:
        return {"n": 0}
    n = len(vals)
    flagged = sum(1 for v in vals if v < 0.5)
    return {
        "n": n,
        "mean": sum(vals) / n,
        "median": percentile(vals, 0.5),
        "min": vals[0],
        "flagged_below_0_5": flagged,
    }


# ---------------------------------------------------------------------------
# Markdown formatting
# ---------------------------------------------------------------------------


def _fmt_pct(v: float) -> str:
    return f"{v:.3f}"


def format_comparison(
    old_scored: list[dict],
    new_scored: list[dict],
    old_texts: dict[str, str],
    new_texts: dict[str, str],
    command: str,
    commit: str,
    *,
    show_apostrophe_caveat: bool = False,
) -> str:
    """Build the full markdown body for docs/results.md."""
    parts: list[str] = []

    # Metadata block.
    parts.append("```")
    parts.append(f"Command: {command}")
    parts.append(f"Date:    {date.today()}")
    parts.append(f"Commit:  {commit}")
    parts.append("```")
    parts.append("")

    # --- Table 1: pass rate by source and template ---
    old_pr = pass_rate_by_source_and_template(old_scored)
    new_pr = pass_rate_by_source_and_template(new_scored)
    all_templates = sorted(set(old_pr) | set(new_pr))

    parts.append("### Pass rate by source and template")
    parts.append("")
    parts.append(
        "| Template | old n | old passed | old rate | new n | new passed | new rate |"
    )
    parts.append(
        "|----------|-------|------------|----------|-------|------------|----------|"
    )
    for tmpl in all_templates:
        o = old_pr.get(tmpl, {"n": 0, "passed": 0, "rate": 0.0})
        n = new_pr.get(tmpl, {"n": 0, "passed": 0, "rate": 0.0})
        parts.append(
            f"| {tmpl} | {o['n']} | {o['passed']} | {_fmt_pct(o['rate'])} "
            f"| {n['n']} | {n['passed']} | {_fmt_pct(n['rate'])} |"
        )
    parts.append("")

    # --- Table 2: invalid reasons ---
    old_ir = invalid_reason_counts(old_scored)
    new_ir = invalid_reason_counts(new_scored)
    all_reasons = sorted(set(old_ir) | set(new_ir))

    parts.append("### Invalid reasons")
    parts.append("")
    parts.append("| Reason | old | new |")
    parts.append("|--------|-----|-----|")
    for reason in all_reasons:
        parts.append(
            f"| {reason} | {old_ir.get(reason, 0)} | {new_ir.get(reason, 0)} |"
        )
    parts.append("")

    # --- Table 3: example-ID and placeholder contamination ---
    old_ex = count_example_ids(old_texts)
    new_ex = count_example_ids(new_texts)
    old_pl = count_placeholder_leak(old_texts)
    new_pl = count_placeholder_leak(new_texts)
    old_bb = count_bare_bracket_answers(old_texts)
    new_bb = count_bare_bracket_answers(new_texts)

    parts.append("### Example-ID and placeholder contamination")
    parts.append("")
    parts.append(
        "| Run | answers with 24V123000 or 25V456000 "
        '| answers with "campaign_id" or "<" '
        "| bare-bracket answers |"
    )
    parts.append(
        "|-----|--------------------------------------"
        "|-------------------------------------|----------------------|"
    )
    parts.append(f"| old | {old_ex} | {old_pl} | {old_bb} |")
    parts.append(f"| new | {new_ex} | {new_pl} | {new_bb} |")
    parts.append("")

    # --- Table 4: faithfulness ---
    old_fs = faithfulness_summary(old_scored)
    new_fs = faithfulness_summary(new_scored)

    parts.append("### Faithfulness")
    parts.append("")
    parts.append("| Metric | old | new |")
    parts.append("|--------|-----|-----|")
    for key in ("n", "mean", "median", "min", "flagged_below_0_5"):
        ov = old_fs.get(key)
        nv = new_fs.get(key)
        o_str = (
            _fmt_pct(ov)
            if isinstance(ov, float)
            else str(ov)
            if ov is not None
            else "—"
        )
        n_str = (
            _fmt_pct(nv)
            if isinstance(nv, float)
            else str(nv)
            if nv is not None
            else "—"
        )
        parts.append(f"| {key} | {o_str} | {n_str} |")
    parts.append("")

    # --- Caveats ---
    parts.append("### Caveats")
    parts.append("")
    parts.append(
        "- Labels and judge rows are scored against the 19 Oct answers; they have"
    )
    parts.append("  not been re-run against the new answers.")
    if show_apostrophe_caveat:
        parts.append(
            "- hw_002, hw_019, and hw_028 were sent with a mojibake apostrophe on"
        )
        parts.append(
            "  19 Oct and correctly on this run, so those three rows are not a"
        )
        parts.append("  like-for-like comparison.")

    return "\n".join(parts)
