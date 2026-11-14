"""Smoke test a live recall-assistant deployment.

Usage (PowerShell):
    $env:LIVE_URL = "https://<your-app-fqdn>"
    $env:LIVE_API_KEY = "<your-api-key>"
    python scripts/smoke_live.py

Optional:
    $env:EXPECTED_CITATIONS = "22V176000,22V687000,22V514000"

Exits non-zero on any failure.
"""

from __future__ import annotations

import os
import sys
import time

import httpx

DEFAULT_CITATIONS = "22V176000,22V687000,22V514000"
QUESTION = "What recalls affect the 2022 Volkswagen Taos?"


def main() -> None:
    base_url = os.environ.get("LIVE_URL", "").rstrip("/")
    api_key = os.environ.get("LIVE_API_KEY", "")
    expected_raw = os.environ.get("EXPECTED_CITATIONS", DEFAULT_CITATIONS)
    expected = {c.strip() for c in expected_raw.split(",") if c.strip()}

    if not base_url:
        print("ERROR: LIVE_URL not set", file=sys.stderr)
        sys.exit(1)
    if not api_key:
        print("ERROR: LIVE_API_KEY not set", file=sys.stderr)
        sys.exit(1)
    if not expected:
        print("ERROR: EXPECTED_CITATIONS is empty", file=sys.stderr)
        sys.exit(1)

    client = httpx.Client(base_url=base_url, timeout=120.0)
    ok = True

    # ── 1. Health check (no auth) ──
    print(f"GET {base_url}/health")
    start = time.perf_counter()
    try:
        resp = client.get("/health")
    except httpx.HTTPError as exc:
        print(f"  FAIL: {exc}", file=sys.stderr)
        sys.exit(1)

    latency_ms = (time.perf_counter() - start) * 1000
    print(f"  status={resp.status_code}  latency={latency_ms:.0f}ms")

    if resp.status_code != 200:
        print(f"  FAIL: expected 200, got {resp.status_code}", file=sys.stderr)
        ok = False
    else:
        body = resp.json()
        print(f"  version={body.get('version')}")
        print(f"  db_reachable={body.get('db_reachable')}")
        print(f"  prompt_hash={body.get('prompt_hash')}")
        if not body.get("db_reachable"):
            print("  FAIL: db_reachable is false", file=sys.stderr)
            ok = False

    # ── 2. Ask question (requires auth) ──
    print(f"\nPOST {base_url}/ask")
    print(f"  question: {QUESTION}")
    headers = {"X-API-Key": api_key}
    start = time.perf_counter()
    try:
        resp = client.post("/ask", json={"question": QUESTION}, headers=headers)
    except httpx.HTTPError as exc:
        print(f"  FAIL: {exc}", file=sys.stderr)
        sys.exit(1)

    latency_ms = (time.perf_counter() - start) * 1000
    print(f"  status={resp.status_code}  latency={latency_ms:.0f}ms")

    if resp.status_code != 200:
        print(f"  FAIL: expected 200, got {resp.status_code}", file=sys.stderr)
        print(f"  body: {resp.text[:500]}", file=sys.stderr)
        ok = False
    else:
        body = resp.json()
        cited = set(body.get("cited_ids", []))
        print(f"  answer_status={body.get('status')}")
        print(f"  cited_ids={sorted(cited)}")
        print(f"  answer={body.get('answer', '')[:200]}...")
        n_sources = len(body.get("sources", []))
        print(f"  sources_returned={n_sources}")

        missing = expected - cited
        if missing:
            print(
                f"  FAIL: missing expected citations: {sorted(missing)}",
                file=sys.stderr,
            )
            ok = False

    if not ok:
        print("\nSMOKE TEST FAILED", file=sys.stderr)
        sys.exit(1)
    print("\nSMOKE TEST PASSED")


if __name__ == "__main__":
    main()
