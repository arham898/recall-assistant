"""CI eval gate: smoke-sample loading, retrieval regression check, advisory
answer check.

Library functions are importable for testing.  CLI entry point via
``python -m recall_assistant.evals.ci_gate``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any

import psycopg

from recall_assistant.config import PROJECT_ROOT, Settings
from recall_assistant.evals.golden import GoldenRow
from recall_assistant.evals.retrieval_eval import (
    MODES,
    RowResult,
    _load_jsonl,
    aggregate_metrics,
    check_no_duplicate_campaigns,
    has_complete_vehicle,
    load_golden_rows,
)
from recall_assistant.ingest.loader import load_recall_rows_by_ids, upsert_recalls
from recall_assistant.ingest.migrator import run_migrations
from recall_assistant.ingest.parser import COL, EXPECTED_COLUMNS, parse_recalls
from recall_assistant.retrieval import Hit, VehicleFilter

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

GATE_THRESHOLD = 0.05
GATE_CONFIGS = ("hybrid_record_unfiltered", "rerank_record_unfiltered")
SMOKE_PATH = PROJECT_ROOT / "evals" / "smoke.jsonl"
CI_SAMPLE_PATH = PROJECT_ROOT / "tests" / "data" / "ci_sample.tsv"
DEFAULT_BASELINE_PATH = PROJECT_ROOT / "results" / "baseline.json"

# ---------------------------------------------------------------------------
# Smoke-question selection
# ---------------------------------------------------------------------------


def select_smoke_questions(seed: int = 42) -> list[GoldenRow]:
    """Select 30 golden rows for the CI smoke set (deterministic).

    Returns 15 generated (3 per template, alphabetical), 10 handwritten
    answerable, and 5 unanswerable.
    """
    rows = load_golden_rows()

    generated_by_template: dict[str, list[GoldenRow]] = {}
    handwritten_answerable: list[GoldenRow] = []
    unanswerable: list[GoldenRow] = []

    for row in rows:
        if row.source == "generated":
            generated_by_template.setdefault(row.template, []).append(row)
        elif row.source == "handwritten" and row.gold_campaign_ids:
            handwritten_answerable.append(row)
        elif not row.gold_campaign_ids:
            unanswerable.append(row)

    rng = random.Random(seed)

    selected: list[GoldenRow] = []
    for template in sorted(generated_by_template):
        pool = generated_by_template[template]
        selected.extend(rng.sample(pool, 3))

    selected.extend(rng.sample(handwritten_answerable, 10))
    selected.extend(rng.sample(unanswerable, 5))

    return selected


# ---------------------------------------------------------------------------
# File hashing (CRLF-normalised)
# ---------------------------------------------------------------------------


def file_sha256(path: Path) -> str:
    """SHA-256 hex digest of *path* with CRLF normalised to LF."""
    data = path.read_bytes().replace(b"\r\n", b"\n")
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# Gate check
# ---------------------------------------------------------------------------


def check_gate(
    current: dict[str, dict[str, Any]],
    baseline_metrics: dict[str, dict[str, Any]],
    threshold: float = GATE_THRESHOLD,
) -> tuple[bool, list[str]]:
    """Compare hit@5 for gate configs.  Return (passed, messages)."""
    passed = True
    messages: list[str] = []
    for cfg in GATE_CONFIGS:
        b_val = baseline_metrics.get(cfg, {}).get("hit@5")
        c_val = current.get(cfg, {}).get("hit@5")
        if b_val is None or c_val is None:
            messages.append(f"{cfg}: missing hit@5 (baseline={b_val}, current={c_val})")
            passed = False
            continue
        drop = round(b_val - c_val, 6)
        if drop > threshold:
            messages.append(
                f"FAIL {cfg}: hit@5 dropped {drop:.6f} "
                f"(baseline={b_val:.4f} -> current={c_val:.4f}, "
                f"threshold={threshold})"
            )
            passed = False
        else:
            messages.append(
                f"OK   {cfg}: hit@5 drop={drop:.6f} "
                f"(baseline={b_val:.4f} -> current={c_val:.4f})"
            )
    return passed, messages


# ---------------------------------------------------------------------------
# Database guards
# ---------------------------------------------------------------------------


def guard_scratch_database(existing_count: int, sample_count: int) -> None:
    """Refuse to run if the database looks like the full index."""
    if existing_count > sample_count:
        print(
            f"ERROR: database has {existing_count} recalls but sample has "
            f"only {sample_count}.  Are you pointing at the full index?  "
            f"Use POSTGRES_DB=recall_ci.",
            file=sys.stderr,
        )
        sys.exit(1)


# ---------------------------------------------------------------------------
# Sample loading
# ---------------------------------------------------------------------------


def load_ci_sample(conn: psycopg.Connection, path: Path) -> int:
    """Run migrations, upsert the CI sample TSV, and verify counts."""
    run_migrations(conn)
    recalls, _skipped = parse_recalls(path)
    upsert_recalls(conn, recalls, allow_mass_delete=True)

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM recalls")
        db_count = cur.fetchone()[0]

    expected = len({r.campaign_id for r in recalls})
    if db_count != expected:
        print(
            f"ERROR: database has {db_count} campaigns but TSV has {expected}",
            file=sys.stderr,
        )
        sys.exit(1)
    return db_count


# ---------------------------------------------------------------------------
# TSV export
# ---------------------------------------------------------------------------


def _format_date(d: date | None) -> str:
    return d.strftime("%Y%m%d") if d else ""


def _format_bool(b: bool) -> str:
    return "Yes" if b else "No"


def _format_int(n: int | None) -> str:
    return str(n) if n is not None else ""


def _assert_safe_field(value: str, campaign_id: str, col_name: str) -> None:
    """Reject fields that would corrupt the tab-delimited format."""
    for ch, label in [("\t", "tab"), ("\n", "newline"), ("\r", "carriage return")]:
        if ch in value:
            raise ValueError(
                f"Field {col_name} of campaign {campaign_id} contains a "
                f"{label} character — cannot write to TSV"
            )


def export_campaigns_tsv(
    conn: psycopg.Connection,
    campaign_ids: set[str],
    path: Path,
) -> int:
    """Write a 29-column NHTSA-format TSV for the given campaigns.

    Produces one row per (campaign, vehicle, component) triple so that
    ``parse_recalls`` round-trips vehicles *and* components.

    Returns the number of distinct campaigns written.
    """
    if not campaign_ids:
        return 0

    id_list = sorted(campaign_ids)
    recall_rows, vehicles_by_cid, components_by_cid = load_recall_rows_by_ids(
        conn, campaign_ids
    )

    # Build rows: one per (campaign, vehicle, component).
    # If a campaign has no vehicles, skip (shouldn't happen in practice).
    # If a campaign has no components in recall_components, use the
    # campaign-level component field for each row.
    tsv_rows: list[list[str]] = []
    record_id = 0

    for cid in id_list:
        data = recall_rows.get(cid)
        if data is None:
            continue

        vehs = vehicles_by_cid.get(cid, [])
        comps = components_by_cid.get(cid, [])
        if not comps:
            comps = [data["component"]]

        # Sort components so the campaign-level component comes first.
        # parse_recalls takes component from the first row for each
        # campaign (_row_to_recall_stub), so the first row's COMPNAME
        # must match data["component"] for a clean round-trip.
        primary = data["component"]
        comps = sorted(comps, key=lambda c: (c != primary, c))

        for make, model, year in vehs:
            for comp in comps:
                record_id += 1
                row = [""] * EXPECTED_COLUMNS
                row[COL["RECORD_ID"]] = str(record_id)
                row[COL["CAMPNO"]] = cid
                row[COL["MAKETXT"]] = make
                row[COL["MODELTXT"]] = model
                row[COL["YEARTXT"]] = str(year) if year is not None else "9999"
                row[COL["MFGCAMPNO"]] = data["manufacturer_campaign_number"]
                row[COL["COMPNAME"]] = comp
                row[COL["MFGNAME"]] = data["manufacturer"]
                row[COL["BGMAN"]] = _format_date(data["manufacturing_begin_date"])
                row[COL["ENDMAN"]] = _format_date(data["manufacturing_end_date"])
                row[COL["RCLTYPECD"]] = data["recall_type"]
                row[COL["POTAFF"]] = _format_int(data["potential_units_affected"])
                row[COL["ODATE"]] = _format_date(data["owner_notification_date"])
                row[COL["INFLUENCED_BY"]] = data["influenced_by"]
                row[COL["MFGTXT"]] = data["manufacturer"]
                row[COL["RCDATE"]] = _format_date(data["report_received_date"])
                row[COL["DATEA"]] = _format_date(data["record_creation_date"])
                row[COL["RPNO"]] = ""
                row[COL["FMVSS"]] = ""
                row[COL["DESC_DEFECT"]] = data["defect_summary"]
                row[COL["CONEQUENCE_DEFECT"]] = data["consequence_summary"]
                row[COL["CORRECTIVE_ACTION"]] = data["corrective_action"]
                row[COL["NOTES"]] = data["notes"]
                row[COL["RCL_CMPT_ID"]] = ""
                row[COL["MFR_COMP_NAME"]] = ""
                row[COL["MFR_COMP_DESC"]] = ""
                row[COL["MFR_COMP_PTNO"]] = ""
                row[COL["DO_NOT_DRIVE"]] = _format_bool(data["do_not_drive"])
                row[COL["PARK_OUTSIDE"]] = _format_bool(data["park_outside"])

                for idx, val in enumerate(row):
                    col_name = next((k for k, v in COL.items() if v == idx), str(idx))
                    _assert_safe_field(val, cid, col_name)

                tsv_rows.append(row)

    # Sort by (campaign_id, make, model, year).
    tsv_rows.sort(
        key=lambda r: (
            r[COL["CAMPNO"]],
            r[COL["MAKETXT"]],
            r[COL["MODELTXT"]],
            r[COL["YEARTXT"]],
        )
    )

    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="latin-1", errors="strict", newline="") as f:
        for row in tsv_rows:
            f.write("\t".join(row) + "\n")

    return len({r[COL["CAMPNO"]] for r in tsv_rows})


# ---------------------------------------------------------------------------
# Smoke eval (record chunker only)
# ---------------------------------------------------------------------------


def _load_smoke_rows(path: Path | None = None) -> list[GoldenRow]:
    """Load smoke questions from the committed JSONL."""
    return _load_jsonl(path or SMOKE_PATH)


def run_smoke_eval(
    conn_url: str,
    smoke_rows: list[GoldenRow],
) -> dict[str, dict[str, Any]]:
    """Run retrieval over smoke questions (record chunker only).

    Returns ``{config_key: aggregate_metrics_dict}``.
    """
    from psycopg_pool import ConnectionPool

    from recall_assistant.ingest.embedder import SentenceTransformerEmbedder
    from recall_assistant.retrieval.rerank import CrossEncoderReranker
    from recall_assistant.retrieval.retriever import Retriever

    settings = Settings()
    pool = ConnectionPool(conn_url, min_size=1, max_size=2, open=True)
    embedder = SentenceTransformerEmbedder(
        model_name=settings.embedding_model, device="cpu"
    )
    reranker = CrossEncoderReranker(model_name=settings.rerank_model, device="cpu")
    retriever = Retriever(pool, embedder, reranker=reranker, chunker="record")

    answerable = [r for r in smoke_rows if r.gold_campaign_ids]

    # Run retrieval: 4 modes × unfiltered/filtered, record chunker only.
    per_question: dict[str, dict[str, RowResult]] = {}
    for row in answerable:
        per_question[row.id] = {}
        has_vehicle = has_complete_vehicle(row)
        if has_vehicle:
            vf = VehicleFilter(make=row.make, model=row.model, year=row.year)

        for mode in MODES:
            method = getattr(retriever, mode)

            t0 = time.perf_counter()
            hits_u: list[Hit] = method(question=row.question, k=10)
            elapsed_u = time.perf_counter() - t0
            key_u = f"{mode}_record_unfiltered"
            check_no_duplicate_campaigns(hits_u, row.id, key_u)
            per_question[row.id][key_u] = RowResult(
                row_id=row.id,
                config_key=key_u,
                retrieved_ids=[h.campaign_id for h in hits_u],
                top_score=hits_u[0].score if hits_u else None,
                latency_s=elapsed_u,
            )

            if has_vehicle:
                t0 = time.perf_counter()
                hits_f: list[Hit] = method(
                    question=row.question, k=10, vehicle_filter=vf
                )
                elapsed_f = time.perf_counter() - t0
                key_f = f"{mode}_record_filtered"
                check_no_duplicate_campaigns(hits_f, row.id, key_f)
                per_question[row.id][key_f] = RowResult(
                    row_id=row.id,
                    config_key=key_f,
                    retrieved_ids=[h.campaign_id for h in hits_f],
                    top_score=hits_f[0].score if hits_f else None,
                    latency_s=elapsed_f,
                )

    pool.close()

    # Aggregate per config.
    all_configs = sorted({k for results in per_question.values() for k in results})
    metrics: dict[str, dict[str, Any]] = {}
    for cfg in all_configs:
        metrics[cfg] = aggregate_metrics(answerable, per_question, cfg)

    return metrics


# ---------------------------------------------------------------------------
# Advisory answer check
# ---------------------------------------------------------------------------


def run_answer_check(
    conn_url: str,
    smoke_rows: list[GoldenRow],
    settings: Settings,
) -> None:
    """Run LLM answers on smoke questions and report pass rate.

    Advisory only — never calls ``sys.exit``.
    """
    if not settings.openrouter_api_key:
        print("OPENROUTER_API_KEY not set — skipping answer check.")
        return

    from psycopg_pool import ConnectionPool

    from recall_assistant.evals.answer_checks import pass_rule_check
    from recall_assistant.evals.prompt_fix_comparison import count_example_ids
    from recall_assistant.ingest.embedder import SentenceTransformerEmbedder
    from recall_assistant.llm.openrouter import build_openrouter_client
    from recall_assistant.rag import answer
    from recall_assistant.retrieval.rerank import CrossEncoderReranker
    from recall_assistant.retrieval.retriever import Retriever

    pool = ConnectionPool(conn_url, min_size=1, max_size=2, open=True)
    embedder = SentenceTransformerEmbedder(
        model_name=settings.embedding_model, device="cpu"
    )
    reranker = CrossEncoderReranker(model_name=settings.rerank_model, device="cpu")
    retriever = Retriever(pool, embedder, reranker=reranker, chunker="record")
    llm_client = build_openrouter_client(settings)

    rows_to_check = smoke_rows[:30]
    passed = 0
    total = 0
    answer_texts: dict[str, str] = {}

    for row in rows_to_check:
        total += 1
        result = answer(
            question=row.question,
            retriever=retriever,
            llm_client=llm_client,
            model=settings.llm_model,
            mode="rerank",
            settings=settings,
        )
        answer_texts[row.id] = result.answer_text
        if pass_rule_check(
            row.pass_rule,
            result.status,
            result.cited_ids,
            row.gold_campaign_ids,
        ):
            passed += 1

    pool.close()

    example_copies = count_example_ids(answer_texts)
    print(
        f"Answer check: {passed}/{total} passed "
        f"({passed / total:.1%}), example-ID copies: {example_copies}"
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="CI eval gate: load sample, check regressions, "
        "run advisory answer check",
    )
    parser.add_argument(
        "--load-sample",
        action="store_true",
        help="Load CI sample TSV into the database",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Run smoke eval and compare against baseline",
    )
    parser.add_argument(
        "--baseline",
        type=Path,
        default=DEFAULT_BASELINE_PATH,
        help="Path to baseline.json (default: results/baseline.json)",
    )
    parser.add_argument(
        "--sample",
        type=Path,
        default=CI_SAMPLE_PATH,
        help="Path to CI sample TSV (default: tests/data/ci_sample.tsv)",
    )
    parser.add_argument(
        "--smoke",
        type=Path,
        default=SMOKE_PATH,
        help="Path to smoke JSONL (default: evals/smoke.jsonl)",
    )
    parser.add_argument(
        "--answers",
        action="store_true",
        help="Run advisory LLM answer check",
    )
    args = parser.parse_args(argv)

    if not (args.load_sample or args.check or args.answers):
        parser.print_help()
        sys.exit(1)

    settings = Settings()

    # --check: verify baseline hashes BEFORE any database connection.
    baseline_data: dict[str, Any] | None = None
    if args.check:
        if not args.baseline.exists():
            print(
                f"ERROR: baseline not found: {args.baseline}",
                file=sys.stderr,
            )
            sys.exit(1)
        baseline_data = json.loads(args.baseline.read_text(encoding="utf-8"))
        expected_hashes = baseline_data.get("sha256", {})
        for label, path in [
            ("ci_sample_tsv", args.sample),
            ("smoke_jsonl", args.smoke),
        ]:
            expected = expected_hashes.get(label)
            actual = file_sha256(path)
            if expected and actual != expected:
                print(
                    f"ERROR: baseline stale — {label} hash mismatch\n"
                    f"  expected: {expected}\n"
                    f"  actual:   {actual}",
                    file=sys.stderr,
                )
                sys.exit(1)

    # --load-sample
    if args.load_sample:
        with psycopg.connect(settings.database_url) as conn:
            n = load_ci_sample(conn, args.sample)
        print(f"Loaded {n} campaigns into database.")

    # --check: run smoke eval and gate
    if args.check:
        assert baseline_data is not None
        smoke_rows = _load_smoke_rows(args.smoke)
        current = run_smoke_eval(settings.database_url, smoke_rows)
        baseline_metrics = baseline_data.get("metrics", {})
        passed, messages = check_gate(current, baseline_metrics)
        for msg in messages:
            print(msg)
        if not passed:
            sys.exit(1)
        print("Eval gate passed.")

    # --answers: advisory LLM check
    if args.answers:
        smoke_rows = _load_smoke_rows(args.smoke)
        run_answer_check(settings.database_url, smoke_rows, settings)


if __name__ == "__main__":
    main()
