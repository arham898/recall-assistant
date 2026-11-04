"""Tests for the CI eval gate module.

Unit tests (no DB) come first; integration tests are marked
``@pytest.mark.integration``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest

from recall_assistant.evals.ci_gate import (
    CI_SAMPLE_PATH,
    SMOKE_PATH,
    check_gate,
    export_campaigns_tsv,
    file_sha256,
    guard_scratch_database,
    select_smoke_questions,
)
from recall_assistant.ingest.parser import parse_recalls

# ===================================================================
# Unit tests (no database)
# ===================================================================


class TestCheckGate:
    """check_gate with the default threshold (0.05)."""

    @staticmethod
    def _metrics(hybrid_hit5: float, rerank_hit5: float) -> dict:
        return {
            "hybrid_record_unfiltered": {"hit@5": hybrid_hit5},
            "rerank_record_unfiltered": {"hit@5": rerank_hit5},
        }

    def test_4pt_drop_passes(self):
        baseline = self._metrics(0.90, 0.90)
        current = self._metrics(0.86, 0.86)
        passed, _msgs = check_gate(current, baseline)
        assert passed

    def test_6pt_drop_fails(self):
        baseline = self._metrics(0.90, 0.90)
        current = self._metrics(0.84, 0.84)
        passed, msgs = check_gate(current, baseline)
        assert not passed
        assert any("FAIL" in m for m in msgs)

    def test_exact_5pt_passes(self):
        """drop == threshold (0.05) does NOT fail (only > does)."""
        baseline = self._metrics(0.90, 0.90)
        current = self._metrics(0.85, 0.85)
        passed, _msgs = check_gate(current, baseline)
        assert passed

    def test_rerank_7pt_drop_fails_while_hybrid_passes(self):
        baseline = self._metrics(0.90, 0.90)
        current = self._metrics(0.86, 0.83)  # hybrid -0.04 ok, rerank -0.07 fail
        passed, msgs = check_gate(current, baseline)
        assert not passed
        fail_msgs = [m for m in msgs if "FAIL" in m]
        ok_msgs = [m for m in msgs if m.startswith("OK")]
        assert len(fail_msgs) == 1
        assert "rerank" in fail_msgs[0]
        assert len(ok_msgs) == 1
        assert "hybrid" in ok_msgs[0]


class TestFileSha256:
    """CRLF normalisation and basic correctness."""

    def test_crlf_and_lf_same_digest(self, tmp_path: Path):
        content = b"line one\nline two\n"
        lf_file = tmp_path / "lf.txt"
        crlf_file = tmp_path / "crlf.txt"
        lf_file.write_bytes(content)
        crlf_file.write_bytes(content.replace(b"\n", b"\r\n"))
        assert file_sha256(lf_file) == file_sha256(crlf_file)

    def test_different_content_different_digest(self, tmp_path: Path):
        a = tmp_path / "a.txt"
        b = tmp_path / "b.txt"
        a.write_bytes(b"hello")
        b.write_bytes(b"world")
        assert file_sha256(a) != file_sha256(b)


class TestGuardScratchDatabase:
    def test_5000_vs_300_raises(self):
        with pytest.raises(SystemExit):
            guard_scratch_database(5000, 300)

    def test_0_vs_300_passes(self):
        guard_scratch_database(0, 300)

    def test_300_vs_300_passes(self):
        guard_scratch_database(300, 300)


class TestIsWorktreeDirty:
    """Test against a real git repo in tmp_path."""

    def test_clean_repo_returns_false(self, tmp_path: Path, monkeypatch):
        # Create a minimal git repo.
        subprocess.run(
            ["git", "init"],
            cwd=str(tmp_path),
            check=True,
            capture_output=True,
        )
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=test",
                "-c",
                "user.email=test@example.com",
                "commit",
                "--allow-empty",
                "-m",
                "init",
            ],
            cwd=str(tmp_path),
            check=True,
            capture_output=True,
        )
        import recall_assistant.evals.run_metadata as rm

        monkeypatch.setattr(rm, "PROJECT_ROOT", tmp_path)
        assert rm.is_worktree_dirty() is False

    def test_untracked_file_returns_true(self, tmp_path: Path, monkeypatch):
        subprocess.run(
            ["git", "init"],
            cwd=str(tmp_path),
            check=True,
            capture_output=True,
        )
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=test",
                "-c",
                "user.email=test@example.com",
                "commit",
                "--allow-empty",
                "-m",
                "init",
            ],
            cwd=str(tmp_path),
            check=True,
            capture_output=True,
        )
        (tmp_path / "untracked.txt").write_text("noise", encoding="utf-8")

        import recall_assistant.evals.run_metadata as rm

        monkeypatch.setattr(rm, "PROJECT_ROOT", tmp_path)
        assert rm.is_worktree_dirty() is True


class TestSelectSmokeQuestions:
    """Run against the real golden files with seed 42."""

    def test_counts_and_templates(self):
        rows = select_smoke_questions(seed=42)
        assert len(rows) == 30

        sources = Counter(r.source for r in rows)
        assert sources["generated"] == 15
        assert sources["handwritten"] == 10
        assert sources["unanswerable"] == 5

        # 3 per template, 5 templates.
        templates = Counter(r.template for r in rows if r.source == "generated")
        assert len(templates) == 5
        assert all(count == 3 for count in templates.values())

        # Answerable handwritten have gold IDs.
        for r in rows:
            if r.source == "handwritten":
                assert r.gold_campaign_ids, f"{r.id} should be answerable"

        # Unanswerable have empty gold.
        for r in rows:
            if r.source == "unanswerable":
                assert not r.gold_campaign_ids, f"{r.id} should be unanswerable"


class TestUpdateBaselineRefusesDirty:
    """update_baseline.main() with is_worktree_dirty patched True."""

    def test_exits_dirty(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr("scripts.update_baseline.is_worktree_dirty", lambda: True)
        # Patch ConnectionPool so any DB access would blow up.
        monkeypatch.setattr(
            "psycopg_pool.ConnectionPool",
            lambda *a, **kw: (_ for _ in ()).throw(
                AssertionError("should not connect")
            ),
        )
        baseline_path = tmp_path / "baseline.json"
        monkeypatch.setattr("scripts.update_baseline.BASELINE_PATH", baseline_path)

        from scripts.update_baseline import main

        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 1
        assert not baseline_path.exists()


class TestUpdateBaselineGuardBlocks:
    """Guard triggers before upsert when DB has too many recalls."""

    def test_guard_blocks_upsert(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr("scripts.update_baseline.is_worktree_dirty", lambda: False)
        baseline_path = tmp_path / "baseline.json"
        monkeypatch.setattr("scripts.update_baseline.BASELINE_PATH", baseline_path)

        # Fake DB connection that reports 5000 existing recalls.
        class FakeCursor:
            def execute(self, *a, **kw):
                pass

            def fetchone(self):
                return (5000,)

            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

        class FakeConn:
            def cursor(self):
                return FakeCursor()

            def close(self):
                pass

        # psycopg is imported locally inside main(), so patch on the
        # psycopg module itself.
        monkeypatch.setattr("psycopg.connect", lambda *a, **kw: FakeConn())
        monkeypatch.setattr("scripts.update_baseline.run_migrations", lambda conn: [])

        # Fake parse_recalls to return 300 campaigns (below the guard's
        # 5000 existing).
        from recall_assistant.ingest.models import Recall

        fake_recalls = [
            Recall(
                campaign_id=f"FAKE_{i:04d}",
                manufacturer="FAKE",
                manufacturer_campaign_number="",
                component="FAKE",
                recall_type="V",
                potential_units_affected=None,
                defect_summary="",
                consequence_summary="",
                corrective_action="",
                notes="",
                report_received_date=None,
                record_creation_date=None,
                manufacturing_begin_date=None,
                manufacturing_end_date=None,
                owner_notification_date=None,
                influenced_by="",
                do_not_drive=False,
                park_outside=False,
            )
            for i in range(300)
        ]
        monkeypatch.setattr(
            "scripts.update_baseline.parse_recalls",
            lambda path: (fake_recalls, 0),
        )

        from scripts.update_baseline import main

        with pytest.raises(SystemExit):
            main()
        assert not baseline_path.exists()


# ===================================================================
# Integration tests (need Postgres)
# ===================================================================


# Seed data: 5 campaigns with record/field chunks including tricky
# characters (double quote, latin-1 é, and a value starting with a quote).
EXPORT_SEED = [
    {
        "campaign_id": "TSV_001",
        "manufacturer": "TOYOTA MOTOR",
        "component": "FUEL PUMP",
        "vehicles": [("TOYOTA", "CAMRY", 2020), ("TOYOTA", "COROLLA", 2021)],
        "defect": 'fuel pump "failure" causing engine stall',
        "consequence": "engine stall increases crash risk",
        "remedy": "dealer will replace fuel pump",
        "components": ["FUEL PUMP"],
    },
    {
        "campaign_id": "TSV_002",
        "manufacturer": "FORD MOTOR",
        "component": "BRAKES",
        "vehicles": [("FORD", "F-150", 2019)],
        "defect": '"Quoted start" brake line corrosion',
        "consequence": "loss of braking increases stopping distance",
        "remedy": "dealer will replace brake lines",
        "components": ["BRAKES"],
    },
    {
        "campaign_id": "TSV_003",
        "manufacturer": "HONDA MOTOR",
        "component": "AIRBAGS",
        "vehicles": [("HONDA", "CIVIC", 2022)],
        "defect": "airbag may not deploy \u00e9l\u00e9ment",
        "consequence": "increased injury risk in a crash",
        "remedy": "dealer will update airbag software",
        "components": ["AIRBAGS"],
    },
    {
        "campaign_id": "TSV_004",
        "manufacturer": "TOYOTA MOTOR",
        "component": "SERVICE BRAKES, HYDRAULIC: pad",
        "vehicles": [("TOYOTA", "RAV4", 2021)],
        "defect": "brake pad wear sensor malfunction",
        "consequence": "driver may not know brakes need replacement",
        "remedy": "dealer will replace brake sensor",
        "components": [
            "SERVICE BRAKES, HYDRAULIC: pad",
            "SERVICE BRAKES, HYDRAULIC: hose",
        ],
    },
    {
        "campaign_id": "TSV_005",
        "manufacturer": "FORD MOTOR",
        "component": "ENGINE",
        "vehicles": [("FORD", "MUSTANG", 2023)],
        "defect": "engine coolant leak from radiator hose",
        "consequence": "overheating may cause engine damage",
        "remedy": "dealer will replace radiator hose",
        "components": ["ENGINE"],
    },
]


def _insert_export_seed(conn):
    """Insert EXPORT_SEED into the test database."""
    with conn.cursor() as cur:
        for r in EXPORT_SEED:
            cur.execute(
                "INSERT INTO recalls "
                "(campaign_id, manufacturer, component, recall_type, "
                "defect_summary, consequence_summary, corrective_action) "
                "VALUES (%(cid)s, %(mfg)s, %(comp)s, 'V', "
                "%(defect)s, %(consequence)s, %(remedy)s)",
                {
                    "cid": r["campaign_id"],
                    "mfg": r["manufacturer"],
                    "comp": r["component"],
                    "defect": r["defect"],
                    "consequence": r["consequence"],
                    "remedy": r["remedy"],
                },
            )
            for make, model, year in r["vehicles"]:
                cur.execute(
                    "INSERT INTO recall_vehicles "
                    "(campaign_id, make, model, year) "
                    "VALUES (%(cid)s, %(make)s, %(model)s, %(year)s)",
                    {
                        "cid": r["campaign_id"],
                        "make": make,
                        "model": model,
                        "year": year,
                    },
                )
            for comp in r["components"]:
                cur.execute(
                    "INSERT INTO recall_components "
                    "(campaign_id, component) VALUES (%s, %s)",
                    (r["campaign_id"], comp),
                )
    conn.commit()


@pytest.mark.integration
class TestExportRoundTrip:
    """export_campaigns_tsv round-trips through parse_recalls."""

    def test_fields_vehicles_components(self, db_conn, tmp_path: Path):
        _insert_export_seed(db_conn)

        cids = {r["campaign_id"] for r in EXPORT_SEED}
        tsv_path = tmp_path / "roundtrip.tsv"
        n = export_campaigns_tsv(db_conn, cids, tsv_path)
        assert n == 5

        recalls, skipped = parse_recalls(tsv_path)
        assert skipped == 0
        assert len(recalls) == 5

        parsed_by_cid = {r.campaign_id: r for r in recalls}
        for seed in EXPORT_SEED:
            cid = seed["campaign_id"]
            parsed = parsed_by_cid[cid]

            # Text fields.
            assert parsed.defect_summary == seed["defect"], (
                f"{cid} defect mismatch: {parsed.defect_summary!r} "
                f"!= {seed['defect']!r}"
            )
            assert parsed.consequence_summary == seed["consequence"]
            assert parsed.corrective_action == seed["remedy"]
            assert parsed.manufacturer == seed["manufacturer"]
            assert parsed.component == seed["component"]

            # Vehicles.
            p_vehs = sorted((v.make, v.model, v.year) for v in parsed.vehicles)
            d_vehs = sorted(seed["vehicles"])
            assert p_vehs == d_vehs, f"{cid} vehicles: {p_vehs} != {d_vehs}"

            # Components.
            assert sorted(parsed.components) == sorted(seed["components"]), (
                f"{cid} components: {sorted(parsed.components)} "
                f"!= {sorted(seed['components'])}"
            )


class TestSampleIntegrity:
    """Every answerable smoke gold ID must be in the committed TSV."""

    def test_gold_ids_in_tsv(self):
        if not SMOKE_PATH.exists() or not CI_SAMPLE_PATH.exists():
            pytest.skip("Sample files not yet generated")

        from recall_assistant.evals.ci_gate import _load_smoke_rows

        smoke = _load_smoke_rows()
        recalls, _ = parse_recalls(CI_SAMPLE_PATH)
        tsv_cids = {r.campaign_id for r in recalls}

        for row in smoke:
            if row.gold_campaign_ids:
                missing = set(row.gold_campaign_ids) - tsv_cids
                assert not missing, (
                    f"smoke row {row.id} gold IDs missing from TSV: {missing}"
                )


@pytest.mark.integration
class TestCheckCLI:
    """Subprocess tests of --check using the five-campaign seed data.

    Uses seed_retrieval_data (already embedded with FakeEmbedder) and
    writes a two-question smoke JSONL + matching TSV in tmp_path,
    passing --sample and --smoke to the CLI.
    """

    @pytest.fixture
    def _env(self, _test_db: str):
        """Env dict targeting the test database; subprocess never reads .env."""
        from urllib.parse import urlparse

        parsed = urlparse(_test_db)
        env = {
            **os.environ,
            "POSTGRES_USER": parsed.username or "recall",
            "POSTGRES_PASSWORD": parsed.password or "recall",
            "POSTGRES_HOST": parsed.hostname or "localhost",
            "POSTGRES_PORT": str(parsed.port or 5432),
            "POSTGRES_DB": parsed.path.lstrip("/"),
        }
        env.pop("OPENROUTER_API_KEY", None)
        return env

    @pytest.fixture
    def _seed_files(self, db_conn, seed_retrieval_data, tmp_path: Path):
        """Write a tiny smoke JSONL and matching TSV from the seed data.

        Two answerable questions whose gold IDs are seed campaigns.
        The TSV is exported from the DB via export_campaigns_tsv.
        """
        from recall_assistant.evals.ci_gate import export_campaigns_tsv
        from recall_assistant.evals.golden import GoldenRow

        # Two smoke questions targeting seed campaigns.
        rows = [
            GoldenRow(
                id="smoke_001",
                question="What is the fuel pump recall for the 2020 Toyota Camry?",
                source="handwritten",
                gold_campaign_ids=["RET_001"],
                reference_answer="ref",
                make="TOYOTA",
                model="CAMRY",
                year=2020,
                pass_rule="cite_any_gold",
            ),
            GoldenRow(
                id="smoke_002",
                question="What is the brake recall for the 2019 Ford F-150?",
                source="handwritten",
                gold_campaign_ids=["RET_002"],
                reference_answer="ref",
                make="FORD",
                model="F-150",
                year=2019,
                pass_rule="cite_any_gold",
            ),
        ]

        smoke_path = tmp_path / "smoke.jsonl"
        with open(smoke_path, "w", encoding="utf-8") as f:
            f.writelines(row.model_dump_json() + "\n" for row in rows)

        cids = {"RET_001", "RET_002", "RET_003", "RET_004", "RET_005"}
        tsv_path = tmp_path / "ci_sample.tsv"
        export_campaigns_tsv(db_conn, cids, tsv_path)

        return smoke_path, tsv_path

    @pytest.fixture
    def _loaded(self, _env, _seed_files):
        """Load the tiny sample into the test DB via subprocess."""
        smoke_path, tsv_path = _seed_files
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "recall_assistant.evals.ci_gate",
                "--load-sample",
                "--sample",
                str(tsv_path),
            ],
            env=_env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, f"load-sample failed:\n{result.stderr}"
        return smoke_path, tsv_path

    @pytest.fixture
    def _baseline_json(self, _loaded, _env, tmp_path: Path):
        """Run smoke eval in-process and write a matching baseline JSON."""
        from recall_assistant.evals.ci_gate import file_sha256, run_smoke_eval

        smoke_path, tsv_path = _loaded
        from recall_assistant.evals.ci_gate import _load_smoke_rows

        smoke_rows = _load_smoke_rows(smoke_path)
        env = _env
        db_url = (
            f"postgresql://{env['POSTGRES_USER']}:{env['POSTGRES_PASSWORD']}"
            f"@{env['POSTGRES_HOST']}:{env['POSTGRES_PORT']}/{env['POSTGRES_DB']}"
        )
        metrics = run_smoke_eval(db_url, smoke_rows)

        baseline = {
            "n_questions": len([r for r in smoke_rows if r.gold_campaign_ids]),
            "sha256": {
                "ci_sample_tsv": file_sha256(tsv_path),
                "smoke_jsonl": file_sha256(smoke_path),
            },
            "metrics": metrics,
        }
        baseline_path = tmp_path / "baseline.json"
        baseline_path.write_text(json.dumps(baseline, indent=2), encoding="utf-8")
        return baseline_path, baseline, smoke_path, tsv_path

    def test_matching_baseline_exits_0(self, _baseline_json, _env):
        baseline_path, _, smoke_path, tsv_path = _baseline_json
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "recall_assistant.evals.ci_gate",
                "--check",
                "--baseline",
                str(baseline_path),
                "--sample",
                str(tsv_path),
                "--smoke",
                str(smoke_path),
            ],
            env=_env,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        assert result.returncode == 0, (
            f"Expected exit 0, got {result.returncode}\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )

    def test_inflated_hybrid_exits_1(self, _baseline_json, _env, tmp_path: Path):
        _, baseline, smoke_path, tsv_path = _baseline_json
        inflated = json.loads(json.dumps(baseline))
        inflated["metrics"]["hybrid_record_unfiltered"]["hit@5"] += 0.08
        inflated_path = tmp_path / "inflated.json"
        inflated_path.write_text(json.dumps(inflated, indent=2), encoding="utf-8")

        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "recall_assistant.evals.ci_gate",
                "--check",
                "--baseline",
                str(inflated_path),
                "--sample",
                str(tsv_path),
                "--smoke",
                str(smoke_path),
            ],
            env=_env,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        assert result.returncode == 1

    def test_wrong_hash_baseline_stale(self, _seed_files, tmp_path: Path):
        """Baseline stale exits before any DB connection.

        Uses POSTGRES_PORT=1 (invalid) to prove no connection is attempted.
        """
        smoke_path, tsv_path = _seed_files
        baseline = {
            "sha256": {
                "ci_sample_tsv": "0" * 64,
                "smoke_jsonl": "0" * 64,
            },
            "metrics": {},
        }
        baseline_path = tmp_path / "stale.json"
        baseline_path.write_text(json.dumps(baseline, indent=2), encoding="utf-8")

        env = {
            **os.environ,
            "POSTGRES_PORT": "1",
            "POSTGRES_DB": "recall_ci_should_not_connect",
        }
        env.pop("OPENROUTER_API_KEY", None)

        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "recall_assistant.evals.ci_gate",
                "--check",
                "--baseline",
                str(baseline_path),
                "--sample",
                str(tsv_path),
                "--smoke",
                str(smoke_path),
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert result.returncode == 1
        assert "baseline stale" in result.stderr
