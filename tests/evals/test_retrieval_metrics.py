"""Unit tests for pure retrieval metrics.

No database, no markers.  Every test includes the hand-worked
arithmetic as comments so the expected value is never a magic number.
"""

from __future__ import annotations

import pytest

from recall_assistant.evals.retrieval_metrics import (
    hit_at_k,
    recall_at_k,
    reciprocal_rank,
)

# ── hit_at_k ────────────────────────────────────────────────────────


class TestHitAtK:
    def test_gold_at_rank_1(self):
        # G at index 0 (rank 1).  First 5 include G.  First 10 include G.
        retrieved = ["G", "A", "B", "C", "D"]
        gold = {"G"}
        assert hit_at_k(retrieved, gold, 5) is True
        assert hit_at_k(retrieved, gold, 10) is True

    def test_gold_at_rank_7(self):
        # G at index 6 (rank 7).
        # First 5: [A B C D E] → no G → False.
        # First 10: [A B C D E F G H I J] → G present → True.
        retrieved = ["A", "B", "C", "D", "E", "F", "G", "H", "I", "J"]
        gold = {"G"}
        assert hit_at_k(retrieved, gold, 5) is False
        assert hit_at_k(retrieved, gold, 10) is True

    def test_gold_not_found(self):
        # Z not in list → False for any k.
        retrieved = ["A", "B", "C", "D", "E"]
        gold = {"Z"}
        assert hit_at_k(retrieved, gold, 5) is False
        assert hit_at_k(retrieved, gold, 10) is False

    def test_list_shorter_than_k(self):
        # List has 3 items, k=10.  [:10] returns all 3.  G at index 1.
        retrieved = ["A", "G", "C"]
        gold = {"G"}
        assert hit_at_k(retrieved, gold, 10) is True

    def test_gold_at_exactly_rank_k(self):
        # G at index 4 (rank 5), k=5.  [:5] = [A B C D G] → True.
        # An off-by-one using [:k-1] = [:4] would miss G.
        retrieved = ["A", "B", "C", "D", "G"]
        gold = {"G"}
        assert hit_at_k(retrieved, gold, 5) is True


# ── recall_at_k ─────────────────────────────────────────────────────


class TestRecallAtK:
    def test_single_gold_at_rank_1(self):
        # 1 gold, found in first 5.  1 / 1 = 1.0.
        retrieved = ["G", "A", "B", "C", "D"]
        gold = {"G"}
        assert recall_at_k(retrieved, gold, 5) == 1.0
        assert recall_at_k(retrieved, gold, 10) == 1.0

    def test_gold_at_rank_7(self):
        # 1 gold at rank 7.
        # recall@5: 0 found / 1 total = 0.0
        # recall@10: 1 found / 1 total = 1.0
        retrieved = ["A", "B", "C", "D", "E", "F", "G", "H", "I", "J"]
        gold = {"G"}
        assert recall_at_k(retrieved, gold, 5) == 0.0
        assert recall_at_k(retrieved, gold, 10) == 1.0

    def test_two_of_three_gold_in_top_5(self):
        # 3 gold: G1, G2, G3.
        # First 5: [G1, A, G2, B, C].  Overlap = {G1, G2} → 2 found.
        # recall@5 = 2 / 3 = 0.6666…
        # First 10: [G1, A, G2, B, C, D, G3, E].  All 3 found.
        # recall@10 = 3 / 3 = 1.0
        retrieved = ["G1", "A", "G2", "B", "C", "D", "G3", "E"]
        gold = {"G1", "G2", "G3"}
        assert recall_at_k(retrieved, gold, 5) == pytest.approx(2 / 3)
        assert recall_at_k(retrieved, gold, 10) == 1.0

    def test_gold_not_found(self):
        # 0 overlap → 0 / 1 = 0.0.
        retrieved = ["A", "B", "C"]
        gold = {"Z"}
        assert recall_at_k(retrieved, gold, 5) == 0.0

    def test_list_shorter_than_k(self):
        # 3-item list, k=10.  G at index 0.  1 / 1 = 1.0.
        retrieved = ["G", "A", "B"]
        gold = {"G"}
        assert recall_at_k(retrieved, gold, 10) == 1.0

    def test_empty_gold(self):
        # Edge case: no gold → 0.0 (avoid division by zero).
        retrieved = ["A", "B"]
        gold: set[str] = set()
        assert recall_at_k(retrieved, gold, 5) == 0.0


# ── reciprocal_rank ─────────────────────────────────────────────────


class TestReciprocalRank:
    def test_gold_at_rank_1(self):
        # First item is gold.  RR = 1 / 1 = 1.0.
        retrieved = ["G", "A", "B"]
        gold = {"G"}
        assert reciprocal_rank(retrieved, gold) == 1.0

    def test_gold_at_rank_7(self):
        # Gold at index 6 (rank 7).  RR = 1 / 7.
        retrieved = ["A", "B", "C", "D", "E", "F", "G"]
        gold = {"G"}
        assert reciprocal_rank(retrieved, gold) == pytest.approx(1 / 7)

    def test_gold_not_found(self):
        # No gold in list → 0.0.
        retrieved = ["A", "B", "C"]
        gold = {"Z"}
        assert reciprocal_rank(retrieved, gold) == 0.0

    def test_multiple_gold_returns_first(self):
        # Two gold: G1, G2.  G2 at rank 2, G1 at rank 4.
        # First gold found is G2 → RR = 1 / 2 = 0.5.
        retrieved = ["A", "G2", "B", "G1", "C"]
        gold = {"G1", "G2"}
        assert reciprocal_rank(retrieved, gold) == 0.5
