"""Pure retrieval-quality metrics: hit@k, recall@k, reciprocal rank.

All functions take a list of retrieved campaign IDs (one per hit, no
duplicates) and a set of gold campaign IDs.  No I/O, no database.
"""

from __future__ import annotations


def hit_at_k(retrieved_ids: list[str], gold_ids: set[str], k: int) -> bool:
    """True if any gold_id appears in the first *k* retrieved IDs."""
    return bool(set(retrieved_ids[:k]) & gold_ids)


def recall_at_k(retrieved_ids: list[str], gold_ids: set[str], k: int) -> float:
    """Fraction of gold_ids found in the first *k* retrieved IDs.

    Denominator is ``len(gold_ids)``, not *k*.  Returns 0.0 when
    *gold_ids* is empty.
    """
    if not gold_ids:
        return 0.0
    return len(set(retrieved_ids[:k]) & gold_ids) / len(gold_ids)


def reciprocal_rank(retrieved_ids: list[str], gold_ids: set[str]) -> float:
    """1 / rank of the first gold recall found (1-based ranks).

    Returns 0.0 when no gold recall appears in *retrieved_ids*.
    """
    for i, cid in enumerate(retrieved_ids, 1):
        if cid in gold_ids:
            return 1.0 / i
    return 0.0
