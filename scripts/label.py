"""Interactive hand-labeling CLI for answer evaluation.

Usage:
    python scripts/label.py --answers PATH --n 100 --seed 42 [--labels PATH]

Picks rows with a fixed seed, stratified: all unanswerable rows, all
handwritten rows, then generated rows at random until n is reached.
Shows one row at a time for pass/fail labeling.  Resumable.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from recall_assistant.config import PROJECT_ROOT
from recall_assistant.evals.answer_eval import load_answers_jsonl
from recall_assistant.evals.golden import GoldenRow
from recall_assistant.evals.retrieval_eval import load_golden_rows

LABELS_DEFAULT = PROJECT_ROOT / "evals" / "labels.jsonl"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Hand-label answer rows as pass or fail.")
    p.add_argument("--answers", type=Path, required=True, help="Answers JSONL path")
    p.add_argument("--n", type=int, default=100, help="Number of rows to label")
    p.add_argument("--seed", type=int, default=42, help="Random seed")
    p.add_argument(
        "--labels", type=Path, default=LABELS_DEFAULT, help="Labels JSONL path"
    )
    return p.parse_args(argv)


# ---------------------------------------------------------------------------
# Stratified sampling
# ---------------------------------------------------------------------------


def select_rows(
    answer_rows: list[dict],
    golden_by_id: dict[str, GoldenRow],
    n: int,
    seed: int,
) -> list[dict]:
    """Pick *n* rows, stratified: all unanswerable, all handwritten, then random generated."""
    unanswerable: list[dict] = []
    handwritten: list[dict] = []
    generated: list[dict] = []

    for row in answer_rows:
        qid = row["question_id"]
        golden = golden_by_id.get(qid)
        if golden is None:
            continue
        if golden.source == "unanswerable":
            unanswerable.append(row)
        elif golden.source == "handwritten":
            handwritten.append(row)
        else:
            generated.append(row)

    fixed = unanswerable + handwritten
    remaining = n - len(fixed)
    if remaining <= 0:
        return fixed[:n]

    sampled = random.Random(seed).sample(generated, min(remaining, len(generated)))
    return fixed + sampled


# ---------------------------------------------------------------------------
# Labels I/O
# ---------------------------------------------------------------------------


def load_existing_labels(path: Path) -> dict[str, dict]:
    """Load existing labels, keyed by question_id.  Empty dict if missing."""
    if not path.exists():
        return {}
    labels: dict[str, dict] = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            labels[row["question_id"]] = row
    return labels


def append_label(path: Path, label_row: dict) -> None:
    """Append one label row to the JSONL file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(label_row, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------


def display_row(
    row: dict,
    golden: GoldenRow,
    index: int,
    total: int,
) -> None:
    """Print one row for labeling.  Does not show status, code checks, or faithfulness."""
    print("\n" + "=" * 72)
    print(f"Row {index} of {total} ({total - index} remaining)")
    print(f"\nQuestion: {golden.question}")
    print(f"\nReference answer: {golden.reference_answer}")

    retrieved_ids = row.get("retrieved_ids", [])
    retrieved_texts = row.get("retrieved_texts", [])
    print(f"\nRetrieved cards ({len(retrieved_ids)}):")
    for i, (cid, text) in enumerate(zip(retrieved_ids, retrieved_texts)):
        print(f"  [{cid}] {text[:300]}")

    print(f"\nAnswer: {row['answer_text']}")


# ---------------------------------------------------------------------------
# Interactive loop
# ---------------------------------------------------------------------------


def label_loop(
    selected: list[dict],
    golden_by_id: dict[str, GoldenRow],
    answers_file: str,
    labels_path: Path,
    existing: dict[str, dict],
    input_fn: Callable[[str], str] = input,
) -> None:
    """Label rows interactively.  Skips already-labelled IDs."""
    pending = [r for r in selected if r["question_id"] not in existing]
    total = len(pending)

    for i, row in enumerate(pending, 1):
        qid = row["question_id"]
        golden = golden_by_id[qid]
        display_row(row, golden, i, total)

        while True:
            choice = input_fn("\nLabel (p=pass, f=fail, q=quit): ").strip().lower()
            if choice in ("p", "f", "q"):
                break
            print("  Invalid input.  Enter p, f, or q.")

        if choice == "q":
            break

        note = input_fn("Note (optional, Enter to skip): ").rstrip("\n")
        label = "pass" if choice == "p" else "fail"
        label_row = {
            "question_id": qid,
            "answers_file": answers_file,
            "label": label,
            "note": note,
            "labelled_at": datetime.now().isoformat(),
        }
        append_label(labels_path, label_row)
        existing[qid] = label_row


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)

    # ── load answers ──
    _metadata, answer_rows = load_answers_jsonl(args.answers)

    # ── load golden rows ──
    golden_rows = load_golden_rows()
    golden_by_id: dict[str, GoldenRow] = {g.id: g for g in golden_rows}

    # ── validate n ──
    n_fixed = sum(
        1
        for r in answer_rows
        if golden_by_id.get(
            r["question_id"],
            GoldenRow(
                id="",
                question="",
                source="generated",
                gold_campaign_ids=[],
                reference_answer="",
                pass_rule="cite_any_gold",
            ),
        ).source
        in ("unanswerable", "handwritten")
    )
    if args.n < n_fixed:
        print(
            f"ERROR: --n {args.n} is below the unanswerable + handwritten "
            f"count ({n_fixed}).  All of those are always included.",
            file=sys.stderr,
        )
        sys.exit(1)

    # ── stratified selection ──
    selected = select_rows(answer_rows, golden_by_id, args.n, args.seed)

    # ── print chosen IDs ──
    chosen_ids = sorted(r["question_id"] for r in selected)
    print(f"Selected {len(chosen_ids)} rows:")
    for qid in chosen_ids:
        print(f"  {qid}")

    # ── load existing labels ──
    existing = load_existing_labels(args.labels)

    # ── warn on answers_file mismatch ──
    answers_basename = args.answers.name
    mismatched = [
        qid
        for qid, lab in existing.items()
        if lab.get("answers_file") != answers_basename
    ]
    if mismatched:
        print(
            f"WARNING: {len(mismatched)} existing label(s) have a different "
            f"answers_file than {answers_basename!r}.",
            file=sys.stderr,
        )

    # ── label ──
    done = sum(1 for r in selected if r["question_id"] in existing)
    remaining = len(selected) - done
    print(f"\n{done} already labelled, {remaining} remaining.\n")
    label_loop(selected, golden_by_id, answers_basename, args.labels, existing)


if __name__ == "__main__":
    main()
