"""Run golden questions through rag.answer() and save JSONL results.

Usage:
    python scripts/run_answers.py --mode MODE --chunker CHUNKER --filter {on,off} [--limit N]

Thin CLI wrapper.  Caches each LLM response on disk keyed by
(question_id, mode, chunker, model, prompt_hash, seal_hash, filter_state).
A re-run with nothing changed makes zero LLM calls.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import date
from typing import Any

from psycopg_pool import ConnectionPool

from recall_assistant.config import PROJECT_ROOT, Settings
from recall_assistant.evals.retrieval_eval import (
    card_seal,
    has_complete_vehicle,
    load_golden_rows,
)
from recall_assistant.evals.run_metadata import (
    git_commit,
    is_worktree_dirty,
    sanitise_path_component,
)
from recall_assistant.rag import _SYSTEM_PROMPT, answer
from recall_assistant.retrieval import VehicleFilter

CACHE_DIR = PROJECT_ROOT / ".cache" / "answers"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run golden questions through rag.answer()"
    )
    parser.add_argument(
        "--mode",
        required=True,
        choices=["dense", "keyword", "hybrid", "rerank"],
    )
    parser.add_argument(
        "--chunker",
        required=True,
        choices=["record", "field"],
    )
    parser.add_argument(
        "--filter",
        required=True,
        choices=["on", "off"],
        help="Apply VehicleFilter when the row has a complete vehicle",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Run N rows per source (timing/cost only, no output file)",
    )
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="Allow running on a dirty worktree",
    )
    return parser.parse_args(argv)


# ---------------------------------------------------------------------------
# Prompt hash
# ---------------------------------------------------------------------------


def prompt_hash() -> str:
    """SHA-256 of the system prompt template, first 12 hex chars."""
    return hashlib.sha256(_SYSTEM_PROMPT.encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------------------
# Seal hash (short digest of the card_seal dict)
# ---------------------------------------------------------------------------


def seal_hash(seal: dict[str, Any]) -> str:
    """MD5 of the JSON-serialised card seal, first 8 hex chars."""
    raw = json.dumps(seal, sort_keys=True).encode("utf-8")
    return hashlib.md5(raw).hexdigest()[:8]


# ---------------------------------------------------------------------------
# Disk cache
# ---------------------------------------------------------------------------


def _sanitise(component: str) -> str:
    """Delegate to the shared implementation in run_metadata."""
    return sanitise_path_component(component)


def _cache_key(
    question_id: str,
    mode: str,
    chunker: str,
    model: str,
    p_hash: str,
    s_hash: str,
    filter_state: str,
) -> str:
    """Deterministic filename for one cache entry."""
    parts = [
        _sanitise(question_id),
        _sanitise(mode),
        _sanitise(chunker),
        _sanitise(model),
        _sanitise(p_hash),
        _sanitise(s_hash),
        _sanitise(filter_state),
    ]
    return "__".join(parts) + ".json"


def _load_cache(key: str) -> dict | None:
    path = CACHE_DIR / key
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return None


def _save_cache(key: str, row: dict) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    (CACHE_DIR / key).write_text(json.dumps(row, ensure_ascii=False), encoding="utf-8")


def check_unique_sanitised_ids(rows: list[Any]) -> None:
    """Exit if two question IDs collide after sanitisation."""
    seen: dict[str, str] = {}
    for row in rows:
        safe = _sanitise(row.id)
        if safe in seen:
            print(
                f"ERROR: sanitised question IDs collide: "
                f"{row.id!r} and {seen[safe]!r} both become {safe!r}",
                file=sys.stderr,
            )
            sys.exit(1)
        seen[safe] = row.id


# ---------------------------------------------------------------------------
# Cost computation
# ---------------------------------------------------------------------------


def compute_cost(
    prompt_tokens: int,
    completion_tokens: int,
    model: str,
    settings: Settings,
) -> float | None:
    """Compute cost from Settings.llm_model_prices.  None if model unknown."""
    prices = settings.llm_model_prices.get(model)
    if prices is None:
        return None
    inp, out, _price_date = prices
    return (prompt_tokens * inp + completion_tokens * out) / 1_000_000


def _project_cost(measured_cost: float, measured_count: int, total_count: int) -> float:
    """Scale measured cost to the full question set."""
    if measured_count == 0:
        return 0.0
    return measured_cost / measured_count * total_count


def _upper_bound_cost(model: str, settings: Settings, n: int) -> float | None:
    """Worst-case cost estimate using max token budgets."""
    prices = settings.llm_model_prices.get(model)
    if prices is None:
        return None
    inp, out, _ = prices
    # Upper bound: 10 hits * 400 tok + 200 overhead ≈ 4200 prompt tokens,
    # plus llm_max_tokens completion tokens.
    per_q = (4200 * inp + settings.llm_max_tokens * out) / 1_000_000
    return per_q * n


# ---------------------------------------------------------------------------
# Run one question
# ---------------------------------------------------------------------------


def _run_one(
    row: Any,
    retriever: Any,
    llm_client: Any,
    model: str,
    mode: str,
    settings: Settings,
    p_hash: str,
    s_hash: str,
    filter_on: bool,
) -> tuple[dict, bool]:
    """Run one golden question; return (row_dict, was_cached)."""
    chunker = retriever._chunker  # set at construction time
    filt = "filtered" if filter_on else "unfiltered"
    key = _cache_key(row.id, mode, chunker, model, p_hash, s_hash, filt)
    cached = _load_cache(key)
    if cached is not None:
        return cached, True

    vf = None
    if filter_on and has_complete_vehicle(row):
        vf = VehicleFilter(make=row.make, model=row.model, year=row.year)

    result = answer(
        question=row.question,
        retriever=retriever,
        llm_client=llm_client,
        model=model,
        mode=mode,
        vehicle_filter=vf,
        settings=settings,
    )

    cost = compute_cost(
        result.usage.prompt_tokens,
        result.usage.completion_tokens,
        model,
        settings,
    )

    row_dict = {
        "question_id": row.id,
        "source": row.source,
        "status": result.status,
        "answer_text": result.answer_text,
        "cited_ids": result.cited_ids,
        "invalid_ids": result.invalid_ids,
        "retrieved_ids": [h.campaign_id for h in result.retrieved_hits],
        "retrieved_texts": [h.text for h in result.retrieved_hits],
        "prompt_tokens": result.usage.prompt_tokens,
        "completion_tokens": result.usage.completion_tokens,
        "cost": cost,
        "retrieval_latency_s": result.retrieval_latency_s,
        "generation_latency_s": result.generation_latency_s,
        "finish_reason": result.usage.finish_reason,
    }

    _save_cache(key, row_dict)
    return row_dict, False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    settings = Settings()
    model = settings.llm_model
    filter_on = args.filter == "on"

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
    check_unique_sanitised_ids(all_rows)
    generated = [r for r in all_rows if r.source == "generated"]
    handwritten = [r for r in all_rows if r.source == "handwritten"]
    unanswerable = [r for r in all_rows if r.source == "unanswerable"]
    total_count = len(all_rows)

    if args.limit:
        generated = generated[: args.limit]
        handwritten = handwritten[: args.limit]
        unanswerable = unanswerable[: args.limit]

    rows = generated + handwritten + unanswerable

    # ── connect + build retriever ──
    pool = ConnectionPool(settings.database_url, min_size=1, max_size=4)

    from recall_assistant.ingest.embedder import SentenceTransformerEmbedder
    from recall_assistant.retrieval.rerank import CrossEncoderReranker
    from recall_assistant.retrieval.retriever import Retriever

    embedder = SentenceTransformerEmbedder(settings.embedding_model)
    reranker = CrossEncoderReranker(settings.rerank_model)
    retriever = Retriever(pool, embedder, reranker=reranker, chunker=args.chunker)

    # ── card seal ──
    with pool.connection() as conn:
        seal = card_seal(conn)
    s_hash = seal_hash(seal)
    p_hash = prompt_hash()

    # ── full-run cost guard ──
    if not args.limit:
        ub = _upper_bound_cost(model, settings, total_count)
        if ub is not None:
            print(f"Cost projection (upper bound): ${ub:.4f}")
            if ub > 1.0:
                print(
                    f"ERROR: projected cost ${ub:.4f} exceeds $1.00.  "
                    "Reduce --limit or use a cheaper model.",
                    file=sys.stderr,
                )
                pool.close()
                sys.exit(1)

    # ── warm up ──
    print("Warming up retriever...")
    getattr(retriever, args.mode)(question="warmup query", k=1)

    # ── build LLM client once ──
    from recall_assistant.llm.openrouter import build_openrouter_client

    llm_client = build_openrouter_client(settings)

    # ── run ──
    print(
        f"Running {len(rows)} questions "
        f"(mode={args.mode}, chunker={args.chunker}, filter={args.filter})..."
    )
    results: list[dict] = []
    fresh_count = 0
    total_cost = 0.0

    for i, row in enumerate(rows, 1):
        row_dict, was_cached = _run_one(
            row,
            retriever,
            llm_client=llm_client,
            model=model,
            mode=args.mode,
            settings=settings,
            p_hash=p_hash,
            s_hash=s_hash,
            filter_on=filter_on,
        )
        results.append(row_dict)
        if not was_cached:
            fresh_count += 1
        if row_dict["cost"] is not None:
            total_cost += row_dict["cost"]
        if i % 10 == 0 or i == len(rows):
            print(f"  {i}/{len(rows)} done ({fresh_count} fresh)")

    # ── report ──
    print(
        f"\nTotal cost: ${total_cost:.4f}  ({fresh_count} fresh, "
        f"{len(rows) - fresh_count} cached)"
    )

    if args.limit:
        projected = _project_cost(total_cost, len(rows), total_count)
        print(f"Projected cost for {total_count} questions: ${projected:.4f}")
        pool.close()
        return

    # ── write JSONL ──
    filt_tag = "filtered" if filter_on else "unfiltered"
    label = f"{args.mode}_{args.chunker}_{filt_tag}"
    command = "python " + " ".join(sys.argv)
    results_dir = PROJECT_ROOT / "results"
    results_dir.mkdir(parents=True, exist_ok=True)
    out_path = results_dir / f"answers_{label}_{date.today()}.jsonl"

    metadata: dict[str, Any] = {
        "type": "metadata",
        "label": label,
        "date": str(date.today()),
        "git_commit": git_commit(),
        "dirty_worktree": dirty,
        "mode": args.mode,
        "chunker": args.chunker,
        "vehicle_filter": filter_on,
        "model": model,
        "prompt_hash": p_hash,
        "card_seal": seal,
        "settings": {
            "retrieval_k": settings.retrieval_k,
            "llm_max_tokens": settings.llm_max_tokens,
            "llm_temperature": settings.llm_temperature,
        },
        "command": command,
    }

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(json.dumps(metadata, ensure_ascii=False) + "\n")
        for row_dict in results:
            f.write(json.dumps(row_dict, ensure_ascii=False) + "\n")

    print(f"\nJSONL written to {out_path}")
    pool.close()


if __name__ == "__main__":
    main()
