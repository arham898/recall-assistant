"""Integration test: start uvicorn on a free port, hit /health."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time

import httpx
import pytest


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.integration
@pytest.mark.slow
def test_uvicorn_starts_and_serves_health():
    """Start the API in a subprocess and verify /health responds."""
    port = _find_free_port()

    env = {
        **os.environ,
        "POSTGRES_DB": "recall_assistant_test",
        "API_KEY": "test-subprocess-key",
        "OPENROUTER_API_KEY": "fake",
        "LLM_MODEL": "fake-model",
    }

    proc = subprocess.Popen(
        [
            sys.executable,
            "scripts/serve.py",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )

    try:
        url = f"http://127.0.0.1:{port}/health"
        deadline = time.time() + 60
        resp = None

        while time.time() < deadline:
            try:
                resp = httpx.get(url, timeout=5)
                if resp.status_code == 200:
                    break
            except (httpx.ConnectError, httpx.ConnectTimeout):
                time.sleep(2)
        else:
            # Dump stderr for diagnosis.
            proc.terminate()
            _, stderr = proc.communicate(timeout=5)
            pytest.fail(
                f"Server did not start within 60 seconds.\n"
                f"stderr: {stderr.decode('utf-8', errors='replace')[-2000:]}"
            )

        assert resp is not None
        body = resp.json()
        assert body["status"] in ("ok", "degraded")
        assert "version" in body
        assert "prompt_hash" in body
    finally:
        proc.terminate()
        proc.wait(timeout=10)
