"""Pure functions for classifying answer quality against golden expectations.

Every function takes primitives (strings, lists) and returns a bool or str.
No I/O, no imports beyond the standard library.

Limitation: ``cite_any_gold`` and ``cite_all_gold`` pass rules also require
"the answer states the reference fact" (see evals/README.md).  That is a
semantic check code cannot do; these functions verify citation presence only.
Faithfulness scoring addresses the semantic gap partially.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Status checks
# ---------------------------------------------------------------------------


def declined(status: str) -> bool:
    """True when rag.py classified the answer as ``'declined'``."""
    return status == "declined"


def invalid(status: str) -> bool:
    """True when rag.py classified the answer as ``'invalid'``."""
    return status == "invalid"


# ---------------------------------------------------------------------------
# Invalid reason (mirrors _classify logic without importing it)
# ---------------------------------------------------------------------------


def invalid_reason(
    answer_text: str,
    finish_reason: str,
    cited_ids: list[str],
    invalid_ids: list[str],
) -> str:
    """Classify *why* an answer was marked invalid.

    Returns one of:

    * ``"cut_off"`` — empty text or finish_reason != "stop"
    * ``"marker_with_text"`` — the decline marker appeared but was not the
      whole response, or appeared alongside citations
    * ``"bad_citation"`` — at least one cited ID was invalid (not in
      retrieved set, or a bare number)
    * ``"uncited"`` — no citations at all and no decline marker

    The caller should only call this when ``status == "invalid"``.
    """
    # Empty or cut-off output.
    if not answer_text or finish_reason != "stop":
        return "cut_off"

    # Marker present but not the whole response (or marker + citations).
    _MARKER = "NO_RELEVANT_RECALL_FOUND"
    if _MARKER in answer_text:
        return "marker_with_text"

    # Bad citations present.
    if invalid_ids:
        return "bad_citation"

    # No citations and no decline marker.
    return "uncited"


# ---------------------------------------------------------------------------
# Gold-set checks
# ---------------------------------------------------------------------------


def wrongly_declined(
    status: str,
    retrieved_ids: list[str],
    gold_campaign_ids: list[str],
) -> bool:
    """True when the model declined but a gold recall was in the retrieved set."""
    if status != "declined":
        return False
    return bool(set(retrieved_ids) & set(gold_campaign_ids))


def cites_gold(cited_ids: list[str], gold_campaign_ids: list[str]) -> bool:
    """True when at least one cited ID is a gold campaign ID."""
    return bool(set(cited_ids) & set(gold_campaign_ids))


def cites_all_gold(cited_ids: list[str], gold_campaign_ids: list[str]) -> bool:
    """True when every gold campaign ID appears in *cited_ids*."""
    return set(gold_campaign_ids).issubset(set(cited_ids))


# ---------------------------------------------------------------------------
# Pass-rule dispatch
# ---------------------------------------------------------------------------


def pass_rule_check(
    pass_rule: str,
    status: str,
    cited_ids: list[str],
    gold_campaign_ids: list[str],
) -> bool:
    """Apply the row's pass rule and return whether the answer passes.

    Raises ``ValueError`` for an unrecognised *pass_rule*.
    """
    if pass_rule == "cite_any_gold":
        return status == "answered" and cites_gold(cited_ids, gold_campaign_ids)
    if pass_rule == "cite_all_gold":
        return status == "answered" and cites_all_gold(cited_ids, gold_campaign_ids)
    if pass_rule == "decline":
        return status == "declined"
    raise ValueError(f"Unknown pass_rule: {pass_rule!r}")
