"""Calibrate the LLM judge against hand labels.

Usage:
    python scripts/calibrate_judge.py --labels evals/labels.jsonl \
        --answers PATH --prompt-version v1 [--allow-dirty]

Splits labelled rows 50/50 into dev and test, runs the judge on all of
them (cached), and reports agreement metrics for each split.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from datetime import date
from pathlib import Path
from typing import Any

from recall_assistant.config import PROJECT_ROOT, Settings
from recall_assistant.evals.answer_eval import (
    load_answers_jsonl,
    load_labels,
    read_jsonl,
)
from recall_assistant.evals.judge import (
    JUDGE_PROMPTS,
    JudgeResult,
    judge,
    judge_prompt_hash,
)
from recall_assistant.evals.retrieval_eval import load_golden_rows
from recall_assistant.evals.run_metadata import (
    git_commit,
    is_worktree_dirty,
    update_results_section,
)
from recall_assistant.evals.run_metadata import (
    sanitise_path_component as _sanitise,
)

JUDGE_CACHE_DIR = PROJECT_ROOT / ".cache" / "judge"
CALIBRATION_JSONL = PROJECT_ROOT / "results" / "judge_calibration.jsonl"
SPLIT_SEED = 42


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Calibrate the LLM judge against hand labels."
    )
    p.add_argument(
        "--labels",
        type=Path,
        default=PROJECT_ROOT / "evals" / "labels.jsonl",
        help="Labels JSONL path",
    )
    p.add_argument("--answers", type=Path, required=True, help="Answers JSONL path")
    p.add_argument("--prompt-version", default="v1", help="Judge prompt version key")
    p.add_argument("--allow-dirty", action="store_true", help="Allow dirty worktree")
    return p.parse_args(argv)


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


def _cache_key(
    question_id: str,
    answers_file: str,
    model: str,
    prompt_version: str,
    p_hash: str,
) -> str:
    """Deterministic filename for one judge cache entry."""
    parts = [
        _sanitise(question_id),
        _sanitise(answers_file),
        _sanitise(model),
        _sanitise(prompt_version),
        _sanitise(p_hash),
    ]
    return "__".join(parts) + ".json"


def _load_cache(key: str) -> dict | None:
    path = JUDGE_CACHE_DIR / key
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return None


def _save_cache(key: str, result: JudgeResult, question_id: str) -> None:
    JUDGE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    (JUDGE_CACHE_DIR / key).write_text(
        json.dumps(
            {
                "question_id": question_id,
                "verdict": result.verdict,
                "reason": result.reason,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Dev/test split
# ---------------------------------------------------------------------------


def split_dev_test(
    label_rows: list[dict],
    seed: int = 42,
) -> tuple[list[dict], list[dict]]:
    """Split label rows 50/50 into dev and test, deterministic by seed."""
    sorted_rows = sorted(label_rows, key=lambda r: r["question_id"])
    copy = list(sorted_rows)
    random.Random(seed).shuffle(copy)
    mid = len(copy) // 2
    return copy[:mid], copy[mid:]


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def compute_metrics(
    predictions: list[str],
    labels: list[str],
) -> dict[str, Any]:
    """Compute agreement, TPR, TNR, precision/recall on fail, kappa, confusion matrix."""
    n = len(predictions)
    tp = fp = fn = tn = 0
    for pred, lab in zip(predictions, labels):
        if pred == "pass" and lab == "pass":
            tp += 1
        elif pred == "pass" and lab == "fail":
            fp += 1
        elif pred == "fail" and lab == "pass":
            fn += 1
        else:
            tn += 1

    agreement = (tp + tn) / n if n else 0.0
    tpr = tp / (tp + fn) if (tp + fn) else None
    tnr = tn / (tn + fp) if (tn + fp) else None
    precision_fail = tn / (fn + tn) if (fn + tn) else None
    recall_fail = tn / (tn + fp) if (tn + fp) else None

    # Cohen's kappa
    if n == 0:
        kappa = 0.0
    else:
        p_o = agreement
        p_yes = ((tp + fp) / n) * ((tp + fn) / n)
        p_no = ((fn + tn) / n) * ((fp + tn) / n)
        p_e = p_yes + p_no
        kappa = (p_o - p_e) / (1 - p_e) if p_e != 1.0 else 0.0

    return {
        "n": n,
        "agreement": agreement,
        "tpr": tpr,
        "tnr": tnr,
        "precision_fail": precision_fail,
        "recall_fail": recall_fail,
        "kappa": kappa,
        "confusion": {"tp": tp, "fp": fp, "fn": fn, "tn": tn},
    }


# ---------------------------------------------------------------------------
# Scored file helpers
# ---------------------------------------------------------------------------


def _scored_path_from_answers(answers_path: Path) -> Path:
    """Derive scored file path from answers path."""
    name = answers_path.name
    if not name.startswith("answers_"):
        msg = f"Answers file does not match expected pattern: {name}"
        raise ValueError(msg)
    scored_name = "scored_" + name[len("answers_") :]
    return answers_path.parent / scored_name


# ---------------------------------------------------------------------------
# Code-check vs human comparison
# ---------------------------------------------------------------------------


def compute_code_vs_human(
    scored_rows: list[dict],
    labels_by_qid: dict[str, str],
) -> dict[str, Any]:
    """Compare pass_rule_passed from scored file with human labels."""
    both_pass = code_pass_human_fail = code_fail_human_pass = both_fail = 0

    for row in scored_rows:
        qid = row["question_id"]
        if qid not in labels_by_qid:
            continue
        code = "pass" if row["pass_rule_passed"] else "fail"
        human = labels_by_qid[qid]
        if code == "pass" and human == "pass":
            both_pass += 1
        elif code == "pass" and human == "fail":
            code_pass_human_fail += 1
        elif code == "fail" and human == "pass":
            code_fail_human_pass += 1
        else:
            both_fail += 1

    total = both_pass + code_pass_human_fail + code_fail_human_pass + both_fail
    agreement = (both_pass + both_fail) / total if total else 0.0

    return {
        "both_pass": both_pass,
        "code_pass_human_fail": code_pass_human_fail,
        "code_fail_human_pass": code_fail_human_pass,
        "both_fail": both_fail,
        "agreement": agreement,
    }


# ---------------------------------------------------------------------------
# Faithfulness by label
# ---------------------------------------------------------------------------


def compute_faithfulness_by_label(
    scored_rows: list[dict],
    labels_by_qid: dict[str, str],
) -> dict[str, Any]:
    """Mean faithfulness for human-pass vs human-fail rows (answered only)."""
    pass_scores: list[float] = []
    fail_scores: list[float] = []

    for row in scored_rows:
        qid = row["question_id"]
        if qid not in labels_by_qid:
            continue
        faith = row.get("faithfulness")
        if faith is None:
            continue
        if labels_by_qid[qid] == "pass":
            pass_scores.append(faith)
        else:
            fail_scores.append(faith)

    def _mean(xs: list[float]) -> float | None:
        return sum(xs) / len(xs) if xs else None

    return {
        "pass_mean": _mean(pass_scores),
        "pass_n": len(pass_scores),
        "fail_mean": _mean(fail_scores),
        "fail_n": len(fail_scores),
    }


# ---------------------------------------------------------------------------
# Cost estimation
# ---------------------------------------------------------------------------


def _estimate_cost(
    prompt_text: str,
    questions: list[str],
    references: list[str],
    candidates: list[str],
    model: str,
    settings: Settings,
) -> float | None:
    """Estimate upper-bound cost for running the judge on all rows."""
    prices = settings.llm_model_prices.get(model)
    if prices is None:
        return None
    inp_price, out_price, _date = prices

    system_chars = len(prompt_text)
    total_user_chars = sum(
        len(q) + len(r) + len(c) + 60  # 60 for "Question: " etc. labels
        for q, r, c in zip(questions, references, candidates)
    )
    avg_user_chars = total_user_chars / len(questions) if questions else 0
    avg_prompt_tokens = (system_chars + avg_user_chars) / 4  # rough char→token
    completion_tokens = 256  # max_tokens

    per_call = (
        avg_prompt_tokens * inp_price + completion_tokens * out_price
    ) / 1_000_000
    return per_call * len(questions)


# ---------------------------------------------------------------------------
# Results output
# ---------------------------------------------------------------------------


def format_calibration_markdown(
    dev_metrics: dict,
    test_metrics: dict,
    code_vs_human: dict,
    faith_by_label: dict,
    error_count: int,
    error_ids: list[str],
    *,
    prompt_version: str,
    model: str,
    command: str,
) -> str:
    """Build the markdown section body for docs/results.md."""
    lines = [
        "```",
        f"Command: {command}",
        f"Date:    {date.today()}",
        "```",
        "",
    ]

    lines.append(f"Prompt version: {prompt_version}  ")
    lines.append(f"Model: {model}  ")
    lines.append("")

    if error_count:
        lines.append(f"**Judge errors**: {error_count} rows excluded from metrics:")
        for eid in error_ids:
            lines.append(f"- `{eid}`")
        lines.append("")

    for label, m in [("Dev", dev_metrics), ("Test", test_metrics)]:
        c = m["confusion"]
        lines.append(f"### {label} split (n={m['n']})")
        lines.append("")
        lines.append("| Metric | Value |")
        lines.append("|--------|-------|")
        lines.append(f"| Agreement | {m['agreement']:.3f} |")
        tpr_str = f"{m['tpr']:.3f}" if m["tpr"] is not None else "n/a"
        tnr_str = f"{m['tnr']:.3f}" if m["tnr"] is not None else "n/a"
        pf_str = (
            f"{m['precision_fail']:.3f}" if m["precision_fail"] is not None else "n/a"
        )
        rf_str = f"{m['recall_fail']:.3f}" if m["recall_fail"] is not None else "n/a"
        lines.append(f"| TPR (sensitivity) | {tpr_str} |")
        lines.append(f"| TNR (specificity) | {tnr_str} |")
        lines.append(f"| Precision (fail) | {pf_str} |")
        lines.append(f"| Recall (fail) | {rf_str} |")
        lines.append(f"| Cohen's kappa | {m['kappa']:.3f} |")
        lines.append("")
        lines.append("Confusion matrix (rows=judge, cols=human):")
        lines.append("")
        lines.append("| | Human pass | Human fail |")
        lines.append("|---|---|---|")
        lines.append(f"| Judge pass | {c['tp']} | {c['fp']} |")
        lines.append(f"| Judge fail | {c['fn']} | {c['tn']} |")
        lines.append("")

    cv = code_vs_human
    lines.append("### Code checks vs human labels")
    lines.append("")
    lines.append("| | Human pass | Human fail |")
    lines.append("|---|---|---|")
    lines.append(f"| Code pass | {cv['both_pass']} | {cv['code_pass_human_fail']} |")
    lines.append(f"| Code fail | {cv['code_fail_human_pass']} | {cv['both_fail']} |")
    lines.append(f"\nAgreement: {cv['agreement']:.3f}")
    lines.append("")

    fb = faith_by_label
    lines.append("### Faithfulness by human label (answered rows)")
    lines.append("")
    pm = f"{fb['pass_mean']:.3f}" if fb["pass_mean"] is not None else "n/a"
    fm = f"{fb['fail_mean']:.3f}" if fb["fail_mean"] is not None else "n/a"
    lines.append("| Label | Mean faithfulness | n |")
    lines.append("|-------|-------------------|---|")
    lines.append(f"| Pass | {pm} | {fb['pass_n']} |")
    lines.append(f"| Fail | {fm} | {fb['fail_n']} |")

    return "\n".join(lines)


def update_calibration_results_md(content: str, *, path: Path | None = None) -> None:
    """Write the calibration section to docs/results.md."""
    header = "## Judge calibration (Module 2.4)"
    update_results_section(header, content, path=path)


def write_judge_rows(
    dev_labels: list[dict],
    test_labels: list[dict],
    judge_verdicts: dict[str, JudgeResult],
    labels_by_qid_full: dict[str, dict],
    *,
    answers_file: str,
    prompt_version: str,
    p_hash: str,
    results_dir: Path | None = None,
) -> Path:
    """Write per-row judge dump to results/judge_rows_{version}_{date}.jsonl."""
    out_dir = results_dir or (PROJECT_ROOT / "results")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"judge_rows_{prompt_version}_{date.today()}.jsonl"

    dev_ids = {lab["question_id"] for lab in dev_labels}

    with open(out_path, "w", encoding="utf-8") as f:
        for lab in dev_labels + test_labels:
            qid = lab["question_id"]
            jr = judge_verdicts.get(qid)
            if jr is None:
                continue
            full_lab = labels_by_qid_full.get(qid, {})
            row = {
                "question_id": qid,
                "split": "dev" if qid in dev_ids else "test",
                "human_label": lab["label"],
                "judge_verdict": jr.verdict,
                "judge_reason": jr.reason,
                "human_note": full_lab.get("note", ""),
                "answers_file": answers_file,
                "prompt_version": prompt_version,
                "prompt_hash": p_hash,
            }
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    return out_path


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    settings = Settings()
    model = settings.llm_model

    # ── validate prompt version ──
    if args.prompt_version not in JUDGE_PROMPTS:
        print(
            f"ERROR: unknown prompt version {args.prompt_version!r}; "
            f"available: {sorted(JUDGE_PROMPTS)}",
            file=sys.stderr,
        )
        sys.exit(1)
    prompt_text = JUDGE_PROMPTS[args.prompt_version]
    p_hash = judge_prompt_hash(prompt_text)

    # ── dirty worktree guard ──
    if is_worktree_dirty() and not args.allow_dirty:
        print(
            "ERROR: worktree is dirty.  Commit first so git_commit is "
            "accurate, or pass --allow-dirty.",
            file=sys.stderr,
        )
        sys.exit(1)

    # ── load data ──
    labels_by_qid_full = load_labels(args.labels)
    label_rows = list(labels_by_qid_full.values())

    _metadata, answer_rows = load_answers_jsonl(args.answers)
    answers_by_qid = {r["question_id"]: r for r in answer_rows}

    golden_rows = load_golden_rows()
    golden_by_id = {g.id: g for g in golden_rows}

    scored_path = _scored_path_from_answers(args.answers)
    scored_rows = read_jsonl(scored_path)

    answers_file = args.answers.name

    # ── cost estimate ──
    questions = []
    references = []
    candidates = []
    for lab in label_rows:
        qid = lab["question_id"]
        a_row = answers_by_qid.get(qid)
        g_row = golden_by_id.get(qid)
        if a_row and g_row:
            questions.append(g_row.question)
            references.append(g_row.reference_answer)
            candidates.append(a_row["answer_text"])

    ub = _estimate_cost(prompt_text, questions, references, candidates, model, settings)
    if ub is not None:
        print(f"Cost projection (upper bound): ${ub:.4f}")
        if ub > 1.0:
            print(
                f"ERROR: projected cost ${ub:.4f} exceeds $1.00.",
                file=sys.stderr,
            )
            sys.exit(1)

    # ── build judge client ──
    from recall_assistant.llm.openrouter import build_openrouter_client

    llm_client = build_openrouter_client(settings, temperature=0.0)

    # ── split dev/test ──
    dev_labels, test_labels = split_dev_test(label_rows, seed=SPLIT_SEED)

    # ── run judge on all labels ──
    judge_verdicts: dict[str, JudgeResult] = {}
    error_ids: list[str] = []

    all_labels = dev_labels + test_labels
    for i, lab in enumerate(all_labels, 1):
        qid = lab["question_id"]
        a_row = answers_by_qid.get(qid)
        g_row = golden_by_id.get(qid)
        if not a_row or not g_row:
            continue

        # check cache
        key = _cache_key(qid, answers_file, model, args.prompt_version, p_hash)
        cached = _load_cache(key)
        if cached is not None:
            judge_verdicts[qid] = JudgeResult(
                verdict=cached["verdict"], reason=cached["reason"]
            )
            print(f"  [{i}/{len(all_labels)}] {qid}: {cached['verdict']} (cached)")
            continue

        result = judge(
            question=g_row.question,
            reference_answer=g_row.reference_answer,
            candidate_answer=a_row["answer_text"],
            llm_client=llm_client,
            model=model,
            prompt_version=args.prompt_version,
        )
        judge_verdicts[qid] = result
        print(f"  [{i}/{len(all_labels)}] {qid}: {result.verdict}")

        # cache only pass/fail, not error
        if result.verdict != "error":
            _save_cache(key, result, qid)
        else:
            error_ids.append(qid)

    # ── compute metrics ──
    labels_by_qid = {lab["question_id"]: lab["label"] for lab in label_rows}

    def _split_metrics(split_labels: list[dict]) -> dict:
        preds = []
        labs = []
        for lab in split_labels:
            qid = lab["question_id"]
            jr = judge_verdicts.get(qid)
            if jr is None or jr.verdict == "error":
                continue
            preds.append(jr.verdict)
            labs.append(lab["label"])
        return compute_metrics(preds, labs)

    dev_metrics = _split_metrics(dev_labels)
    test_metrics = _split_metrics(test_labels)

    # ── code-check vs human ──
    code_vs_human = compute_code_vs_human(scored_rows, labels_by_qid)

    # ── faithfulness by label ──
    faith_by_label = compute_faithfulness_by_label(scored_rows, labels_by_qid)

    # ── print report ──
    error_count = len(error_ids)
    if error_count:
        print(f"\nJudge errors ({error_count}):")
        for eid in error_ids:
            jr = judge_verdicts[eid]
            print(f"  {eid}: {jr.reason}")

    print("\n-- Dev split --")
    _print_metrics(dev_metrics)
    print("\n-- Test split --")
    _print_metrics(test_metrics)

    print("\n-- Code checks vs human labels --")
    cv = code_vs_human
    print(f"  Both pass:           {cv['both_pass']}")
    print(f"  Code pass/human fail: {cv['code_pass_human_fail']}")
    print(f"  Code fail/human pass: {cv['code_fail_human_pass']}")
    print(f"  Both fail:           {cv['both_fail']}")
    print(f"  Agreement:           {cv['agreement']:.3f}")

    print("\n-- Faithfulness by human label --")
    fb = faith_by_label
    pm = f"{fb['pass_mean']:.3f}" if fb["pass_mean"] is not None else "n/a"
    fm = f"{fb['fail_mean']:.3f}" if fb["fail_mean"] is not None else "n/a"
    print(f"  Pass mean: {pm} (n={fb['pass_n']})")
    print(f"  Fail mean: {fm} (n={fb['fail_n']})")

    # ── append to calibration JSONL ──
    cal_row = {
        "prompt_version": args.prompt_version,
        "model": model,
        "split_seed": SPLIT_SEED,
        "dev": dev_metrics,
        "test": test_metrics,
        "code_vs_human": code_vs_human,
        "faithfulness_by_label": faith_by_label,
        "error_count": error_count,
        "error_ids": error_ids,
        "commit": git_commit(),
        "date": str(date.today()),
    }
    CALIBRATION_JSONL.parent.mkdir(parents=True, exist_ok=True)
    with open(CALIBRATION_JSONL, "a", encoding="utf-8") as f:
        f.write(json.dumps(cal_row, ensure_ascii=False) + "\n")
    print(f"\nCalibration row appended to {CALIBRATION_JSONL}")

    # ── per-row dump (before results.md so verdicts survive a formatting failure) ──
    dump_path = write_judge_rows(
        dev_labels,
        test_labels,
        judge_verdicts,
        labels_by_qid_full,
        answers_file=answers_file,
        prompt_version=args.prompt_version,
        p_hash=p_hash,
    )
    print(f"Per-row dump written to {dump_path}")

    # ── update docs/results.md ──
    command = (
        f"python scripts/calibrate_judge.py "
        f"--labels {args.labels} --answers {args.answers} "
        f"--prompt-version {args.prompt_version}"
    )
    md = format_calibration_markdown(
        dev_metrics,
        test_metrics,
        code_vs_human,
        faith_by_label,
        error_count,
        error_ids,
        prompt_version=args.prompt_version,
        model=model,
        command=command,
    )
    update_calibration_results_md(md)
    print("docs/results.md updated.")


def _print_metrics(m: dict) -> None:
    c = m["confusion"]
    print(f"  n={m['n']}  agreement={m['agreement']:.3f}  kappa={m['kappa']:.3f}")
    tpr_str = f"{m['tpr']:.3f}" if m["tpr"] is not None else "n/a"
    tnr_str = f"{m['tnr']:.3f}" if m["tnr"] is not None else "n/a"
    print(f"  TPR={tpr_str}  TNR={tnr_str}")
    pf = f"{m['precision_fail']:.3f}" if m["precision_fail"] is not None else "n/a"
    rf = f"{m['recall_fail']:.3f}" if m["recall_fail"] is not None else "n/a"
    print(f"  Precision(fail)={pf}  Recall(fail)={rf}")
    print(f"  TP={c['tp']}  FP={c['fp']}  FN={c['fn']}  TN={c['tn']}")


if __name__ == "__main__":
    main()
