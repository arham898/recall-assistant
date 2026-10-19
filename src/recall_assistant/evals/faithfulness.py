"""Faithfulness scoring via HHEM-2.1-Open.

``strip_citations`` and ``build_premise`` are pure helpers.
``HHEMScorer`` lazy-loads the model on first call to ``score()``.
"""

from __future__ import annotations

import re

REVISION = "de9a5e6ba9ba49531a022dba17e35ffeb2a6fad8"
MODEL_NAME = "vectara/hallucination_evaluation_model"

_CITATION_GROUP = re.compile(r"\s*\[[^\]]*\]")
_DOUBLE_SPACE = re.compile(r"  +")


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def strip_citations(text: str) -> str:
    """Remove every bracketed citation group and collapse double spaces."""
    stripped = _CITATION_GROUP.sub("", text)
    return _DOUBLE_SPACE.sub(" ", stripped)


def build_premise(retrieved_texts: list[str]) -> str:
    """Join retrieved source texts with two newlines."""
    return "\n\n".join(retrieved_texts)


# ---------------------------------------------------------------------------
# HHEM scorer (lazy load)
# ---------------------------------------------------------------------------


class HHEMScorer:
    """Scores (premise, hypothesis) pairs with HHEM-2.1-Open.

    The model is loaded on the first call to ``score()``, never at
    import time.  ``trust_remote_code=True`` is required by the model.
    """

    def __init__(self) -> None:
        self._model = None

    def score(self, premise: str, hypothesis: str) -> float:
        """Return P(consistent) in [0, 1]."""
        if self._model is None:
            self._load()
        scores = self._model.predict([(premise, hypothesis)])
        return scores[0].item()

    def _load(self) -> None:
        from transformers import AutoModelForSequenceClassification

        print(f"Loading {MODEL_NAME} (revision {REVISION})...")
        self._model = AutoModelForSequenceClassification.from_pretrained(
            MODEL_NAME,
            trust_remote_code=True,
            revision=REVISION,
        )
        self._model.eval()

    def __call__(self, premise: str, hypothesis: str) -> float:
        return self.score(premise, hypothesis)
