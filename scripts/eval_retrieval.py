"""Evaluate retrieval quality on the golden evaluation set.

Usage:
    python scripts/eval_retrieval.py --label NAME [--limit N]

Thin CLI wrapper.  Testable logic lives in
``recall_assistant.evals.retrieval_eval``.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from datetime import date
from typing import Any

from psycopg_pool import ConnectionPool

from recall_assistant.config import PROJECT_ROOT, Settings
from recall_assistant.evals.retrieval_eval import (
    CHUNKERS,
    MODES,
    aggregate_metrics,
    bucket_label,
    card_seal,
    classify_gold_visibility,
    compute_found_rates,
    format_markdown,
    get_vehicle_recall_count,
    has_complete_vehicle,
    load_golden_rows,
    percentile,
    run_retrieval_loop,
    update_results_md,
)
from recall_assistant.evals.run_metadata import git_commit, is_worktree_dirty
from recall_assistant.retrieval import VehicleFilter
from recall_assistant.retrieval.retriever import Retriever

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluate retrieval on golden set")
    parser.add_argument("--label", required=True, help="Run label for output files")
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Limit to first N rows (timing only, no output files)",
    )
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="Allow running on a dirty worktree (not for baseline)",
    )
    return parser.parse_args(argv)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    settings = Settings()

    # ── dirty worktree guard ──
    dirty = is_worktree_dirty()
    if dirty and not args.limit and not args.allow_dirty:
        print(
            "ERROR: worktree is dirty.  Commit first so git_commit is "
            "accurate, or pass --allow-dirty.",
            file=sys.stderr,
        )
        sys.exit(1)

    # ── load golden rows ──
    all_rows = load_golden_rows()
    answerable = [r for r in all_rows if r.gold_campaign_ids]
    unanswerable = [r for r in all_rows if not r.gold_campaign_ids]
    full_answerable = len(answerable)
    full_unanswerable = len(unanswerable)

    if args.limit:
        answerable = answerable[: args.limit]
        unanswerable = unanswerable[: args.limit]

    gt5 = sum(1 for r in answerable if len(r.gold_campaign_ids) > 5)

    # ── connect ──
    pool = ConnectionPool(settings.database_url, min_size=1, max_size=4)

    # ── build retrievers ──
    from recall_assistant.ingest.embedder import SentenceTransformerEmbedder
    from recall_assistant.retrieval.rerank import CrossEncoderReranker

    embedder = SentenceTransformerEmbedder(settings.embedding_model)
    reranker = CrossEncoderReranker(settings.rerank_model)
    retrievers: dict[str, Retriever] = {}
    for chunker in CHUNKERS:
        retrievers[chunker] = Retriever(
            pool, embedder, reranker=reranker, chunker=chunker
        )

    # ── warm-up: every mode on each retriever ──
    print("Warming up retrievers...")
    n_warmup = len(CHUNKERS) * len(MODES)
    t0_warmup = time.perf_counter()
    for ret in retrievers.values():
        for mode in MODES:
            getattr(ret, mode)(question="warmup query", k=1)
    t_warmup = time.perf_counter() - t0_warmup
    print(f"  {n_warmup} warm-up calls in {t_warmup:.1f}s")

    # ── main retrieval loop ──
    print(f"Running retrieval on {len(answerable)} answerable rows...")
    t_start = time.perf_counter()
    per_question, n_unfiltered_only = run_retrieval_loop(retrievers, answerable)
    t_main = time.perf_counter() - t_start
    n_with_vehicle = len(answerable) - n_unfiltered_only
    n_main_calls = n_with_vehicle * len(MODES) * len(
        CHUNKERS
    ) * 2 + n_unfiltered_only * len(MODES) * len(CHUNKERS)
    print(
        f"  {n_main_calls} calls in {t_main:.1f}s  (unfiltered-only rows: {n_unfiltered_only})"
    )

    # ── rerank score distribution: extra calls for unanswerable ──
    print(f"Running rerank on {len(unanswerable)} unanswerable rows...")
    rerank_scores: dict[str, dict[str, float | None]] = defaultdict(dict)

    # Collect answerable rerank scores (already computed)
    for row in answerable:
        for chunker in CHUNKERS:
            for filt in ("unfiltered", "filtered"):
                key = f"rerank_{chunker}_{filt}"
                rr = per_question.get(row.id, {}).get(key)
                rerank_scores[row.id][key] = rr.top_score if rr else None

    # Extra rerank calls for unanswerable rows
    t0_unans = time.perf_counter()
    n_unans_calls = 0
    for row in unanswerable:
        for chunker in CHUNKERS:
            ret = retrievers[chunker]
            # unfiltered
            hits_u = ret.rerank(question=row.question, k=10)
            rerank_scores[row.id][f"rerank_{chunker}_unfiltered"] = (
                hits_u[0].score if hits_u else None
            )
            n_unans_calls += 1
            # filtered (only when vehicle is complete)
            if has_complete_vehicle(row):
                vf = VehicleFilter(make=row.make, model=row.model, year=row.year)
                hits_f = ret.rerank(question=row.question, k=10, vehicle_filter=vf)
                rerank_scores[row.id][f"rerank_{chunker}_filtered"] = (
                    hits_f[0].score if hits_f else None
                )
                n_unans_calls += 1
    t_unans = time.perf_counter() - t0_unans
    print(f"  {n_unans_calls} calls in {t_unans:.1f}s")

    # ── vehicle recall count buckets ──
    print("Computing vehicle recall-count buckets...")
    vehicle_buckets: dict[str, str] = {}
    with pool.connection() as conn:
        for row in answerable:
            if has_complete_vehicle(row):
                count = get_vehicle_recall_count(conn, row.make, row.model, row.year)
                vehicle_buckets[row.id] = bucket_label(count)

    # ── hidden-component classification ──
    print("Classifying hidden/visible gold recalls...")
    visibility: dict[str, dict[str, bool]] = {}
    component_rows = [
        r
        for r in answerable
        if r.source == "generated" and r.component_group and r.template != "all_recalls"
    ]
    with pool.connection() as conn:
        for row in component_rows:
            visibility[row.id] = classify_gold_visibility(conn, row)

    # ── card seal ──
    with pool.connection() as conn:
        seal = card_seal(conn)

    # ── build config keys ──
    configs: list[str] = []
    for mode in MODES:
        for chunker in CHUNKERS:
            for filt in ("unfiltered", "filtered"):
                configs.append(f"{mode}_{chunker}_{filt}")

    # ── aggregate metrics ──
    print("Aggregating metrics...")
    summary: dict[str, Any] = {}

    overall: dict[str, Any] = {}
    for cfg in configs:
        overall[cfg] = aggregate_metrics(answerable, per_question, cfg)
    summary["overall"] = overall

    by_source: dict[str, dict[str, Any]] = {}
    for src in ("generated", "handwritten"):
        subset = [r for r in answerable if r.source == src]
        if not subset:
            continue
        by_source[src] = {}
        for cfg in configs:
            by_source[src][cfg] = aggregate_metrics(subset, per_question, cfg)
    summary["by_source"] = by_source

    by_template: dict[str, dict[str, Any]] = {}
    generated = [r for r in answerable if r.source == "generated"]
    templates = sorted({r.template for r in generated if r.template})
    for tmpl in templates:
        subset = [r for r in generated if r.template == tmpl]
        by_template[tmpl] = {}
        for cfg in configs:
            by_template[tmpl][cfg] = aggregate_metrics(subset, per_question, cfg)
    summary["by_template"] = by_template

    by_tag: dict[str, dict[str, Any]] = {}
    handwritten = [r for r in answerable if r.source == "handwritten"]
    all_tags = sorted({t for r in handwritten for t in r.tags if t != "handwritten"})
    for tag in all_tags:
        subset = [r for r in handwritten if tag in r.tags]
        by_tag[tag] = {}
        for cfg in configs:
            by_tag[tag][cfg] = aggregate_metrics(subset, per_question, cfg)
    summary["by_tag"] = by_tag

    by_bucket: dict[str, dict[str, Any]] = {}
    for bkt in ("1", "2-3", "4-10", ">10"):
        subset = [r for r in answerable if vehicle_buckets.get(r.id) == bkt]
        if not subset:
            continue
        by_bucket[bkt] = {}
        for cfg in configs:
            by_bucket[bkt][cfg] = aggregate_metrics(subset, per_question, cfg)
    summary["by_vehicle_bucket"] = by_bucket

    hv: dict[str, Any] = {}
    for cfg in configs:
        hv[cfg] = compute_found_rates(component_rows, per_question, cfg, visibility)
    summary["hidden_vs_visible"] = hv

    rsd: dict[str, dict[str, Any]] = {}
    for chunker in CHUNKERS:
        for filt in ("unfiltered", "filtered"):
            key = f"rerank_{chunker}_{filt}"
            ans_scores = [
                rerank_scores[r.id][key]
                for r in answerable
                if r.id in rerank_scores
                and key in rerank_scores[r.id]
                and rerank_scores[r.id][key] is not None
            ]
            unans_scores = [
                rerank_scores[r.id][key]
                for r in unanswerable
                if r.id in rerank_scores
                and key in rerank_scores[r.id]
                and rerank_scores[r.id][key] is not None
            ]
            rsd_entry: dict[str, Any] = {}
            for group_name, scores in [
                ("answerable", ans_scores),
                ("unanswerable", unans_scores),
            ]:
                if scores:
                    s = sorted(scores)
                    rsd_entry[group_name] = {
                        "n": len(s),
                        "min": s[0],
                        "p25": percentile(s, 0.25),
                        "median": percentile(s, 0.5),
                        "p75": percentile(s, 0.75),
                        "max": s[-1],
                    }
                else:
                    rsd_entry[group_name] = {"n": 0}
            rsd[key] = rsd_entry
    summary["rerank_score_distribution"] = rsd

    # ── timing estimate mode ──
    if args.limit:
        # Extrapolate each phase to the full set independently
        est_warmup = t_warmup  # fixed cost, not extrapolated
        est_main = t_main * (full_answerable / len(answerable)) if answerable else 0
        n_full_unans_calls = full_unanswerable * len(CHUNKERS) * 2  # all have vehicles
        est_unans = (
            t_unans * (full_unanswerable / len(unanswerable)) if unanswerable else 0
        )
        est_total = est_warmup + est_main + est_unans
        n_full_main_calls = (
            full_answerable * len(MODES) * len(CHUNKERS) * 2
        )  # all have vehicles

        print(f"\n--- Timing estimate (--limit {args.limit}) ---")
        print(f"Warm-up:      {n_warmup} calls, {t_warmup:.1f}s")
        print(
            f"Answerable:   {n_main_calls} calls in {t_main:.1f}s "
            f"→ est {n_full_main_calls} calls in {est_main:.0f}s for {full_answerable} rows"
        )
        print(
            f"Unanswerable: {n_unans_calls} calls in {t_unans:.1f}s "
            f"→ est {n_full_unans_calls} calls in {est_unans:.0f}s for {full_unanswerable} rows"
        )
        print(f"Estimated total: {est_total:.0f}s")
        pool.close()
        return

    # ── build results dict ──
    command = "python " + " ".join(sys.argv)
    results = {
        "metadata": {
            "label": args.label,
            "date": str(date.today()),
            "git_commit": git_commit(),
            "dirty_worktree": dirty,
            "retrieval_settings": {
                "k": settings.retrieval_k,
                "rrf_k": settings.rrf_k,
                "ef_search": settings.ef_search,
                "pool_size": settings.pool_size,
                "rerank_candidates": settings.rerank_candidates,
                "embedding_model": settings.embedding_model,
                "rerank_model": settings.rerank_model,
            },
            "card_seal": seal,
        },
        "configs": configs,
        "per_question": {
            row_id: {
                cfg: {
                    "retrieved_ids": rr.retrieved_ids,
                    "top_score": rr.top_score,
                    "latency_s": rr.latency_s,
                }
                for cfg, rr in cfg_results.items()
            }
            for row_id, cfg_results in per_question.items()
        },
        "summary": summary,
        "gt5_gold_count": gt5,
    }

    # ── write JSON ──
    results_dir = PROJECT_ROOT / "results"
    results_dir.mkdir(parents=True, exist_ok=True)
    out_path = results_dir / f"retrieval_{args.label}_{date.today()}.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)
    print(f"\nJSON written to {out_path}")

    # ── update docs/results.md ──
    md_content = format_markdown(results, command)
    update_results_md(args.label, md_content)
    print(f"docs/results.md updated (label={args.label})")

    pool.close()


if __name__ == "__main__":
    main()
