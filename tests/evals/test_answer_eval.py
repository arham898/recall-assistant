"""Tests for answer_eval.py scoring logic and results.md writer.

All tests use hand-built rows — no database, no network, no model
downloads.  Every expected value is hand-worked in comments.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from recall_assistant.evals.answer_eval import (
    score_answers,
    update_answer_results_md,
)
from tests.evals.conftest import make_answer_row as _answer_row
from tests.evals.conftest import make_golden as _golden

# ---------------------------------------------------------------------------
# Fixtures: 9 hand-built rows
# ---------------------------------------------------------------------------

# Generated — remedy template — passes (cite_all_gold, all cited)
G1_GOLDEN = _golden("G1", "generated", ["C1"], "cite_all_gold", template="remedy")
G1_ROW = _answer_row(
    "G1",
    "generated",
    "answered",
    ["C1"],
    retrieved_ids=["C1", "X1", "X2"],
    cost=0.001,
    generation_latency_s=1.0,
    retrieval_latency_s=0.05,
)

# Generated — remedy template — fails (cite_all_gold, no gold cited)
G2_GOLDEN = _golden("G2", "generated", ["C2"], "cite_all_gold", template="remedy")
G2_ROW = _answer_row(
    "G2",
    "generated",
    "answered",
    ["X3"],
    invalid_ids=[],
    retrieved_ids=["C2", "X3", "X4"],
    cost=0.001,
    generation_latency_s=2.0,
    retrieval_latency_s=0.05,
)

# Generated — consequence template — passes (cite_all_gold)
G3_GOLDEN = _golden(
    "G3", "generated", ["C3", "C4"], "cite_all_gold", template="consequence"
)
G3_ROW = _answer_row(
    "G3",
    "generated",
    "answered",
    ["C3", "C4"],
    retrieved_ids=["C3", "C4", "X5"],
    cost=0.002,
    generation_latency_s=1.5,
    retrieval_latency_s=0.04,
)

# Generated — consequence template — wrongly declined (gold C5 retrieved)
G4_GOLDEN = _golden("G4", "generated", ["C5"], "cite_all_gold", template="consequence")
G4_ROW = _answer_row(
    "G4",
    "generated",
    "declined",
    [],
    answer_text="NO_RELEVANT_RECALL_FOUND",
    retrieved_ids=["C5", "X6", "X7"],
    cost=0.0005,
    generation_latency_s=0.5,
    retrieval_latency_s=0.03,
)

# Handwritten — passes (cite_any_gold, one gold cited)
H1_GOLDEN = _golden("H1", "handwritten", ["C6", "C7"], "cite_any_gold")
H1_ROW = _answer_row(
    "H1",
    "handwritten",
    "answered",
    ["C6"],
    retrieved_ids=["C6", "X8"],
    cost=0.001,
    generation_latency_s=1.2,
    retrieval_latency_s=0.06,
)

# Handwritten — invalid (bad citation)
H2_GOLDEN = _golden("H2", "handwritten", ["C8"], "cite_any_gold")
H2_ROW = _answer_row(
    "H2",
    "handwritten",
    "invalid",
    [],
    invalid_ids=["99V999"],
    retrieved_ids=["C8", "X9"],
    answer_text="Issue [99V999].",
    cost=0.001,
    generation_latency_s=0.8,
    retrieval_latency_s=0.04,
)

# Unanswerable — declined (correct)
U1_GOLDEN = _golden("U1", "unanswerable", [], "decline")
U1_ROW = _answer_row(
    "U1",
    "unanswerable",
    "declined",
    [],
    answer_text="NO_RELEVANT_RECALL_FOUND",
    cost=0.0005,
    generation_latency_s=0.3,
    retrieval_latency_s=0.02,
)

# Unanswerable — invalid (marker with extra text)
U2_GOLDEN = _golden("U2", "unanswerable", [], "decline")
U2_ROW = _answer_row(
    "U2",
    "unanswerable",
    "invalid",
    [],
    answer_text="NO_RELEVANT_RECALL_FOUND but maybe check the brakes",
    cost=0.0005,
    generation_latency_s=0.4,
    retrieval_latency_s=0.03,
)

# Unanswerable — answered (the model answered when it shouldn't have)
U3_GOLDEN = _golden("U3", "unanswerable", [], "decline")
U3_ROW = _answer_row(
    "U3",
    "unanswerable",
    "answered",
    ["X10"],
    retrieved_ids=["X10"],
    cost=0.001,
    generation_latency_s=1.1,
    retrieval_latency_s=0.05,
)

ALL_GOLDEN = {
    g.id: g
    for g in [
        G1_GOLDEN,
        G2_GOLDEN,
        G3_GOLDEN,
        G4_GOLDEN,
        H1_GOLDEN,
        H2_GOLDEN,
        U1_GOLDEN,
        U2_GOLDEN,
        U3_GOLDEN,
    ]
}

ALL_ROWS = [G1_ROW, G2_ROW, G3_ROW, G4_ROW, H1_ROW, H2_ROW, U1_ROW, U2_ROW, U3_ROW]


# ═══════════════════════════════════════════════════════════════════
# 1. score_answers — overall aggregation
# ═══════════════════════════════════════════════════════════════════


class TestScoreAnswers:
    @pytest.fixture
    def scored(self):
        scores, checked = score_answers(ALL_ROWS, ALL_GOLDEN)
        return scores, checked

    # ── generated ──

    def test_generated_n(self, scored):
        scores, _ = scored
        # G1, G2, G3, G4 = 4 generated rows.
        assert scores["generated"]["n"] == 4

    def test_generated_passed(self, scored):
        scores, _ = scored
        # G1: answered, cited C1, gold [C1] → cite_all_gold → pass
        # G2: answered, cited X3, gold [C2] → cite_all_gold → fail (no gold cited)
        # G3: answered, cited [C3, C4], gold [C3, C4] → cite_all_gold → pass
        # G4: declined, gold [C5] → cite_all_gold → fail
        # Passed: 2
        assert scores["generated"]["passed"] == 2

    def test_generated_pass_rate(self, scored):
        scores, _ = scored
        # 2 / 4 = 0.5
        assert scores["generated"]["pass_rate"] == pytest.approx(0.5)

    def test_generated_wrongly_declined(self, scored):
        scores, _ = scored
        # G4 is declined and C5 is in retrieved → wrongly_declined
        # Others are not declined.
        assert scores["generated"]["wrongly_declined"] == 1

    def test_generated_invalid_count(self, scored):
        scores, _ = scored
        # None of G1-G4 has status "invalid".
        assert scores["generated"]["invalid_count"] == 0

    # ── by template ──

    def test_by_template_remedy(self, scored):
        scores, _ = scored
        tmpl = scores["generated"]["by_template"]["remedy"]
        # G1 (pass), G2 (fail) → n=2, passed=1, rate=0.5
        assert tmpl["n"] == 2
        assert tmpl["passed"] == 1
        assert tmpl["pass_rate"] == pytest.approx(0.5)
        assert tmpl["wrongly_declined"] == 0
        assert tmpl["invalid_count"] == 0

    def test_by_template_consequence(self, scored):
        scores, _ = scored
        tmpl = scores["generated"]["by_template"]["consequence"]
        # G3 (pass), G4 (fail, wrongly_declined) → n=2, passed=1, wd=1
        assert tmpl["n"] == 2
        assert tmpl["passed"] == 1
        assert tmpl["pass_rate"] == pytest.approx(0.5)
        assert tmpl["wrongly_declined"] == 1
        assert tmpl["invalid_count"] == 0

    # ── handwritten ──

    def test_handwritten_n(self, scored):
        scores, _ = scored
        # H1, H2 = 2 handwritten rows.
        assert scores["handwritten"]["n"] == 2

    def test_handwritten_passed(self, scored):
        scores, _ = scored
        # H1: answered, cited C6, gold [C6, C7] → cite_any_gold → pass
        # H2: invalid → fail
        assert scores["handwritten"]["passed"] == 1

    def test_handwritten_pass_rate(self, scored):
        scores, _ = scored
        # 1 / 2 = 0.5
        assert scores["handwritten"]["pass_rate"] == pytest.approx(0.5)

    def test_handwritten_invalid_count(self, scored):
        scores, _ = scored
        # H2 is invalid.
        assert scores["handwritten"]["invalid_count"] == 1

    # ── unanswerable ──

    def test_unanswerable_n(self, scored):
        scores, _ = scored
        assert scores["unanswerable"]["n"] == 3

    def test_unanswerable_declined(self, scored):
        scores, _ = scored
        # U1 declined correctly.
        assert scores["unanswerable"]["declined"] == 1

    def test_unanswerable_invalid(self, scored):
        scores, _ = scored
        # U2 is invalid (marker with extra text).
        assert scores["unanswerable"]["invalid"] == 1

    def test_unanswerable_answered(self, scored):
        scores, _ = scored
        # U3 answered when it shouldn't have.
        assert scores["unanswerable"]["answered"] == 1

    def test_unanswerable_invalid_reasons(self, scored):
        scores, _ = scored
        # U2 has marker in text → marker_with_text
        reasons = scores["unanswerable"]["invalid_reasons"]
        assert reasons == {"marker_with_text": 1}

    # ── cost ──

    def test_cost_total(self, scored):
        scores, _ = scored
        # G1=0.001, G2=0.001, G3=0.002, G4=0.0005,
        # H1=0.001, H2=0.001,
        # U1=0.0005, U2=0.0005, U3=0.001
        # Total = 0.001+0.001+0.002+0.0005+0.001+0.001
        #       +0.0005+0.0005+0.001 = 0.0085
        assert scores["cost"]["total"] == pytest.approx(0.0085)

    def test_cost_per_question(self, scored):
        scores, _ = scored
        # 0.0085 / 9 ≈ 0.000944
        assert scores["cost"]["per_question"] == pytest.approx(0.0085 / 9)

    # ── latency ──

    def test_generation_latency_p50(self, scored):
        scores, _ = scored
        # Generation latencies sorted:
        # [0.3, 0.4, 0.5, 0.8, 1.0, 1.1, 1.2, 1.5, 2.0]
        # p50: ceil(9 * 0.5) - 1 = ceil(4.5) - 1 = 5 - 1 = 4 → index 4 → 1.0
        assert scores["latency_generation"]["p50"] == pytest.approx(1.0)

    def test_generation_latency_p95(self, scored):
        scores, _ = scored
        # p95: ceil(9 * 0.95) - 1 = ceil(8.55) - 1 = 9 - 1 = 8 → index 8 → 2.0
        assert scores["latency_generation"]["p95"] == pytest.approx(2.0)

    def test_total_latency_p50(self, scored):
        scores, _ = scored
        # Total latencies = retrieval + generation:
        # G1: 0.05+1.0=1.05, G2: 0.05+2.0=2.05, G3: 0.04+1.5=1.54,
        # G4: 0.03+0.5=0.53, H1: 0.06+1.2=1.26, H2: 0.04+0.8=0.84,
        # U1: 0.02+0.3=0.32, U2: 0.03+0.4=0.43, U3: 0.05+1.1=1.15
        # Sorted: [0.32, 0.43, 0.53, 0.84, 1.05, 1.15, 1.26, 1.54, 2.05]
        # p50: index 4 → 1.05
        assert scores["latency_total"]["p50"] == pytest.approx(1.05)

    # ── checked rows ──

    def test_checked_row_count(self, scored):
        _, checked = scored
        assert len(checked) == 9

    def test_checked_row_g4_wrongly_declined(self, scored):
        _, checked = scored
        g4 = next(r for r in checked if r["question_id"] == "G4")
        assert g4["wrongly_declined"] is True
        assert g4["pass_rule_passed"] is False

    def test_checked_row_h2_invalid_reason(self, scored):
        _, checked = scored
        h2 = next(r for r in checked if r["question_id"] == "H2")
        assert h2["invalid"] is True
        assert h2["invalid_reason"] == "bad_citation"


# ═══════════════════════════════════════════════════════════════════
# 2. update_answer_results_md — two-labels coexist
# ═══════════════════════════════════════════════════════════════════


class TestUpdateAnswerResultsMd:
    def test_two_labels_coexist(self, tmp_path: Path):
        md = tmp_path / "results.md"

        # Start with an unrelated section that must survive.
        md.write_text(
            "## Module 1.3\n\nExisting content.\n",
            encoding="utf-8",
        )

        # Write label alpha.
        update_answer_results_md("alpha", "Alpha content.", path=md)
        text = md.read_text(encoding="utf-8")
        assert "## Module 1.3" in text
        assert "Existing content." in text
        assert "## Answer evaluation: alpha (Module 2.3)" in text
        assert "Alpha content." in text

        # Write label beta — alpha and Module 1.3 must survive.
        update_answer_results_md("beta", "Beta content.", path=md)
        text = md.read_text(encoding="utf-8")
        assert "## Module 1.3" in text
        assert "Existing content." in text
        assert "## Answer evaluation: alpha (Module 2.3)" in text
        assert "Alpha content." in text
        assert "## Answer evaluation: beta (Module 2.3)" in text
        assert "Beta content." in text

        # Re-write alpha — beta must survive, alpha is replaced not duplicated.
        update_answer_results_md("alpha", "Alpha v2.", path=md)
        text = md.read_text(encoding="utf-8")
        assert "## Module 1.3" in text
        assert "Existing content." in text
        assert "## Answer evaluation: alpha (Module 2.3)" in text
        assert "Alpha v2." in text
        assert "Alpha content." not in text  # replaced
        assert "## Answer evaluation: beta (Module 2.3)" in text
        assert "Beta content." in text

        # Verify only one "alpha" header exists (not duplicated).
        assert text.count("## Answer evaluation: alpha (Module 2.3)") == 1
