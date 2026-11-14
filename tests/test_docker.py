"""Test: build and run the Docker image, verify /health."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import time
import uuid
from pathlib import Path
from urllib.request import urlopen

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.docker
def test_health_returns_200_with_db_reachable():
    """Start the containerised API and assert /health is green."""
    ci = os.environ.get("CI", "") != ""

    # Docker must be available in CI; skip locally when missing.
    result = subprocess.run(
        ["docker", "info"],
        check=False,
        capture_output=True,
        timeout=10,
    )
    if result.returncode != 0:
        if ci:
            pytest.fail("Docker is required in CI but 'docker info' failed")
        pytest.skip("Docker not available locally")

    # Use a pre-built image when RECALL_IMAGE is set (CI), else build.
    image = os.environ.get("RECALL_IMAGE", "")
    if not image:
        image = f"recall-assistant-test:{uuid.uuid4().hex[:8]}"
        subprocess.run(
            ["docker", "build", "-t", image, "."],
            cwd=str(PROJECT_ROOT),
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )

    port = _find_free_port()
    name = f"recall-test-{uuid.uuid4().hex[:8]}"

    cmd = [
        "docker",
        "run",
        "--detach",
        "--name",
        name,
        "-p",
        f"{port}:8000",
        "--add-host=host.docker.internal:host-gateway",
        "-e",
        "POSTGRES_HOST=host.docker.internal",
        "-e",
        f"POSTGRES_USER={os.environ.get('POSTGRES_USER', 'recall')}",
        "-e",
        f"POSTGRES_PASSWORD={os.environ.get('POSTGRES_PASSWORD', 'recall')}",
        "-e",
        f"POSTGRES_DB={os.environ.get('POSTGRES_DB', 'recall_assistant')}",
        "-e",
        f"POSTGRES_PORT={os.environ.get('POSTGRES_PORT', '5432')}",
        "-e",
        "API_KEY=test-docker-key",
        "-e",
        "OPENROUTER_API_KEY=fake",
        "-e",
        "LLM_MODEL=fake",
        image,
    ]
    subprocess.run(
        cmd,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    try:
        url = f"http://127.0.0.1:{port}/health"
        deadline = time.time() + 60
        resp_body = None

        while time.time() < deadline:
            try:
                with urlopen(url, timeout=5) as resp:
                    if resp.status == 200:
                        resp_body = json.loads(resp.read().decode("utf-8"))
                        break
            except (OSError, ValueError):
                time.sleep(2)
        else:
            logs = subprocess.run(
                ["docker", "logs", name],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            pytest.fail(
                f"Container did not respond within 60 s.\n"
                f"stdout: {logs.stdout[-2000:]}\n"
                f"stderr: {logs.stderr[-2000:]}"
            )

        assert resp_body is not None
        assert resp_body["db_reachable"] is True
    finally:
        subprocess.run(
            ["docker", "stop", name], check=False, capture_output=True, timeout=15
        )
        subprocess.run(
            ["docker", "rm", name], check=False, capture_output=True, timeout=15
        )
