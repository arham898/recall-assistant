"""Tests for prompt-fix comparison helpers and CLI script.

No database, no network, no model downloads.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from recall_assistant.config import PROJECT_ROOT
from recall_assistant.evals.answer_eval import load_answers_jsonl
from recall_assistant.evals.prompt_fix_comparison import (
    count_example_ids,
    count_placeholder_leak,
    faithfulness_summary,
)

# ═══════════════════════════════════════════════════════════════════
# 1. count_example_ids
# ═══════════════════════════════════════════════════════════════════


class TestCountExampleIds:
    def test_finds_id_without_brackets(self):
        # Plain text, no brackets — still counted.
        texts = {"q1": "The recall 24V123000 affects certain trucks."}
        assert count_example_ids(texts) == 1

    def test_zero_when_absent(self):
        texts = {"q1": "Brake hose replaced under 20V477000."}
        assert count_example_ids(texts) == 0

    def test_both_ids_counted_once(self):
        # Both example IDs in one answer → counted once.
        texts = {"q1": "Recalls 24V123000 and 25V456000 apply here."}
        assert count_example_ids(texts) == 1

    def test_multiple_answers(self):
        texts = {
            "q1": "See 24V123000.",
            "q2": "No example IDs here.",
            "q3": "Also 25V456000.",
        }
        assert count_example_ids(texts) == 2


# ═══════════════════════════════════════════════════════════════════
# 2. count_placeholder_leak
# ═══════════════════════════════════════════════════════════════════


class TestCountPlaceholderLeak:
    def test_finds_campaign_id_text(self):
        texts = {"q1": "Cite the campaign_id of each source."}
        assert count_placeholder_leak(texts) == 1

    def test_finds_angle_bracket(self):
        texts = {"q1": "Use [<campaign_id>] format."}
        assert count_placeholder_leak(texts) == 1

    def test_zero_when_clean(self):
        texts = {"q1": "The recall [20V477000] involves brake hoses."}
        assert count_placeholder_leak(texts) == 0


# ═══════════════════════════════════════════════════════════════════
# 3. faithfulness_summary
# ═══════════════════════════════════════════════════════════════════


class TestFaithfulnessSummary:
    def test_excludes_none_values(self):
        scored = [
            {"faithfulness": 0.9},
            {"faithfulness": None},
            {"faithfulness": 0.3},
            {"faithfulness": None},
            {"faithfulness": 0.8},
        ]
        result = faithfulness_summary(scored)
        # n counts only non-None rows.
        assert result["n"] == 3
        assert result["flagged_below_0_5"] == 1
        assert result["min"] == pytest.approx(0.3)

    def test_all_none(self):
        scored = [{"faithfulness": None}, {"faithfulness": None}]
        result = faithfulness_summary(scored)
        assert result["n"] == 0


# ═══════════════════════════════════════════════════════════════════
# 4. load_answers_jsonl skips metadata
# ═══════════════════════════════════════════════════════════════════


class TestLoadAnswersSkipsMetadata:
    def test_metadata_excluded(self, tmp_path: Path):
        path = tmp_path / "answers.jsonl"
        lines = [
            json.dumps({"type": "metadata", "label": "test"}),
            json.dumps({"question_id": "q1", "answer_text": "Answer one."}),
            json.dumps({"question_id": "q2", "answer_text": "Answer two."}),
        ]
        path.write_text("\n".join(lines), encoding="utf-8")

        metadata, rows = load_answers_jsonl(path)
        assert metadata["type"] == "metadata"
        assert len(rows) == 2
        assert rows[0]["question_id"] == "q1"


# ═══════════════════════════════════════════════════════════════════
# 5. Subprocess: compare_prompt_fix.py writes to custom path
# ═══════════════════════════════════════════════════════════════════


def _write_answers_jsonl(path: Path, rows: list[dict]) -> None:
    """Write a minimal answers JSONL with a metadata header."""
    lines = [json.dumps({"type": "metadata", "label": "test"})]
    lines.extend(json.dumps(r) for r in rows)
    path.write_text("\n".join(lines), encoding="utf-8")


def _write_scored_jsonl(path: Path, rows: list[dict]) -> None:
    """Write a minimal scored JSONL (no metadata header)."""
    path.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")


class TestSubprocessComparison:
    def test_writes_to_custom_results_md(self, tmp_path: Path):
        # Minimal answer row.
        answer_row = {
            "question_id": "q1",
            "answer_text": "Brake fix [20V477000].",
            "status": "answered",
            "cited_ids": ["20V477000"],
            "invalid_ids": [],
        }
        scored_row = {
            "question_id": "q1",
            "source": "generated",
            "template": "remedy",
            "status": "answered",
            "invalid": False,
            "invalid_reason": "",
            "pass_rule_passed": True,
            "faithfulness": 0.95,
        }

        old_ans = tmp_path / "old_answers.jsonl"
        old_scr = tmp_path / "old_scored.jsonl"
        new_ans = tmp_path / "new_answers.jsonl"
        new_scr = tmp_path / "new_scored.jsonl"
        results_md = tmp_path / "results.md"

        _write_answers_jsonl(old_ans, [answer_row])
        _write_scored_jsonl(old_scr, [scored_row])
        _write_answers_jsonl(new_ans, [answer_row])
        _write_scored_jsonl(new_scr, [scored_row])

        result = subprocess.run(
            [
                sys.executable,
                "scripts/compare_prompt_fix.py",
                "--old-answers",
                str(old_ans),
                "--old-scored",
                str(old_scr),
                "--new-answers",
                str(new_ans),
                "--new-scored",
                str(new_scr),
                "--results-md",
                str(results_md),
                "--allow-dirty",
            ],
            capture_output=True,
            text=True,
            check=False,
            cwd=str(PROJECT_ROOT),
        )
        assert result.returncode == 0, (
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
        assert results_md.exists()
        content = results_md.read_text(encoding="utf-8")
        assert "## Prompt-fix comparison (Module 2.5b)" in content
        assert "Pass rate by template" in content
        assert "Caveats" in content
