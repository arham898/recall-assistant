"""Leakage check for golden evaluation questions.

A question leaks if any of its 5-grams (lower-cased alphanumeric words)
appears in the gold recalls' defect_summary, consequence_summary,
corrective_action or component text.

The row's own vehicle name (year, make, model) is masked before
building 5-grams so that the vehicle identity — which the question
must contain — cannot produce a false-positive overlap.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from recall_assistant.evals.golden import GoldenRow

_WORD_RE = re.compile(r"[a-zA-Z0-9]+")


def _extract_words(text: str) -> list[str]:
    """Extract lower-cased alphanumeric words from text."""
    return [w.lower() for w in _WORD_RE.findall(text)]


def _ngrams(words: list[str], n: int) -> set[tuple[str, ...]]:
    """Return the set of n-grams from a word list."""
    return {tuple(words[i : i + n]) for i in range(len(words) - n + 1)}


def _mask_vehicle(question: str, row: GoldenRow) -> str:
    """Remove the vehicle name from the question text.

    Replaces the first occurrence of the vehicle's words (year, make,
    model as contiguous tokens) with a gap marker so that no 5-gram
    can span the vehicle identity.
    """
    if row.year is None or row.make is None or row.model is None:
        return question
    vehicle_words = _extract_words(f"{row.year} {row.make} {row.model}")
    q_words = _WORD_RE.findall(question)
    vlen = len(vehicle_words)
    for i in range(len(q_words) - vlen + 1):
        if [w.lower() for w in q_words[i : i + vlen]] == vehicle_words:
            q_words[i : i + vlen] = ["__MASKED__"]
            break
    return " ".join(q_words)


def check_leakage(
    rows: list[GoldenRow],
    recall_texts: dict[str, dict[str, str]],
) -> list[tuple[GoldenRow, str, tuple[str, ...]]]:
    """Check rows for 5-gram leakage against gold recall texts.

    recall_texts maps campaign_id -> {field: text} where field is one of
    defect_summary, consequence_summary, corrective_action, component.

    The row's vehicle name is masked before building question 5-grams.

    Returns a list of (row, field, ngram) for each leaking row.
    """
    flagged: list[tuple[GoldenRow, str, tuple[str, ...]]] = []
    for row in rows:
        masked = _mask_vehicle(row.question, row)
        q_grams = _ngrams(_extract_words(masked), 5)
        if not q_grams:
            continue
        for cid in row.gold_campaign_ids:
            texts = recall_texts.get(cid, {})
            for field, text in texts.items():
                t_grams = _ngrams(_extract_words(text), 5)
                overlap = q_grams & t_grams
                for gram in overlap:
                    flagged.append((row, field, gram))
    return flagged
