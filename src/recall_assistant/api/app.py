"""FastAPI application factory with lifespan management."""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from fastapi import FastAPI

from recall_assistant import __version__
from recall_assistant.api.middleware import RequestIDMiddleware
from recall_assistant.api.routes_ask import router as ask_router
from recall_assistant.api.routes_health import router as health_router
from recall_assistant.api.routes_recalls import router as recalls_router
from recall_assistant.config import Settings

if TYPE_CHECKING:
    from recall_assistant.llm import LLMClient

log = logging.getLogger("recall_assistant.api")


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Build expensive objects at startup, close them on shutdown."""
    from psycopg_pool import ConnectionPool

    from recall_assistant.ingest.embedder import SentenceTransformerEmbedder
    from recall_assistant.llm.openrouter import OpenRouterClient
    from recall_assistant.retrieval.rerank import CrossEncoderReranker
    from recall_assistant.retrieval.retriever import Retriever

    settings: Settings = app.state.settings
    log.info("Starting recall-assistant API v%s", __version__)

    pool = ConnectionPool(
        settings.database_url,
        min_size=2,
        max_size=settings.db_pool_max,
        open=True,
    )
    app.state.pool = pool

    embedder = SentenceTransformerEmbedder(model_name=settings.embedding_model)
    app.state.embedder = embedder

    reranker = CrossEncoderReranker(model_name=settings.rerank_model)
    app.state.reranker = reranker

    llm_client = OpenRouterClient(
        api_key=settings.openrouter_api_key,
        temperature=settings.llm_temperature,
        request_timeout=settings.llm_request_timeout,
        max_retries=settings.llm_max_retries,
        max_retry_wait=settings.llm_max_retry_wait,
    )
    app.state.llm_client = llm_client

    app.state.retriever_factory = lambda chunker: Retriever(
        pool, embedder, reranker, chunker=chunker
    )

    log.info("Startup complete")
    yield

    log.info("Shutting down — closing connection pool")
    pool.close()


def create_app(
    settings: Settings | None = None,
    *,
    pool: object | None = None,
    embedder: object | None = None,
    reranker: object | None = None,
    llm_client: LLMClient | None = None,
    retriever_factory: Callable | None = None,
) -> FastAPI:
    """Build and return the FastAPI application.

    When all components are provided the production lifespan is skipped
    and the pre-built objects are stored on ``app.state`` directly.
    Tests use this path to inject fakes.
    """
    if settings is None:
        settings = Settings()

    if not settings.api_key:
        raise ValueError("API_KEY must be set")

    components_provided = all(
        x is not None for x in (pool, embedder, reranker, llm_client, retriever_factory)
    )

    if components_provided:
        # Test / pre-built path: skip lifespan entirely.
        @asynccontextmanager
        async def _noop_lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
            yield

        lifespan = _noop_lifespan
    else:
        lifespan = _lifespan

    app = FastAPI(
        title="Recall Assistant API",
        version=__version__,
        lifespan=lifespan,
    )
    app.state.settings = settings

    if components_provided:
        app.state.pool = pool
        app.state.embedder = embedder
        app.state.reranker = reranker
        app.state.llm_client = llm_client
        app.state.retriever_factory = retriever_factory

    # Ensure the app logger has a handler so structured log lines
    # reach the console under uvicorn (uvicorn only configures its
    # own "uvicorn" logger, not ours).
    api_logger = logging.getLogger("recall_assistant.api")
    if not api_logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter("%(levelname)s:     %(message)s"),
        )
        api_logger.addHandler(handler)
        api_logger.setLevel(logging.INFO)

    app.add_middleware(RequestIDMiddleware)

    app.include_router(health_router)
    app.include_router(ask_router)
    app.include_router(recalls_router)

    return app
