"""Tests for faithfulness.py, answer_eval faithfulness integration, and dedup.

No model download anywhere.  Every expected value is hand-worked in comments.
"""

from __future__ import annotations

import pytest

from recall_assistant.evals.answer_eval import (
    _dedup,
    check_one_row,
    score_answers,
)
from recall_assistant.evals.faithfulness import (
    HHEMScorer,
    build_premise,
    strip_citations,
)
from tests.evals.conftest import make_answer_row as _answer_row
from tests.evals.conftest import make_golden as _golden

# ═══════════════════════════════════════════════════════════════════
# 1. strip_citations
# ═══════════════════════════════════════════════════════════════════


class TestStripCitations:
    def test_single_citation(self):
        assert strip_citations("Brake issue [24V123000].") == "Brake issue."

    def test_multi_id_bracket(self):
        assert strip_citations("Issue [24V123000, 24V456000].") == "Issue."

    def test_short_campaign_id(self):
        assert strip_citations("Issue [90S9] noted.") == "Issue noted."

    def test_two_groups(self):
        # "A [X]. B [Y]." → "A. B."
        result = strip_citations("A [X]. B [Y].")
        assert result == "A. B."

    def test_no_brackets_unchanged(self):
        text = "No brackets here at all."
        assert strip_citations(text) == text

    def test_collapses_double_spaces(self):
        # "[X]  following" → after strip " following", double space collapses
        assert strip_citations("text [X]  following") == "text following"


# ═══════════════════════════════════════════════════════════════════
# 2. build_premise
# ═══════════════════════════════════════════════════════════════════


class TestBuildPremise:
    def test_joins_with_double_newline(self):
        assert build_premise(["aaa", "bbb", "ccc"]) == "aaa\n\nbbb\n\nccc"

    def test_single_text(self):
        assert build_premise(["only one"]) == "only one"

    def test_empty(self):
        assert build_premise([]) == ""


# ═══════════════════════════════════════════════════════════════════
# 3. cited_ids deduplication
# ═══════════════════════════════════════════════════════════════════


class TestCitedIdsDedup:
    def test_dedup_preserves_order(self):
        assert _dedup(["A", "B", "A", "C", "B"]) == ["A", "B", "C"]

    def test_no_duplicates_unchanged(self):
        assert _dedup(["A", "B", "C"]) == ["A", "B", "C"]

    def test_empty(self):
        assert _dedup([]) == []

    def test_check_one_row_deduplicates(self):
        """Repeated cited_ids from rag.py are deduplicated in scored output."""
        golden = _golden("G1", "generated", ["C1"], "cite_all_gold")
        row = _answer_row(
            "G1",
            "generated",
            "answered",
            ["C1", "C1", "C1"],
            retrieved_ids=["C1", "X1"],
        )
        checked = check_one_row(row, golden)
        # Deduped: only one C1.
        assert checked["cited_ids"] == ["C1"]


# ═══════════════════════════════════════════════════════════════════
# 4. Scorer applied only to answered rows
# ═══════════════════════════════════════════════════════════════════


class FakeScorer:
    """Returns a fixed score and records calls."""

    def __init__(self, score: float = 0.85) -> None:
        self._score = score
        self.calls: list[tuple[str, str]] = []

    def __call__(self, premise: str, hypothesis: str) -> float:
        self.calls.append((premise, hypothesis))
        return self._score


class TestScorerOnlyOnAnswered:
    def test_answered_gets_score(self):
        golden = _golden("A1", "generated", ["C1"], "cite_all_gold")
        row = _answer_row(
            "A1",
            "generated",
            "answered",
            ["C1"],
            answer_text="Brake issue [C1].",
            retrieved_texts=["source text"],
            retrieved_ids=["C1"],
        )
        fake = FakeScorer(0.92)
        checked = check_one_row(row, golden, scorer=fake)
        assert checked["faithfulness"] == pytest.approx(0.92)
        assert len(fake.calls) == 1
        # Hypothesis should have citations stripped.
        assert "[C1]" not in fake.calls[0][1]

    def test_declined_gets_null(self):
        golden = _golden("D1", "unanswerable", [], "decline")
        row = _answer_row(
            "D1",
            "unanswerable",
            "declined",
            [],
            answer_text="NO_RELEVANT_RECALL_FOUND",
        )
        fake = FakeScorer(0.99)
        checked = check_one_row(row, golden, scorer=fake)
        assert checked["faithfulness"] is None
        assert len(fake.calls) == 0

    def test_invalid_gets_null(self):
        golden = _golden("I1", "handwritten", ["C1"], "cite_any_gold")
        row = _answer_row("I1", "handwritten", "invalid", [])
        fake = FakeScorer(0.99)
        checked = check_one_row(row, golden, scorer=fake)
        assert checked["faithfulness"] is None
        assert len(fake.calls) == 0


# ═══════════════════════════════════════════════════════════════════
# 5. Faithfulness aggregation in score_answers
# ═══════════════════════════════════════════════════════════════════


class TestFaithfulnessAggregation:
    def test_faithfulness_stats(self):
        """Hand-worked faithfulness stats from 4 rows with known scores."""
        goldens = {
            g.id: g
            for g in [
                _golden("F1", "generated", ["C1"], "cite_all_gold", template="remedy"),
                _golden("F2", "generated", ["C2"], "cite_all_gold", template="remedy"),
                _golden("F3", "generated", ["C3"], "cite_all_gold", template="remedy"),
                _golden("F4", "generated", ["C4"], "cite_all_gold", template="remedy"),
                _golden("D1", "unanswerable", [], "decline"),
            ]
        }
        rows = [
            _answer_row("F1", "generated", "answered", ["C1"], retrieved_ids=["C1"]),
            _answer_row("F2", "generated", "answered", ["C2"], retrieved_ids=["C2"]),
            _answer_row("F3", "generated", "answered", ["C3"], retrieved_ids=["C3"]),
            _answer_row("F4", "generated", "answered", ["C4"], retrieved_ids=["C4"]),
            _answer_row(
                "D1",
                "unanswerable",
                "declined",
                [],
                answer_text="NO_RELEVANT_RECALL_FOUND",
            ),
        ]

        # Scores assigned: F1=0.95, F2=0.80, F3=0.40, F4=0.60
        # Sorted: [0.40, 0.60, 0.80, 0.95]
        call_count = 0
        score_map = {"F1": 0.95, "F2": 0.80, "F3": 0.40, "F4": 0.60}

        def fake_scorer(premise: str, hypothesis: str) -> float:
            nonlocal call_count
            call_count += 1
            # Match by call order: rows are F1, F2, F3, F4 (D1 is skipped).
            qids = ["F1", "F2", "F3", "F4"]
            return score_map[qids[call_count - 1]]

        scores, checked = score_answers(rows, goldens, scorer=fake_scorer)

        # Scorer called only for the 4 answered rows, not D1.
        assert call_count == 4

        f = scores["faithfulness"]
        assert f["n"] == 4
        # mean = (0.95 + 0.80 + 0.40 + 0.60) / 4 = 2.75 / 4 = 0.6875
        assert f["mean"] == pytest.approx(0.6875)
        # sorted: [0.40, 0.60, 0.80, 0.95]
        # median: ceil(4*0.5)-1 = 2-1 = 1 → index 1 → 0.60
        assert f["median"] == pytest.approx(0.60)
        # p25: ceil(4*0.25)-1 = 1-1 = 0 → index 0 → 0.40
        assert f["p25"] == pytest.approx(0.40)
        assert f["min"] == pytest.approx(0.40)
        # flagged below 0.5: only 0.40 < 0.5 → 1
        assert f["flagged_below_0_5"] == 1
        # lowest 5 (sorted by score asc): F3=0.40, F4=0.60, F2=0.80, F1=0.95
        assert f["lowest_5"] == [
            ("F3", 0.40),
            ("F4", 0.60),
            ("F2", 0.80),
            ("F1", 0.95),
        ]

        # D1 (declined) has faithfulness=None.
        d1 = next(r for r in checked if r["question_id"] == "D1")
        assert d1["faithfulness"] is None

    def test_no_scorer_gives_empty_faithfulness(self):
        """Without a scorer, faithfulness dict has n=0."""
        goldens = {"A1": _golden("A1", "generated", ["C1"], "cite_all_gold")}
        rows = [
            _answer_row("A1", "generated", "answered", ["C1"], retrieved_ids=["C1"])
        ]
        scores, _ = score_answers(rows, goldens, scorer=None)
        assert scores["faithfulness"]["n"] == 0


# ═══════════════════════════════════════════════════════════════════
# 6. Lazy-load guard
# ═══════════════════════════════════════════════════════════════════


class TestLazyLoad:
    def test_import_does_not_load_model(self):
        """Importing faithfulness.py and constructing HHEMScorer does not load."""
        scorer = HHEMScorer()
        assert scorer._model is None
