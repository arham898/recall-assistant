"""Tests for scripts/smoke_live.py against an in-process server with fakes.

No real database, no ML models, no LLM network calls.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
import time
from contextlib import contextmanager

import httpx
import pytest
import uvicorn

from recall_assistant.retrieval import Hit
from tests.api.conftest import TEST_API_KEY, create_test_app


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _BrokenPool:
    """Pool whose connection() always raises, making /health report db_reachable=false."""

    @contextmanager
    def connection(self):
        raise ConnectionError("fake database down")
        yield  # pragma: no cover

    def close(self) -> None:
        pass


@pytest.fixture()
def fake_server():
    """Start a uvicorn server with fakes in a thread; yield (port, server); shut down."""
    port = _find_free_port()
    app = create_test_app(
        llm_text="Brake recall [25V001].",
        hits=[
            Hit(
                campaign_id="25V001",
                chunk_id=1,
                text="brake hose recall",
                score=0.9,
                rank=1,
            ),
        ],
    )
    config = uvicorn.Config(app=app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            resp = httpx.get(f"http://127.0.0.1:{port}/health", timeout=1)
            if resp.status_code == 200:
                break
        except (httpx.ConnectError, httpx.ConnectTimeout):
            pass
        time.sleep(0.1)
    else:
        raise RuntimeError(f"Server did not start on port {port} within 10 seconds")

    yield port

    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture()
def degraded_server():
    """Server whose /health reports db_reachable=false (HTTP 200, status=degraded)."""
    port = _find_free_port()
    app = create_test_app(
        llm_text="Brake recall [25V001].",
        hits=[
            Hit(
                campaign_id="25V001",
                chunk_id=1,
                text="brake hose recall",
                score=0.9,
                rank=1,
            ),
        ],
        pool=_BrokenPool(),
    )
    config = uvicorn.Config(app=app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            resp = httpx.get(f"http://127.0.0.1:{port}/health", timeout=1)
            if resp.status_code == 200:
                break
        except (httpx.ConnectError, httpx.ConnectTimeout):
            pass
        time.sleep(0.1)
    else:
        raise RuntimeError(f"Server did not start on port {port} within 10 seconds")

    yield port

    server.should_exit = True
    thread.join(timeout=5)


def _run_smoke(env: dict[str, str], timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "scripts/smoke_live.py"],
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )


def test_smoke_live_success(fake_server):
    """Server with fakes returns a cited answer; smoke_live.py exits 0."""
    env = {
        **os.environ,
        "LIVE_URL": f"http://127.0.0.1:{fake_server}",
        "LIVE_API_KEY": TEST_API_KEY,
        "EXPECTED_CITATIONS": "25V001",
    }
    result = _run_smoke(env)

    assert result.returncode == 0, (
        f"smoke_live.py failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "25V001" in result.stdout
    assert "SMOKE TEST PASSED" in result.stdout


def test_smoke_live_wrong_api_key(fake_server):
    """Wrong API key causes smoke_live.py to exit non-zero."""
    env = {
        **os.environ,
        "LIVE_URL": f"http://127.0.0.1:{fake_server}",
        "LIVE_API_KEY": "wrong-key",
        "EXPECTED_CITATIONS": "25V001",
    }
    result = _run_smoke(env)

    assert result.returncode != 0, (
        f"Expected non-zero exit code:\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "401" in result.stderr


def test_smoke_live_unreachable_url():
    """Unreachable URL causes smoke_live.py to exit non-zero."""
    env = {
        **os.environ,
        "LIVE_URL": "http://127.0.0.1:1",
        "LIVE_API_KEY": "any-key",
        "EXPECTED_CITATIONS": "25V001",
    }
    result = _run_smoke(env, timeout=15)

    assert result.returncode != 0, (
        f"Expected non-zero exit code:\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "FAIL" in result.stderr
    assert "Traceback" not in result.stderr


def test_smoke_live_missing_citation(fake_server):
    """Expected citation not in response causes smoke_live.py to exit non-zero."""
    env = {
        **os.environ,
        "LIVE_URL": f"http://127.0.0.1:{fake_server}",
        "LIVE_API_KEY": TEST_API_KEY,
        "EXPECTED_CITATIONS": "99V999",
    }
    result = _run_smoke(env)

    assert result.returncode != 0, (
        f"Expected non-zero exit code:\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "99V999" in result.stderr


def test_smoke_live_db_unreachable(degraded_server):
    """Server with db_reachable=false makes smoke_live.py exit non-zero.

    The /health route (routes_health.py:18-40) catches any exception from
    pool.connection() and returns HTTP 200 with
    {"status": "degraded", "db_reachable": false, ...}.
    smoke_live.py treats db_reachable=false as a failure.
    """
    env = {
        **os.environ,
        "LIVE_URL": f"http://127.0.0.1:{degraded_server}",
        "LIVE_API_KEY": TEST_API_KEY,
        "EXPECTED_CITATIONS": "25V001",
    }
    result = _run_smoke(env)

    assert result.returncode != 0, (
        f"Expected non-zero exit code:\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "db_reachable" in result.stderr
