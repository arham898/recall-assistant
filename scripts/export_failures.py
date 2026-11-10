"""Export a failure analysis worksheet from scored + label + judge data.

Usage:
    python scripts/export_failures.py \\
        --answers PATH --scored PATH --labels PATH --judge-rows PATH

Refuses to run on a dirty worktree (no override).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

from recall_assistant.config import PROJECT_ROOT
from recall_assistant.evals.answer_eval import (
    load_answers_jsonl,
    load_labels,
    read_jsonl,
)
from recall_assistant.evals.failure_analysis import (
    build_failure_rows,
    format_worksheet,
)
from recall_assistant.evals.retrieval_eval import load_golden_rows
from recall_assistant.evals.run_metadata import git_commit, is_worktree_dirty

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export a failure analysis worksheet",
    )
    parser.add_argument(
        "--answers",
        required=True,
        type=Path,
        help="Answers JSONL from run_answers.py",
    )
    parser.add_argument(
        "--scored",
        required=True,
        type=Path,
        help="Scored JSONL from score_answers.py",
    )
    parser.add_argument(
        "--labels",
        required=True,
        type=Path,
        help="Hand labels JSONL",
    )
    parser.add_argument(
        "--judge-rows",
        required=True,
        type=Path,
        help="Judge rows JSONL",
    )
    return parser.parse_args(argv)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)

    if is_worktree_dirty():
        print(
            "ERROR: worktree is dirty. Commit first, so the worksheet "
            "records a clean commit.",
            file=sys.stderr,
        )
        sys.exit(1)

    commit = git_commit()

    # Load inputs.
    metadata, answer_rows = load_answers_jsonl(args.answers)
    scored = read_jsonl(args.scored)
    labels = load_labels(args.labels)
    judge_rows_data = read_jsonl(args.judge_rows)

    golden_by_id = {g.id: g for g in load_golden_rows()}

    # Build failure rows.
    try:
        failure_rows = build_failure_rows(
            answer_rows,
            scored,
            labels,
            judge_rows_data,
            golden_by_id,
        )
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    # Write worksheet.
    worksheet_path = PROJECT_ROOT / "docs" / "error-analysis.md"
    worksheet_path.write_text(
        format_worksheet(failure_rows, commit),
        encoding="utf-8",
    )

    # Write JSONL.
    label = metadata.get("label", "unknown")
    jsonl_path = PROJECT_ROOT / "results" / f"failures_{label}_{date.today()}.jsonl"
    with open(jsonl_path, "w", encoding="utf-8") as f:
        f.writelines(json.dumps(row, ensure_ascii=False) + "\n" for row in failure_rows)

    print(f"{len(failure_rows)} failure rows written.")
    print(f"  Worksheet: {worksheet_path}")
    print(f"  JSONL:     {jsonl_path}")


if __name__ == "__main__":
    main()
