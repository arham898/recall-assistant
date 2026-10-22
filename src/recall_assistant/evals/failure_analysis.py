"""Failure analysis: three-way union of code-check, human-label, and judge failures.

Pure library -- no CLI, no model loading.  The scripts
``scripts/export_failures.py`` and ``scripts/tally_failures.py``
wire the CLI around these functions.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from recall_assistant.evals.golden import GoldenRow
from recall_assistant.evals.run_metadata import update_results_section

# ---------------------------------------------------------------------------
# Category legend
# ---------------------------------------------------------------------------

CATEGORY_LEGEND: dict[str, str] = {
    "1": "Retrieval miss: declined correctly",
    "1b": "Retrieval miss: answered from neighbours",
    "2": "Missed but retrieved",
    "3": "Wrong year or vehicle admitted",
    "4": "Wrong component admitted on a count",
    "5": "Cited ID not in retrieved set",
    "6": "Right ID, wrong description",
    "7": "Unanswerable answered",
    "8": "Format leak",
    "9": "Safety advice omitted",
    "10": "Reference problem, not answer problem",
    "11": "Judge false fail: answer correct",
}

VALID_CATEGORY_KEYS: set[str] = set(CATEGORY_LEGEND)

# Ordered keys for tables (legend order).
_LEGEND_ORDER: list[str] = [
    "1",
    "1b",
    "2",
    "3",
    "4",
    "5",
    "6",
    "7",
    "8",
    "9",
    "10",
    "11",
]

# ---------------------------------------------------------------------------
# Build failure rows (three-way union)
# ---------------------------------------------------------------------------


def build_failure_rows(
    answers: list[dict],
    scored: list[dict],
    labels: dict[str, dict],
    judge_rows: list[dict],
    golden_by_id: dict[str, GoldenRow],
) -> list[dict]:
    """Build the union of all failure rows from three signals.

    A row is a failure if ANY of:
    - ``pass_rule_passed is False`` in *scored*
    - ``label == "fail"`` in *labels* (may not exist for this question_id)
    - ``judge_verdict == "fail"`` in *judge_rows* (may not exist)

    Raises ``ValueError`` listing question_ids present in *scored* but
    missing from *golden_by_id* or *answers*.
    """
    answers_by_qid = {r["question_id"]: r for r in answers}
    judge_by_qid = {r["question_id"]: r for r in judge_rows}

    # Validate: every scored qid must exist in golden and answers.
    missing_golden: list[str] = []
    missing_answers: list[str] = []
    for row in scored:
        qid = row["question_id"]
        if qid not in golden_by_id:
            missing_golden.append(qid)
        if qid not in answers_by_qid:
            missing_answers.append(qid)

    parts: list[str] = []
    if missing_golden:
        parts.append(f"Missing from golden set: {missing_golden}")
    if missing_answers:
        parts.append(f"Missing from answers file: {missing_answers}")
    if parts:
        raise ValueError("\n".join(parts))

    failures: list[dict] = []
    for row in scored:
        qid = row["question_id"]

        failed_signals: list[str] = []
        if row["pass_rule_passed"] is False:
            failed_signals.append("code")
        if qid in labels and labels[qid]["label"] == "fail":
            failed_signals.append("human")
        if qid in judge_by_qid and judge_by_qid[qid]["judge_verdict"] == "fail":
            failed_signals.append("judge")

        if not failed_signals:
            continue

        golden = golden_by_id[qid]
        answer = answers_by_qid[qid]

        # Effective template: use source when template is None.
        template = row["template"] if row["template"] is not None else row["source"]

        gold_ids = row["gold_campaign_ids"]
        retrieved_ids = answer["retrieved_ids"]
        gold_in_retrieved = len(set(gold_ids) & set(retrieved_ids))

        # Human note: prefer labels note, fall back to judge human_note.
        human_note = ""
        if qid in labels:
            human_note = labels[qid].get("note", "")
        elif qid in judge_by_qid:
            human_note = judge_by_qid[qid].get("human_note", "")

        judge_reason = ""
        if qid in judge_by_qid:
            judge_reason = judge_by_qid[qid].get("judge_reason", "")

        # Code-check fields from the scored row.
        invalid_reason_raw = row.get("invalid_reason", "")
        code_checks = {
            "status": row["status"] if row.get("invalid") else "valid",
            "invalid_reason": invalid_reason_raw if invalid_reason_raw else "none",
            "pass_rule": row["pass_rule"],
            "cites_gold": row["cites_gold"],
            "cites_all_gold": row["cites_all_gold"],
            "wrongly_declined": row["wrongly_declined"],
        }

        failures.append(
            {
                "question_id": qid,
                "template": template,
                "question": golden.question,
                "reference_answer": golden.reference_answer,
                "answer_text": answer["answer_text"],
                "cited_ids": row["cited_ids"],
                "invalid_ids": row.get("invalid_ids", []),
                "retrieved_ids": retrieved_ids,
                "gold_campaign_ids": gold_ids,
                "gold_in_retrieved": gold_in_retrieved,
                "code_checks": code_checks,
                "human_note": human_note,
                "judge_reason": judge_reason,
                "failed_signals": failed_signals,
                "category": "",
            }
        )

    failures.sort(key=lambda r: (r["template"], r["question_id"]))
    return failures


# ---------------------------------------------------------------------------
# Worksheet formatting
# ---------------------------------------------------------------------------


def _format_cited_ids(row: dict) -> str:
    """Format cited IDs, marking invalid ones with '(not retrieved)'.

    Invalid IDs may be absent from ``cited_ids`` (the scored file splits
    them into a separate ``invalid_ids`` list), so they are appended at
    the end if not already seen.
    """
    invalid_set = set(row.get("invalid_ids", []))
    parts: list[str] = []
    seen: set[str] = set()
    for cid in row["cited_ids"]:
        if cid in seen:
            continue
        seen.add(cid)
        if cid in invalid_set:
            parts.append(f"{cid} (not retrieved)")
        else:
            parts.append(cid)
    # Append invalid IDs that were not in cited_ids.
    for cid in row.get("invalid_ids", []):
        if cid in seen:
            continue
        seen.add(cid)
        parts.append(f"{cid} (not retrieved)")
    return ", ".join(parts)


def format_worksheet(rows: list[dict], commit: str) -> str:
    """Format failure rows as a markdown worksheet with category legend header."""
    lines: list[str] = []
    lines.append("# Failure analysis worksheet")
    lines.append("")
    lines.append(f"Commit: {commit}")
    lines.append(f"Date: {date.today()}")
    lines.append(f"Rows: {len(rows)}")
    lines.append("")
    lines.append("## Category legend")
    lines.append("")
    lines.append("| # | Description |")
    lines.append("|---|-------------|")
    for key in _LEGEND_ORDER:
        lines.append(f"| {key} | {CATEGORY_LEGEND[key]} |")
    lines.append("")
    lines.append("## Failures")

    for row in rows:
        lines.append("")
        lines.append("---")
        lines.append("")
        lines.append(f"### {row['question_id']}")
        lines.append("")
        lines.append(f"- **Template:** {row['template']}")
        lines.append(f"- **Failed signals:** {', '.join(row['failed_signals'])}")
        lines.append(f"- **Gold IDs:** {', '.join(row['gold_campaign_ids'])}")
        lines.append(f"- **Cited IDs:** {_format_cited_ids(row)}")
        lines.append(f"- **Retrieved IDs:** {', '.join(row['retrieved_ids'])}")
        lines.append(
            f"- **Gold in retrieved:** "
            f"{row['gold_in_retrieved']}/{len(row['gold_campaign_ids'])}"
        )
        cc = row["code_checks"]
        lines.append(
            f"- **Code checks:** status={cc['status']}, "
            f"invalid_reason={cc['invalid_reason']}, "
            f"pass_rule={cc['pass_rule']}, "
            f"cites_gold={cc['cites_gold']}, "
            f"cites_all_gold={cc['cites_all_gold']}, "
            f"wrongly_declined={cc['wrongly_declined']}"
        )
        lines.append("")
        lines.append(f"**Question:** {row['question']}")
        lines.append("")
        lines.append(f"**Reference:** {row['reference_answer']}")
        lines.append("")
        lines.append(f"**Answer:** {row['answer_text']}")
        lines.append("")
        lines.append(f"**Human note:** {row['human_note']}")
        lines.append("")
        lines.append(f"**Judge reason:** {row['judge_reason']}")
        lines.append("")
        lines.append(f"**Category:** {row['category']}")

    lines.append("")
    lines.append("---")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Worksheet parsing
# ---------------------------------------------------------------------------

_HEADING_RE = re.compile(r"^### (.+)$", re.MULTILINE)
_TEMPLATE_RE = re.compile(r"^\- \*\*Template:\*\*[ \t]*(.*)$", re.MULTILINE)
_CATEGORY_RE = re.compile(r"^\*\*Category:\*\*[ \t]*(.*)$", re.MULTILINE)
_CAT_NUM_RE = re.compile(r"^(\d+[a-z]?)\b")


def parse_worksheet(text: str) -> list[dict]:
    """Parse a filled-in worksheet, extracting question_id and categories.

    Returns a list of dicts with keys: ``question_id``, ``template``,
    ``category_raw``, ``category_numbers``.
    """
    # Split into blocks by ### headings.
    heading_matches = list(_HEADING_RE.finditer(text))
    parsed: list[dict] = []

    for i, match in enumerate(heading_matches):
        qid = match.group(1).strip()

        # Block text is from this heading to the next heading (or end).
        start = match.end()
        end = (
            heading_matches[i + 1].start()
            if i + 1 < len(heading_matches)
            else len(text)
        )
        block = text[start:end]

        # Extract template.
        tmpl_match = _TEMPLATE_RE.search(block)
        template = tmpl_match.group(1).strip() if tmpl_match else ""

        # Extract category.
        cat_match = _CATEGORY_RE.search(block)
        category_raw = cat_match.group(1).strip() if cat_match else ""

        # Parse category numbers from semicolon-separated string.
        category_numbers: list[str] = []
        if category_raw:
            for part in category_raw.split(";"):
                part = part.strip()
                if not part:
                    continue
                num_match = _CAT_NUM_RE.match(part)
                if num_match:
                    category_numbers.append(num_match.group(1))

        parsed.append(
            {
                "question_id": qid,
                "template": template,
                "category_raw": category_raw,
                "category_numbers": category_numbers,
            }
        )

    return parsed


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_categories(
    parsed_rows: list[dict],
) -> tuple[list[str], list[str]]:
    """Validate parsed worksheet categories.

    Returns ``(empty_ids, unknown_ids)`` where:
    - *empty_ids*: question_ids with blank Category lines
    - *unknown_ids*: question_ids with category numbers not in the legend
    """
    empty_ids: list[str] = []
    unknown_ids: list[str] = []

    for row in parsed_rows:
        if not row["category_raw"]:
            empty_ids.append(row["question_id"])
            continue
        for num in row["category_numbers"]:
            if num not in VALID_CATEGORY_KEYS:
                unknown_ids.append(row["question_id"])
                break

    return empty_ids, unknown_ids


# ---------------------------------------------------------------------------
# Tallying
# ---------------------------------------------------------------------------


def tally_categories(
    parsed_rows: list[dict],
) -> tuple[dict[str, int], dict[str, dict[str, int]]]:
    """Count failures by category and by (template, category).

    A row with ``"5;6"`` counts once in category 5 and once in category 6.

    Returns ``(by_category, by_template)``.
    """
    by_category: dict[str, int] = {k: 0 for k in _LEGEND_ORDER}
    by_template: dict[str, dict[str, int]] = {}

    for row in parsed_rows:
        template = row["template"]
        if template not in by_template:
            by_template[template] = {k: 0 for k in _LEGEND_ORDER}

        for num in row["category_numbers"]:
            by_category[num] = by_category.get(num, 0) + 1
            by_template[template][num] = by_template[template].get(num, 0) + 1

    return by_category, by_template


# ---------------------------------------------------------------------------
# Tally markdown
# ---------------------------------------------------------------------------


def format_tally_markdown(
    by_category: dict[str, int],
    by_template: dict[str, dict[str, int]],
    n_rows: int,
    n_multi: int,
    commit: str,
    command: str,
) -> str:
    """Format the tally as markdown for docs/results.md."""
    lines: list[str] = []
    lines.append("```")
    lines.append(f"Command: {command}")
    lines.append(f"Date:    {date.today()}")
    lines.append(f"Commit:  {commit}")
    lines.append("```")
    lines.append("")
    lines.append(f"{n_rows} failure rows, {n_multi} with more than one category.")
    lines.append("")

    # By category table.
    lines.append("### By category")
    lines.append("")
    lines.append("| # | Category | Count |")
    lines.append("|---|----------|-------|")
    for key in _LEGEND_ORDER:
        count = by_category.get(key, 0)
        lines.append(f"| {key} | {CATEGORY_LEGEND[key]} | {count} |")
    lines.append("")

    # By template table.
    lines.append("### By template")
    lines.append("")
    header_cols = ["Template"] + _LEGEND_ORDER + ["Total"]
    lines.append("| " + " | ".join(header_cols) + " |")
    lines.append("|" + "|".join("---" for _ in header_cols) + "|")

    for template in sorted(by_template):
        counts = by_template[template]
        row_total = sum(counts.get(k, 0) for k in _LEGEND_ORDER)
        cells = (
            [template]
            + [str(counts.get(k, 0)) for k in _LEGEND_ORDER]
            + [str(row_total)]
        )
        lines.append("| " + " | ".join(cells) + " |")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Results.md writer
# ---------------------------------------------------------------------------


def update_failure_results_md(content: str, *, path: Path | None = None) -> None:
    """Replace or append the failure analysis section in docs/results.md."""
    update_results_section("## Failure analysis (Module 2.5)", content, path=path)
