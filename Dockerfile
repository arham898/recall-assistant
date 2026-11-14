# ---------- Stage 0: uv binary ----------
FROM ghcr.io/astral-sh/uv:0.13.8 AS uv

# ---------- Stage 1: build ----------
FROM python:3.12-slim AS builder

COPY --from=uv /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

# Install dependencies first (layer-cached when only source changes).
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Install the project package itself.
COPY src/ src/
COPY scripts/ scripts/
RUN uv sync --frozen --no-dev

# Download embedding + reranker models into the image.
ENV HF_HOME=/app/models
RUN uv run python scripts/download_models.py

# ---------- Stage 2: runtime ----------
FROM python:3.12-slim

RUN groupadd -r appuser && useradd -r -g appuser -d /app appuser

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/src /app/src
COPY --from=builder /app/scripts /app/scripts
COPY --from=builder --chown=appuser:appuser /app/models /app/models

ENV PATH="/app/.venv/bin:$PATH" \
    HF_HOME=/app/models \
    HF_HUB_OFFLINE=1 \
    PYTHONUNBUFFERED=1

USER appuser
EXPOSE 8000
CMD ["python", "scripts/serve.py"]
