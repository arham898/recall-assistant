"""Tests for label.py, judge.py, and calibrate_judge.py.

No database, no network, no model downloads.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from scripts.calibrate_judge import (
    _cache_key,
    _scored_path_from_answers,
    compute_code_vs_human,
    compute_faithfulness_by_label,
    compute_metrics,
    split_dev_test,
)
from scripts.label import load_existing_labels, select_rows

from recall_assistant.config import PROJECT_ROOT
from recall_assistant.evals.judge import (
    JUDGE_PROMPTS,
    _parse_judge_response,
    judge,
    judge_prompt_hash,
)
from recall_assistant.rag import DECLINE_MARKER
from tests.evals.conftest import make_answer_row, make_golden
from tests.fakes import FakeLLMClient

# ═══════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════


def _build_answer_rows_and_golden(
    n_unanswerable: int = 3,
    n_handwritten: int = 3,
    n_generated: int = 10,
):
    """Build answer-row dicts and golden_by_id for sampling tests."""
    rows = []
    golden_by_id = {}

    for i in range(n_unanswerable):
        qid = f"unans_{i}"
        rows.append(make_answer_row(qid, "unanswerable", "declined", []))
        golden_by_id[qid] = make_golden(qid, "unanswerable", [], "decline")

    for i in range(n_handwritten):
        qid = f"hw_{i}"
        rows.append(make_answer_row(qid, "handwritten", "answered", [f"C{i}"]))
        golden_by_id[qid] = make_golden(qid, "handwritten", [f"C{i}"], "cite_any_gold")

    for i in range(n_generated):
        qid = f"gen_{i}"
        rows.append(make_answer_row(qid, "generated", "answered", [f"G{i}"]))
        golden_by_id[qid] = make_golden(
            qid, "generated", [f"G{i}"], "cite_all_gold", template="remedy"
        )

    return rows, golden_by_id


# ═══════════════════════════════════════════════════════════════════
# 1. Stratified sampling
# ═══════════════════════════════════════════════════════════════════


class TestStratifiedSampling:
    def test_all_unanswerable_included(self):
        rows, golden = _build_answer_rows_and_golden()
        selected = select_rows(rows, golden, n=10, seed=42)
        unanswerable_ids = {
            r["question_id"]
            for r in selected
            if golden[r["question_id"]].source == "unanswerable"
        }
        assert unanswerable_ids == {"unans_0", "unans_1", "unans_2"}

    def test_all_handwritten_included(self):
        rows, golden = _build_answer_rows_and_golden()
        selected = select_rows(rows, golden, n=10, seed=42)
        hw_ids = {
            r["question_id"]
            for r in selected
            if golden[r["question_id"]].source == "handwritten"
        }
        assert hw_ids == {"hw_0", "hw_1", "hw_2"}

    def test_generated_fills_remaining(self):
        rows, golden = _build_answer_rows_and_golden()
        selected = select_rows(rows, golden, n=10, seed=42)
        gen_ids = [
            r["question_id"]
            for r in selected
            if golden[r["question_id"]].source == "generated"
        ]
        # 3 unanswerable + 3 handwritten = 6 fixed, so 4 generated
        assert len(gen_ids) == 4

    def test_total_equals_n(self):
        rows, golden = _build_answer_rows_and_golden()
        selected = select_rows(rows, golden, n=10, seed=42)
        assert len(selected) == 10

    def test_deterministic_with_seed(self):
        rows, golden = _build_answer_rows_and_golden()
        a = select_rows(rows, golden, n=10, seed=42)
        b = select_rows(rows, golden, n=10, seed=42)
        assert [r["question_id"] for r in a] == [r["question_id"] for r in b]

    def test_different_seed_different_selection(self):
        rows, golden = _build_answer_rows_and_golden(n_generated=20)
        a = select_rows(rows, golden, n=10, seed=42)
        b = select_rows(rows, golden, n=10, seed=99)
        gen_a = {
            r["question_id"]
            for r in a
            if golden[r["question_id"]].source == "generated"
        }
        gen_b = {
            r["question_id"]
            for r in b
            if golden[r["question_id"]].source == "generated"
        }
        assert gen_a != gen_b

    def test_refuses_n_below_fixed_count(self, tmp_path):
        """label.py exits non-zero when --n < unanswerable + handwritten."""
        metadata = {"type": "metadata", "label": "test", "date": "2026-01-01"}
        # 1 unanswerable + 1 handwritten + 1 generated = fixed count is 2
        rows = [
            metadata,
            {
                "question_id": "hw_031",
                "source": "unanswerable",
                "status": "declined",
                "answer_text": "NO_RELEVANT_RECALL_FOUND",
                "cited_ids": [],
                "invalid_ids": [],
                "retrieved_ids": ["X"],
                "retrieved_texts": ["t"],
                "prompt_tokens": 1,
                "completion_tokens": 1,
                "cost": 0.0,
                "retrieval_latency_s": 0.0,
                "generation_latency_s": 0.0,
                "finish_reason": "stop",
            },
            {
                "question_id": "hw_001",
                "source": "handwritten",
                "status": "answered",
                "answer_text": "a",
                "cited_ids": [],
                "invalid_ids": [],
                "retrieved_ids": ["X"],
                "retrieved_texts": ["t"],
                "prompt_tokens": 1,
                "completion_tokens": 1,
                "cost": 0.0,
                "retrieval_latency_s": 0.0,
                "generation_latency_s": 0.0,
                "finish_reason": "stop",
            },
            {
                "question_id": "gen_remedy_WESTERN_STAR_47X_2021",
                "source": "generated",
                "status": "answered",
                "answer_text": "a",
                "cited_ids": [],
                "invalid_ids": [],
                "retrieved_ids": ["X"],
                "retrieved_texts": ["t"],
                "prompt_tokens": 1,
                "completion_tokens": 1,
                "cost": 0.0,
                "retrieval_latency_s": 0.0,
                "generation_latency_s": 0.0,
                "finish_reason": "stop",
            },
        ]
        answers_path = tmp_path / "answers.jsonl"
        with open(answers_path, "w", encoding="utf-8") as f:
            f.writelines(json.dumps(r) + "\n" for r in rows)

        result = subprocess.run(
            [
                sys.executable,
                "scripts/label.py",
                "--answers",
                str(answers_path),
                "--n",
                "1",
                "--labels",
                str(tmp_path / "labels.jsonl"),
            ],
            capture_output=True,
            text=True,
            check=False,
            cwd=str(PROJECT_ROOT),
        )
        assert result.returncode != 0
        assert "below" in result.stderr.lower()
        assert "2" in result.stderr  # the required minimum


# ═══════════════════════════════════════════════════════════════════
# 2. Resumability
# ═══════════════════════════════════════════════════════════════════


class TestResumability:
    def test_existing_labels_skipped(self, tmp_path):
        labels_path = tmp_path / "labels.jsonl"
        labels_path.write_text(
            json.dumps(
                {
                    "question_id": "hw_0",
                    "answers_file": "a.jsonl",
                    "label": "pass",
                    "note": "",
                    "labelled_at": "2026-01-01",
                }
            )
            + "\n"
            + json.dumps(
                {
                    "question_id": "gen_0",
                    "answers_file": "a.jsonl",
                    "label": "fail",
                    "note": "wrong",
                    "labelled_at": "2026-01-01",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        existing = load_existing_labels(labels_path)
        assert "hw_0" in existing
        assert "gen_0" in existing
        assert existing["hw_0"]["label"] == "pass"
        assert existing["gen_0"]["note"] == "wrong"

    def test_missing_file_returns_empty(self, tmp_path):
        existing = load_existing_labels(tmp_path / "nonexistent.jsonl")
        assert existing == {}


# ═══════════════════════════════════════════════════════════════════
# 3. Interactive labeling
# ═══════════════════════════════════════════════════════════════════


class TestLabelInteractive:
    def test_pass_label_recorded(self, tmp_path):
        from scripts.label import label_loop

        labels_path = tmp_path / "labels.jsonl"
        rows, golden = _build_answer_rows_and_golden(
            n_unanswerable=0, n_handwritten=0, n_generated=1
        )
        inputs = iter(["p", ""])
        label_loop(
            rows,
            golden,
            "test.jsonl",
            labels_path,
            {},
            input_fn=lambda _prompt: next(inputs),
        )
        written = [
            json.loads(line)
            for line in labels_path.read_text(encoding="utf-8").strip().split("\n")
        ]
        assert len(written) == 1
        assert written[0]["label"] == "pass"
        assert written[0]["note"] == ""

    def test_fail_with_note(self, tmp_path):
        from scripts.label import label_loop

        labels_path = tmp_path / "labels.jsonl"
        rows, golden = _build_answer_rows_and_golden(
            n_unanswerable=0, n_handwritten=0, n_generated=1
        )
        inputs = iter(["f", "wrong remedy"])
        label_loop(
            rows,
            golden,
            "test.jsonl",
            labels_path,
            {},
            input_fn=lambda _prompt: next(inputs),
        )
        written = [
            json.loads(line)
            for line in labels_path.read_text(encoding="utf-8").strip().split("\n")
        ]
        assert len(written) == 1
        assert written[0]["label"] == "fail"
        assert written[0]["note"] == "wrong remedy"

    def test_quit_stops_early(self, tmp_path):
        from scripts.label import label_loop

        labels_path = tmp_path / "labels.jsonl"
        rows, golden = _build_answer_rows_and_golden(
            n_unanswerable=0, n_handwritten=0, n_generated=3
        )
        inputs = iter(["q"])
        label_loop(
            rows,
            golden,
            "test.jsonl",
            labels_path,
            {},
            input_fn=lambda _prompt: next(inputs),
        )
        assert (
            not labels_path.exists()
            or labels_path.read_text(encoding="utf-8").strip() == ""
        )


# ═══════════════════════════════════════════════════════════════════
# 4. Label subprocess test (real golden IDs)
# ═══════════════════════════════════════════════════════════════════


class TestLabelSubprocess:
    def test_piped_labeling(self, tmp_path):
        """Run label.py as a subprocess with piped stdin on real golden IDs."""
        # Build a minimal answers JSONL with real golden IDs
        metadata = {"type": "metadata", "label": "test", "date": "2026-01-01"}
        # unanswerable row comes first in stratified order
        unans_row = {
            "question_id": "hw_031",
            "source": "unanswerable",
            "status": "declined",
            "answer_text": "NO_RELEVANT_RECALL_FOUND",
            "cited_ids": [],
            "invalid_ids": [],
            "retrieved_ids": ["FAKE001"],
            "retrieved_texts": ["Some text"],
            "prompt_tokens": 100,
            "completion_tokens": 10,
            "cost": 0.0001,
            "retrieval_latency_s": 0.1,
            "generation_latency_s": 0.5,
            "finish_reason": "stop",
        }
        hw_row = {
            "question_id": "hw_001",
            "source": "handwritten",
            "status": "answered",
            "answer_text": "The screen may go black [24V800000].",
            "cited_ids": ["24V800000"],
            "invalid_ids": [],
            "retrieved_ids": ["24V800000"],
            "retrieved_texts": ["Backup camera recall text"],
            "prompt_tokens": 200,
            "completion_tokens": 30,
            "cost": 0.0002,
            "retrieval_latency_s": 0.1,
            "generation_latency_s": 0.8,
            "finish_reason": "stop",
        }
        gen_row = {
            "question_id": "gen_remedy_WESTERN_STAR_47X_2021",
            "source": "generated",
            "status": "answered",
            "answer_text": "Brake recall fix [22V817000].",
            "cited_ids": ["22V817000"],
            "invalid_ids": [],
            "retrieved_ids": ["22V817000"],
            "retrieved_texts": ["Brake modulator recall text"],
            "prompt_tokens": 300,
            "completion_tokens": 40,
            "cost": 0.0003,
            "retrieval_latency_s": 0.1,
            "generation_latency_s": 1.0,
            "finish_reason": "stop",
        }

        answers_path = tmp_path / "answers.jsonl"
        with open(answers_path, "w", encoding="utf-8") as f:
            f.writelines(
                json.dumps(obj) + "\n" for obj in [metadata, unans_row, hw_row, gen_row]
            )

        labels_path = tmp_path / "labels.jsonl"

        # Pipe: pass first row (no note), fail second (with note), quit third
        stdin_text = "p\n\nf\nwrong remedy\nq\n"

        result = subprocess.run(
            [
                sys.executable,
                "scripts/label.py",
                "--answers",
                str(answers_path),
                "--n",
                "3",
                "--labels",
                str(labels_path),
            ],
            input=stdin_text,
            capture_output=True,
            text=True,
            check=False,
            cwd=str(PROJECT_ROOT),
        )
        assert result.returncode == 0, (
            f"stderr: {result.stderr}\nstdout: {result.stdout}"
        )

        # Check the written labels
        written = [
            json.loads(line)
            for line in labels_path.read_text(encoding="utf-8").strip().split("\n")
        ]
        assert len(written) == 2
        assert written[0]["label"] == "pass"
        assert written[0]["note"] == ""
        assert written[1]["label"] == "fail"
        assert written[1]["note"] == "wrong remedy"


# ═══════════════════════════════════════════════════════════════════
# 5. Judge response parsing
# ═══════════════════════════════════════════════════════════════════


class TestParseJudgeResponse:
    def test_valid_pass(self):
        r = _parse_judge_response('{"verdict": "pass", "reason": "Correct facts."}')
        assert r.verdict == "pass"
        assert r.reason == "Correct facts."

    def test_valid_fail(self):
        r = _parse_judge_response('{"verdict": "fail", "reason": "Missing key info."}')
        assert r.verdict == "fail"

    def test_markdown_fenced(self):
        text = '```json\n{"verdict": "pass", "reason": "ok"}\n```'
        r = _parse_judge_response(text)
        assert r.verdict == "pass"

    def test_malformed_json(self):
        r = _parse_judge_response("not json at all")
        assert r.verdict == "error"
        assert "parse error" in r.reason.lower()

    def test_missing_verdict_key(self):
        r = _parse_judge_response('{"result": "pass", "reason": "ok"}')
        assert r.verdict == "error"
        assert "parse error" in r.reason.lower()

    def test_invalid_verdict_value(self):
        r = _parse_judge_response('{"verdict": "maybe", "reason": "uncertain"}')
        assert r.verdict == "error"
        assert "parse error" in r.reason.lower()


# ═══════════════════════════════════════════════════════════════════
# 6. Judge function
# ═══════════════════════════════════════════════════════════════════


class TestJudgeFunction:
    def test_judge_pass(self):
        fake = FakeLLMClient(text='{"verdict": "pass", "reason": "Correct."}')
        r = judge(
            question="What is the brake recall?",
            reference_answer="Recall 24V123000: brake hose crack.",
            candidate_answer="The brake hose may crack [24V123000].",
            llm_client=fake,
            model="test-model",
        )
        assert r.verdict == "pass"
        assert len(fake.calls) == 1

    def test_decline_marker_replaced(self):
        fake = FakeLLMClient(
            text='{"verdict": "pass", "reason": "Correctly declined."}'
        )
        judge(
            question="Recall on 2022 Pinto?",
            reference_answer="No matching recall found.",
            candidate_answer=DECLINE_MARKER,
            llm_client=fake,
            model="test-model",
        )
        messages = fake.calls[0][0]
        user_msg = messages[1]["content"]
        assert "The assistant declined to answer." in user_msg
        assert DECLINE_MARKER not in user_msg

    def test_malformed_response_gives_error(self):
        fake = FakeLLMClient(text="I think it passes")
        r = judge(
            question="q",
            reference_answer="ref",
            candidate_answer="ans",
            llm_client=fake,
            model="m",
        )
        assert r.verdict == "error"
        assert "parse error" in r.reason.lower()


# ═══════════════════════════════════════════════════════════════════
# 7. Prompt registry and hash
# ═══════════════════════════════════════════════════════════════════


class TestJudgePromptRegistry:
    def test_v1_present(self):
        assert "v1" in JUDGE_PROMPTS
        assert len(JUDGE_PROMPTS["v1"]) > 100

    def test_unknown_version_raises(self):
        fake = FakeLLMClient(text='{"verdict": "pass", "reason": "ok"}')
        with pytest.raises(ValueError, match="Unknown prompt version"):
            judge(
                question="q",
                reference_answer="r",
                candidate_answer="a",
                llm_client=fake,
                model="m",
                prompt_version="v99",
            )


class TestJudgePromptHash:
    def test_hash_format(self):
        h = judge_prompt_hash("some prompt text")
        assert len(h) == 12
        assert all(c in "0123456789abcdef" for c in h)

    def test_different_text_different_hash(self):
        h1 = judge_prompt_hash("prompt A")
        h2 = judge_prompt_hash("prompt B")
        assert h1 != h2


class TestJudgeCacheKeyMiss:
    def test_prompt_text_change_misses_cache(self):
        """Same version string but different prompt text → different cache key."""
        key_a = _cache_key(
            "Q1", "a.jsonl", "model/x", "v1", judge_prompt_hash("prompt A")
        )
        key_b = _cache_key(
            "Q1", "a.jsonl", "model/x", "v1", judge_prompt_hash("prompt B")
        )
        assert key_a != key_b


# ═══════════════════════════════════════════════════════════════════
# 8. Metric arithmetic
# ═══════════════════════════════════════════════════════════════════


class TestMetricArithmetic:
    def test_perfect_agreement(self):
        preds = ["pass", "pass", "fail", "fail"]
        labels = ["pass", "pass", "fail", "fail"]
        m = compute_metrics(preds, labels)
        assert m["agreement"] == 1.0
        assert m["kappa"] == 1.0
        assert m["confusion"] == {"tp": 2, "fp": 0, "fn": 0, "tn": 2}

    def test_complete_disagreement(self):
        """TP=0 FP=5 FN=5 TN=0 → kappa=-1.0."""
        preds = ["pass"] * 5 + ["fail"] * 5
        labels = ["fail"] * 5 + ["pass"] * 5
        m = compute_metrics(preds, labels)
        assert m["agreement"] == 0.0
        assert m["kappa"] == pytest.approx(-1.0)
        assert m["confusion"] == {"tp": 0, "fp": 5, "fn": 5, "tn": 0}

    def test_hand_worked_kappa(self):
        """TP=7 FP=2 FN=1 TN=5, N=15.

        p_o = 12/15 = 0.8
        p_yes = (9/15)(8/15) = 72/225 ≈ 0.32
        p_no  = (6/15)(7/15) = 42/225 ≈ 0.18667
        p_e = 0.50667
        kappa = (0.8 - 0.50667) / (1 - 0.50667) ≈ 0.5946
        """
        preds = ["pass"] * 7 + ["pass"] * 2 + ["fail"] * 1 + ["fail"] * 5
        labels = ["pass"] * 7 + ["fail"] * 2 + ["pass"] * 1 + ["fail"] * 5
        m = compute_metrics(preds, labels)
        assert m["agreement"] == pytest.approx(12 / 15)
        assert m["kappa"] == pytest.approx(0.5946, abs=0.001)
        assert m["tpr"] == pytest.approx(7 / 8)
        assert m["tnr"] == pytest.approx(5 / 7)
        assert m["precision_fail"] == pytest.approx(5 / 6)
        assert m["recall_fail"] == pytest.approx(5 / 7)


# ═══════════════════════════════════════════════════════════════════
# 9. Dev/test split
# ═══════════════════════════════════════════════════════════════════


class TestDevTestSplit:
    def test_deterministic(self):
        rows = [{"question_id": f"Q{i}"} for i in range(20)]
        dev1, test1 = split_dev_test(rows, seed=42)
        dev2, test2 = split_dev_test(rows, seed=42)
        assert [r["question_id"] for r in dev1] == [r["question_id"] for r in dev2]
        assert [r["question_id"] for r in test1] == [r["question_id"] for r in test2]

    def test_disjoint(self):
        rows = [{"question_id": f"Q{i}"} for i in range(20)]
        dev, test = split_dev_test(rows, seed=42)
        dev_ids = {r["question_id"] for r in dev}
        test_ids = {r["question_id"] for r in test}
        assert dev_ids & test_ids == set()
        assert dev_ids | test_ids == {f"Q{i}" for i in range(20)}

    def test_roughly_equal_size(self):
        rows = [{"question_id": f"Q{i}"} for i in range(21)]
        dev, test = split_dev_test(rows, seed=42)
        assert abs(len(dev) - len(test)) <= 1

    def test_shuffle_not_just_sorted_halves(self):
        """Seed 42 must not put q01..q10 in dev (i.e. the split is shuffled)."""
        rows = [{"question_id": f"q{i:02d}"} for i in range(1, 21)]
        dev, _test = split_dev_test(rows, seed=42)
        dev_ids = {r["question_id"] for r in dev}
        first_half_sorted = {f"q{i:02d}" for i in range(1, 11)}
        assert dev_ids != first_half_sorted

    def test_different_seeds_different_dev_sets(self):
        """Seed 42 and seed 43 produce different dev sets."""
        rows = [{"question_id": f"q{i:02d}"} for i in range(1, 21)]
        dev_42, _ = split_dev_test(rows, seed=42)
        dev_43, _ = split_dev_test(rows, seed=43)
        ids_42 = {r["question_id"] for r in dev_42}
        ids_43 = {r["question_id"] for r in dev_43}
        assert ids_42 != ids_43


# ═══════════════════════════════════════════════════════════════════
# 10. Code-check vs human
# ═══════════════════════════════════════════════════════════════════


class TestCodeVsHuman:
    def test_four_cell_breakdown(self):
        scored = [
            {"question_id": "Q1", "pass_rule_passed": True},
            {"question_id": "Q2", "pass_rule_passed": True},
            {"question_id": "Q3", "pass_rule_passed": False},
            {"question_id": "Q4", "pass_rule_passed": False},
        ]
        labels = {"Q1": "pass", "Q2": "fail", "Q3": "pass", "Q4": "fail"}
        result = compute_code_vs_human(scored, labels)
        assert result["both_pass"] == 1
        assert result["code_pass_human_fail"] == 1
        assert result["code_fail_human_pass"] == 1
        assert result["both_fail"] == 1
        assert result["agreement"] == pytest.approx(0.5)


# ═══════════════════════════════════════════════════════════════════
# 11. Scored file path derivation
# ═══════════════════════════════════════════════════════════════════


class TestScoredPathDerivation:
    def test_standard_path(self):
        p = Path("results/answers_rerank_record_filtered_2026-10-19.jsonl")
        assert _scored_path_from_answers(p) == Path(
            "results/scored_rerank_record_filtered_2026-10-19.jsonl"
        )

    def test_non_answers_prefix_raises(self):
        with pytest.raises(ValueError):
            _scored_path_from_answers(Path("results/foo_bar.jsonl"))


# ═══════════════════════════════════════════════════════════════════
# 12. Edge cases
# ═══════════════════════════════════════════════════════════════════


class TestEdgeCases:
    def test_question_id_with_slash(self):
        golden = make_golden("gen/subdir/Q1", "generated", ["C1"], "cite_all_gold")
        row = make_answer_row("gen/subdir/Q1", "generated", "answered", ["C1"])
        golden_by_id = {golden.id: golden}
        selected = select_rows([row], golden_by_id, n=1, seed=42)
        assert len(selected) == 1
        assert selected[0]["question_id"] == "gen/subdir/Q1"

    def test_citation_90S9(self):
        fake = FakeLLMClient(text='{"verdict": "pass", "reason": "ok"}')
        golden = make_golden("hw_special", "handwritten", ["90S9"], "cite_any_gold")
        row = make_answer_row(
            "hw_special",
            "handwritten",
            "answered",
            ["90S9"],
            answer_text="Issue with tire [90S9].",
            retrieved_ids=["90S9"],
        )
        r = judge(
            question=golden.question,
            reference_answer=golden.reference_answer,
            candidate_answer=row["answer_text"],
            llm_client=fake,
            model="test-model",
        )
        assert r.verdict == "pass"


# ═══════════════════════════════════════════════════════════════════
# 12b. Display row (capsys)
# ═══════════════════════════════════════════════════════════════════


class TestDisplayRow:
    def test_shows_expected_fields_hides_anchoring(self, capsys):
        """display_row prints question, reference, answer, cards but not status/checks."""
        from scripts.label import display_row

        golden = make_golden(
            "disp_test", "generated", ["22V817000"], "cite_all_gold", template="remedy"
        )
        row = make_answer_row(
            "disp_test",
            "generated",
            "invalid",
            ["22V817000"],
            answer_text="DISTINCTIVE_ANSWER_XYZ_42",
            retrieved_ids=["22V817000", "99V001000"],
            retrieved_texts=["Brake modulator recall text", "Other recall text"],
        )
        display_row(row, golden, index=1, total=5)
        out = capsys.readouterr().out

        # Must contain these
        assert golden.question in out
        assert golden.reference_answer in out
        assert "DISTINCTIVE_ANSWER_XYZ_42" in out
        assert "22V817000" in out

        # Must NOT contain anchoring fields
        assert "invalid" not in out.lower()
        assert "status" not in out.lower()
        assert "pass_rule" not in out.lower()
        assert "faithfulness" not in out.lower()


# ═══════════════════════════════════════════════════════════════════
# 13. Subprocess --help tests
# ═══════════════════════════════════════════════════════════════════


class TestScriptHelp:
    def test_label_help(self):
        result = subprocess.run(
            [sys.executable, "scripts/label.py", "--help"],
            capture_output=True,
            text=True,
            check=False,
            cwd=str(PROJECT_ROOT),
        )
        assert result.returncode == 0
        assert "--answers" in result.stdout
        assert "--n" in result.stdout
        assert "--seed" in result.stdout
        assert "--labels" in result.stdout
        assert "ModuleNotFoundError" not in result.stderr
        assert "ImportError" not in result.stderr

    def test_calibrate_judge_help(self):
        result = subprocess.run(
            [sys.executable, "scripts/calibrate_judge.py", "--help"],
            capture_output=True,
            text=True,
            check=False,
            cwd=str(PROJECT_ROOT),
        )
        assert result.returncode == 0
        assert "--labels" in result.stdout
        assert "--answers" in result.stdout
        assert "--prompt-version" in result.stdout
        assert "ModuleNotFoundError" not in result.stderr
        assert "ImportError" not in result.stderr


# ═══════════════════════════════════════════════════════════════════
# 14. Error verdict exclusion
# ═══════════════════════════════════════════════════════════════════


class TestErrorVerdictExcluded:
    def test_error_excluded_from_metrics(self):
        """Error verdicts should not be passed to compute_metrics."""
        # If we filter errors before calling compute_metrics,
        # only pass/fail rows are counted.
        preds = ["pass", "fail"]  # 2 rows after filtering out 1 error
        labels = ["pass", "fail"]
        m = compute_metrics(preds, labels)
        assert m["n"] == 2
        assert m["agreement"] == 1.0


# ═══════════════════════════════════════════════════════════════════
# 15. Faithfulness by label
# ═══════════════════════════════════════════════════════════════════


class TestFaithfulnessByLabel:
    def test_mean_faithfulness(self):
        scored = [
            {"question_id": "Q1", "faithfulness": 0.9},
            {"question_id": "Q2", "faithfulness": 0.8},
            {"question_id": "Q3", "faithfulness": 0.3},
            {"question_id": "Q4", "faithfulness": None},  # excluded
        ]
        labels = {"Q1": "pass", "Q2": "pass", "Q3": "fail", "Q4": "pass"}
        result = compute_faithfulness_by_label(scored, labels)
        assert result["pass_mean"] == pytest.approx(0.85)
        assert result["pass_n"] == 2
        assert result["fail_mean"] == pytest.approx(0.3)
        assert result["fail_n"] == 1
