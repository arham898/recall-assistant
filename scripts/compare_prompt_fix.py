"""Compare a prompt-fix answer run against the 19 Oct baseline.

Usage:
    python scripts/compare_prompt_fix.py \
        --old-answers  results/answers_hybrid_record_unfiltered_2026-10-19.jsonl \
        --old-scored   results/scored_hybrid_record_unfiltered_2026-10-19.jsonl \
        --new-answers  results/answers_hybrid_record_unfiltered_YYYY-MM-DD.jsonl \
        --new-scored   results/scored_hybrid_record_unfiltered_YYYY-MM-DD.jsonl

Writes a comparison section to docs/results.md via update_results_section().
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from recall_assistant.evals.prompt_fix_comparison import (
    format_comparison,
    load_answer_texts,
    load_scored,
)
from recall_assistant.evals.run_metadata import (
    git_commit,
    is_worktree_dirty,
    update_results_section,
)


def section_header(tag: str | None = None) -> str:
    """Build the results.md section header, optionally including a tag."""
    if tag:
        return f"## Prompt-fix comparison: {tag} (Module 2.5b)"
    return "## Prompt-fix comparison (Module 2.5b)"


# Backward-compatible constant for the untagged case.
SECTION_HEADER = section_header()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compare a prompt-fix run against the baseline",
    )
    parser.add_argument(
        "--old-answers", required=True, type=Path, help="Baseline answers JSONL"
    )
    parser.add_argument(
        "--old-scored", required=True, type=Path, help="Baseline scored JSONL"
    )
    parser.add_argument(
        "--new-answers", required=True, type=Path, help="New answers JSONL"
    )
    parser.add_argument(
        "--new-scored", required=True, type=Path, help="New scored JSONL"
    )
    parser.add_argument(
        "--results-md",
        type=Path,
        default=None,
        help="Override output path (default: docs/results.md)",
    )
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="Allow running on a dirty worktree",
    )
    parser.add_argument(
        "--tag",
        default=None,
        help="Optional tag included in the results.md section header",
    )
    return parser.parse_args(argv)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)

    # Validate input files exist.
    for label, path in [
        ("--old-answers", args.old_answers),
        ("--old-scored", args.old_scored),
        ("--new-answers", args.new_answers),
        ("--new-scored", args.new_scored),
    ]:
        if not path.exists():
            print(f"ERROR: {label} file not found: {path}", file=sys.stderr)
            sys.exit(1)

    # Dirty-tree guard.
    if not args.allow_dirty and is_worktree_dirty():
        print(
            "ERROR: worktree is dirty. Commit first, so the results "
            "record a clean commit.",
            file=sys.stderr,
        )
        sys.exit(1)

    commit = git_commit()
    command = "python " + " ".join(sys.argv)

    # Load data.
    old_scored = load_scored(args.old_scored)
    new_scored = load_scored(args.new_scored)
    old_texts = load_answer_texts(args.old_answers)
    new_texts = load_answer_texts(args.new_answers)

    # Format and write.
    content = format_comparison(
        old_scored, new_scored, old_texts, new_texts, command, commit
    )
    header = section_header(args.tag)
    update_results_section(header, content, path=args.results_md)

    # Print to stdout.
    print(f"{header}\n")
    print(content)


if __name__ == "__main__":
    main()
