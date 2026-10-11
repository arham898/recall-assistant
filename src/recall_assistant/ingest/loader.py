"""Load Recall records into Postgres with upsert semantics."""

import logging

import psycopg

from recall_assistant.ingest.models import Recall

logger = logging.getLogger(__name__)


def upsert_recalls(conn: psycopg.Connection, recalls: list[Recall]) -> int:
    """Upsert recalls and their vehicles into the database.

    Uses ON CONFLICT DO UPDATE so re-running is safe and picks up
    any changes in the source data.  Returns the number of campaigns upserted.
    """
    upserted = 0

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
            upserted += 1

    conn.commit()
    logger.info("Upserted %d campaigns", upserted)
    return upserted
