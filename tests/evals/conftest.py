"""Test fixtures for golden evaluation set tests.

Built around campaign 24V123000 (2021 FORD RANGER, SERVICE BRAKES,
HYDRAULIC: brake hose).  Inserts into recalls, recall_vehicles AND
recall_components.  Does not modify existing fixtures.
"""

from __future__ import annotations

import psycopg
import pytest

# ---------------------------------------------------------------------------
# Seed data
# ---------------------------------------------------------------------------

EVAL_SEED_RECALLS = [
    # Primary brakes test
    {
        "campaign_id": "24V123000",
        "manufacturer": "FORD MOTOR",
        "component": "SERVICE BRAKES, HYDRAULIC: brake hose",
        "vehicles": [("FORD", "RANGER", 2021)],
        "defect": "brake hose may crack under pressure",
        "consequence": "loss of brake fluid may increase stopping distance",
        "remedy": "dealer will replace brake hose assembly",
        "components": [("24V123000", "SERVICE BRAKES, HYDRAULIC: brake hose")],
    },
    # Second brake category, same vehicle
    {
        "campaign_id": "24V124000",
        "manufacturer": "FORD MOTOR",
        "component": "PARKING BRAKE: cable",
        "vehicles": [("FORD", "RANGER", 2021)],
        "defect": "parking brake cable may seize",
        "consequence": "vehicle may roll when parked",
        "remedy": "dealer will replace parking brake cable",
        "components": [("24V124000", "PARKING BRAKE: cable")],
    },
    # Shared recall across two makes, all_recalls for LINCOLN
    {
        "campaign_id": "24V125000",
        "manufacturer": "FORD MOTOR",
        "component": "STEERING: column",
        "vehicles": [
            ("FORD", "RANGER", 2021),
            ("LINCOLN", "CORSAIR", 2021),
        ],
        "defect": "steering column may loosen",
        "consequence": "loss of steering control",
        "remedy": "dealer will tighten steering column bolts",
        "components": [("24V125000", "STEERING: column")],
    },
    # Multi-group recall: airbags AND steering
    {
        "campaign_id": "24V126000",
        "manufacturer": "FORD MOTOR",
        "component": "AIR BAGS: frontal",
        "vehicles": [("FORD", "RANGER", 2021)],
        "defect": "airbag and steering linkage may fail",
        "consequence": "increased injury risk",
        "remedy": "dealer will replace airbag module and linkage",
        "components": [
            ("24V126000", "AIR BAGS: frontal"),
            ("24V126000", "STEERING: linkage"),
        ],
    },
    # EQUIPMENT-only recall, no mapped group
    {
        "campaign_id": "24V127000",
        "manufacturer": "FORD MOTOR",
        "component": "EQUIPMENT: label",
        "vehicles": [("FORD", "RANGER", 2021)],
        "defect": "equipment label may be incorrect",
        "consequence": "incorrect tire pressure information",
        "remedy": "dealer will apply corrected label",
        "components": [("24V127000", "EQUIPMENT: label")],
    },
    # Year NULL — never sampled
    {
        "campaign_id": "24V141000",
        "manufacturer": "FORD MOTOR",
        "component": "SERVICE BRAKES, HYDRAULIC: hose",
        "vehicles": [("FORD", "RANGER", None)],
        "defect": "brake hose null year",
        "consequence": "null year consequence",
        "remedy": "null year remedy",
        "components": [("24V141000", "SERVICE BRAKES, HYDRAULIC: hose")],
    },
    # Year 9999 — never sampled
    {
        "campaign_id": "24V142000",
        "manufacturer": "FORD MOTOR",
        "component": "SERVICE BRAKES, HYDRAULIC: hose",
        "vehicles": [("FORD", "RANGER", 9999)],
        "defect": "brake hose 9999 year",
        "consequence": "9999 year consequence",
        "remedy": "9999 year remedy",
        "components": [("24V142000", "SERVICE BRAKES, HYDRAULIC: hose")],
    },
    # Year 2017 — never sampled
    {
        "campaign_id": "24V143000",
        "manufacturer": "FORD MOTOR",
        "component": "SERVICE BRAKES, HYDRAULIC: hose",
        "vehicles": [("FORD", "RANGER", 2017)],
        "defect": "brake hose 2017 year",
        "consequence": "2017 year consequence",
        "remedy": "2017 year remedy",
        "components": [("24V143000", "SERVICE BRAKES, HYDRAULIC: hose")],
    },
    # JAGUAR — never sampled
    {
        "campaign_id": "24V150000",
        "manufacturer": "JAGUAR LAND ROVER",
        "component": "TIRES: sidewall",
        "vehicles": [("JAGUAR", "XF", 2021)],
        "defect": "tire sidewall defect",
        "consequence": "tire blowout risk",
        "remedy": "dealer will replace tires",
        "components": [("24V150000", "TIRES: sidewall")],
    },
    # LAND ROVER — never sampled
    {
        "campaign_id": "24V151000",
        "manufacturer": "JAGUAR LAND ROVER",
        "component": "TIRES: tread",
        "vehicles": [("LAND ROVER", "DEFENDER", 2022)],
        "defect": "tire tread separation",
        "consequence": "loss of vehicle control",
        "remedy": "dealer will replace tires",
        "components": [("24V151000", "TIRES: tread")],
    },
    # Backup camera: pedestrian alert only — NOT backup_camera gold
    {
        "campaign_id": "24V160000",
        "manufacturer": "TEST MOTORS",
        "component": "BACK OVER PREVENTION:WARNINGS:EXTERNAL/PEDESTRIAN ALERT",
        "vehicles": [("TESTCAM", "SEDAN", 2022)],
        "defect": "pedestrian warning sound may not activate",
        "consequence": "pedestrians may not hear the vehicle",
        "remedy": "dealer will update sound module",
        "components": [
            (
                "24V160000",
                "BACK OVER PREVENTION:WARNINGS:EXTERNAL/PEDESTRIAN ALERT",
            ),
        ],
    },
    # Backup camera: camera + pedestrian alert — IS backup_camera gold
    {
        "campaign_id": "24V161000",
        "manufacturer": "TEST MOTORS",
        "component": "BACK OVER PREVENTION:DISPLAY FUNCTION",
        "vehicles": [("TESTCAM", "SEDAN", 2022)],
        "defect": "rearview camera display may fail",
        "consequence": "driver cannot see behind the vehicle",
        "remedy": "dealer will replace camera module",
        "components": [
            ("24V161000", "BACK OVER PREVENTION:DISPLAY FUNCTION"),
            (
                "24V161000",
                "BACK OVER PREVENTION:WARNINGS:EXTERNAL/PEDESTRIAN ALERT",
            ),
        ],
    },
    # Backup camera: AUTOMATIC SYSTEM BRAKING (space after colon) — NOT gold
    {
        "campaign_id": "24V162000",
        "manufacturer": "TEST MOTORS",
        "component": "BACK OVER PREVENTION: AUTOMATIC SYSTEM BRAKING",
        "vehicles": [("TESTCAM", "SEDAN", 2022)],
        "defect": "automatic braking system may not engage",
        "consequence": "vehicle may not stop when reversing",
        "remedy": "dealer will update braking software",
        "components": [
            ("24V162000", "BACK OVER PREVENTION: AUTOMATIC SYSTEM BRAKING"),
        ],
    },
]

# 11 campaigns on ACME WIDGET 2021 for 10-gold limit test
for i in range(11):
    cid = f"24V{130 + i:03d}000"
    EVAL_SEED_RECALLS.append(
        {
            "campaign_id": cid,
            "manufacturer": "ACME CORP",
            "component": "SERVICE BRAKES, HYDRAULIC: pad",
            "vehicles": [("ACME", "WIDGET", 2021)],
            "defect": f"brake pad defect variant {i}",
            "consequence": f"brake pad consequence {i}",
            "remedy": f"brake pad remedy {i}",
            "components": [(cid, "SERVICE BRAKES, HYDRAULIC: pad")],
        }
    )

# 6 vehicles for TESTMAKE1 per-make cap test (cap=5, 6 eligible)
for i, model_name in enumerate(
    ["MODEL-A", "MODEL-B", "MODEL-C", "MODEL-D", "MODEL-E", "MODEL-F"]
):
    cid = f"24V{144 + i:03d}000"
    EVAL_SEED_RECALLS.append(
        {
            "campaign_id": cid,
            "manufacturer": "TESTMAKE1 CORP",
            "component": "TIRES: tread",
            "vehicles": [("TESTMAKE1", model_name, 2021)],
            "defect": f"tire tread defect {model_name}",
            "consequence": f"tire consequence {model_name}",
            "remedy": f"tire remedy {model_name}",
            "components": [(cid, "TIRES: tread")],
        }
    )


@pytest.fixture
def eval_seed_data(db_conn: psycopg.Connection):
    """Insert test campaigns with recalls, vehicles and components."""
    with db_conn.cursor() as cur:
        for r in EVAL_SEED_RECALLS:
            cur.execute(
                "INSERT INTO recalls "
                "(campaign_id, manufacturer, component, recall_type, "
                "defect_summary, consequence_summary, corrective_action) "
                "VALUES (%(cid)s, %(mfg)s, %(comp)s, 'V', "
                "%(defect)s, %(consequence)s, %(remedy)s)",
                {
                    "cid": r["campaign_id"],
                    "mfg": r["manufacturer"],
                    "comp": r["component"],
                    "defect": r["defect"],
                    "consequence": r["consequence"],
                    "remedy": r["remedy"],
                },
            )
            for make, model, year in r["vehicles"]:
                cur.execute(
                    "INSERT INTO recall_vehicles "
                    "(campaign_id, make, model, year) "
                    "VALUES (%(cid)s, %(make)s, %(model)s, %(year)s)",
                    {
                        "cid": r["campaign_id"],
                        "make": make,
                        "model": model,
                        "year": year,
                    },
                )
            for comp_cid, comp_str in r["components"]:
                cur.execute(
                    "INSERT INTO recall_components "
                    "(campaign_id, component) "
                    "VALUES (%(cid)s, %(comp)s)",
                    {"cid": comp_cid, "comp": comp_str},
                )
    db_conn.commit()
