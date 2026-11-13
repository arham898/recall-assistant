"""GET /health — open endpoint, no auth required."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request

from recall_assistant import __version__
from recall_assistant.api.schemas import HealthResponse
from recall_assistant.rag import prompt_hash

log = logging.getLogger("recall_assistant.api")

router = APIRouter()


@router.get("/health", response_model=HealthResponse)
def health(request: Request) -> HealthResponse:
    """Check database connectivity and report model configuration."""
    settings = request.app.state.settings
    pool = request.app.state.pool

    db_ok = False
    try:
        with pool.connection() as conn:
            conn.execute("SELECT 1")
        db_ok = True
    except Exception:
        log.debug("Health check DB probe failed", exc_info=True)

    return HealthResponse(
        status="ok" if db_ok else "degraded",
        version=__version__,
        db_reachable=db_ok,
        embedding_model=settings.embedding_model,
        rerank_model=settings.rerank_model,
        llm_model=settings.llm_model,
        prompt_hash=prompt_hash(),
    )
