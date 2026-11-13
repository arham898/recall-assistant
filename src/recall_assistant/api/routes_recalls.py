"""GET /recalls/{campaign_id} — single recall lookup."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from recall_assistant.api.deps import require_api_key
from recall_assistant.api.schemas import RecallResponse, VehicleItem
from recall_assistant.ingest.loader import load_recall_rows_by_ids

router = APIRouter(dependencies=[Depends(require_api_key)])


@router.get("/recalls/{campaign_id}", response_model=RecallResponse)
def get_recall(campaign_id: str, request: Request) -> RecallResponse:
    """Return a single recall with its vehicles and components."""
    pool = request.app.state.pool
    with pool.connection() as conn:
        recalls, vehicles, components = load_recall_rows_by_ids(conn, {campaign_id})

    if campaign_id not in recalls:
        raise HTTPException(status_code=404, detail="Recall not found")

    row = recalls[campaign_id]
    vehs = vehicles.get(campaign_id, [])
    comps = components.get(campaign_id, [])

    return RecallResponse(
        campaign_id=campaign_id,
        manufacturer=row["manufacturer"],
        component=row["component"],
        defect_summary=row.get("defect_summary", ""),
        consequence_summary=row.get("consequence_summary", ""),
        corrective_action=row.get("corrective_action", ""),
        vehicles=[
            VehicleItem(make=make, model=model, year=year) for make, model, year in vehs
        ],
        components=comps,
    )
