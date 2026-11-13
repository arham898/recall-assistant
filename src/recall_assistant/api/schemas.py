"""Request and response schemas for the recall-assistant API."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# --- POST /ask ---


class AskRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=2000)
    make: str | None = None
    model: str | None = None
    year: int | None = None
    mode: Literal["dense", "keyword", "hybrid", "rerank"] | None = None
    chunker: Literal["record", "field"] | None = None
    stream: bool = False


class SourceItem(BaseModel):
    campaign_id: str
    chunk_id: int
    text: str
    score: float
    rank: int


class AskResponse(BaseModel):
    status: Literal["answered", "declined", "invalid"]
    answer: str
    cited_ids: list[str]
    sources: list[SourceItem]


# --- GET /health ---


class HealthResponse(BaseModel):
    status: str
    version: str
    db_reachable: bool
    embedding_model: str
    rerank_model: str
    llm_model: str
    prompt_hash: str


# --- GET /recalls/{campaign_id} ---


class VehicleItem(BaseModel):
    make: str
    model: str
    year: int | None


class RecallResponse(BaseModel):
    campaign_id: str
    manufacturer: str
    component: str
    defect_summary: str
    consequence_summary: str
    corrective_action: str
    vehicles: list[VehicleItem]
    components: list[str]
