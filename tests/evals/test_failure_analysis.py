"""Tests for failure_analysis.py: three-way union, worksheet round-trip, validation.

No database, no network, no model downloads.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from recall_assistant.config import PROJECT_ROOT
from recall_assistant.evals.failure_analysis import (
    VALID_CATEGORY_KEYS,
    build_failure_rows,
    format_worksheet,
    parse_worksheet,
    tally_categories,
    validate_categories,
)
from tests.evals.conftest import make_answer_row, make_golden

# ═══════════════════════════════════════════════════════════════════
# Fixture data
# ═══════════════════════════════════════════════════════════════════

# Golden rows (7 questions).
_GOLDEN_A = make_golden("QA", "generated", ["C1"], "cite_all_gold", template="remedy")
_GOLDEN_B = make_golden(
    "QB", "generated", ["C2"], "cite_all_gold", template="consequence"
)
_GOLDEN_C = make_golden("QC", "generated", ["C3"], "cite_all_gold", template="remedy")
_GOLDEN_D = make_golden("QD", "handwritten", ["C4"], "cite_any_gold")
_GOLDEN_E = make_golden(
    "QE", "generated", ["C5"], "cite_all_gold", template="existence"
)
_GOLDEN_SLASH = make_golden(
    "gen/sub/Q1",
    "generated",
    ["C6"],
    "cite_all_gold",
    template="existence",
)
_GOLDEN_90S9 = make_golden("hw_90S9", "handwritten", ["90S9"], "cite_any_gold")

GOLDEN_BY_ID: dict = {
    g.id: g
    for g in [
        _GOLDEN_A,
        _GOLDEN_B,
        _GOLDEN_C,
        _GOLDEN_D,
        _GOLDEN_E,
        _GOLDEN_SLASH,
        _GOLDEN_90S9,
    ]
}

# Answer rows.
ANSWERS: list[dict] = [
    make_answer_row("QA", "generated", "answered", ["C1"], retrieved_ids=["C1", "X1"]),
    make_answer_row("QB", "generated", "answered", ["X2"], retrieved_ids=["C2", "X2"]),
    make_answer_row("QC", "generated", "answered", [], retrieved_ids=["C3", "X3"]),
    make_answer_row(
        "QD", "handwritten", "answered", ["X4"], retrieved_ids=["C4", "X4"]
    ),
    make_answer_row("QE", "generated", "answered", ["C5"], retrieved_ids=["C5", "X5"]),
    make_answer_row(
        "gen/sub/Q1",
        "generated",
        "answered",
        ["X6"],
        retrieved_ids=["C6", "X6"],
    ),
    make_answer_row(
        "hw_90S9",
        "handwritten",
        "answered",
        ["90S9"],
        retrieved_ids=["90S9"],
    ),
]

# Scored rows.  source is needed for effective template when template=None.
#   QA: code pass   QB: code fail (bad_citation)   QC: code fail   QD: code fail
#   QE: code pass   gen/sub/Q1: code fail   hw_90S9: code pass
SCORED: list[dict] = [
    {
        "question_id": "QA",
        "template": "remedy",
        "source": "generated",
        "status": "answered",
        "invalid": False,
        "invalid_reason": "",
        "pass_rule": "cite_all_gold",
        "pass_rule_passed": True,
        "cites_gold": True,
        "cites_all_gold": True,
        "wrongly_declined": False,
        "cited_ids": ["C1"],
        "invalid_ids": [],
        "gold_campaign_ids": ["C1"],
    },
    {
        "question_id": "QB",
        "template": "consequence",
        "source": "generated",
        "status": "invalid",
        "invalid": True,
        "invalid_reason": "bad_citation",
        "pass_rule": "cite_all_gold",
        "pass_rule_passed": False,
        "cites_gold": False,
        "cites_all_gold": False,
        "wrongly_declined": False,
        "cited_ids": ["X2", "BAD1"],
        "invalid_ids": ["BAD1"],
        "gold_campaign_ids": ["C2"],
    },
    {
        "question_id": "QC",
        "template": "remedy",
        "source": "generated",
        "status": "answered",
        "invalid": False,
        "invalid_reason": "",
        "pass_rule": "cite_all_gold",
        "pass_rule_passed": False,
        "cites_gold": False,
        "cites_all_gold": False,
        "wrongly_declined": False,
        "cited_ids": [],
        "invalid_ids": [],
        "gold_campaign_ids": ["C3"],
    },
    {
        "question_id": "QD",
        "template": None,
        "source": "handwritten",
        "status": "answered",
        "invalid": False,
        "invalid_reason": "",
        "pass_rule": "cite_any_gold",
        "pass_rule_passed": False,
        "cites_gold": False,
        "cites_all_gold": False,
        "wrongly_declined": False,
        "cited_ids": ["X4"],
        "invalid_ids": [],
        "gold_campaign_ids": ["C4"],
    },
    {
        "question_id": "QE",
        "template": "existence",
        "source": "generated",
        "status": "answered",
        "invalid": False,
        "invalid_reason": "",
        "pass_rule": "cite_all_gold",
        "pass_rule_passed": True,
        "cites_gold": True,
        "cites_all_gold": True,
        "wrongly_declined": False,
        "cited_ids": ["C5"],
        "invalid_ids": [],
        "gold_campaign_ids": ["C5"],
    },
    {
        "question_id": "gen/sub/Q1",
        "template": "existence",
        "source": "generated",
        "status": "answered",
        "invalid": False,
        "invalid_reason": "",
        "pass_rule": "cite_all_gold",
        "pass_rule_passed": False,
        "cites_gold": False,
        "cites_all_gold": False,
        "wrongly_declined": False,
        "cited_ids": ["X6"],
        "invalid_ids": [],
        "gold_campaign_ids": ["C6"],
    },
    {
        "question_id": "hw_90S9",
        "template": None,
        "source": "handwritten",
        "status": "invalid",
        "invalid": True,
        "invalid_reason": "bad_citation",
        "pass_rule": "cite_any_gold",
        "pass_rule_passed": True,
        "cites_gold": False,
        "cites_all_gold": False,
        "wrongly_declined": False,
        "cited_ids": ["90S9"],
        "invalid_ids": ["90S9"],
        "gold_campaign_ids": ["90S9"],
    },
]

# Labels (keyed by question_id).
#   QA: human fail   QB: human fail   QC: human pass   QE: human pass
#   hw_90S9: human fail
LABELS: dict[str, dict] = {
    "QA": {"question_id": "QA", "label": "fail", "note": "wrong remedy described"},
    "QB": {"question_id": "QB", "label": "fail", "note": "missing gold"},
    "QC": {"question_id": "QC", "label": "pass", "note": ""},
    "QE": {"question_id": "QE", "label": "pass", "note": ""},
    "hw_90S9": {"question_id": "hw_90S9", "label": "fail", "note": "bad description"},
}

# Judge rows.
#   QA: judge pass   QB: judge fail   QD: judge fail   QE: judge pass
JUDGE_ROWS: list[dict] = [
    {
        "question_id": "QA",
        "judge_verdict": "pass",
        "judge_reason": "OK",
        "human_note": "",
    },
    {
        "question_id": "QB",
        "judge_verdict": "fail",
        "judge_reason": "Missing gold recall",
        "human_note": "",
    },
    {
        "question_id": "QD",
        "judge_verdict": "fail",
        "judge_reason": "Wrong ID cited",
        "human_note": "",
    },
    {
        "question_id": "QE",
        "judge_verdict": "pass",
        "judge_reason": "Correct",
        "human_note": "",
    },
]


def _build() -> list[dict]:
    """Helper: build failure rows with the module-level fixtures."""
    return build_failure_rows(ANSWERS, SCORED, LABELS, JUDGE_ROWS, GOLDEN_BY_ID)


# ═══════════════════════════════════════════════════════════════════
# 1. Three-way union
# ═══════════════════════════════════════════════════════════════════


class TestBuildFailureRows:
    def test_three_way_union(self):
        """Six failure rows present, QE (all-pass) absent."""
        rows = _build()
        ids = {r["question_id"] for r in rows}
        assert "QA" in ids  # human-only
        assert "QB" in ids  # code + human + judge
        assert "QC" in ids  # code-only (human passed)
        assert "QD" in ids  # code + judge (no human label)
        assert "gen/sub/Q1" in ids  # code-only (no human, no judge)
        assert "hw_90S9" in ids  # human-only (code pass, no judge)
        assert "QE" not in ids  # all three pass -> excluded

    def test_judge_only_failure(self):
        """A row that only the judge failed appears with judge-only signal."""
        scored = [
            {
                "question_id": "X",
                "template": "remedy",
                "source": "generated",
                "status": "answered",
                "invalid": False,
                "invalid_reason": "",
                "pass_rule": "cite_all_gold",
                "pass_rule_passed": True,
                "cites_gold": True,
                "cites_all_gold": True,
                "wrongly_declined": False,
                "cited_ids": ["C1"],
                "invalid_ids": [],
                "gold_campaign_ids": ["C1"],
            }
        ]
        labels = {"X": {"question_id": "X", "label": "pass", "note": ""}}
        judge = [
            {
                "question_id": "X",
                "judge_verdict": "fail",
                "judge_reason": "bad",
                "human_note": "",
            }
        ]
        golden = {
            "X": make_golden(
                "X", "generated", ["C1"], "cite_all_gold", template="remedy"
            )
        }
        answers = [
            make_answer_row("X", "generated", "answered", ["C1"], retrieved_ids=["C1"])
        ]

        rows = build_failure_rows(answers, scored, labels, judge, golden)
        assert len(rows) == 1
        assert rows[0]["failed_signals"] == ["judge"]

    def test_code_only_when_human_passed(self):
        """QC: code failed, human passed, no judge -> code-only."""
        rows = _build()
        qc = next(r for r in rows if r["question_id"] == "QC")
        assert qc["failed_signals"] == ["code"]

    def test_gold_in_retrieved_count(self):
        """QB: gold=[C2], retrieved=[C2, X2] -> gold_in_retrieved=1."""
        rows = _build()
        qb = next(r for r in rows if r["question_id"] == "QB")
        assert qb["gold_in_retrieved"] == 1

    def test_slash_in_question_id(self):
        """gen/sub/Q1 appears in failures."""
        rows = _build()
        slash = next(r for r in rows if r["question_id"] == "gen/sub/Q1")
        assert slash["failed_signals"] == ["code"]
        assert slash["template"] == "existence"

    def test_90s9_citation(self):
        """hw_90S9 has 90S9 in cited_ids and in invalid_ids."""
        rows = _build()
        s9 = next(r for r in rows if r["question_id"] == "hw_90S9")
        assert "90S9" in s9["cited_ids"]
        assert "90S9" in s9["invalid_ids"]
        assert "90S9" in s9["gold_campaign_ids"]
        assert s9["failed_signals"] == ["human"]

    def test_invalid_id_marked_in_worksheet(self):
        """An invalid_id absent from cited_ids appears marked in the worksheet."""
        golden = {"Z": make_golden("Z", "handwritten", ["26V365000"], "cite_any_gold")}
        answers = [
            make_answer_row(
                "Z",
                "handwritten",
                "answered",
                ["26V365000"],
                retrieved_ids=["26V365000"],
            )
        ]
        scored = [
            {
                "question_id": "Z",
                "template": None,
                "source": "handwritten",
                "status": "invalid",
                "invalid": True,
                "invalid_reason": "bad_citation",
                "pass_rule": "cite_any_gold",
                "pass_rule_passed": False,
                "cites_gold": True,
                "cites_all_gold": True,
                "wrongly_declined": False,
                "cited_ids": ["26V365000"],
                "invalid_ids": ["24V123000"],
                "gold_campaign_ids": ["26V365000"],
            }
        ]
        rows = build_failure_rows(answers, scored, {}, [], golden)
        assert len(rows) == 1
        assert rows[0]["invalid_ids"] == ["24V123000"]

        worksheet = format_worksheet(rows, "abc")
        assert "24V123000 (not retrieved)" in worksheet
        assert "26V365000" in worksheet

    def test_invalid_ids_in_jsonl(self):
        """The JSONL row keeps invalid_ids even when disjoint from cited_ids."""
        golden = {
            "Z": make_golden(
                "Z", "generated", ["G1"], "cite_all_gold", template="remedy"
            )
        }
        answers = [
            make_answer_row("Z", "generated", "answered", ["G1"], retrieved_ids=["G1"])
        ]
        scored = [
            {
                "question_id": "Z",
                "template": "remedy",
                "source": "generated",
                "status": "invalid",
                "invalid": True,
                "invalid_reason": "bad_citation",
                "pass_rule": "cite_all_gold",
                "pass_rule_passed": False,
                "cites_gold": True,
                "cites_all_gold": True,
                "wrongly_declined": False,
                "cited_ids": ["G1"],
                "invalid_ids": ["FAKE999"],
                "gold_campaign_ids": ["G1"],
            }
        ]
        rows = build_failure_rows(answers, scored, {}, [], golden)
        assert len(rows) == 1
        assert rows[0]["invalid_ids"] == ["FAKE999"]
        assert "FAKE999" not in rows[0]["cited_ids"]

    def test_code_checks_line_in_worksheet(self):
        """The Code checks bullet appears with the scored row's fields."""
        rows = _build()
        worksheet = format_worksheet(rows, "abc")
        # QB is invalid with bad_citation
        assert "status=invalid, invalid_reason=bad_citation" in worksheet
        # QC is valid (not invalid), so status=valid, invalid_reason=none
        assert "status=valid, invalid_reason=none" in worksheet

    def test_effective_template_from_source(self):
        """QD has template=None in scored, so effective template is 'handwritten'."""
        rows = _build()
        qd = next(r for r in rows if r["question_id"] == "QD")
        assert qd["template"] == "handwritten"

    def test_sorted_by_template_then_qid(self):
        """Rows are sorted by (template, question_id)."""
        rows = _build()
        keys = [(r["template"], r["question_id"]) for r in rows]
        assert keys == sorted(keys)

    def test_missing_golden_raises(self):
        """Scored qid not in golden_by_id -> ValueError."""
        scored = [
            {
                "question_id": "MISSING",
                "template": "remedy",
                "source": "generated",
                "status": "answered",
                "invalid": False,
                "invalid_reason": "",
                "pass_rule": "cite_all_gold",
                "pass_rule_passed": False,
                "cites_gold": False,
                "cites_all_gold": False,
                "wrongly_declined": False,
                "cited_ids": [],
                "invalid_ids": [],
                "gold_campaign_ids": ["C1"],
            }
        ]
        answers = [make_answer_row("MISSING", "generated", "answered", [])]
        with pytest.raises(ValueError, match="Missing from golden set"):
            build_failure_rows(answers, scored, {}, [], {"X": _GOLDEN_A})

    def test_missing_answers_raises(self):
        """Scored qid not in answers -> ValueError."""
        scored = [
            {
                "question_id": "QA",
                "template": "remedy",
                "source": "generated",
                "status": "answered",
                "invalid": False,
                "invalid_reason": "",
                "pass_rule": "cite_all_gold",
                "pass_rule_passed": False,
                "cites_gold": False,
                "cites_all_gold": False,
                "wrongly_declined": False,
                "cited_ids": [],
                "invalid_ids": [],
                "gold_campaign_ids": ["C1"],
            }
        ]
        with pytest.raises(ValueError, match="Missing from answers file"):
            build_failure_rows([], scored, {}, [], GOLDEN_BY_ID)


# ═══════════════════════════════════════════════════════════════════
# 2. Worksheet round-trip
# ═══════════════════════════════════════════════════════════════════


class TestWorksheetRoundTrip:
    def test_export_edit_tally(self):
        """Export, fill categories (one row with '5;6'), parse, tally."""
        rows = _build()
        worksheet = format_worksheet(rows, "abc1234")

        # Assign a category to each failure block.
        # Rows are sorted by (template, qid).  Expected order:
        #   consequence/QB, existence/gen/sub/Q1, handwritten/QD,
        #   handwritten/hw_90S9, remedy/QA, remedy/QC
        categories = ["2", "1b", "1", "5;6", "10", "5"]
        lines = worksheet.split("\n")
        edited: list[str] = []
        cat_iter = iter(categories)
        for line in lines:
            if line.startswith("**Category:**"):
                cat = next(cat_iter)
                edited.append(f"**Category:** {cat}")
            else:
                edited.append(line)
        edited_text = "\n".join(edited)

        parsed = parse_worksheet(edited_text)
        assert len(parsed) == len(rows)

        by_cat, by_tmpl = tally_categories(parsed)

        # "5;6" on hw_90S9 + "5" on QC -> cat 5 gets 2
        assert by_cat["5"] == 2
        assert by_cat["6"] == 1
        assert by_cat["2"] == 1
        assert by_cat["1b"] == 1
        assert by_cat["1"] == 1
        assert by_cat["10"] == 1

        # Template "handwritten" should have cats 1 and 5+6
        assert by_tmpl["handwritten"]["1"] == 1
        assert by_tmpl["handwritten"]["5"] == 1
        assert by_tmpl["handwritten"]["6"] == 1

    def test_multiline_answer_survives(self):
        """An answer with newlines parses correctly (category still found)."""
        golden = {
            "X": make_golden(
                "X", "generated", ["C1"], "cite_all_gold", template="remedy"
            )
        }
        answers = [
            make_answer_row(
                "X",
                "generated",
                "answered",
                ["C1"],
                answer_text="Line one.\nLine two.\nLine three.",
                retrieved_ids=["C1"],
            )
        ]
        scored = [
            {
                "question_id": "X",
                "template": "remedy",
                "source": "generated",
                "status": "answered",
                "invalid": False,
                "invalid_reason": "",
                "pass_rule": "cite_all_gold",
                "pass_rule_passed": False,
                "cites_gold": True,
                "cites_all_gold": True,
                "wrongly_declined": False,
                "cited_ids": ["C1"],
                "invalid_ids": [],
                "gold_campaign_ids": ["C1"],
            }
        ]
        rows = build_failure_rows(answers, scored, {}, [], golden)
        worksheet = format_worksheet(rows, "abc")

        # Fill category.
        worksheet = worksheet.replace("**Category:** ", "**Category:** 3", 1)

        parsed = parse_worksheet(worksheet)
        assert len(parsed) == 1
        assert parsed[0]["category_numbers"] == ["3"]


# ═══════════════════════════════════════════════════════════════════
# 3. Validation
# ═══════════════════════════════════════════════════════════════════


class TestValidation:
    def test_refuse_empty_category(self):
        """Unfilled Category lines are reported as empty_ids."""
        rows = _build()
        worksheet = format_worksheet(rows, "abc1234")

        # Fill only the first two, leave the rest empty.
        lines = worksheet.split("\n")
        edited: list[str] = []
        filled = 0
        for line in lines:
            if line.startswith("**Category:**"):
                if filled < 2:
                    edited.append("**Category:** 1")
                    filled += 1
                else:
                    edited.append(line)  # empty
            else:
                edited.append(line)

        parsed = parse_worksheet("\n".join(edited))
        empty_ids, unknown_ids = validate_categories(parsed)
        assert len(empty_ids) == len(rows) - 2
        assert len(unknown_ids) == 0

    def test_refuse_unknown_category(self):
        """A category number not in the legend is reported."""
        rows = _build()
        worksheet = format_worksheet(rows, "abc1234")

        lines = worksheet.split("\n")
        edited: list[str] = []
        first = True
        for line in lines:
            if line.startswith("**Category:**"):
                if first:
                    edited.append("**Category:** 99")
                    first = False
                else:
                    edited.append("**Category:** 1")
            else:
                edited.append(line)

        parsed = parse_worksheet("\n".join(edited))
        empty_ids, unknown_ids = validate_categories(parsed)
        assert len(unknown_ids) == 1
        assert len(empty_ids) == 0

    def test_all_legend_keys_valid(self):
        """Every key in VALID_CATEGORY_KEYS passes validation."""
        for key in VALID_CATEGORY_KEYS:
            row = {
                "question_id": "test",
                "template": "remedy",
                "category_raw": key,
                "category_numbers": [key],
            }
            empty, unknown = validate_categories([row])
            assert empty == []
            assert unknown == []

    def test_parenthetical_note_ignored(self):
        """Tokens inside '(...)' are not parsed as categories."""
        row = {
            "question_id": "test",
            "template": "remedy",
            "category_raw": (
                "2;1b;3;8 (19V860000 retrieved and skipped;"
                " 21V395000 not retrieved; 4 wrong-year extras; bare list)"
            ),
            "category_numbers": parse_worksheet(
                "### test\n"
                "- **Template:** remedy\n"
                "**Category:** 2;1b;3;8 (19V860000 retrieved and skipped;"
                " 21V395000 not retrieved; 4 wrong-year extras; bare list)\n"
            )[0]["category_numbers"],
        }
        assert row["category_numbers"] == ["2", "1b", "3", "8"]

    def test_bare_token_refused_as_unknown(self):
        """A semicolon-separated token that is not a legend key is refused."""
        parsed = parse_worksheet(
            "### test\n- **Template:** remedy\n**Category:** 5; bare list\n"
        )
        _empty_ids, unknown_ids = validate_categories(parsed)
        assert "test" in unknown_ids


# ═══════════════════════════════════════════════════════════════════
# 4. Subprocess tests
# ═══════════════════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════════════════
# 5. Encoding round-trip
# ═══════════════════════════════════════════════════════════════════


class TestEncodingRoundTrip:
    def test_curly_quote_survives_golden_load_and_worksheet_write(self, tmp_path):
        """A \u2019 in a golden JSONL question survives through to the worksheet file."""
        from recall_assistant.evals.retrieval_eval import _load_jsonl

        # Write a golden JSONL with a literal UTF-8 curly right single quote
        # (not a JSON \u escape — the real golden files have literal UTF-8).
        golden_path = tmp_path / "golden.jsonl"
        golden_path.write_bytes(
            '{"id":"Q_CURLY","question":"I\u2019ve heard about a recall",'
            '"source":"handwritten","template":null,"component_group":null,'
            '"gold_campaign_ids":["C1"],"reference_answer":"ref",'
            '"reference_facts":[],"make":"TEST","model":"CAR","year":2020,'
            '"pass_rule":"cite_any_gold","tags":[],"notes":null}\n'.encode("utf-8")
        )

        golden_rows = _load_jsonl(golden_path)
        golden_by_id = {g.id: g for g in golden_rows}

        answers = [
            make_answer_row(
                "Q_CURLY", "handwritten", "answered", ["C1"], retrieved_ids=["C1"]
            )
        ]
        scored = [
            {
                "question_id": "Q_CURLY",
                "template": None,
                "source": "handwritten",
                "status": "answered",
                "invalid": False,
                "invalid_reason": "",
                "pass_rule": "cite_any_gold",
                "pass_rule_passed": False,
                "cites_gold": True,
                "cites_all_gold": True,
                "wrongly_declined": False,
                "cited_ids": ["C1"],
                "invalid_ids": [],
                "gold_campaign_ids": ["C1"],
            }
        ]

        rows = build_failure_rows(answers, scored, {}, [], golden_by_id)
        worksheet_text = format_worksheet(rows, "abc")

        out_path = tmp_path / "error-analysis.md"
        out_path.write_text(worksheet_text, encoding="utf-8")

        read_back = out_path.read_text(encoding="utf-8")
        assert "\u2019" in read_back, (
            f"Expected curly quote U+2019 but got: "
            f"{[hex(ord(c)) for c in read_back if ord(c) > 127][:10]}"
        )
        assert "I\u2019ve" in read_back


# ═══════════════════════════════════════════════════════════════════
# 6. Subprocess tests
# ═══════════════════════════════════════════════════════════════════


class TestScriptSubprocess:
    def test_export_failures_help(self):
        result = subprocess.run(
            [sys.executable, "scripts/export_failures.py", "--help"],
            capture_output=True,
            text=True,
            check=False,
            cwd=str(PROJECT_ROOT),
        )
        assert result.returncode == 0
        assert "--answers" in result.stdout
        assert "--scored" in result.stdout
        assert "--labels" in result.stdout
        assert "--judge-rows" in result.stdout
        assert "ModuleNotFoundError" not in result.stderr
        assert "ImportError" not in result.stderr

    def test_tally_refuses_empty_category(self, tmp_path):
        """tally_failures.py exits 1 with the unfilled row id in stderr."""
        rows = _build()
        worksheet = format_worksheet(rows, "abc1234")

        # Fill all but one Category line.
        lines = worksheet.split("\n")
        edited: list[str] = []
        filled = 0
        unfilled_qid: str | None = None
        for line in lines:
            if line.startswith("**Category:**"):
                if filled < len(rows) - 1:
                    edited.append("**Category:** 1")
                    filled += 1
                else:
                    edited.append(line)  # leave empty
                    # Find the qid of this block by looking backwards for ###
                    for prev in reversed(edited[:-1]):
                        if prev.startswith("### "):
                            unfilled_qid = prev[4:]
                            break
            else:
                edited.append(line)

        worksheet_path = tmp_path / "error-analysis.md"
        worksheet_path.write_text("\n".join(edited), encoding="utf-8")

        result = subprocess.run(
            [
                sys.executable,
                "scripts/tally_failures.py",
                "--worksheet",
                str(worksheet_path),
            ],
            capture_output=True,
            text=True,
            check=False,
            cwd=str(PROJECT_ROOT),
        )
        assert result.returncode == 1
        assert "empty Category" in result.stderr or "empty" in result.stderr.lower()
        assert unfilled_qid is not None
        assert unfilled_qid in result.stderr
