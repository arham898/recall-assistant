"""RAG pipeline: retrieve sources, prompt the LLM, parse citations, classify.

answer() is the main entry point.  It returns an AnswerResult with a
status of "answered", "declined" or "invalid", along with the cited
campaign IDs, the full prompt, token usage and latencies.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from recall_assistant.config import Settings
from recall_assistant.llm import LLMResult
from recall_assistant.retrieval import Hit, VehicleFilter

if TYPE_CHECKING:
    from recall_assistant.llm import LLMClient

DECLINE_MARKER = "NO_RELEVANT_RECALL_FOUND"

_CITATION_BRACKET = re.compile(r"\[([^\]]+)\]")

_SYSTEM_PROMPT = """\
You are a vehicle-recall assistant.  Answer the user's question using \
only the numbered sources below.  Each source header gives its campaign \
ID, a code of two digits, the letter V and six digits.  Whenever you \
use a source, write its campaign ID in square brackets, with nothing \
else inside the brackets.  Cite the campaign ID, never the source \
number.  When citing more than one recall, put all the IDs in one \
bracket, separated by commas.

If the sources do not contain information that answers the question, \
respond with exactly:
NO_RELEVANT_RECALL_FOUND

Do not add any other text if the sources cannot answer the question.\
"""


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AnswerResult:
    """Full output of a single answer() call."""

    status: Literal["answered", "declined", "invalid"]
    answer_text: str
    cited_ids: list[str]
    invalid_ids: list[str]
    retrieved_hits: list[Hit]
    prompt_messages: list[dict]
    usage: LLMResult
    retrieval_latency_s: float
    generation_latency_s: float


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _retrieve(
    question: str,
    retriever: object,
    mode: str,
    k: int,
    vehicle_filter: VehicleFilter | None,
    settings: Settings,
) -> tuple[list[Hit], float]:
    """Dispatch to the correct retriever method, timed."""
    method = getattr(retriever, mode)
    kwargs: dict = {"k": k}
    if vehicle_filter is not None:
        kwargs["vehicle_filter"] = vehicle_filter
    if mode in ("dense", "hybrid", "rerank"):
        kwargs["pool_size"] = settings.pool_size
        kwargs["ef_search"] = settings.ef_search
    if mode in ("hybrid", "rerank"):
        kwargs["rrf_k"] = settings.rrf_k
    if mode == "rerank":
        kwargs["rerank_candidates"] = settings.rerank_candidates

    start = time.perf_counter()
    hits: list[Hit] = method(question, **kwargs)
    latency = time.perf_counter() - start
    return hits, latency


def _build_messages(question: str, hits: list[Hit]) -> list[dict]:
    """Build the chat messages: system prompt + numbered sources + question."""
    source_lines = []
    for i, hit in enumerate(hits, 1):
        source_lines.append(f"Source {i} (campaign {hit.campaign_id}): {hit.text}")

    sources_block = "\n\n".join(source_lines)
    user_content = f"{sources_block}\n\nQuestion: {question}"

    return [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]


def _parse_citations(text: str) -> list[str]:
    """Extract all tokens from square brackets in the response."""
    tokens: list[str] = []
    for match in _CITATION_BRACKET.finditer(text):
        inner = match.group(1)
        for part in inner.split(","):
            stripped = part.strip()
            if stripped:
                tokens.append(stripped)
    return tokens


def _classify(
    text: str,
    cited_tokens: list[str],
    retrieved_ids: set[str],
    finish_reason: str,
) -> tuple[Literal["answered", "declined", "invalid"], list[str], list[str]]:
    """Determine answer status.

    Returns (status, valid_cited_ids, invalid_ids).
    """
    # Empty or cut-off output is always invalid.
    if not text or finish_reason != "stop":
        return ("invalid", [], [])

    # Decline: whole response (stripped) is the marker, optionally + ".".
    stripped = text.strip()
    is_whole_decline = stripped in (DECLINE_MARKER, DECLINE_MARKER + ".")

    # Marker present anywhere but not the entire response → invalid.
    marker_present = DECLINE_MARKER in text

    # Partition cited tokens into valid and invalid.
    valid: list[str] = []
    invalid: list[str] = []
    for token in cited_tokens:
        if token.isdigit():
            invalid.append(token)
        elif token in retrieved_ids:
            valid.append(token)
        else:
            invalid.append(token)

    has_citations = bool(valid) or bool(invalid)

    if is_whole_decline and not has_citations:
        return ("declined", [], [])

    # Marker present but not the whole response, or marker + citations.
    if marker_present:
        return ("invalid", valid, invalid)

    if invalid:
        return ("invalid", valid, invalid)

    if valid:
        return ("answered", valid, [])

    # No citations and no decline marker.
    return ("invalid", [], [])


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def answer(
    question: str,
    retriever: object,
    llm_client: LLMClient,
    model: str,
    mode: Literal["dense", "keyword", "hybrid", "rerank"] = "hybrid",
    k: int = 10,
    vehicle_filter: VehicleFilter | None = None,
    settings: Settings | None = None,
) -> AnswerResult:
    """Retrieve sources, prompt the LLM, parse citations, classify.

    The chunker is set on the Retriever at construction time, not here.
    """
    settings = settings or Settings()

    # 1. Retrieve
    hits, retrieval_latency = _retrieve(
        question, retriever, mode, k, vehicle_filter, settings
    )

    # 2. Build prompt
    messages = _build_messages(question, hits)

    # 3. Generate (latency includes any retry waits inside the client)
    start = time.perf_counter()
    llm_result = llm_client.generate(
        messages, model=model, max_tokens=settings.llm_max_tokens
    )
    generation_latency = time.perf_counter() - start

    # 4. Parse and classify
    cited_tokens = _parse_citations(llm_result.text)
    retrieved_ids = {h.campaign_id for h in hits}
    status, valid_ids, invalid_ids = _classify(
        llm_result.text, cited_tokens, retrieved_ids, llm_result.finish_reason
    )

    return AnswerResult(
        status=status,
        answer_text=llm_result.text,
        cited_ids=valid_ids,
        invalid_ids=invalid_ids,
        retrieved_hits=hits,
        prompt_messages=messages,
        usage=llm_result,
        retrieval_latency_s=retrieval_latency,
        generation_latency_s=generation_latency,
    )
