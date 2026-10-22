"""Tests for pure answer-check functions and the _classify mutation guard.

Every expected value is hand-worked in comments so nothing is a magic number.
No database, no network, no model downloads.
"""

from __future__ import annotations

import pytest

from recall_assistant.evals.answer_checks import (
    cites_all_gold,
    cites_gold,
    declined,
    invalid,
    invalid_reason,
    pass_rule_check,
    wrongly_declined,
)
from recall_assistant.rag import _classify

# ═══════════════════════════════════════════════════════════════════
# 1. declined()
# ═══════════════════════════════════════════════════════════════════


class TestDeclined:
    def test_true_when_declined(self):
        assert declined("declined") is True

    def test_false_when_answered(self):
        assert declined("answered") is False

    def test_false_when_invalid(self):
        assert declined("invalid") is False


# ═══════════════════════════════════════════════════════════════════
# 2. invalid()
# ═══════════════════════════════════════════════════════════════════


class TestInvalid:
    def test_true_when_invalid(self):
        assert invalid("invalid") is True

    def test_false_when_answered(self):
        assert invalid("answered") is False

    def test_false_when_declined(self):
        assert invalid("declined") is False


# ═══════════════════════════════════════════════════════════════════
# 3. invalid_reason()
# ═══════════════════════════════════════════════════════════════════


class TestInvalidReason:
    def test_cut_off_empty_text(self):
        # Empty answer text → cut_off regardless of finish_reason.
        assert invalid_reason("", "stop", [], []) == "cut_off"

    def test_cut_off_finish_reason_length(self):
        # Model hit token limit.
        assert (
            invalid_reason("Brake issue [RET_001].", "length", ["RET_001"], [])
            == "cut_off"
        )

    def test_cut_off_finish_reason_content_filter(self):
        assert invalid_reason("Some text.", "content_filter", [], []) == "cut_off"

    def test_marker_with_text(self):
        # Decline marker embedded in a longer answer.
        text = "I think NO_RELEVANT_RECALL_FOUND but maybe check brakes."
        assert invalid_reason(text, "stop", [], []) == "marker_with_text"

    def test_marker_plus_citation(self):
        # Marker present alongside a citation.
        text = "NO_RELEVANT_RECALL_FOUND [24V123000]"
        assert invalid_reason(text, "stop", ["24V123000"], []) == "marker_with_text"

    def test_bad_citation(self):
        # Cited ID not in retrieved set.
        assert (
            invalid_reason("Issue [99V999].", "stop", [], ["99V999"]) == "bad_citation"
        )

    def test_bad_citation_bare_number(self):
        # Bare number in brackets (e.g. "[2]").
        assert invalid_reason("See [2].", "stop", [], ["2"]) == "bad_citation"

    def test_uncited(self):
        # Text with no citations and no marker.
        assert invalid_reason("I don't know about that.", "stop", [], []) == "uncited"


# ═══════════════════════════════════════════════════════════════════
# 4. wrongly_declined()
# ═══════════════════════════════════════════════════════════════════


class TestWronglyDeclined:
    def test_true_when_declined_and_gold_retrieved(self):
        # Declined, but gold C2 was in the retrieved set.
        assert wrongly_declined("declined", ["C1", "C2", "C3"], ["C2"]) is True

    def test_false_when_declined_but_no_gold_retrieved(self):
        # Declined, and no gold ID was retrieved.
        assert wrongly_declined("declined", ["X1", "X2"], ["C1"]) is False

    def test_false_when_answered(self):
        # Not declined → not wrongly declined.
        assert wrongly_declined("answered", ["C1"], ["C1"]) is False

    def test_false_when_invalid(self):
        assert wrongly_declined("invalid", ["C1"], ["C1"]) is False


# ═══════════════════════════════════════════════════════════════════
# 5. cites_gold()
# ═══════════════════════════════════════════════════════════════════


class TestCitesGold:
    def test_one_match(self):
        # C2 is in both lists.
        assert cites_gold(["C1", "C2"], ["C2", "C3"]) is True

    def test_no_match(self):
        # No overlap.
        assert cites_gold(["C1", "C2"], ["C3", "C4"]) is False

    def test_empty_cited(self):
        assert cites_gold([], ["C1"]) is False

    def test_empty_gold(self):
        assert cites_gold(["C1"], []) is False

    def test_both_empty(self):
        assert cites_gold([], []) is False


# ═══════════════════════════════════════════════════════════════════
# 6. cites_all_gold()
# ═══════════════════════════════════════════════════════════════════


class TestCitesAllGold:
    def test_all_cited(self):
        # Gold {C1, C2} ⊆ cited {C1, C2, X1}.
        assert cites_all_gold(["C1", "C2", "X1"], ["C1", "C2"]) is True

    def test_partial_cited(self):
        # Only C1 cited, C2 missing.
        assert cites_all_gold(["C1", "X1"], ["C1", "C2"]) is False

    def test_superset_cited(self):
        # Cited is a strict superset of gold.
        assert cites_all_gold(["C1", "C2", "C3"], ["C1"]) is True

    def test_empty_gold(self):
        # Empty gold → trivially true (vacuous truth).
        assert cites_all_gold(["C1"], []) is True

    def test_empty_cited_nonempty_gold(self):
        assert cites_all_gold([], ["C1"]) is False


# ═══════════════════════════════════════════════════════════════════
# 7. pass_rule_check()
# ═══════════════════════════════════════════════════════════════════


class TestPassRuleCheck:
    # ── cite_any_gold ──

    def test_cite_any_gold_pass(self):
        # Answered, C1 cited and is gold.
        assert (
            pass_rule_check("cite_any_gold", "answered", ["C1", "X1"], ["C1", "C2"])
            is True
        )

    def test_cite_any_gold_fail_no_gold_cited(self):
        # Answered, but only X1 cited — no gold.
        assert pass_rule_check("cite_any_gold", "answered", ["X1"], ["C1"]) is False

    def test_cite_any_gold_fail_declined(self):
        assert pass_rule_check("cite_any_gold", "declined", [], ["C1"]) is False

    def test_cite_any_gold_fail_invalid(self):
        assert pass_rule_check("cite_any_gold", "invalid", ["C1"], ["C1"]) is False

    # ── cite_all_gold ──

    def test_cite_all_gold_pass(self):
        # Answered, all gold IDs cited.
        assert (
            pass_rule_check(
                "cite_all_gold", "answered", ["C1", "C2", "X1"], ["C1", "C2"]
            )
            is True
        )

    def test_cite_all_gold_fail_partial(self):
        # Only C1, missing C2.
        assert (
            pass_rule_check("cite_all_gold", "answered", ["C1", "X1"], ["C1", "C2"])
            is False
        )

    def test_cite_all_gold_fail_invalid_status(self):
        assert (
            pass_rule_check("cite_all_gold", "invalid", ["C1", "C2"], ["C1", "C2"])
            is False
        )

    # ── decline ──

    def test_decline_pass(self):
        assert pass_rule_check("decline", "declined", [], []) is True

    def test_decline_fail_answered(self):
        assert pass_rule_check("decline", "answered", ["X1"], []) is False

    def test_decline_fail_invalid(self):
        assert pass_rule_check("decline", "invalid", [], []) is False

    # ── unknown ──

    def test_unknown_rule_raises(self):
        with pytest.raises(ValueError, match="Unknown pass_rule"):
            pass_rule_check("unknown", "answered", ["C1"], ["C1"])


# ═══════════════════════════════════════════════════════════════════
# 8. Partial-marker mutation guard (_classify directly)
# ═══════════════════════════════════════════════════════════════════


class TestPartialMarkerClassification:
    def test_partial_marker_not_declined(self):
        """NO_RECALL_FOUND (not the exact marker) must be classified invalid.

        The exact marker is NO_RELEVANT_RECALL_FOUND.  If _classify were
        mutated to use substring matching (e.g. ``"NO_RECALL_FOUND" in text``),
        the shorter string would wrongly match and this test would fail.
        """
        status, valid, invalid_ids = _classify("NO_RECALL_FOUND", [], set(), "stop")
        assert status == "invalid"
        assert valid == []
        assert invalid_ids == []


# ═══════════════════════════════════════════════════════════════════
# 9. _classify: one-digit-off cited ID
# ═══════════════════════════════════════════════════════════════════


class TestClassifyOneDigitOff:
    def test_one_digit_off_lands_in_invalid_ids(self):
        """24V477000 is one digit off from retrieved 20V477000.

        _classify must partition it into invalid_ids, not valid.
        Real-world failure: gen_remedy_FOREST_RIVER_SUNSEEKER_2021
        cited 24V477000 instead of 20V477000 (docs/error-analysis.md).
        """
        status, valid, invalid_ids = _classify(
            "Forest River will repair the wiring [24V477000].",
            ["24V477000"],
            {"20V477000", "21V096000"},
            "stop",
        )
        assert status == "invalid"
        assert valid == []
        assert invalid_ids == ["24V477000"]
