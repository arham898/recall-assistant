"""Tests for the RAG answer pipeline.

Unit tests use FakeLLMClient + FakeRetriever (no database).
The integration test uses seed_retrieval_data with a real Retriever.
"""

from __future__ import annotations

import pytest

from recall_assistant.config import Settings
from recall_assistant.rag import DECLINE_MARKER, answer
from recall_assistant.retrieval import Hit, VehicleFilter
from tests.fakes import (
    FakeLLMClient,
    FakeReranker,
    FakeRetriever,
)

# ---------------------------------------------------------------------------
# Shared fixtures for unit tests
# ---------------------------------------------------------------------------

FAKE_HITS = [
    Hit(campaign_id="RET_001", chunk_id=1, text="fuel pump failure", score=0.9, rank=1),
    Hit(
        campaign_id="RET_002",
        chunk_id=2,
        text="brake line corrosion",
        score=0.8,
        rank=2,
    ),
    Hit(
        campaign_id="RET_003", chunk_id=3, text="airbag deploy issue", score=0.7, rank=3
    ),
]


def _answer(text, *, finish_reason="stop", mode="hybrid", hits=None, **kw):
    """Shortcut to call answer() with FakeLLMClient + FakeRetriever."""
    llm = FakeLLMClient(text=text, finish_reason=finish_reason)
    retriever = FakeRetriever(hits or FAKE_HITS)
    result = answer(
        question="test question",
        retriever=retriever,
        llm_client=llm,
        model="test-model",
        mode=mode,
        **kw,
    )
    return result, llm, retriever


# ---------------------------------------------------------------------------
# Prompt building
# ---------------------------------------------------------------------------


class TestPromptBuilding:
    def test_sources_contain_campaign_ids(self):
        _, llm, _ = _answer("reply [RET_001].")
        messages = llm.calls[0][0]
        user_msg = messages[1]["content"]
        assert "Source 1 (campaign RET_001):" in user_msg
        assert "Source 2 (campaign RET_002):" in user_msg
        assert "Source 3 (campaign RET_003):" in user_msg

    def test_sources_numbered_sequentially(self):
        _, llm, _ = _answer("reply [RET_001].")
        user_msg = llm.calls[0][0][1]["content"]
        assert "Source 1" in user_msg
        assert "Source 2" in user_msg
        assert "Source 3" in user_msg

    def test_llm_receives_all_campaign_ids(self):
        _, llm, _ = _answer("reply [RET_001].")
        user_msg = llm.calls[0][0][1]["content"]
        for hit in FAKE_HITS:
            assert hit.campaign_id in user_msg

    def test_model_and_max_tokens_passed_through(self):
        llm = FakeLLMClient(text="reply [RET_001].")
        retriever = FakeRetriever(FAKE_HITS)
        settings = Settings()
        answer(
            question="q",
            retriever=retriever,
            llm_client=llm,
            model="my-model",
            settings=settings,
        )
        _, model, max_tokens = llm.calls[0]
        assert model == "my-model"
        assert max_tokens == settings.llm_max_tokens


# ---------------------------------------------------------------------------
# Answered status
# ---------------------------------------------------------------------------


class TestAnsweredStatus:
    def test_valid_citation(self):
        result, _, _ = _answer("Brake issue [RET_002].")
        assert result.status == "answered"
        assert result.cited_ids == ["RET_002"]
        assert result.invalid_ids == []

    def test_multiple_ids_one_bracket(self):
        result, _, _ = _answer("Both [RET_001, RET_002].")
        assert result.status == "answered"
        assert result.cited_ids == ["RET_001", "RET_002"]

    def test_multiple_brackets(self):
        result, _, _ = _answer("[RET_001] and [RET_002].")
        assert result.status == "answered"
        assert result.cited_ids == ["RET_001", "RET_002"]


# ---------------------------------------------------------------------------
# Declined status
# ---------------------------------------------------------------------------


class TestDeclinedStatus:
    def test_decline_marker(self):
        result, _, _ = _answer(DECLINE_MARKER)
        assert result.status == "declined"
        assert result.cited_ids == []
        assert result.invalid_ids == []

    def test_decline_trailing_period(self):
        result, _, _ = _answer(DECLINE_MARKER + ".")
        assert result.status == "declined"


# ---------------------------------------------------------------------------
# Invalid status
# ---------------------------------------------------------------------------


class TestInvalidStatus:
    def test_lowercase_marker(self):
        result, _, _ = _answer("no_relevant_recall_found")
        assert result.status == "invalid"

    def test_marker_on_later_line(self):
        result, _, _ = _answer(f"Some text\n{DECLINE_MARKER}")
        assert result.status == "invalid"

    def test_no_citations_no_decline(self):
        result, _, _ = _answer("I don't know about that.")
        assert result.status == "invalid"

    def test_cited_id_not_in_retrieved(self):
        result, _, _ = _answer("Issue [99V999].")
        assert result.status == "invalid"
        assert "99V999" in result.invalid_ids

    def test_source_number_instead_of_id(self):
        result, _, _ = _answer("See [2].")
        assert result.status == "invalid"
        assert "2" in result.invalid_ids

    def test_decline_plus_citation(self):
        result, _, _ = _answer(f"{DECLINE_MARKER} [RET_001]")
        assert result.status == "invalid"

    def test_empty_output(self):
        result, _, _ = _answer("")
        assert result.status == "invalid"

    def test_finish_reason_length(self):
        result, _, _ = _answer("Brake issue [RET_001].", finish_reason="length")
        assert result.status == "invalid"

    def test_finish_reason_content_filter(self):
        result, _, _ = _answer("Brake issue [RET_001].", finish_reason="content_filter")
        assert result.status == "invalid"


# ---------------------------------------------------------------------------
# FakeRetriever recording
# ---------------------------------------------------------------------------


class TestFakeRetrieverRecording:
    def test_records_method(self):
        _, _, retriever = _answer("reply [RET_001].", mode="dense")
        assert retriever.calls[0][0] == "dense"

    def test_records_kwargs(self):
        vf = VehicleFilter(make="FORD", model="RANGER", year=2021)
        _, _, retriever = _answer("reply [RET_001].", k=5, vehicle_filter=vf)
        _, _, kwargs = retriever.calls[0]
        assert kwargs["k"] == 5
        assert kwargs["vehicle_filter"] == vf

    def test_chunker_not_passed_to_retriever(self):
        """Chunker is set on the Retriever at construction, not per-call."""
        _, _, retriever = _answer("reply [RET_001].")
        _, _, kwargs = retriever.calls[0]
        assert "chunker" not in kwargs


# ---------------------------------------------------------------------------
# Integration test
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestRAGIntegration:
    def _run(self, _test_db, seed_retrieval_data, chunker):
        """Run the full pipeline with the given chunker."""
        from psycopg_pool import ConnectionPool

        from recall_assistant.retrieval.retriever import Retriever

        embedder = seed_retrieval_data
        reranker = FakeReranker([0.5] * 5)
        pool = ConnectionPool(_test_db, min_size=1, max_size=2, open=True)

        try:
            retriever = Retriever(pool, embedder, reranker=reranker, chunker=chunker)
            llm = FakeLLMClient(text="Brake issue [RET_002].")
            result = answer(
                question="brake problem",
                retriever=retriever,
                llm_client=llm,
                model="test-model",
                mode="hybrid",
            )

            assert result.status == "answered"
            assert "RET_002" in result.cited_ids
            assert result.retrieval_latency_s > 0
            assert result.generation_latency_s > 0

            # Verify LLM received all retrieved campaign IDs.
            user_msg = llm.calls[0][0][1]["content"]
            for hit in result.retrieved_hits:
                assert hit.campaign_id in user_msg
        finally:
            pool.close()

    def test_end_to_end_record_chunker(self, _test_db, seed_retrieval_data):
        """Full pipeline with record chunker."""
        self._run(_test_db, seed_retrieval_data, chunker="record")

    def test_end_to_end_field_chunker(self, _test_db, seed_retrieval_data):
        """Full pipeline with field chunker."""
        self._run(_test_db, seed_retrieval_data, chunker="field")
