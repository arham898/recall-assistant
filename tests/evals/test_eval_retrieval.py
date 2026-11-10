"""Tests for the retrieval evaluation logic.

Aggregation tests use hand-built GoldenRows and fake results — no DB.
Integration tests for hidden classification use a local fixture.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from recall_assistant.config import PROJECT_ROOT
from recall_assistant.evals.golden import GoldenRow
from recall_assistant.evals.retrieval_eval import (
    RowResult,
    aggregate_metrics,
    bucket_label,
    check_no_duplicate_campaigns,
    classify_gold_visibility,
    compute_found_rates,
    run_retrieval_loop,
    update_results_md,
)
from recall_assistant.retrieval import Hit, VehicleFilter

# ---------------------------------------------------------------------------
# Helpers: build a GoldenRow without needing every field
# ---------------------------------------------------------------------------


def _row(
    id: str,
    source: str,
    gold: list[str],
    make: str | None = "TEST",
    model: str | None = "CAR",
    year: int | None = 2020,
    template: str | None = None,
    component_group: str | None = None,
    tags: list[str] | None = None,
) -> GoldenRow:
    return GoldenRow(
        id=id,
        question=f"Question for {id}",
        source=source,
        template=template,
        component_group=component_group,
        gold_campaign_ids=gold,
        reference_answer="ref",
        reference_facts=[],
        make=make,
        model=model,
        year=year,
        pass_rule="cite_all_gold" if len(gold) > 1 else "cite_any_gold",
        tags=tags or [],
        notes=None,
    )


# ---------------------------------------------------------------------------
# Fixture: 5 test rows + fake results for one config
# ---------------------------------------------------------------------------

ROW_A = _row(
    "A",
    "generated",
    ["C1"],
    make="FORD",
    model="MUSTANG",
    year=2020,
    template="remedy",
    component_group="brakes",
)
ROW_B = _row(
    "B",
    "generated",
    ["C2", "C3"],
    make="TOYOTA",
    model="CAMRY",
    year=2021,
    template="consequence",
    component_group="engine",
)
ROW_C = _row(
    "C",
    "handwritten",
    ["C4"],
    make="HONDA",
    model="CIVIC",
    year=2022,
    tags=["handwritten", "vague"],
)
ROW_D = _row(
    "D",
    "generated",
    ["C1", "C5", "C6"],
    make="BMW",
    model="X5",
    year=2021,
    template="count",
    component_group="brakes",
)
ROW_E = _row(
    "E",
    "handwritten",
    ["C7"],
    make="NISSAN",
    model="ALTIMA",
    year=2020,
    tags=["handwritten", "specific", "vague"],
)

ALL_ROWS = [ROW_A, ROW_B, ROW_C, ROW_D, ROW_E]

# Pre-built results for config "hybrid_record_unfiltered"
CFG = "hybrid_record_unfiltered"

FAKE_RESULTS: dict[str, dict[str, RowResult]] = {
    # A: C1 at rank 1
    "A": {
        CFG: RowResult(
            "A",
            CFG,
            ["C1", "X1", "X2", "X3", "X4", "X5", "X6", "X7", "X8", "X9"],
            0.9,
            0.10,
        )
    },
    # B: C2 at rank 6, C3 at rank 9
    "B": {
        CFG: RowResult(
            "B",
            CFG,
            ["X1", "X2", "X3", "X4", "X5", "C2", "X6", "X7", "C3", "X8"],
            0.8,
            0.20,
        )
    },
    # C: C4 at rank 3
    "C": {
        CFG: RowResult(
            "C",
            CFG,
            ["X1", "X2", "C4", "X3", "X4", "X5", "X6", "X7", "X8", "X9"],
            0.7,
            0.30,
        )
    },
    # D: C1 at rank 1, C5 at rank 3 — C6 not in list
    "D": {
        CFG: RowResult(
            "D",
            CFG,
            ["C1", "X1", "C5", "X2", "X3", "X4", "X5b", "X6", "X7", "X8"],
            0.85,
            0.40,
        )
    },
    # E: no gold found
    "E": {
        CFG: RowResult(
            "E",
            CFG,
            ["X1", "X2", "X3", "X4", "X5", "X6", "X7", "X8", "X9", "X10"],
            0.3,
            0.50,
        )
    },
}

# Buckets (consistent with gold count and vehicle recall counts):
# A: FORD/MUSTANG/2020, bucket "2-3" (3 recalls)
# B: TOYOTA/CAMRY/2021, bucket "4-10" (5 recalls)
# C: HONDA/CIVIC/2022, bucket "4-10" (7 recalls)
# D: BMW/X5/2021, bucket "4-10" (6 recalls)
# E: NISSAN/ALTIMA/2020, bucket "2-3" (2 recalls)
FAKE_BUCKETS = {
    "A": "2-3",
    "B": "4-10",
    "C": "4-10",
    "D": "4-10",
    "E": "2-3",
}


# ═══════════════════════════════════════════════════════════════════
# 5a. Aggregation tests
# ═══════════════════════════════════════════════════════════════════


class TestAggregateMetrics:
    def test_overall(self):
        m = aggregate_metrics(ALL_ROWS, FAKE_RESULTS, CFG)

        assert m["n"] == 5

        # hit@5:
        # A: C1 in first 5 → True
        # B: C2 at rank 6, C3 at rank 9, neither in first 5 → False
        # C: C4 at rank 3 → True
        # D: C1 at rank 1 → True
        # E: no gold → False
        # (T+F+T+T+F) / 5 = 3/5 = 0.6
        assert m["hit@5"] == pytest.approx(0.6)

        # MRR:
        # A: RR = 1/1 = 1.0
        # B: first gold C2 at rank 6 → RR = 1/6
        # C: C4 at rank 3 → RR = 1/3
        # D: C1 at rank 1 → RR = 1.0
        # E: no gold → RR = 0.0
        # (1.0 + 1/6 + 1/3 + 1.0 + 0.0) / 5 = 2.5 / 5 = 0.5
        assert m["mrr"] == pytest.approx(0.5)

    def test_by_source_generated(self):
        subset = [ROW_A, ROW_B, ROW_D]
        m = aggregate_metrics(subset, FAKE_RESULTS, CFG)
        # hit@5: (T + F + T) / 3 = 2/3
        assert m["n"] == 3
        assert m["hit@5"] == pytest.approx(2 / 3)

    def test_by_source_handwritten(self):
        subset = [ROW_C, ROW_E]
        m = aggregate_metrics(subset, FAKE_RESULTS, CFG)
        # hit@5: (T + F) / 2 = 1/2
        assert m["n"] == 2
        assert m["hit@5"] == pytest.approx(0.5)

    def test_by_template_remedy(self):
        subset = [ROW_A]
        m = aggregate_metrics(subset, FAKE_RESULTS, CFG)
        assert m["n"] == 1
        assert m["hit@5"] == 1.0

    def test_by_template_consequence(self):
        subset = [ROW_B]
        m = aggregate_metrics(subset, FAKE_RESULTS, CFG)
        assert m["n"] == 1
        assert m["hit@5"] == 0.0

    def test_by_tag_vague(self):
        # Rows with "vague" tag: C and E
        subset = [ROW_C, ROW_E]
        m = aggregate_metrics(subset, FAKE_RESULTS, CFG)
        # hit@5: (T + F) / 2 = 0.5
        assert m["n"] == 2
        assert m["hit@5"] == pytest.approx(0.5)

    def test_by_bucket_2_3(self):
        # Bucket "2-3": rows A (bucket 2-3), E (bucket 2-3)
        subset = [r for r in ALL_ROWS if FAKE_BUCKETS[r.id] == "2-3"]
        assert len(subset) == 2
        m = aggregate_metrics(subset, FAKE_RESULTS, CFG)
        # hit@5: A=T, E=F → (T+F)/2 = 0.5
        assert m["hit@5"] == pytest.approx(0.5)

    def test_by_bucket_4_10(self):
        # Bucket "4-10": rows B, C, D
        subset = [r for r in ALL_ROWS if FAKE_BUCKETS[r.id] == "4-10"]
        assert len(subset) == 3
        m = aggregate_metrics(subset, FAKE_RESULTS, CFG)
        # hit@5: B=F, C=T, D=T → (F+T+T)/3 = 2/3
        assert m["hit@5"] == pytest.approx(2 / 3)


# ═══════════════════════════════════════════════════════════════════
# 5b. Found-rate (hidden/visible) aggregation test
# ═══════════════════════════════════════════════════════════════════


class TestFoundRates:
    def test_hidden_visible_found_rates(self):
        # Rows with component_group (excluding all_recalls): A, B, D
        component_rows = [ROW_A, ROW_B, ROW_D]

        # Visibility supplied by hand:
        # A (brakes, gold={"C1"}): C1 visible
        # B (engine, gold={"C2","C3"}): C2 visible, C3 hidden
        # D (brakes, gold={"C1","C5","C6"}): C1 visible, C5 visible, C6 hidden
        visibility = {
            "A": {"C1": True},
            "B": {"C2": True, "C3": False},
            "D": {"C1": True, "C5": True, "C6": False},
        }

        rates = compute_found_rates(component_rows, FAKE_RESULTS, CFG, visibility)

        # Visible gold recalls: (A,C1), (B,C2), (D,C1), (D,C5) = 4
        # All 4 appear in their row's top 10:
        #   C1 in row A at rank 1 ✓
        #   C2 in row B at rank 6 ✓
        #   C1 in row D at rank 1 ✓
        #   C5 in row D at rank 3 ✓
        # visible_found = 4, visible_total = 4 → rate = 1.0
        assert rates["visible_found"] == 4
        assert rates["visible_total"] == 4
        assert rates["visible_rate"] == pytest.approx(1.0)

        # Hidden gold recalls: (B,C3), (D,C6) = 2
        #   C3 in row B at rank 9 → in top 10 ✓ → found
        #   C6 not in row D's list → not found
        # hidden_found = 1, hidden_total = 2 → rate = 0.5
        assert rates["hidden_found"] == 1
        assert rates["hidden_total"] == 2
        assert rates["hidden_rate"] == pytest.approx(0.5)


# ═══════════════════════════════════════════════════════════════════
# 5c. Bucket boundaries
# ═══════════════════════════════════════════════════════════════════


class TestBucketLabel:
    def test_boundaries(self):
        assert bucket_label(1) == "1"
        assert bucket_label(2) == "2-3"
        assert bucket_label(3) == "2-3"
        assert bucket_label(4) == "4-10"
        assert bucket_label(10) == "4-10"
        assert bucket_label(11) == ">10"


# ═══════════════════════════════════════════════════════════════════
# 5d. Duplicate guard
# ═══════════════════════════════════════════════════════════════════


class TestDuplicateGuard:
    def test_clean_list_passes(self):
        hits = [Hit("A", 1, "t", 0.5, 1), Hit("B", 2, "t", 0.4, 2)]
        check_no_duplicate_campaigns(hits, "row1", "dense_record_unfiltered")

    def test_duplicate_stops(self):
        hits = [Hit("A", 1, "t", 0.5, 1), Hit("A", 2, "t", 0.4, 2)]
        with pytest.raises(SystemExit):
            check_no_duplicate_campaigns(hits, "row1", "dense_record_unfiltered")


# ═══════════════════════════════════════════════════════════════════
# 5e. Percentile test
# ═══════════════════════════════════════════════════════════════════


class TestPercentile:
    def test_nearest_rank_ceiling(self):
        # Build 5 rows each with a known latency.
        # Latencies (unsorted): 0.30, 0.10, 0.50, 0.20, 0.40
        # Sorted: [0.10, 0.20, 0.30, 0.40, 0.50]
        # p50: ceil(5 * 0.5) - 1 = ceil(2.5) - 1 = 3 - 1 = 2 → 0.30
        # p95: ceil(5 * 0.95) - 1 = ceil(4.75) - 1 = 5 - 1 = 4 → 0.50
        rows = [
            _row(f"P{i}", "generated", [f"G{i}"], template="remedy") for i in range(5)
        ]
        latencies = [0.30, 0.10, 0.50, 0.20, 0.40]
        pq: dict[str, dict[str, RowResult]] = {}
        for r, lat in zip(rows, latencies):
            pq[r.id] = {CFG: RowResult(r.id, CFG, [r.gold_campaign_ids[0]], 0.9, lat)}
        m = aggregate_metrics(rows, pq, CFG)
        assert m["latency_p50"] == pytest.approx(0.30)
        assert m["latency_p95"] == pytest.approx(0.50)


# ═══════════════════════════════════════════════════════════════════
# 5f. Retrieval loop test with recording fake
# ═══════════════════════════════════════════════════════════════════


class RecordingRetriever:
    """Duck-typed Retriever that records every call's kwargs."""

    def __init__(self, hits: list[Hit]) -> None:
        self._hits = hits
        self.calls: list[tuple[str, str, dict]] = []

    def _record(self, method: str, question: str, **kwargs: object) -> list[Hit]:
        self.calls.append((method, question, kwargs))
        return self._hits

    def dense(self, question: str, **kwargs: object) -> list[Hit]:
        return self._record("dense", question, **kwargs)

    def keyword(self, question: str, **kwargs: object) -> list[Hit]:
        return self._record("keyword", question, **kwargs)

    def hybrid(self, question: str, **kwargs: object) -> list[Hit]:
        return self._record("hybrid", question, **kwargs)

    def rerank(self, question: str, **kwargs: object) -> list[Hit]:
        return self._record("rerank", question, **kwargs)


class TestRetrievalLoop:
    def test_filter_kwargs(self):
        """Filtered configs pass the row's VehicleFilter; unfiltered pass None."""
        dummy_hits = [Hit("X", 1, "text", 0.5, 1)]
        rec_record = RecordingRetriever(dummy_hits)
        rec_field = RecordingRetriever(dummy_hits)
        retrievers = {"record": rec_record, "field": rec_field}

        test_row = _row(
            "T1",
            "generated",
            ["X"],
            make="FORD",
            model="RANGER",
            year=2021,
            template="remedy",
        )
        _results, n_unfiltered_only = run_retrieval_loop(retrievers, [test_row])
        assert n_unfiltered_only == 0

        # Each retriever should get 4 modes x 2 filters = 8 calls
        assert len(rec_record.calls) == 8
        assert len(rec_field.calls) == 8

        for ret in (rec_record, rec_field):
            for _method, _question, kwargs in ret.calls:
                vf = kwargs.get("vehicle_filter")
                if vf is not None:
                    assert isinstance(vf, VehicleFilter)
                    assert vf.make == "FORD"
                    assert vf.model == "RANGER"
                    assert vf.year == 2021

        # Half unfiltered, half filtered.
        for ret in (rec_record, rec_field):
            unfiltered = [c for c in ret.calls if c[2].get("vehicle_filter") is None]
            filtered = [c for c in ret.calls if c[2].get("vehicle_filter") is not None]
            assert len(unfiltered) == 4  # 4 modes, unfiltered
            assert len(filtered) == 4  # 4 modes, filtered

    def test_no_vehicle_row_gets_unfiltered_only(self):
        """A row with no vehicle runs unfiltered only — no filtered calls."""
        dummy_hits = [Hit("Y", 1, "text", 0.5, 1)]
        rec_record = RecordingRetriever(dummy_hits)
        rec_field = RecordingRetriever(dummy_hits)
        retrievers = {"record": rec_record, "field": rec_field}

        # make=None → incomplete vehicle
        no_vehicle_row = _row(
            "NV1",
            "generated",
            ["Y"],
            make=None,
            model=None,
            year=None,
            template="remedy",
        )
        _results, n_unfiltered_only = run_retrieval_loop(retrievers, [no_vehicle_row])
        assert n_unfiltered_only == 1

        # Each retriever: 4 modes x 1 (unfiltered only) = 4 calls
        assert len(rec_record.calls) == 4
        assert len(rec_field.calls) == 4

        # Every call must have vehicle_filter absent or None
        for ret in (rec_record, rec_field):
            for _method, _question, kwargs in ret.calls:
                assert kwargs.get("vehicle_filter") is None


# ═══════════════════════════════════════════════════════════════════
# 5g. Subprocess test
# ═══════════════════════════════════════════════════════════════════


class TestScriptImportable:
    def test_eval_retrieval_help(self):
        result = subprocess.run(
            [sys.executable, "scripts/eval_retrieval.py", "--help"],
            capture_output=True,
            text=True,
            check=False,
            cwd=str(PROJECT_ROOT),
        )
        assert result.returncode == 0
        assert "--label" in result.stdout
        assert "ModuleNotFoundError" not in result.stderr, result.stderr
        assert "ImportError" not in result.stderr, result.stderr


# ═══════════════════════════════════════════════════════════════════
# 5h. Two-label coexistence test
# ═══════════════════════════════════════════════════════════════════


class TestUpdateResultsMd:
    def test_two_labels_coexist(self, tmp_path: Path):
        md = tmp_path / "results.md"

        # Start with an unrelated section that must survive.
        md.write_text(
            "## Module 1.3\n\nExisting content that must survive.\n",
            encoding="utf-8",
        )

        # Write label alpha
        update_results_md("alpha", "Alpha content line 1.", path=md)
        text = md.read_text(encoding="utf-8")
        assert "## Module 1.3" in text
        assert "Existing content that must survive." in text
        assert "## Retrieval evaluation: alpha (Module 2.2)" in text
        assert "Alpha content line 1." in text

        # Write label beta — alpha and Module 1.3 must survive
        update_results_md("beta", "Beta content line 1.", path=md)
        text = md.read_text(encoding="utf-8")
        assert "## Module 1.3" in text
        assert "Existing content that must survive." in text
        assert "## Retrieval evaluation: alpha (Module 2.2)" in text
        assert "Alpha content line 1." in text
        assert "## Retrieval evaluation: beta (Module 2.2)" in text
        assert "Beta content line 1." in text

        # Re-write alpha — beta and Module 1.3 must survive unchanged
        update_results_md("alpha", "Alpha v2 content.", path=md)
        text = md.read_text(encoding="utf-8")
        assert "## Module 1.3" in text
        assert "Existing content that must survive." in text
        assert "## Retrieval evaluation: alpha (Module 2.2)" in text
        assert "Alpha v2 content." in text
        assert "Alpha content line 1." not in text  # replaced
        assert "## Retrieval evaluation: beta (Module 2.2)" in text
        assert "Beta content line 1." in text


# ═══════════════════════════════════════════════════════════════════
# 5i. Integration test — hidden classification with own fixture
# ═══════════════════════════════════════════════════════════════════


@pytest.fixture
def hidden_test_data(db_conn):
    """Insert 3 test recalls for hidden-classification testing.

    Uses TESTMAKE/HIDDEN/2021 — a vehicle no other test touches.
    Component strings use real-data spacing (spaces after colons)
    to verify TRIM works through the column argument.
    """
    cur = db_conn.cursor()

    # Recall 1: visible for brakes (card component in brakes categories)
    cur.execute(
        "INSERT INTO recalls (campaign_id, manufacturer, component, "
        "recall_type, defect_summary) VALUES "
        "(%s, %s, %s, %s, %s)",
        (
            "HIDDEN_VIS",
            "TESTMFR",
            "SERVICE BRAKES, HYDRAULIC: PAD",
            "V",
            "Visible brake test",
        ),
    )
    cur.execute(
        "INSERT INTO recall_vehicles (campaign_id, make, model, year) "
        "VALUES (%s, %s, %s, %s)",
        ("HIDDEN_VIS", "TESTMAKE", "HIDDEN", 2021),
    )
    cur.execute(
        "INSERT INTO recall_components (campaign_id, component) VALUES (%s, %s)",
        ("HIDDEN_VIS", "SERVICE BRAKES, HYDRAULIC: PAD"),
    )

    # Recall 2: hidden for brakes (card component is ELECTRICAL SYSTEM,
    # but has a recall_components row for brakes)
    cur.execute(
        "INSERT INTO recalls (campaign_id, manufacturer, component, "
        "recall_type, defect_summary) VALUES "
        "(%s, %s, %s, %s, %s)",
        (
            "HIDDEN_HID",
            "TESTMFR",
            "ELECTRICAL SYSTEM: WIRING",
            "V",
            "Hidden brake test",
        ),
    )
    cur.execute(
        "INSERT INTO recall_vehicles (campaign_id, make, model, year) "
        "VALUES (%s, %s, %s, %s)",
        ("HIDDEN_HID", "TESTMAKE", "HIDDEN", 2021),
    )
    cur.execute(
        "INSERT INTO recall_components (campaign_id, component) VALUES (%s, %s)",
        ("HIDDEN_HID", "SERVICE BRAKES, HYDRAULIC: ANTILOCK"),
    )
    cur.execute(
        "INSERT INTO recall_components (campaign_id, component) VALUES (%s, %s)",
        ("HIDDEN_HID", "ELECTRICAL SYSTEM: WIRING"),
    )

    # Recall 3: hidden for backup_camera (card matches the excluded
    # sub-component WARNINGS:EXTERNAL/PEDESTRIAN ALERT)
    cur.execute(
        "INSERT INTO recalls (campaign_id, manufacturer, component, "
        "recall_type, defect_summary) VALUES "
        "(%s, %s, %s, %s, %s)",
        (
            "HIDDEN_CAM",
            "TESTMFR",
            "BACK OVER PREVENTION: WARNINGS: EXTERNAL/PEDESTRIAN ALERT",
            "V",
            "Backup camera hidden test",
        ),
    )
    cur.execute(
        "INSERT INTO recall_vehicles (campaign_id, make, model, year) "
        "VALUES (%s, %s, %s, %s)",
        ("HIDDEN_CAM", "TESTMAKE", "HIDDEN", 2021),
    )
    cur.execute(
        "INSERT INTO recall_components (campaign_id, component) VALUES (%s, %s)",
        (
            "HIDDEN_CAM",
            "BACK OVER PREVENTION: SENSING SYSTEM: CAMERA",
        ),
    )
    cur.execute(
        "INSERT INTO recall_components (campaign_id, component) VALUES (%s, %s)",
        (
            "HIDDEN_CAM",
            "BACK OVER PREVENTION: WARNINGS: EXTERNAL/PEDESTRIAN ALERT",
        ),
    )

    db_conn.commit()


@pytest.mark.integration
class TestHiddenClassification:
    def test_visible_brake(self, db_conn, hidden_test_data):
        """Card 'SERVICE BRAKES, HYDRAULIC: PAD' is visible for brakes."""
        row = _row(
            "vis_test",
            "generated",
            ["HIDDEN_VIS"],
            make="TESTMAKE",
            model="HIDDEN",
            year=2021,
            template="remedy",
            component_group="brakes",
        )
        vis = classify_gold_visibility(db_conn, row)
        assert vis == {"HIDDEN_VIS": True}

    def test_hidden_brake(self, db_conn, hidden_test_data):
        """Card 'ELECTRICAL SYSTEM: WIRING' is hidden for brakes."""
        row = _row(
            "hid_test",
            "generated",
            ["HIDDEN_HID"],
            make="TESTMAKE",
            model="HIDDEN",
            year=2021,
            template="remedy",
            component_group="brakes",
        )
        vis = classify_gold_visibility(db_conn, row)
        assert vis == {"HIDDEN_HID": False}

    def test_backup_camera_exclusion(self, db_conn, hidden_test_data):
        """Card with excluded sub-component is hidden for backup_camera.

        'BACK OVER PREVENTION: WARNINGS: EXTERNAL/PEDESTRIAN ALERT'
        matches the top-level category but is excluded by
        ExcludedSub('WARNINGS', 'EXTERNAL/PEDESTRIAN ALERT').
        This proves TRIM works with real-data spacing (spaces after ':')
        through the column parameter.
        """
        row = _row(
            "cam_test",
            "generated",
            ["HIDDEN_CAM"],
            make="TESTMAKE",
            model="HIDDEN",
            year=2021,
            template="remedy",
            component_group="backup_camera",
        )
        vis = classify_gold_visibility(db_conn, row)
        assert vis == {"HIDDEN_CAM": False}
