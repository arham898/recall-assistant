"""Shared fixtures for ingest tests."""

from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def sample_rcl_path() -> Path:
    return FIXTURES_DIR / "sample_rcl.tsv"
