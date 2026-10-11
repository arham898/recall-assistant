# recall-assistant

A personal learning project that builds a question-answering service over public NHTSA vehicle recall data. The system is constructed in layers — starting with data ingestion and retrieval, progressing through evaluation, API deployment, and agent tooling — to explore the full stack of a RAG application on real-world public data.

## Data source and licence

This project uses vehicle recall data published by the [National Highway Traffic Safety Administration (NHTSA)](https://www.nhtsa.gov/recalls). NHTSA data is produced by a US federal agency and is in the **public domain** (not subject to copyright under 17 U.S.C. § 105).

## Architecture

<!-- TODO: add architecture diagram and description after layer 1 -->

_Placeholder — will be filled in as layers are built._

## Layers

- [ ] Layer 1: Hybrid retrieval (ingestion, embedding, BM25 + vector search, reranking)
- [ ] Layer 2: Evals (golden question set, retrieval and answer-quality metrics)
- [ ] Layer 3: API and deployment (FastAPI service, Docker Compose, CI)
- [ ] Layer 4: Tracing (OpenTelemetry spans, cost and latency tracking)
- [ ] Layer 5: Agent (tool-using agent with retrieval and SQL tools)
- [ ] Layer 6: MCP server (expose retrieval as an MCP tool)
- [ ] Layer 7: Claude SDK (swap LLM backend to Anthropic SDK)

## How to run

```bash
# Install dependencies
uv sync

# Start Postgres (once Docker is running)
docker compose up -d db

# Run fast tests (no database needed)
uv run pytest -m "not integration"

# Run all tests
uv run pytest

# Lint and format check
uv run ruff check . && uv run ruff format --check .
```

## Results

| Metric | Value | Command |
|--------|-------|---------|
| _TBD_  | _TBD_ | _TBD_   |

## Limitations

- This is a learning project, not a production system.
- Only covers NHTSA recall data; does not include other vehicle safety datasets.
- LLM answers are not guaranteed to be accurate — always verify against the source data.
- No authentication or access control is implemented.
