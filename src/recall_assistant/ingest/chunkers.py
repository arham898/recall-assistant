"""Chunkers that turn Recall records into text chunks for embedding.

Two strategies:
  record — one chunk per campaign with all fields joined.
  field  — one chunk per text field (defect, consequence, remedy).

Both accept a tokenizer (any object with encode() and __call__())
so tests can substitute a fake without downloading model weights.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from recall_assistant.ingest.models import Recall


class Tokenizer(Protocol):
    """Minimal tokenizer interface used by the chunkers."""

    def encode(self, text: str) -> list[int]: ...

    def __call__(
        self,
        text: str,
        *,
        return_offsets_mapping: bool = False,
        add_special_tokens: bool = True,
    ) -> dict: ...


@dataclass(frozen=True)
class Chunk:
    """A single text chunk ready for embedding."""

    campaign_id: str
    chunker: str
    field_name: str  # always numbered: 'all:0', 'defect_summary:1', etc.
    body: str


# ---------------------------------------------------------------------------
# Vehicle header helpers
# ---------------------------------------------------------------------------

_MAX_VEHICLES_IN_PREFIX = 5


def _compact_vehicles(recall: Recall) -> str:
    """Distinct make+model, capped at _MAX_VEHICLES_IN_PREFIX, no years."""
    pairs = sorted({f"{v.make} {v.model}" for v in recall.vehicles})
    if len(pairs) <= _MAX_VEHICLES_IN_PREFIX:
        return ", ".join(pairs)
    shown = ", ".join(pairs[:_MAX_VEHICLES_IN_PREFIX])
    return f"{shown}, and {len(pairs) - _MAX_VEHICLES_IN_PREFIX} more"


def _full_vehicles(recall: Recall) -> str:
    """Full vehicle list with year ranges, for the record chunk body."""
    # Group years by make+model
    by_pair: dict[str, list[int | None]] = {}
    for v in recall.vehicles:
        key = f"{v.make} {v.model}"
        by_pair.setdefault(key, []).append(v.year)

    parts: list[str] = []
    for pair in sorted(by_pair):
        years = sorted(y for y in by_pair[pair] if y is not None)
        has_unknown = any(y is None for y in by_pair[pair])
        if years:
            year_str = _compress_year_list(years)
            if has_unknown:
                year_str += ", Unknown"
        elif has_unknown:
            year_str = "Unknown"
        else:
            year_str = ""
        parts.append(f"{pair} ({year_str})" if year_str else pair)

    if len(parts) <= _MAX_VEHICLES_IN_PREFIX:
        return ", ".join(parts)
    shown = ", ".join(parts[:_MAX_VEHICLES_IN_PREFIX])
    return f"{shown}, and {len(parts) - _MAX_VEHICLES_IN_PREFIX} more"


def _compress_year_list(years: list[int]) -> str:
    """Compress [2018, 2019, 2020, 2022] into '2018-2020, 2022'."""
    if not years:
        return ""
    ranges: list[str] = []
    start = prev = years[0]
    for y in years[1:]:
        if y == prev + 1:
            prev = y
        else:
            ranges.append(f"{start}-{prev}" if prev > start else str(start))
            start = prev = y
    ranges.append(f"{start}-{prev}" if prev > start else str(start))
    return ", ".join(ranges)


# ---------------------------------------------------------------------------
# Splitting long text into token-bounded windows
# ---------------------------------------------------------------------------


def _split_windows(
    prefix: str,
    text: str,
    tokenizer: Tokenizer,
    max_tokens: int,
    overlap: int,
) -> list[str]:
    """Split text into windows that each fit within max_tokens when
    prepended with prefix.

    Uses the tokenizer's offset mapping to cut at token boundaries in the
    original string, preserving case and spacing.
    """
    full = f"{prefix}{text}"
    n_full = len(tokenizer.encode(full))
    if n_full <= max_tokens:
        return [full]

    # Tokenise prefix and text separately to get offset mappings
    prefix_tokens = tokenizer.encode(prefix)
    n_prefix = len(prefix_tokens)

    text_capacity = max_tokens - n_prefix
    if text_capacity <= overlap:
        raise ValueError(
            f"Prefix is {n_prefix} tokens, leaving {text_capacity} for text "
            f"(must be > overlap={overlap}). Compact the vehicle list or "
            f"raise max_tokens."
        )

    # Get token offsets for the text portion
    text_enc = tokenizer(
        text,
        return_offsets_mapping=True,
        add_special_tokens=False,
    )
    token_ids: list[int] = text_enc["input_ids"]
    offsets: list[tuple[int, int]] = text_enc["offset_mapping"]

    windows: list[str] = []
    start_tok = 0
    while start_tok < len(token_ids):
        end_tok = min(start_tok + text_capacity, len(token_ids))

        # Character range in the original text
        char_start = offsets[start_tok][0]
        char_end = offsets[end_tok - 1][1]
        window_text = text[char_start:char_end]
        windows.append(f"{prefix}{window_text}")

        if end_tok >= len(token_ids):
            break
        start_tok = end_tok - overlap

    return windows


# ---------------------------------------------------------------------------
# Public chunkers
# ---------------------------------------------------------------------------


def chunk_record(
    recall: Recall,
    tokenizer: Tokenizer,
    max_tokens: int = 400,
    overlap: int = 50,
) -> list[Chunk]:
    """One chunk per campaign with all fields joined.

    Vehicles and component go first so they survive truncation.
    Splits into overlapping windows if the full text exceeds max_tokens.
    """
    prefix = f"Vehicles: {_full_vehicles(recall)}\nComponent: {recall.component}\n"
    text = (
        f"Defect: {recall.defect_summary}\n"
        f"Consequence: {recall.consequence_summary}\n"
        f"Remedy: {recall.corrective_action}"
    )

    windows = _split_windows(prefix, text, tokenizer, max_tokens, overlap)
    return [
        Chunk(
            campaign_id=recall.campaign_id,
            chunker="record",
            field_name=f"all:{i}",
            body=window,
        )
        for i, window in enumerate(windows)
    ]


def chunk_fields(
    recall: Recall,
    tokenizer: Tokenizer,
    max_tokens: int = 400,
    overlap: int = 50,
) -> list[Chunk]:
    """One chunk per non-empty text field, prefixed with make/model and
    component so each chunk is self-contained.

    Splits long fields into overlapping windows.
    """
    compact = _compact_vehicles(recall)
    prefix = f"{compact} | {recall.component} — "

    chunks: list[Chunk] = []
    for field_name, text in [
        ("defect_summary", recall.defect_summary),
        ("consequence_summary", recall.consequence_summary),
        ("corrective_action", recall.corrective_action),
    ]:
        if not text.strip():
            continue
        windows = _split_windows(prefix, text, tokenizer, max_tokens, overlap)
        for i, window in enumerate(windows):
            chunks.append(
                Chunk(
                    campaign_id=recall.campaign_id,
                    chunker="field",
                    field_name=f"{field_name}:{i}",
                    body=window,
                )
            )
    return chunks
