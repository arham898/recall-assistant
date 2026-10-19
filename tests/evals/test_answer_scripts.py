"""Tests for run_answers.py and score_answers.py.

Cache, cost computation, cost projection, and subprocess --help tests.
No database, no network, no model downloads.
"""

from __future__ import annotations

import subprocess
import sys

import pytest
from scripts.run_answers import (
    _cache_key,
    _load_cache,
    _project_cost,
    _save_cache,
    _upper_bound_cost,
    compute_cost,
    prompt_hash,
    seal_hash,
)

from recall_assistant.config import PROJECT_ROOT, Settings

# ═══════════════════════════════════════════════════════════════════
# 1. Cache key
# ═══════════════════════════════════════════════════════════════════


class TestCacheKey:
    def test_same_inputs_cache_hit(self, tmp_path, monkeypatch):
        """Pre-populated cache entry is returned by _load_cache."""
        monkeypatch.setattr("scripts.run_answers.CACHE_DIR", tmp_path)
        key = _cache_key("Q1", "rerank", "record", "model/x", "aaa", "bbb")
        row = {"question_id": "Q1", "status": "answered"}
        _save_cache(key, row)
        loaded = _load_cache(key)
        assert loaded is not None
        assert loaded["question_id"] == "Q1"

    def test_changed_prompt_hash_misses(self, tmp_path, monkeypatch):
        """Different prompt hash → cache miss."""
        monkeypatch.setattr("scripts.run_answers.CACHE_DIR", tmp_path)
        key_old = _cache_key("Q1", "rerank", "record", "model/x", "aaa", "bbb")
        _save_cache(key_old, {"question_id": "Q1"})

        key_new = _cache_key("Q1", "rerank", "record", "model/x", "zzz", "bbb")
        assert _load_cache(key_new) is None

    def test_changed_model_misses(self, tmp_path, monkeypatch):
        """Different model → cache miss."""
        monkeypatch.setattr("scripts.run_answers.CACHE_DIR", tmp_path)
        key_old = _cache_key("Q1", "rerank", "record", "model/x", "aaa", "bbb")
        _save_cache(key_old, {"question_id": "Q1"})

        key_new = _cache_key("Q1", "rerank", "record", "model/y", "aaa", "bbb")
        assert _load_cache(key_new) is None

    def test_changed_seal_hash_misses(self, tmp_path, monkeypatch):
        """Different seal hash → cache miss."""
        monkeypatch.setattr("scripts.run_answers.CACHE_DIR", tmp_path)
        key_old = _cache_key("Q1", "rerank", "record", "model/x", "aaa", "bbb")
        _save_cache(key_old, {"question_id": "Q1"})

        key_new = _cache_key("Q1", "rerank", "record", "model/x", "aaa", "ccc")
        assert _load_cache(key_new) is None


# ═══════════════════════════════════════════════════════════════════
# 2. Prompt hash and seal hash
# ═══════════════════════════════════════════════════════════════════


class TestHashes:
    def test_prompt_hash_is_12_hex(self):
        h = prompt_hash()
        assert len(h) == 12
        assert all(c in "0123456789abcdef" for c in h)

    def test_prompt_hash_deterministic(self):
        assert prompt_hash() == prompt_hash()

    def test_seal_hash_changes_with_data(self):
        seal_a = {"record": {"count": 100, "md5": "aaa"}}
        seal_b = {"record": {"count": 100, "md5": "bbb"}}
        assert seal_hash(seal_a) != seal_hash(seal_b)

    def test_seal_hash_deterministic(self):
        seal = {"record": {"count": 100, "md5": "aaa"}}
        assert seal_hash(seal) == seal_hash(seal)


# ═══════════════════════════════════════════════════════════════════
# 3. Cost computation
# ═══════════════════════════════════════════════════════════════════


class TestCostComputation:
    def test_known_model(self):
        settings = Settings()
        # google/gemini-2.5-flash-lite: $0.10/Mtok input, $0.40/Mtok output
        # 1000 prompt + 200 completion
        # = (1000 * 0.10 + 200 * 0.40) / 1_000_000
        # = (100 + 80) / 1_000_000
        # = 180 / 1_000_000
        # = 0.00018
        cost = compute_cost(1000, 200, "google/gemini-2.5-flash-lite", settings)
        assert cost == pytest.approx(0.00018)

    def test_unknown_model_returns_none(self):
        settings = Settings()
        cost = compute_cost(1000, 200, "unknown/model", settings)
        assert cost is None


# ═══════════════════════════════════════════════════════════════════
# 4. Cost projection
# ═══════════════════════════════════════════════════════════════════


class TestCostProjection:
    def test_projection_arithmetic(self):
        # 5 rows at $0.001 total → per-row = $0.0002
        # Projected for 150: $0.0002 * 150 = $0.03
        projected = _project_cost(0.001, 5, 150)
        assert projected == pytest.approx(0.03)

    def test_projection_zero_count(self):
        assert _project_cost(0.0, 0, 150) == 0.0

    def test_upper_bound_known_model(self):
        settings = Settings()
        # Upper bound for 1 question:
        # (4200 * 0.10 + 1024 * 0.40) / 1_000_000
        # = (420 + 409.6) / 1_000_000
        # = 829.6 / 1_000_000
        # = 0.0008296
        ub = _upper_bound_cost("google/gemini-2.5-flash-lite", settings, 1)
        assert ub == pytest.approx(0.0008296)

    def test_upper_bound_unknown_model(self):
        settings = Settings()
        assert _upper_bound_cost("unknown/model", settings, 150) is None


# ═══════════════════════════════════════════════════════════════════
# 5. Subprocess --help tests (no DB, no network)
# ═══════════════════════════════════════════════════════════════════


class TestScriptHelp:
    def test_run_answers_help(self):
        result = subprocess.run(
            [sys.executable, "scripts/run_answers.py", "--help"],
            capture_output=True,
            text=True,
            check=False,
            cwd=str(PROJECT_ROOT),
        )
        assert result.returncode == 0
        assert "--mode" in result.stdout
        assert "--chunker" in result.stdout
        assert "--limit" in result.stdout
        assert "ModuleNotFoundError" not in result.stderr, result.stderr
        assert "ImportError" not in result.stderr, result.stderr

    def test_score_answers_help(self):
        result = subprocess.run(
            [sys.executable, "scripts/score_answers.py", "--help"],
            capture_output=True,
            text=True,
            check=False,
            cwd=str(PROJECT_ROOT),
        )
        assert result.returncode == 0
        assert "--answers" in result.stdout
        assert "ModuleNotFoundError" not in result.stderr, result.stderr
        assert "ImportError" not in result.stderr, result.stderr
