"""Measure token lengths of both chunkers on the real NHTSA data.

Reports distribution statistics for each chunker strategy, broken down
by field for the field chunker.  Uses the actual chunker functions and
the BAAI/bge-small-en-v1.5 BertTokenizer so counts are exact.

Usage:
    uv run python scripts/measure_tokens.py
"""

from __future__ import annotations

import statistics
import sys
from pathlib import Path

from transformers import AutoTokenizer

from recall_assistant.config import Settings
from recall_assistant.ingest.chunkers import (
    _compact_vehicles,
    _full_vehicles,
    chunk_fields,
    chunk_record,
)
from recall_assistant.ingest.filters import filter_recalls
from recall_assistant.ingest.parser import parse_recalls

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "raw"


def measure(chunks: list, tokenizer, label: str) -> None:
    """Tokenize every chunk and print distribution stats."""
    lengths = []
    for chunk in chunks:
        tokens = tokenizer.encode(chunk.body)
        lengths.append(len(tokens))

    over_512 = [length for length in lengths if length > 512]
    over_256 = [length for length in lengths if length > 256]

    print(f"\n{'=' * 60}")
    print(f"Chunker: {label}")
    print(f"{'=' * 60}")
    print(f"  Total chunks:    {len(lengths)}")
    print(f"  Min tokens:      {min(lengths)}")
    print(f"  Max tokens:      {max(lengths)}")
    print(f"  Mean tokens:     {statistics.mean(lengths):.1f}")
    print(f"  Median tokens:   {statistics.median(lengths):.1f}")
    print(f"  p90 tokens:      {sorted(lengths)[int(len(lengths) * 0.9)]}")
    print(f"  p95 tokens:      {sorted(lengths)[int(len(lengths) * 0.95)]}")
    print(f"  p99 tokens:      {sorted(lengths)[int(len(lengths) * 0.99)]}")
    print(
        f"  > 256 tokens:    {len(over_256)}"
        f" ({100 * len(over_256) / len(lengths):.2f}%)"
    )
    print(
        f"  > 512 tokens:    {len(over_512)}"
        f" ({100 * len(over_512) / len(lengths):.2f}%)"
    )

    if over_512:
        print("\n  Longest 10 chunks (tokens):")
        for i, length in enumerate(sorted(lengths, reverse=True)[:10]):
            print(f"    {i + 1}. {length}")


def main() -> None:
    settings = Settings()

    # Find the flat file
    flat_files = sorted(DATA_DIR.glob("FLAT_RCL*.txt"))
    if not flat_files:
        print(f"No FLAT_RCL*.txt found in {DATA_DIR} — run download_data.py first")
        sys.exit(1)

    print(f"Parsing {flat_files[0]} ...")
    recalls, skipped = parse_recalls(flat_files[0])
    print(f"  Parsed {len(recalls)} campaigns ({skipped} rows skipped)")

    recalls = filter_recalls(
        recalls,
        recall_type=settings.recall_type_filter,
        min_model_year=settings.min_model_year,
    )
    print(f"  After filter: {len(recalls)} campaigns")

    # Load tokenizer
    print("Loading tokenizer ...")
    tokenizer = AutoTokenizer.from_pretrained(settings.embedding_model)

    # Build chunks using the actual chunker functions
    record_chunks = []
    field_chunks = []
    for r in recalls:
        record_chunks.extend(
            chunk_record(
                r,
                tokenizer,
                max_tokens=settings.chunk_max_tokens,
                overlap=settings.chunk_overlap_tokens,
            )
        )
        field_chunks.extend(
            chunk_fields(
                r,
                tokenizer,
                max_tokens=settings.chunk_max_tokens,
                overlap=settings.chunk_overlap_tokens,
            )
        )

    measure(record_chunks, tokenizer, "record (one chunk per campaign, with splitting)")
    measure(field_chunks, tokenizer, "field (one chunk per text field, with splitting)")

    # Break down field chunks by field_name prefix
    for fname in ["defect_summary", "consequence_summary", "corrective_action"]:
        subset = [c for c in field_chunks if c.field_name.startswith(fname)]
        if subset:
            measure(subset, tokenizer, f"field — {fname}")

    # Integrity checks: full-text coverage and strictly-increasing starts
    print(f"\n{'=' * 60}")
    print("Integrity checks (all recalls, both chunkers)")
    print(f"{'=' * 60}")

    over_limit = 0
    monotonicity_failures = 0
    coverage_failures = 0

    for r in recalls:
        for chunker_fn, label in [(chunk_record, "record"), (chunk_fields, "field")]:
            chunks = chunker_fn(
                r,
                tokenizer,
                max_tokens=settings.chunk_max_tokens,
                overlap=settings.chunk_overlap_tokens,
            )

            # Token limit
            for c in chunks:
                if len(tokenizer.encode(c.body)) > settings.chunk_max_tokens:
                    over_limit += 1

            if len(chunks) <= 1:
                continue

            # Determine prefix
            if label == "record":
                prefix = f"Vehicles: {_full_vehicles(r)}\nComponent: {r.component}\n"
            else:
                prefix = f"{_compact_vehicles(r)} | {r.component} — "

            bodies = [c.body[len(prefix) :] for c in chunks]

            # Build the original text that was split
            if label == "record":
                orig = (
                    f"Defect: {r.defect_summary}\n"
                    f"Consequence: {r.consequence_summary}\n"
                    f"Remedy: {r.corrective_action}"
                )
            else:
                fname = chunks[0].field_name.split(":")[0]
                field_map = {
                    "defect_summary": r.defect_summary,
                    "consequence_summary": r.consequence_summary,
                    "corrective_action": r.corrective_action,
                }
                orig = field_map.get(fname, "")

            # Strictly increasing starts
            starts = []
            for body in bodies:
                snippet = body[:40] if len(body) >= 40 else body
                pos = orig.find(snippet)
                starts.append(pos)

            for i in range(1, len(starts)):
                if starts[i] >= 0 and starts[i - 1] >= 0 and starts[i] <= starts[i - 1]:
                    monotonicity_failures += 1
                    break

            # Full text coverage
            covered = set()
            for body in bodies:
                snippet = body[:40] if len(body) >= 40 else body
                idx = orig.find(snippet)
                if idx >= 0:
                    covered.update(range(idx, idx + len(body)))

            for i, ch in enumerate(orig):
                if not ch.isspace() and i not in covered:
                    coverage_failures += 1
                    break

    print(f"  Chunks exceeding {settings.chunk_max_tokens} tokens: {over_limit}")
    print(f"  Strictly-increasing-starts failures:  {monotonicity_failures}")
    print(f"  Full-text coverage failures:          {coverage_failures}")


if __name__ == "__main__":
    main()
