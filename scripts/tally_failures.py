"""Tally categorized failures from a filled-in worksheet.

Usage:
    python scripts/tally_failures.py --worksheet docs/error-analysis.md

Parses and validates the worksheet before checking the worktree, so
category errors are reported without requiring a clean tree.  Refuses
to write results on a dirty worktree (no override).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from recall_assistant.evals.failure_analysis import (
    format_tally_markdown,
    parse_worksheet,
    tally_categories,
    update_failure_results_md,
    validate_categories,
)
from recall_assistant.evals.run_metadata import git_commit, is_worktree_dirty

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Tally categorized failures from a worksheet",
    )
    parser.add_argument(
        "--worksheet",
        required=True,
        type=Path,
        help="Path to the filled-in error-analysis.md",
    )
    return parser.parse_args(argv)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)

    # 1. Parse worksheet (before dirty-tree guard).
    text = args.worksheet.read_text(encoding="utf-8")
    parsed_rows = parse_worksheet(text)

    # 2. Validate categories (before dirty-tree guard).
    empty_ids, unknown_ids = validate_categories(parsed_rows)

    if empty_ids:
        print(
            f"ERROR: {len(empty_ids)} row(s) have empty Category lines: {empty_ids}",
            file=sys.stderr,
        )
        sys.exit(1)

    if unknown_ids:
        print(
            f"ERROR: {len(unknown_ids)} row(s) have unknown category numbers: "
            f"{unknown_ids}",
            file=sys.stderr,
        )
        sys.exit(1)

    # 3. Dirty-tree guard (after validation, before any write).
    if is_worktree_dirty():
        print(
            "ERROR: worktree is dirty. Commit first, so the results "
            "record a clean commit.",
            file=sys.stderr,
        )
        sys.exit(1)

    commit = git_commit()

    # 4. Tally and write.
    by_category, by_template = tally_categories(parsed_rows)
    n_multi = sum(1 for r in parsed_rows if len(r["category_numbers"]) > 1)

    command = "python " + " ".join(sys.argv)
    content = format_tally_markdown(
        by_category,
        by_template,
        len(parsed_rows),
        n_multi,
        commit,
        command,
    )
    update_failure_results_md(content)

    summary = f"{len(parsed_rows)} failure rows, {n_multi} with more than one category."
    print(summary)


if __name__ == "__main__":
    main()
