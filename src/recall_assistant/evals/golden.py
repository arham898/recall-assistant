"""Schema for golden evaluation rows.

GoldenRow is the pydantic model used by the generator and validator
scripts.  Each row in the JSONL files must parse as a GoldenRow.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class ReferenceFact(BaseModel):
    """One fact backing a golden answer, tied to a specific campaign."""

    campaign_id: str
    field: Literal["defect_summary", "consequence_summary", "corrective_action"]
    text: str


class GoldenRow(BaseModel):
    """One row in the golden evaluation set."""

    id: str
    question: str
    source: Literal["generated", "handwritten", "unanswerable"]
    template: str | None = None
    component_group: str | None = None
    gold_campaign_ids: list[str]
    reference_answer: str
    reference_facts: list[ReferenceFact] = []
    make: str | None = None
    model: str | None = None
    year: int | None = None
    pass_rule: Literal["cite_any_gold", "cite_all_gold", "decline"]
    tags: list[str] = []
    notes: str | None = None
