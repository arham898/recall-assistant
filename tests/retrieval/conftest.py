"""Fixtures for retrieval tests.

RecordingEmbedder — wraps FakeEmbedder and records every call so tests
can assert that embed_query applies the BGE prefix.

LookupEmbedder — maps specific texts to chosen vectors so tests can
control which cards rank where in dense search.
"""

from __future__ import annotations

from tests.fakes import FakeEmbedder

# ---------------------------------------------------------------------------
# Recording embedder
# ---------------------------------------------------------------------------


class RecordingEmbedder:
    """Wraps FakeEmbedder and records every embed() call."""

    def __init__(self) -> None:
        self._inner = FakeEmbedder()
        self.calls: list[list[str]] = []

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return self._inner.embed(texts)


class LookupEmbedder:
    """Maps specific texts to chosen vectors; falls back to FakeEmbedder."""

    def __init__(self, mapping: dict[str, list[float]]) -> None:
        self._mapping = mapping
        self._fallback = FakeEmbedder()

    def embed(self, texts: list[str]) -> list[list[float]]:
        result = []
        for t in texts:
            if t in self._mapping:
                result.append(self._mapping[t])
            else:
                result.append(self._fallback.embed([t])[0])
        return result
