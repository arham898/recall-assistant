"""Tests for recall_assistant.ingest.chunkers."""

from __future__ import annotations

from typing import ClassVar

import pytest

from recall_assistant.ingest.chunkers import (
    _compact_vehicles,
    _component_text,
    _compress_year_list,
    _full_vehicles,
    chunk_fields,
    chunk_record,
)
from recall_assistant.ingest.models import Recall, RecallVehicle

# ---------------------------------------------------------------------------
# Fake tokenizer — splits on whitespace, provides offset mapping
# ---------------------------------------------------------------------------


class FakeTokenizer:
    """Word-level tokenizer for deterministic tests without model weights.

    Each whitespace-delimited word is one token.  __call__ returns
    character-level offset_mapping for each token so the chunker can
    slice the original string.  Matches the interface used by
    transformers.AutoTokenizer.__call__().
    """

    def encode(self, text: str, **_kwargs) -> list[int]:
        return list(range(len(text.split())))

    def __call__(
        self,
        text: str,
        *,
        return_offsets_mapping: bool = False,
        add_special_tokens: bool = True,
    ) -> dict:
        words = text.split()
        ids = list(range(len(words)))
        result: dict = {"input_ids": ids}
        if return_offsets_mapping:
            offsets: list[tuple[int, int]] = []
            pos = 0
            for word in words:
                start = text.index(word, pos)
                end = start + len(word)
                offsets.append((start, end))
                pos = end
            result["offset_mapping"] = offsets
        return result


@pytest.fixture
def tokenizer() -> FakeTokenizer:
    return FakeTokenizer()


# ---------------------------------------------------------------------------
# Fixture recalls
# ---------------------------------------------------------------------------


def _make_recall(
    campaign_id: str = "24V001",
    component: str = "ENGINE",
    components: list[str] | None = None,
    defect: str = "The engine may stall unexpectedly.",
    consequence: str = "Stalling increases crash risk.",
    remedy: str = "Dealers will update the software.",
    vehicles: list[RecallVehicle] | None = None,
) -> Recall:
    if vehicles is None:
        vehicles = [
            RecallVehicle(make="TOYOTA", model="CAMRY", year=2020),
            RecallVehicle(make="TOYOTA", model="COROLLA", year=2021),
        ]
    if components is None:
        components = [component]
    return Recall(
        campaign_id=campaign_id,
        manufacturer="TOYOTA MOTOR",
        manufacturer_campaign_number="ABC123",
        component=component,
        recall_type="V",
        potential_units_affected=50000,
        defect_summary=defect,
        consequence_summary=consequence,
        corrective_action=remedy,
        notes="",
        report_received_date=None,
        record_creation_date=None,
        manufacturing_begin_date=None,
        manufacturing_end_date=None,
        owner_notification_date=None,
        influenced_by="",
        do_not_drive=False,
        park_outside=False,
        vehicles=vehicles,
        components=components,
    )


# ---------------------------------------------------------------------------
# Helper tests
# ---------------------------------------------------------------------------


class TestCompressYearList:
    def test_consecutive(self):
        assert _compress_year_list([2018, 2019, 2020]) == "2018-2020"

    def test_gaps(self):
        assert _compress_year_list([2018, 2020, 2022]) == "2018, 2020, 2022"

    def test_mixed(self):
        assert _compress_year_list([2018, 2019, 2020, 2022]) == "2018-2020, 2022"

    def test_single(self):
        assert _compress_year_list([2020]) == "2020"

    def test_empty(self):
        assert _compress_year_list([]) == ""


class TestCompactVehicles:
    def test_few_vehicles(self):
        recall = _make_recall()
        result = _compact_vehicles(recall)
        assert "TOYOTA CAMRY" in result
        assert "TOYOTA COROLLA" in result

    def test_many_vehicles_capped(self):
        vehicles = [
            RecallVehicle(make=f"MAKE{i}", model=f"MODEL{i}", year=2020)
            for i in range(8)
        ]
        recall = _make_recall(vehicles=vehicles)
        result = _compact_vehicles(recall)
        assert "and 3 more" in result

    def test_deduplicates(self):
        vehicles = [
            RecallVehicle(make="TOYOTA", model="CAMRY", year=2020),
            RecallVehicle(make="TOYOTA", model="CAMRY", year=2021),
        ]
        recall = _make_recall(vehicles=vehicles)
        result = _compact_vehicles(recall)
        # Only one "TOYOTA CAMRY" since it's distinct make+model
        assert result == "TOYOTA CAMRY"


class TestFullVehicles:
    def test_year_ranges(self):
        vehicles = [
            RecallVehicle(make="FORD", model="F-150", year=2018),
            RecallVehicle(make="FORD", model="F-150", year=2019),
            RecallVehicle(make="FORD", model="F-150", year=2020),
            RecallVehicle(make="FORD", model="F-150", year=2022),
        ]
        recall = _make_recall(vehicles=vehicles)
        result = _full_vehicles(recall)
        assert "2018-2020, 2022" in result

    def test_unknown_year(self):
        vehicles = [RecallVehicle(make="GRACO", model="SEAT", year=None)]
        recall = _make_recall(vehicles=vehicles)
        result = _full_vehicles(recall)
        assert "Unknown" in result


# ---------------------------------------------------------------------------
# Record chunker
# ---------------------------------------------------------------------------


class TestChunkRecord:
    def test_single_chunk_structure(self, tokenizer):
        recall = _make_recall()
        chunks = chunk_record(recall, tokenizer)

        assert len(chunks) == 1
        c = chunks[0]
        assert c.campaign_id == "24V001"
        assert c.chunker == "record"
        assert c.field_name == "all:0"

    def test_vehicles_before_text(self, tokenizer):
        """Vehicles and component appear before defect/consequence/remedy."""
        recall = _make_recall()
        chunks = chunk_record(recall, tokenizer)
        body = chunks[0].body
        vehicles_pos = body.index("Vehicles:")
        component_pos = body.index("Component:")
        defect_pos = body.index("Defect:")
        assert vehicles_pos < component_pos < defect_pos

    def test_all_fields_present(self, tokenizer):
        recall = _make_recall()
        body = chunk_record(recall, tokenizer)[0].body
        assert "ENGINE" in body
        assert "engine may stall" in body
        assert "crash risk" in body
        assert "update the software" in body
        assert "TOYOTA CAMRY" in body
        assert "TOYOTA COROLLA" in body

    def test_splits_long_text(self, tokenizer):
        """A recall with text exceeding max_tokens produces multiple windows."""
        # With FakeTokenizer, each word = 1 token.
        # max_tokens=20, overlap=5 → should split.
        long_defect = " ".join(f"word{i}" for i in range(50))
        recall = _make_recall(defect=long_defect)
        chunks = chunk_record(recall, tokenizer, max_tokens=20, overlap=5)

        assert len(chunks) > 1
        # Each chunk should have numbered field_name
        for i, c in enumerate(chunks):
            assert c.field_name == f"all:{i}"
        # Every window starts with the vehicles/component prefix
        for c in chunks:
            assert c.body.startswith("Vehicles:")

    def test_split_windows_overlap(self, tokenizer):
        """Adjacent windows share content (overlap)."""
        long_defect = " ".join(f"word{i}" for i in range(50))
        recall = _make_recall(consequence="", remedy="", defect=long_defect)
        chunks = chunk_record(recall, tokenizer, max_tokens=20, overlap=5)

        if len(chunks) >= 2:
            # The end of window 0 should overlap with the start of window 1
            # (after the prefix)
            words_0 = set(chunks[0].body.split())
            words_1 = set(chunks[1].body.split())
            overlap_words = words_0 & words_1
            # Must share more than just the prefix words
            prefix_words = set(chunks[0].body.split("\n")[0].split())
            non_prefix_overlap = overlap_words - prefix_words
            assert len(non_prefix_overlap) > 0


# ---------------------------------------------------------------------------
# Field chunker
# ---------------------------------------------------------------------------


class TestChunkFields:
    def test_three_fields(self, tokenizer):
        recall = _make_recall()
        chunks = chunk_fields(recall, tokenizer)

        assert len(chunks) == 3
        field_names = [c.field_name for c in chunks]
        assert "defect_summary:0" in field_names
        assert "consequence_summary:0" in field_names
        assert "corrective_action:0" in field_names

    def test_skips_empty_field(self, tokenizer):
        recall = _make_recall(consequence="")
        chunks = chunk_fields(recall, tokenizer)

        field_names = [c.field_name for c in chunks]
        assert len(chunks) == 2
        assert not any("consequence" in fn for fn in field_names)

    def test_prefix_contains_context(self, tokenizer):
        recall = _make_recall()
        chunks = chunk_fields(recall, tokenizer)

        for c in chunks:
            assert "TOYOTA" in c.body
            assert "ENGINE" in c.body

    def test_all_chunks_have_field_chunker(self, tokenizer):
        recall = _make_recall()
        for c in chunk_fields(recall, tokenizer):
            assert c.chunker == "field"
            assert c.campaign_id == "24V001"

    def test_splits_long_field(self, tokenizer):
        long_defect = " ".join(f"word{i}" for i in range(50))
        recall = _make_recall(defect=long_defect)
        chunks = chunk_fields(recall, tokenizer, max_tokens=20, overlap=5)

        defect_chunks = [c for c in chunks if "defect_summary" in c.field_name]
        assert len(defect_chunks) > 1
        assert defect_chunks[0].field_name == "defect_summary:0"
        assert defect_chunks[1].field_name == "defect_summary:1"


# ---------------------------------------------------------------------------
# Exact body tests (single-component baseline)
# ---------------------------------------------------------------------------


class TestExactBody:
    """Assert full card text for the default single-component fixture.

    These tests catch any accidental change to card format; the expected
    strings are hardcoded, not built by helper functions.
    """

    def test_single_component_record_exact_body(self, tokenizer):
        recall = _make_recall()
        body = chunk_record(recall, tokenizer)[0].body
        assert body == (
            "Vehicles: TOYOTA CAMRY (2020), TOYOTA COROLLA (2021)\n"
            "Component: ENGINE\n"
            "Defect: The engine may stall unexpectedly.\n"
            "Consequence: Stalling increases crash risk.\n"
            "Remedy: Dealers will update the software."
        )

    def test_single_component_field_exact_body(self, tokenizer):
        recall = _make_recall()
        chunks = chunk_fields(recall, tokenizer)
        defect_chunk = next(c for c in chunks if c.field_name == "defect_summary:0")
        assert defect_chunk.body == (
            "TOYOTA CAMRY, TOYOTA COROLLA | ENGINE — The engine may stall unexpectedly."
        )


# ---------------------------------------------------------------------------
# Component text helper
# ---------------------------------------------------------------------------


class TestComponentText:
    def test_multi_components_joined_sorted_deduped(self):
        """Components are sorted, de-duped, joined by ' | '."""
        recall = _make_recall(
            component="SEAT BELTS:PRETENSIONER",
            components=[
                "SEAT BELTS:PRETENSIONER",
                "AIR BAGS:FRONTAL:DRIVER SIDE:INFLATOR MODULE",
                "AIR BAGS:SIDE/WINDOW",
                "AIR BAGS:FRONTAL:PASSENGER SIDE:INFLATOR MODULE",
                "AIR BAGS:SIDE/WINDOW",  # duplicate
            ],
        )
        assert _component_text(recall) == (
            "AIR BAGS:FRONTAL:DRIVER SIDE:INFLATOR MODULE | "
            "AIR BAGS:FRONTAL:PASSENGER SIDE:INFLATOR MODULE | "
            "AIR BAGS:SIDE/WINDOW | "
            "SEAT BELTS:PRETENSIONER"
        )

    def test_single_component(self):
        recall = _make_recall(component="ENGINE", components=["ENGINE"])
        assert _component_text(recall) == "ENGINE"

    def test_empty_components_raises(self):
        recall = _make_recall(component="ENGINE", components=[])
        with pytest.raises(ValueError, match="no components"):
            _component_text(recall)


# ---------------------------------------------------------------------------
# Multi-component chunks
# ---------------------------------------------------------------------------


class TestMultiComponentChunks:
    """Multi-component recalls list all components on every card."""

    MULTI_COMPONENTS: ClassVar[list[str]] = [
        "SEAT BELTS:PRETENSIONER",
        "AIR BAGS:FRONTAL",
    ]

    def test_record_chunker_lists_all_components(self, tokenizer):
        recall = _make_recall(
            component="SEAT BELTS:PRETENSIONER",
            components=self.MULTI_COMPONENTS,
        )
        body = chunk_record(recall, tokenizer)[0].body
        assert "AIR BAGS:FRONTAL | SEAT BELTS:PRETENSIONER" in body

    def test_field_chunker_lists_all_components(self, tokenizer):
        recall = _make_recall(
            component="SEAT BELTS:PRETENSIONER",
            components=self.MULTI_COMPONENTS,
        )
        for c in chunk_fields(recall, tokenizer):
            assert "AIR BAGS:FRONTAL | SEAT BELTS:PRETENSIONER" in c.body

    def test_components_appear_in_order(self, tokenizer):
        """Components are sorted regardless of input order."""
        recall = _make_recall(
            component="SEAT BELTS:PRETENSIONER",
            components=[
                "SEAT BELTS:PRETENSIONER",
                "ENGINE",
                "AIR BAGS:FRONTAL",
            ],
        )
        body = chunk_record(recall, tokenizer)[0].body
        pos_airbags = body.index("AIR BAGS:FRONTAL")
        pos_engine = body.index("ENGINE")
        pos_seatbelts = body.index("SEAT BELTS:PRETENSIONER")
        assert pos_airbags < pos_engine < pos_seatbelts


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_prefix_too_long_raises(self, tokenizer):
        """If the vehicle prefix is longer than max_tokens - overlap,
        the chunker should raise ValueError."""
        vehicles = [
            RecallVehicle(make=f"LONGMAKENAME{i}", model=f"LONGMODEL{i}", year=2020)
            for i in range(20)
        ]
        recall = _make_recall(vehicles=vehicles)
        with pytest.raises(ValueError, match="Prefix"):
            chunk_record(recall, tokenizer, max_tokens=10, overlap=5)

    def test_no_vehicles(self, tokenizer):
        recall = _make_recall(vehicles=[])
        chunks = chunk_record(recall, tokenizer)
        assert len(chunks) >= 1
        assert "Vehicles:" in chunks[0].body

    def test_all_empty_fields_still_produces_no_field_chunks(self, tokenizer):
        recall = _make_recall(defect="", consequence="", remedy="")
        chunks = chunk_fields(recall, tokenizer)
        assert chunks == []


# ---------------------------------------------------------------------------
# Real tokenizer tests (require model download, marked slow)
# ---------------------------------------------------------------------------


@pytest.mark.slow
class TestWithRealTokenizer:
    """Tests that run _split_windows with the actual bge-small-en-v1.5
    BertTokenizer to catch interface mismatches that the FakeTokenizer
    cannot detect.

    Marked slow because the first run downloads ~30 MB of tokenizer
    files.  CI should run these with: uv run pytest -m slow
    """

    @pytest.fixture
    def real_tokenizer(self):
        from transformers import AutoTokenizer

        return AutoTokenizer.from_pretrained("BAAI/bge-small-en-v1.5")

    def test_windows_respect_max_tokens(self, real_tokenizer):
        """Every window, including the prefix, fits within max_tokens."""
        from recall_assistant.ingest.chunkers import _split_windows

        prefix = "TOYOTA CAMRY, FORD F-150 | ENGINE\n"
        # ~800 words → well over 400 tokens for BERT WordPiece
        text = (
            "The Engine Control Module (ECM) software may cause the "
            "fuel injectors to remain open longer than intended, "
            "resulting in excess fuel entering the combustion chambers. "
        ) * 20

        windows = _split_windows(
            prefix, text, real_tokenizer, max_tokens=400, overlap=50
        )

        assert len(windows) > 1
        for window in windows:
            n_tokens = len(real_tokenizer.encode(window))
            assert n_tokens <= 400, f"Window has {n_tokens} tokens, exceeds 400"

    def test_consecutive_windows_overlap(self, real_tokenizer):
        """Adjacent windows share content from the overlap region."""
        from recall_assistant.ingest.chunkers import _split_windows

        prefix = "HONDA CIVIC | BRAKES — "
        text = " ".join(f"word{i}" for i in range(500))

        windows = _split_windows(
            prefix, text, real_tokenizer, max_tokens=100, overlap=20
        )

        assert len(windows) >= 3
        for i in range(len(windows) - 1):
            # Strip the shared prefix and check text overlap
            body_a = windows[i][len(prefix) :]
            body_b = windows[i + 1][len(prefix) :]
            # The end of window A should share words with the start of B
            words_a = body_a.split()
            words_b = body_b.split()
            tail_a = set(words_a[-25:])  # generous tail
            head_b = set(words_b[:25])  # generous head
            shared = tail_a & head_b
            assert len(shared) > 0, f"Windows {i} and {i + 1} have no overlap"

    def test_capitals_preserved(self, real_tokenizer):
        """Offset-based slicing preserves original casing (not lowercased
        by the tokenizer's decode path)."""
        from recall_assistant.ingest.chunkers import _split_windows

        prefix = "TOYOTA Camry | ENGINE — "
        text = "The ECM Software May CAUSE Fuel Injectors to FAIL. " * 30

        windows = _split_windows(
            prefix, text, real_tokenizer, max_tokens=100, overlap=20
        )

        for window in windows:
            assert window.startswith("TOYOTA Camry")
            # Check that mixed-case words survive in the text body
            body = window[len(prefix) :]
            if "ECM" in text[: len(body) + 100]:
                assert "ECM" in window  # uppercase acronym preserved
            if "CAUSE" in text[: len(body) + 100]:
                assert "CAUSE" in window  # uppercase word preserved

    def test_no_mid_word_splits(self, real_tokenizer):
        """Windows must not start or end mid-word (e.g. WEBASTO'S)."""
        from recall_assistant.ingest.chunkers import _split_windows

        prefix = "Vehicles: WEBASTO (Unknown)\nComponent: SUNROOF\n"
        text = (
            "WEBASTO'S WEBSITE AT HTTP://WWW.SUNROOFCHECK.COM FOR MORE "
            "INFORMATION ABOUT THE RECALL AND REPLACEMENT PROCEDURES. "
        ) * 15

        windows = _split_windows(
            prefix, text, real_tokenizer, max_tokens=100, overlap=20
        )

        assert len(windows) > 1
        for i, window in enumerate(windows):
            body = window[len(prefix) :]
            if i > 0 and body:
                # Non-first window: must not start mid-word
                # (the char before our slice in the original text should be
                # whitespace, or we should start at position 0)
                start_in_text = text.index(body[:20])
                if start_in_text > 0:
                    assert text[start_in_text - 1].isspace(), (
                        f"Window {i} starts mid-word: "
                        f"...{text[start_in_text - 5 : start_in_text + 15]}..."
                    )

    def test_long_url_does_not_loop(self, real_tokenizer):
        """A single word longer than the window capacity is cut at the
        token boundary rather than causing an infinite loop."""
        from recall_assistant.ingest.chunkers import _split_windows

        prefix = "P — "
        long_url = "HTTP://WWW." + "X" * 300 + ".COM"
        text = f"Visit {long_url} for details."

        windows = _split_windows(
            prefix, text, real_tokenizer, max_tokens=100, overlap=20
        )

        assert len(windows) >= 1
        for window in windows:
            n_tokens = len(real_tokenizer.encode(window))
            assert n_tokens <= 100, f"Window has {n_tokens} tokens, exceeds 100"

    def test_window_starts_strictly_increase(self, real_tokenizer):
        """The start offset of each window must strictly increase."""
        from recall_assistant.ingest.chunkers import _split_windows

        prefix = "HONDA CIVIC | BRAKES — "
        text = " ".join(f"word{i}" for i in range(500))

        windows = _split_windows(
            prefix, text, real_tokenizer, max_tokens=100, overlap=20
        )

        starts = []
        for window in windows:
            body = window[len(prefix) :]
            start = text.find(body[:30])
            starts.append(start)

        for i in range(1, len(starts)):
            assert starts[i] > starts[i - 1], (
                f"Window {i} start ({starts[i]}) does not advance past "
                f"window {i - 1} start ({starts[i - 1]})"
            )

    def test_full_text_coverage(self, real_tokenizer):
        """Every character of the original text must fall inside at least
        one window."""
        from recall_assistant.ingest.chunkers import _split_windows

        prefix = "FORD F-150 | ENGINE — "
        text = " ".join(f"word{i}" for i in range(500))

        windows = _split_windows(
            prefix, text, real_tokenizer, max_tokens=100, overlap=20
        )

        covered = set()
        for window in windows:
            body = window[len(prefix) :]
            start = text.find(body)
            if start >= 0:
                covered.update(range(start, start + len(body)))

        # Every non-whitespace character should be covered
        for i, ch in enumerate(text):
            if not ch.isspace():
                assert i in covered, (
                    f"Character at position {i} ({ch!r}) not covered by any window"
                )
