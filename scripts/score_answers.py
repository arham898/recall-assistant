"""Score an answers JSONL file and update docs/results.md.

Usage:
    python scripts/score_answers.py --answers PATH [--faithfulness {hhem,none}]

Reads the JSONL produced by ``run_answers.py``, applies code checks
from ``answer_checks.py``, optionally scores faithfulness with HHEM,
writes a scored JSONL to results/, and appends a labelled section to
docs/results.md.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

from recall_assistant.config import PROJECT_ROOT
from recall_assistant.evals.answer_eval import (
    format_answer_markdown,
    load_answers_jsonl,
    score_answers,
    update_answer_results_md,
)
from recall_assistant.evals.retrieval_eval import load_golden_rows

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Score an answers JSONL file")
    parser.add_argument(
        "--answers",
        required=True,
        type=Path,
        help="Path to the answers JSONL from run_answers.py",
    )
    parser.add_argument(
        "--faithfulness",
        choices=["hhem", "none"],
        default="hhem",
        help="Faithfulness scorer (default: hhem)",
    )
    return parser.parse_args(argv)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)

    if not args.answers.exists():
        print(f"ERROR: file not found: {args.answers}", file=sys.stderr)
        sys.exit(1)

    # Load answers and golden rows.
    metadata, rows = load_answers_jsonl(args.answers)
    all_golden = load_golden_rows()
    golden_by_id = {g.id: g for g in all_golden}

    # Build scorer.
    scorer = None
    if args.faithfulness == "hhem":
        from recall_assistant.evals.faithfulness import MODEL_NAME, REVISION, HHEMScorer

        scorer = HHEMScorer()
        metadata["faithfulness_model"] = MODEL_NAME
        metadata["faithfulness_revision"] = REVISION

    # Score.
    scores, checked = score_answers(rows, golden_by_id, scorer=scorer)

    # Write scored JSONL.
    label = metadata.get("label", "unknown")
    results_dir = PROJECT_ROOT / "results"
    results_dir.mkdir(parents=True, exist_ok=True)
    scored_path = results_dir / f"scored_{label}_{date.today()}.jsonl"

    with open(scored_path, "w", encoding="utf-8") as f:
        f.writelines(
            json.dumps(checked_row, ensure_ascii=False) + "\n"
            for checked_row in checked
        )
    print(f"Scored JSONL written to {scored_path}")

    # Update docs/results.md.
    command = "python " + " ".join(sys.argv)
    md_content = format_answer_markdown(scores, metadata, command)
    update_answer_results_md(label, md_content)
    print(f"docs/results.md updated (label={label})")

    # Summary to stdout.
    gen = scores.get("generated", {})
    hw = scores.get("handwritten", {})
    u = scores.get("unanswerable", {})
    print(f"\nGenerated:   {gen.get('passed', 0)}/{gen.get('n', 0)} passed")
    print(f"Handwritten: {hw.get('passed', 0)}/{hw.get('n', 0)} passed")
    print(
        f"Unanswerable: {u.get('declined', 0)} declined, "
        f"{u.get('invalid', 0)} invalid, {u.get('answered', 0)} answered"
    )
    c = scores.get("cost", {})
    print(f"Total cost: ${c.get('total', 0):.5f}")


if __name__ == "__main__":
    main()
