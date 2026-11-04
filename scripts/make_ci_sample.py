"""Generate the CI smoke sample: evals/smoke.jsonl and tests/data/ci_sample.tsv.

One-time script run against the FULL recall database.

Usage:
    uv run python scripts/make_ci_sample.py
"""

from __future__ import annotations

import logging
import random
import sys
from pathlib import Path

import psycopg

from recall_assistant.config import Settings
from recall_assistant.evals.ci_gate import (
    CI_SAMPLE_PATH,
    SMOKE_PATH,
    export_campaigns_tsv,
    select_smoke_questions,
)
from recall_assistant.evals.golden import GoldenRow
from recall_assistant.ingest.embedder import SentenceTransformerEmbedder
from recall_assistant.ingest.parser import parse_recalls
from recall_assistant.retrieval.hybrid import hybrid_search

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def _write_smoke_jsonl(rows: list[GoldenRow], path: Path) -> None:
    """Write smoke questions to JSONL, sorted by id for determinism."""
    sorted_rows = sorted(rows, key=lambda r: r.id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.writelines(row.model_dump_json() + "\n" for row in sorted_rows)
    logger.info("Wrote %d rows to %s", len(sorted_rows), path)


def _verify_round_trip(
    conn: psycopg.Connection,
    tsv_path: Path,
    campaign_ids: set[str],
) -> None:
    """Parse the written TSV back and compare every field to the DB."""
    recalls, skipped = parse_recalls(tsv_path)
    parsed_by_cid = {r.campaign_id: r for r in recalls}

    if len(parsed_by_cid) != len(campaign_ids):
        print(
            f"ERROR: expected {len(campaign_ids)} campaigns in TSV, "
            f"got {len(parsed_by_cid)} (skipped {skipped})",
            file=sys.stderr,
        )
        sys.exit(1)

    # Load database rows for comparison.
    id_list = sorted(campaign_ids)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM recalls WHERE campaign_id = ANY(%s) ORDER BY campaign_id",
            (id_list,),
        )
        col_names = [desc.name for desc in cur.description]
        db_recalls = {
            row[col_names.index("campaign_id")]: dict(zip(col_names, row))
            for row in cur.fetchall()
        }

        cur.execute(
            "SELECT campaign_id, make, model, year "
            "FROM recall_vehicles WHERE campaign_id = ANY(%s) "
            "ORDER BY campaign_id, make, model, year",
            (id_list,),
        )
        db_vehicles: dict[str, list[tuple[str, str, int | None]]] = {}
        for cid, make, model, year in cur.fetchall():
            db_vehicles.setdefault(cid, []).append((make, model, year))

        cur.execute(
            "SELECT campaign_id, component "
            "FROM recall_components WHERE campaign_id = ANY(%s) "
            "ORDER BY campaign_id, component",
            (id_list,),
        )
        db_components: dict[str, list[str]] = {}
        for cid, comp in cur.fetchall():
            db_components.setdefault(cid, []).append(comp)

    for cid in id_list:
        parsed = parsed_by_cid.get(cid)
        db = db_recalls.get(cid)
        if parsed is None or db is None:
            print(f"ERROR: campaign {cid} missing from parsed or DB", file=sys.stderr)
            sys.exit(1)

        # Compare text fields.
        for field in (
            "manufacturer",
            "defect_summary",
            "consequence_summary",
            "corrective_action",
            "notes",
            "component",
            "recall_type",
            "influenced_by",
        ):
            p_val = getattr(parsed, field)
            d_val = db[field]
            if p_val != d_val:
                print(
                    f"ERROR: campaign {cid} field {field} mismatch:\n"
                    f"  DB:     {d_val!r}\n"
                    f"  Parsed: {p_val!r}",
                    file=sys.stderr,
                )
                sys.exit(1)

        # Compare vehicles.
        p_vehs = sorted((v.make, v.model, v.year) for v in parsed.vehicles)
        d_vehs = sorted(db_vehicles.get(cid, []))
        if p_vehs != d_vehs:
            print(
                f"ERROR: campaign {cid} vehicles mismatch:\n"
                f"  DB:     {d_vehs}\n"
                f"  Parsed: {p_vehs}",
                file=sys.stderr,
            )
            sys.exit(1)

        # Compare components.
        p_comps = sorted(parsed.components)
        d_comps = sorted(db_components.get(cid, []))
        if p_comps != d_comps:
            print(
                f"ERROR: campaign {cid} components mismatch:\n"
                f"  DB:     {d_comps}\n"
                f"  Parsed: {p_comps}",
                file=sys.stderr,
            )
            sys.exit(1)

    logger.info("Round-trip verification passed for %d campaigns", len(id_list))


def main() -> None:
    settings = Settings()

    # 1. Select smoke questions.
    smoke_rows = select_smoke_questions(seed=42)
    logger.info(
        "Selected %d smoke questions: %s",
        len(smoke_rows),
        dict(__import__("collections").Counter(r.source for r in smoke_rows)),
    )

    # 2. Write smoke.jsonl.
    _write_smoke_jsonl(smoke_rows, SMOKE_PATH)

    # 3. Collect campaign IDs: gold + top-30 hybrid candidates.
    answerable = [r for r in smoke_rows if r.gold_campaign_ids]
    all_campaign_ids: set[str] = set()
    for row in answerable:
        all_campaign_ids.update(row.gold_campaign_ids)

    logger.info("Loading embedder for candidate retrieval ...")
    embedder = SentenceTransformerEmbedder(
        model_name=settings.embedding_model, device="cpu"
    )

    with psycopg.connect(settings.database_url) as conn:
        for row in answerable:
            # Call hybrid_search with k=30: same function rerank_search
            # calls internally (rerank.py:73-83) with k=rerank_candidates.
            hits = hybrid_search(conn, embedder, row.question, chunker="record", k=30)
            all_campaign_ids.update(h.campaign_id for h in hits)

        logger.info(
            "Collected %d unique campaigns (gold + hybrid candidates)",
            len(all_campaign_ids),
        )

        # 4. Filler if below 300.
        if len(all_campaign_ids) < 300:
            with conn.cursor() as cur:
                cur.execute("SELECT campaign_id FROM recalls ORDER BY campaign_id")
                all_db_ids = [row[0] for row in cur.fetchall()]
            available = [cid for cid in all_db_ids if cid not in all_campaign_ids]
            needed = 300 - len(all_campaign_ids)
            filler = random.Random(42).sample(available, min(needed, len(available)))
            all_campaign_ids.update(filler)
            logger.info(
                "Added %d filler campaigns → %d total",
                len(filler),
                len(all_campaign_ids),
            )

        # 5. Export TSV.
        logger.info(
            "Exporting %d campaigns to %s ...", len(all_campaign_ids), CI_SAMPLE_PATH
        )
        n_written = export_campaigns_tsv(conn, all_campaign_ids, CI_SAMPLE_PATH)
        logger.info("Wrote %d campaigns to TSV", n_written)

        # 6. Verify round-trip.
        _verify_round_trip(conn, CI_SAMPLE_PATH, all_campaign_ids)

    # 7. Smoke integrity: every answerable gold ID must be in the TSV.
    recalls, _ = parse_recalls(CI_SAMPLE_PATH)
    tsv_cids = {r.campaign_id for r in recalls}
    for row in answerable:
        missing = set(row.gold_campaign_ids) - tsv_cids
        if missing:
            print(
                f"ERROR: smoke row {row.id} has gold IDs not in TSV: {missing}",
                file=sys.stderr,
            )
            sys.exit(1)

    logger.info(
        "Done. %d campaigns in TSV, %d smoke questions in JSONL.",
        n_written,
        len(smoke_rows),
    )


if __name__ == "__main__":
    main()
