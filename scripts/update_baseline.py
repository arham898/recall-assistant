"""Update the CI eval-gate baseline.

Run against the scratch database (POSTGRES_DB=recall_ci) after committing
the sample TSV and smoke JSONL.

Usage:
    POSTGRES_DB=recall_ci uv run python scripts/update_baseline.py
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from datetime import date

from recall_assistant.config import PROJECT_ROOT, Settings
from recall_assistant.evals.ci_gate import (
    CI_SAMPLE_PATH,
    SMOKE_PATH,
    _load_smoke_rows,
    file_sha256,
    guard_scratch_database,
    run_smoke_eval,
)
from recall_assistant.evals.run_metadata import git_commit, is_worktree_dirty
from recall_assistant.ingest.migrator import run_migrations
from recall_assistant.ingest.parser import parse_recalls

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

BASELINE_PATH = PROJECT_ROOT / "results" / "baseline.json"


def main(argv: list[str] | None = None) -> None:
    # 1. Dirty-tree guard (no override).
    if is_worktree_dirty():
        print(
            "ERROR: worktree is dirty. Commit first so the baseline "
            "records a clean commit.",
            file=sys.stderr,
        )
        sys.exit(1)

    settings = Settings()
    import psycopg

    # 2. Connect, run migrations, count existing recalls.
    conn = psycopg.connect(settings.database_url)
    run_migrations(conn)

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM recalls")
        existing_count = cur.fetchone()[0]

    # 3. Count sample campaigns (parse only, don't upsert yet).
    recalls, _skipped = parse_recalls(CI_SAMPLE_PATH)
    sample_count = len({r.campaign_id for r in recalls})

    # 4. Guard: refuse if DB has more than sample.
    guard_scratch_database(existing_count, sample_count)

    # 5. NOW upsert.
    from recall_assistant.ingest.loader import upsert_recalls

    upsert_recalls(conn, recalls, allow_mass_delete=True)

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM recalls")
        db_count = cur.fetchone()[0]
    assert db_count == sample_count, (
        f"DB has {db_count} campaigns but sample has {sample_count}"
    )
    conn.close()
    logger.info("Loaded %d campaigns into %s", sample_count, settings.postgres_db)

    # 6. Embed (subprocess — same as CI does).
    logger.info("Embedding with record chunker ...")
    subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "embed.py"),
            "--chunker",
            "record",
        ],
        check=True,
    )

    # 7. Run smoke eval.
    smoke_rows = _load_smoke_rows()
    answerable = [r for r in smoke_rows if r.gold_campaign_ids]
    logger.info("Running smoke eval on %d answerable questions ...", len(answerable))
    metrics = run_smoke_eval(settings.database_url, smoke_rows)

    for cfg, m in sorted(metrics.items()):
        logger.info(
            "  %s: hit@5=%.4f mrr=%.4f (n=%d)", cfg, m["hit@5"], m["mrr"], m["n"]
        )

    # 8. Write baseline.
    baseline = {
        "git_commit": git_commit(),
        "date": str(date.today()),
        "command": "python scripts/update_baseline.py",
        "n_questions": len(answerable),
        "sha256": {
            "ci_sample_tsv": file_sha256(CI_SAMPLE_PATH),
            "smoke_jsonl": file_sha256(SMOKE_PATH),
        },
        "metrics": metrics,
    }

    BASELINE_PATH.parent.mkdir(parents=True, exist_ok=True)
    BASELINE_PATH.write_text(json.dumps(baseline, indent=2) + "\n", encoding="utf-8")
    logger.info("Wrote baseline to %s", BASELINE_PATH)


if __name__ == "__main__":
    main()
