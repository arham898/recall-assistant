"""Command-line interface for recall-assistant.

Two commands:
  search — print retrieval hits
  ask    — print LLM-generated answer with citations

Run via: python -m recall_assistant.cli search|ask ...
"""

from __future__ import annotations

import argparse
import sys

from recall_assistant.config import Settings
from recall_assistant.retrieval import VehicleFilter

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_vehicle_filter(args: argparse.Namespace) -> VehicleFilter | None:
    if args.make or args.model_name or args.year:
        return VehicleFilter(make=args.make, model=args.model_name, year=args.year)
    return None


def _get_components(pool, campaign_ids: list[str]) -> dict[str, str]:
    """Look up component for each campaign_id from the recalls table."""
    if not campaign_ids:
        return {}
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT campaign_id, component FROM recalls WHERE campaign_id = ANY(%s)",
            (campaign_ids,),
        )
        return {row[0]: row[1] for row in cur.fetchall()}


def _estimate_cost(
    settings: Settings,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
) -> str:
    """Estimate cost from settings-based price table, or 'unknown'."""
    if model not in settings.llm_model_prices:
        return "unknown"
    prompt_price, completion_price, _date = settings.llm_model_prices[model]
    cost = (
        prompt_tokens * prompt_price + completion_tokens * completion_price
    ) / 1_000_000
    return f"${cost:.6f}"


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def _add_retrieval_args(parser: argparse.ArgumentParser) -> None:
    """Add the shared retrieval flags to a subparser."""
    parser.add_argument("query", help="Search query or question")
    parser.add_argument(
        "--mode",
        choices=["dense", "keyword", "hybrid", "rerank"],
        default=None,
        help="Retrieval mode (default from config)",
    )
    parser.add_argument(
        "--chunker",
        choices=["record", "field"],
        default=None,
        help="Chunker type (default from config)",
    )
    parser.add_argument("--k", type=int, default=None, help="Number of results")
    parser.add_argument("--make", default=None, help="Vehicle make filter")
    # --model collides with argparse internals, use --model-name
    parser.add_argument(
        "--model-name", default=None, dest="model_name", help="Vehicle model filter"
    )
    parser.add_argument("--year", type=int, default=None, help="Vehicle year filter")


def _create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="recall-assistant",
        description="Question answering over NHTSA vehicle recall data",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    search_p = sub.add_parser("search", help="Search recalls")
    _add_retrieval_args(search_p)

    ask_p = sub.add_parser("ask", help="Ask a question about recalls")
    _add_retrieval_args(ask_p)
    ask_p.add_argument(
        "--show-prompt", action="store_true", help="Print the prompt sent to the LLM"
    )

    return parser


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_search(args: argparse.Namespace, settings: Settings) -> None:
    """Execute the search command."""
    from psycopg_pool import ConnectionPool

    from recall_assistant.ingest.embedder import SentenceTransformerEmbedder
    from recall_assistant.retrieval.rerank import CrossEncoderReranker
    from recall_assistant.retrieval.retriever import Retriever

    mode = args.mode or settings.default_mode
    chunker = args.chunker or settings.default_chunker
    k = args.k or settings.retrieval_k
    vehicle_filter = _build_vehicle_filter(args)

    embedder = SentenceTransformerEmbedder(model_name=settings.embedding_model)
    reranker = (
        CrossEncoderReranker(model_name=settings.rerank_model)
        if mode == "rerank"
        else None
    )

    pool = ConnectionPool(settings.database_url, min_size=1, max_size=2, open=True)
    try:
        retriever = Retriever(pool, embedder, reranker=reranker, chunker=chunker)
        method = getattr(retriever, mode)
        hits = method(args.query, k=k, vehicle_filter=vehicle_filter)

        components = _get_components(pool, [h.campaign_id for h in hits])

        print(f"{'Rank':<6} {'Campaign ID':<14} {'Component':<20} {'Score':<10} Text")
        print("-" * 80)
        for h in hits:
            comp = components.get(h.campaign_id, "?")
            snippet = h.text[:60].replace("\n", " ")
            if len(h.text) > 60:
                snippet += "..."
            print(
                f"{h.rank:<6} {h.campaign_id:<14} {comp:<20} {h.score:<10.4f} {snippet}"
            )
    finally:
        pool.close()


def cmd_ask(args: argparse.Namespace, settings: Settings) -> None:
    """Execute the ask command."""
    if not settings.openrouter_api_key:
        print(
            "Error: OPENROUTER_API_KEY not set. "
            "Add it to .env or set the environment variable.",
            file=sys.stderr,
        )
        sys.exit(1)
    if not settings.llm_model:
        print(
            "Error: LLM_MODEL not set. Add it to .env or set the environment variable.",
            file=sys.stderr,
        )
        sys.exit(1)

    from psycopg_pool import ConnectionPool

    from recall_assistant.ingest.embedder import SentenceTransformerEmbedder
    from recall_assistant.llm.openrouter import OpenRouterClient
    from recall_assistant.rag import answer
    from recall_assistant.retrieval.rerank import CrossEncoderReranker
    from recall_assistant.retrieval.retriever import Retriever

    mode = args.mode or settings.default_mode
    chunker = args.chunker or settings.default_chunker
    k = args.k or settings.retrieval_k
    vehicle_filter = _build_vehicle_filter(args)
    model = settings.llm_model

    embedder = SentenceTransformerEmbedder(model_name=settings.embedding_model)
    reranker = (
        CrossEncoderReranker(model_name=settings.rerank_model)
        if mode == "rerank"
        else None
    )

    pool = ConnectionPool(settings.database_url, min_size=1, max_size=2, open=True)
    try:
        retriever = Retriever(pool, embedder, reranker=reranker, chunker=chunker)

        llm_client = OpenRouterClient(
            api_key=settings.openrouter_api_key,
            temperature=settings.llm_temperature,
            request_timeout=settings.llm_request_timeout,
            max_retries=settings.llm_max_retries,
            max_retry_wait=settings.llm_max_retry_wait,
        )

        result = answer(
            question=args.query,
            retriever=retriever,
            llm_client=llm_client,
            model=model,
            mode=mode,
            k=k,
            vehicle_filter=vehicle_filter,
            settings=settings,
        )

        components = _get_components(
            pool, [h.campaign_id for h in result.retrieved_hits]
        )

        # --- Output ---
        print(f"Model: {model}")
        print(f"Mode: {mode} | Chunker: {chunker}")
        print()

        print("Retrieved:")
        for h in result.retrieved_hits:
            comp = components.get(h.campaign_id, "?")
            print(f"  [{h.rank}] {h.campaign_id} | {comp}")
        print()

        print("Answer:")
        print(result.answer_text)
        print()

        print(f"Cited: {', '.join(result.cited_ids) or '(none)'}")
        status_line = f"Status: {result.status}"
        if result.invalid_ids:
            status_line += f"  Invalid IDs: {result.invalid_ids}"
        print(status_line)
        print()

        print(f"Retrieval latency:  {result.retrieval_latency_s:.3f}s")
        gen_line = f"Generation latency: {result.generation_latency_s:.3f}s"
        if result.usage.retries > 0:
            gen_line += " (includes retry waits)"
        print(gen_line)
        print(f"Retries: {result.usage.retries}")
        print(
            f"Tokens: {result.usage.prompt_tokens} prompt "
            f"+ {result.usage.completion_tokens} completion"
        )
        print()

        if result.usage.cost is not None:
            print(f"OpenRouter cost: ${result.usage.cost:.6f}")
        else:
            print("OpenRouter cost: (not reported)")

        estimated = _estimate_cost(
            settings, model, result.usage.prompt_tokens, result.usage.completion_tokens
        )
        print(f"Estimated cost:  {estimated}")

        if args.show_prompt:
            print()
            print("--- Prompt messages ---")
            for msg in result.prompt_messages:
                print(f"[{msg['role']}]")
                print(msg["content"])
                print()
    finally:
        pool.close()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    # Ensure stdout can handle any character when redirected on Windows.
    sys.stdout.reconfigure(encoding="utf-8")

    parser = _create_parser()
    args = parser.parse_args()
    settings = Settings()

    if args.command == "search":
        cmd_search(args, settings)
    elif args.command == "ask":
        cmd_ask(args, settings)


if __name__ == "__main__":
    main()
