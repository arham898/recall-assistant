"""Retrieval types, protocols and the shared embed_query helper.

Hit is the universal return type for all four retrieval modes.
VehicleFilter normalises make/model to upper case so callers don't
need to worry about casing.  embed_query applies the BGE query prefix
once, used by dense, hybrid and rerank.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol

if TYPE_CHECKING:
    from recall_assistant.ingest.embedder import Embedder

Chunker = Literal["record", "field"]

BGE_QUERY_PREFIX = "Represent this sentence for searching relevant passages: "

_VALID_CHUNKERS: set[str] = {"record", "field"}


def _validate_chunker(chunker: str) -> None:
    """Raise ValueError if chunker is not 'record' or 'field'."""
    if chunker not in _VALID_CHUNKERS:
        raise ValueError(f"chunker must be 'record' or 'field', got {chunker!r}")


@dataclass(frozen=True)
class Hit:
    """One recall campaign returned by a retrieval mode."""

    campaign_id: str
    chunk_id: int
    text: str
    score: float
    rank: int


@dataclass(frozen=True)
class VehicleFilter:
    """Optional make/model/year filters, normalised to upper case."""

    make: str | None = None
    model: str | None = None
    year: int | None = None

    def __post_init__(self) -> None:
        if self.make is not None:
            object.__setattr__(self, "make", self.make.upper())
        if self.model is not None:
            object.__setattr__(self, "model", self.model.upper())


class Reranker(Protocol):
    """Scores (query, passage) pairs.  Higher is better."""

    def score(self, query: str, passages: list[str]) -> list[float]: ...


def embed_query(embedder: Embedder, question: str) -> list[float]:
    """Embed a question with the BGE query prefix.

    Single entry point used by dense, hybrid and rerank so the prefix
    is never forgotten.
    """
    return embedder.embed([BGE_QUERY_PREFIX + question])[0]


def _extract_words(question: str) -> list[str]:
    """Extract alphanumeric words from a question for keyword search."""
    return re.findall(r"[a-zA-Z0-9]+", question)
