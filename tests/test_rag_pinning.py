"""Pinning tests for rag.answer().

These tests assert exact output for each status case using FakeLLMClient
and FakeRetriever.  They must pass on the old code before any rag.py
refactor so we can prove the refactor preserves behaviour.
"""

from __future__ import annotations

import pytest

from recall_assistant.rag import DECLINE_MARKER, answer
from recall_assistant.retrieval import Hit
from tests.fakes import FakeLLMClient, FakeRetriever

HITS = [
    Hit(campaign_id="RET_001", chunk_id=1, text="fuel pump failure", score=0.9, rank=1),
    Hit(
        campaign_id="RET_002",
        chunk_id=2,
        text="brake line corrosion",
        score=0.8,
        rank=2,
    ),
]


def _run(text: str, *, finish_reason: str = "stop"):
    """Call answer() with canned fakes and return the AnswerResult."""
    llm = FakeLLMClient(text=text, finish_reason=finish_reason)
    retriever = FakeRetriever(HITS)
    return answer(
        question="test question",
        retriever=retriever,
        llm_client=llm,
        model="test-model",
        mode="hybrid",
    )


# ---- Five pinning cases ----


CASES = [
    pytest.param(
        "Brake issue [RET_001].",
        "stop",
        "answered",
        ["RET_001"],
        [],
        id="answered",
    ),
    pytest.param(
        DECLINE_MARKER,
        "stop",
        "declined",
        [],
        [],
        id="declined",
    ),
    pytest.param(
        "",
        "stop",
        "invalid",
        [],
        [],
        id="empty",
    ),
    pytest.param(
        "Brake issue [RET_001].",
        "length",
        "invalid",
        [],
        [],
        id="finish_reason_length",
    ),
    pytest.param(
        "reply [99V999].",
        "stop",
        "invalid",
        [],
        ["99V999"],
        id="invalid_id",
    ),
]


@pytest.mark.parametrize(
    "text, finish_reason, expected_status, expected_cited, expected_invalid",
    CASES,
)
def test_pinning(
    text, finish_reason, expected_status, expected_cited, expected_invalid
):
    result = _run(text, finish_reason=finish_reason)
    assert result.status == expected_status
    assert result.cited_ids == expected_cited
    assert result.invalid_ids == expected_invalid


# ---- Additional exact-value checks ----


def test_answered_exact_text():
    result = _run("Brake issue [RET_001].")
    assert result.answer_text == "Brake issue [RET_001]."


def test_answered_hits_preserved():
    result = _run("Brake issue [RET_001].")
    assert len(result.retrieved_hits) == 2
    assert result.retrieved_hits[0].campaign_id == "RET_001"
    assert result.retrieved_hits[1].campaign_id == "RET_002"


def test_latencies_positive():
    result = _run("Brake issue [RET_001].")
    assert result.retrieval_latency_s >= 0
    assert result.generation_latency_s >= 0
