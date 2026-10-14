"""Load Recall records into Postgres with upsert semantics."""

import logging

import psycopg

from recall_assistant.ingest.models import Recall

logger = logging.getLogger(__name__)


class MassDeleteBlocked(Exception):
    """Raised when a delete would remove more than the safety threshold."""


def check_mass_delete(
    to_remove: int,
    total_existing: int,
    threshold: float = 0.10,
) -> bool:
    """Return True if the delete should be blocked.

    Pure function so it can be tested without a database.
    """
    if to_remove <= 0 or total_existing <= 0:
        return False
    return to_remove / total_existing > threshold


def upsert_recalls(
    conn: psycopg.Connection,
    recalls: list[Recall],
    *,
    allow_mass_delete: bool = False,
    mass_delete_threshold: float = 0.10,
) -> tuple[int, int]:
    """Upsert recalls and their vehicles, then delete stale campaigns.

    Uses ON CONFLICT DO UPDATE so re-running is safe and picks up
    any changes in the source data.

    Campaigns in the database that are not in the new set are deleted.
    ON DELETE CASCADE on recall_vehicles and chunks handles cleanup.

    Raises MassDeleteBlocked if the delete would remove more than
    mass_delete_threshold (default 10%) of existing recalls, unless
    allow_mass_delete is True.

    Returns (upserted_count, removed_count).
    """
    upserted = 0
    new_campaign_ids = [r.campaign_id for r in recalls]

    with conn.cursor() as cur:
        for recall in recalls:
            cur.execute(
                """
                INSERT INTO recalls (
                    campaign_id, manufacturer, manufacturer_campaign_number,
                    component, recall_type, potential_units_affected,
                    defect_summary, consequence_summary, corrective_action, notes,
                    report_received_date, record_creation_date,
                    manufacturing_begin_date, manufacturing_end_date,
                    owner_notification_date, influenced_by,
                    do_not_drive, park_outside
                ) VALUES (
                    %(campaign_id)s, %(manufacturer)s, %(manufacturer_campaign_number)s,
                    %(component)s, %(recall_type)s, %(potential_units_affected)s,
                    %(defect_summary)s, %(consequence_summary)s, %(corrective_action)s,
                    %(notes)s, %(report_received_date)s, %(record_creation_date)s,
                    %(manufacturing_begin_date)s, %(manufacturing_end_date)s,
                    %(owner_notification_date)s, %(influenced_by)s,
                    %(do_not_drive)s, %(park_outside)s
                )
                ON CONFLICT (campaign_id) DO UPDATE SET
                    manufacturer = EXCLUDED.manufacturer,
                    manufacturer_campaign_number = EXCLUDED.manufacturer_campaign_number,
                    component = EXCLUDED.component,
                    recall_type = EXCLUDED.recall_type,
                    potential_units_affected = EXCLUDED.potential_units_affected,
                    defect_summary = EXCLUDED.defect_summary,
                    consequence_summary = EXCLUDED.consequence_summary,
                    corrective_action = EXCLUDED.corrective_action,
                    notes = EXCLUDED.notes,
                    report_received_date = EXCLUDED.report_received_date,
                    record_creation_date = EXCLUDED.record_creation_date,
                    manufacturing_begin_date = EXCLUDED.manufacturing_begin_date,
                    manufacturing_end_date = EXCLUDED.manufacturing_end_date,
                    owner_notification_date = EXCLUDED.owner_notification_date,
                    influenced_by = EXCLUDED.influenced_by,
                    do_not_drive = EXCLUDED.do_not_drive,
                    park_outside = EXCLUDED.park_outside
                """,
                {
                    "campaign_id": recall.campaign_id,
                    "manufacturer": recall.manufacturer,
                    "manufacturer_campaign_number": recall.manufacturer_campaign_number,
                    "component": recall.component,
                    "recall_type": recall.recall_type,
                    "potential_units_affected": recall.potential_units_affected,
                    "defect_summary": recall.defect_summary,
                    "consequence_summary": recall.consequence_summary,
                    "corrective_action": recall.corrective_action,
                    "notes": recall.notes,
                    "report_received_date": recall.report_received_date,
                    "record_creation_date": recall.record_creation_date,
                    "manufacturing_begin_date": recall.manufacturing_begin_date,
                    "manufacturing_end_date": recall.manufacturing_end_date,
                    "owner_notification_date": recall.owner_notification_date,
                    "influenced_by": recall.influenced_by,
                    "do_not_drive": recall.do_not_drive,
                    "park_outside": recall.park_outside,
                },
            )

            # Replace vehicles: delete old ones, insert current set.
            cur.execute(
                "DELETE FROM recall_vehicles WHERE campaign_id = %s",
                (recall.campaign_id,),
            )
            for v in recall.vehicles:
                cur.execute(
                    """
                    INSERT INTO recall_vehicles (campaign_id, make, model, year)
                    VALUES (%s, %s, %s, %s)
                    """,
                    (recall.campaign_id, v.make, v.model, v.year),
                )
            # Replace components: delete old ones, insert current set.
            cur.execute(
                "DELETE FROM recall_components WHERE campaign_id = %s",
                (recall.campaign_id,),
            )
            for comp in recall.components:
                cur.execute(
                    "INSERT INTO recall_components (campaign_id, component) "
                    "VALUES (%s, %s)",
                    (recall.campaign_id, comp),
                )
            upserted += 1

        # Delete campaigns no longer in the filtered set.
        # ON DELETE CASCADE handles recall_vehicles and chunks.
        cur.execute("SELECT count(*) FROM recalls")
        total_existing = cur.fetchone()[0]

        cur.execute(
            "SELECT count(*) FROM recalls WHERE campaign_id <> ALL(%s)",
            (new_campaign_ids,),
        )
        to_remove = cur.fetchone()[0]

        if to_remove > 0:
            if not allow_mass_delete and check_mass_delete(
                to_remove, total_existing, mass_delete_threshold
            ):
                conn.rollback()
                raise MassDeleteBlocked(
                    f"Would delete {to_remove} of {total_existing} recalls "
                    f"({to_remove / total_existing:.1%}), exceeding the "
                    f"{mass_delete_threshold:.0%} safety threshold. "
                    f"Pass --allow-mass-delete to proceed."
                )

            cur.execute(
                "DELETE FROM recalls WHERE campaign_id <> ALL(%s)",
                (new_campaign_ids,),
            )
            logger.info("Removed %d stale campaigns", to_remove)

    conn.commit()
    logger.info("Upserted %d campaigns, removed %d", upserted, to_remove)
    return upserted, to_remove
