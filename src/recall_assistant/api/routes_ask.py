"""POST /ask — question answering with optional SSE streaming."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncGenerator

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.sse import EventSourceResponse
from starlette.concurrency import iterate_in_threadpool

from recall_assistant.api.deps import require_api_key
from recall_assistant.api.schemas import AskRequest, AskResponse, SourceItem
from recall_assistant.llm.openrouter import CreditLimitError
from recall_assistant.rag import answer as rag_answer
from recall_assistant.rag import build_prompt, classify, retrieve
from recall_assistant.retrieval import VehicleFilter
from recall_assistant.retrieval.retriever import Retriever

log = logging.getLogger("recall_assistant.api")

router = APIRouter(dependencies=[Depends(require_api_key)])


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _vehicle_filter(body: AskRequest) -> VehicleFilter | None:
    if body.make or body.model or body.year:
        return VehicleFilter(make=body.make, model=body.model, year=body.year)
    return None


def _build_retriever(request: Request, chunker: str) -> Retriever:
    return request.app.state.retriever_factory(chunker)


def _hits_to_sources(hits: list) -> list[SourceItem]:
    return [
        SourceItem(
            campaign_id=h.campaign_id,
            chunk_id=h.chunk_id,
            text=h.text,
            score=h.score,
            rank=h.rank,
        )
        for h in hits
    ]


def _log_completion(
    request: Request,
    status: str,
    prompt_tokens: int,
    completion_tokens: int,
    latency_ms: float,
) -> None:
    """Emit one structured log line per /ask request.

    *latency_ms* is wall-clock time from the start of retrieval through
    the end of LLM generation (retrieval + LLM, not just LLM).
    """
    request_id = getattr(request.state, "request_id", "?")
    log.info(
        "request_id=%s status=%s prompt_tokens=%d completion_tokens=%d latency_ms=%.1f",
        request_id,
        status,
        prompt_tokens,
        completion_tokens,
        latency_ms,
    )


# ---------------------------------------------------------------------------
# Non-streaming: call rag.answer() in a thread, return JSON
# ---------------------------------------------------------------------------


def _call_answer(request: Request, body: AskRequest):
    """Call the synchronous rag.answer(), return the AnswerResult."""
    settings = request.app.state.settings
    mode = body.mode or settings.default_mode
    chunker = body.chunker or settings.default_chunker
    retriever = _build_retriever(request, chunker)

    return rag_answer(
        question=body.question,
        retriever=retriever,
        llm_client=request.app.state.llm_client,
        model=settings.llm_model,
        mode=mode,
        k=settings.retrieval_k,
        vehicle_filter=_vehicle_filter(body),
        settings=settings,
    )


async def _json_response(body: AskRequest, request: Request) -> AskResponse:
    start = time.perf_counter()
    try:
        result = await asyncio.to_thread(_call_answer, request, body)
    except CreditLimitError:
        raise HTTPException(
            status_code=503,
            detail="LLM service temporarily unavailable",
        )

    _log_completion(
        request,
        result.status,
        result.usage.prompt_tokens,
        result.usage.completion_tokens,
        (time.perf_counter() - start) * 1000,
    )

    return AskResponse(
        status=result.status,
        answer=result.answer_text,
        cited_ids=result.cited_ids,
        sources=_hits_to_sources(result.retrieved_hits),
    )


# ---------------------------------------------------------------------------
# Streaming: retrieve, open LLM stream, yield SSE events
# ---------------------------------------------------------------------------


def _do_retrieve(request: Request, body: AskRequest):
    """Run rag.retrieve() synchronously — called via to_thread."""
    settings = request.app.state.settings
    mode = body.mode or settings.default_mode
    chunker = body.chunker or settings.default_chunker
    retriever = _build_retriever(request, chunker)
    return retrieve(
        body.question,
        retriever,
        mode,
        settings.retrieval_k,
        _vehicle_filter(body),
        settings,
    )


def _open_stream(request: Request, messages: list[dict]):
    """Open the LLM stream synchronously — called via to_thread."""
    settings = request.app.state.settings
    return request.app.state.llm_client.stream(
        messages, settings.llm_model, settings.llm_max_tokens
    )


def _sse_event(event_type: str, data: dict) -> str:
    """Format one SSE event as a wire-format string."""
    payload = json.dumps(data, ensure_ascii=False)
    return f"event: {event_type}\ndata: {payload}\n\n"


async def _sse_generator(
    stream_iter: object,
    hits: list,
    request: Request,
    start_time: float,
) -> AsyncGenerator[str, None]:
    """Yield SSE events: sources, tokens, done.

    Always emits exactly one ``done`` event, even if the stream fails.
    """
    sources_data = [
        {
            "campaign_id": h.campaign_id,
            "chunk_id": h.chunk_id,
            "text": h.text,
            "score": h.score,
            "rank": h.rank,
        }
        for h in hits
    ]
    yield _sse_event("sources", {"sources": sources_data})

    full_text = ""
    final_event = None

    try:
        async for event in iterate_in_threadpool(stream_iter):
            if event.token:
                full_text += event.token
                yield _sse_event("token", {"token": event.token})
            if event.finish_reason is not None:
                final_event = event
    except Exception:  # noqa: BLE001
        latency_ms = (time.perf_counter() - start_time) * 1000
        yield _sse_event(
            "done",
            {
                "status": "invalid",
                "cited_ids": [],
                "answer": full_text,
                "error": "LLM stream interrupted",
                "latency_ms": round(latency_ms, 1),
            },
        )
        _log_completion(request, "invalid", 0, 0, latency_ms)
        return

    if final_event is None:
        latency_ms = (time.perf_counter() - start_time) * 1000
        yield _sse_event(
            "done",
            {
                "status": "invalid",
                "cited_ids": [],
                "answer": full_text,
                "error": "LLM stream ended without finish signal",
                "latency_ms": round(latency_ms, 1),
            },
        )
        _log_completion(request, "invalid", 0, 0, latency_ms)
        return

    status, cited_ids, _invalid_ids = classify(
        full_text, hits, final_event.finish_reason
    )
    latency_ms = (time.perf_counter() - start_time) * 1000
    usage = final_event.usage
    pt = usage.prompt_tokens if usage else 0
    ct = usage.completion_tokens if usage else 0
    cost = usage.cost if usage else None
    yield _sse_event(
        "done",
        {
            "status": status,
            "cited_ids": cited_ids,
            "answer": full_text,
            "prompt_tokens": pt,
            "completion_tokens": ct,
            "cost": cost,
            "latency_ms": round(latency_ms, 1),
        },
    )
    _log_completion(request, status, pt, ct, latency_ms)


async def _stream_response(body: AskRequest, request: Request) -> EventSourceResponse:
    """Do all fallible work, then return EventSourceResponse."""
    start_time = time.perf_counter()

    # 1. Retrieve — sync, run in thread.
    hits, _retrieval_latency = await asyncio.to_thread(_do_retrieve, request, body)

    # 2. Build prompt — pure, cheap.
    messages = build_prompt(body.question, hits)

    # 3. Open the LLM stream — sync, run in thread.
    #    CreditLimitError / timeout / etc. raise here, before we return
    #    the EventSourceResponse, so the client sees a plain 503.
    stream_iter = await asyncio.to_thread(_open_stream, request, messages)

    return EventSourceResponse(_sse_generator(stream_iter, hits, request, start_time))


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------


@router.post("/ask")
async def ask(body: AskRequest, request: Request):
    """Answer a recall question.  JSON or SSE depending on body.stream."""
    if body.stream:
        try:
            return await _stream_response(body, request)
        except CreditLimitError:
            raise HTTPException(
                status_code=503,
                detail="LLM service temporarily unavailable",
            )
    return await _json_response(body, request)
