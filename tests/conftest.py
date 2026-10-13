"""Shared test fixtures.

Integration tests use a separate database (recall_assistant_test) so
they never touch the real one.  The database is created once per
session, migrations are applied, and each test gets a connection that
rolls back all changes on teardown.
"""

import psycopg
import pytest

from recall_assistant.config import Settings
from tests.fakes import FakeEmbedder

TEST_DB = "recall_assistant_test"


@pytest.fixture(scope="session")
def _test_db():
    """Create the test database (once per session) and apply migrations."""
    settings = Settings()

    # Connect to the default 'postgres' database to create the test DB.
    admin_url = (
        f"postgresql://{settings.postgres_user}:{settings.postgres_password}"
        f"@{settings.postgres_host}:{settings.postgres_port}/postgres"
    )
    admin_conn = psycopg.connect(admin_url, autocommit=True)
    with admin_conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (TEST_DB,))
        if not cur.fetchone():
            cur.execute(f"CREATE DATABASE {TEST_DB}")
    admin_conn.close()

    # Connect to the test database and run migrations.
    test_url = (
        f"postgresql://{settings.postgres_user}:{settings.postgres_password}"
        f"@{settings.postgres_host}:{settings.postgres_port}/{TEST_DB}"
    )
    conn = psycopg.connect(test_url)

    from recall_assistant.ingest.migrator import run_migrations

    run_migrations(conn)
    conn.close()

    return test_url


@pytest.fixture
def db_conn(_test_db: str):
    """Yield a connection to the test database.

    All writes are rolled back after the test so tests are isolated.
    """
    conn = psycopg.connect(_test_db)
    yield conn

    conn.rollback()
    # Clean tables in dependency order so FK constraints are satisfied.
    with conn.cursor() as cur:
        cur.execute("DELETE FROM chunks")
        cur.execute("DELETE FROM recall_vehicles")
        cur.execute("DELETE FROM recalls")
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Seed data (moved unchanged from tests/retrieval/conftest.py)
# ---------------------------------------------------------------------------

SEED_RECALLS = [
    {
        "campaign_id": "RET_001",
        "manufacturer": "TOYOTA MOTOR",
        "component": "FUEL PUMP",
        "vehicles": [("TOYOTA", "CAMRY", 2020), ("TOYOTA", "COROLLA", 2021)],
        "defect": "fuel pump failure causing engine stall",
        "consequence": "engine stall increases crash risk",
        "remedy": "dealer will replace fuel pump",
    },
    {
        "campaign_id": "RET_002",
        "manufacturer": "FORD MOTOR",
        "component": "BRAKES",
        "vehicles": [("FORD", "F-150", 2019)],
        "defect": "brake line corrosion causing fluid leak",
        "consequence": "loss of braking increases stopping distance",
        "remedy": "dealer will replace brake lines",
    },
    {
        "campaign_id": "RET_003",
        "manufacturer": "HONDA MOTOR",
        "component": "AIRBAGS",
        "vehicles": [("HONDA", "CIVIC", 2022)],
        "defect": "airbag may not deploy in crash",
        "consequence": "increased injury risk in a crash",
        "remedy": "dealer will update airbag software",
    },
    {
        "campaign_id": "RET_004",
        "manufacturer": "TOYOTA MOTOR",
        "component": "BRAKES",
        "vehicles": [("TOYOTA", "RAV4", 2021), ("TOYOTA", "CAMRY", 2020)],
        "defect": "brake pad wear sensor malfunction",
        "consequence": "driver may not know brakes need replacement",
        "remedy": "dealer will replace brake sensor",
    },
    {
        "campaign_id": "RET_005",
        "manufacturer": "FORD MOTOR",
        "component": "ENGINE",
        "vehicles": [("FORD", "MUSTANG", 2023)],
        "defect": "engine coolant leak from radiator hose",
        "consequence": "overheating may cause engine damage",
        "remedy": "dealer will replace radiator hose",
    },
]


def _record_body(r: dict) -> str:
    """Build a record-chunker body from seed data."""
    vehs = ", ".join(f"{make} {model} ({year})" for make, model, year in r["vehicles"])
    return (
        f"Vehicles: {vehs}\n"
        f"Component: {r['component']}\n"
        f"Defect: {r['defect']}\n"
        f"Consequence: {r['consequence']}\n"
        f"Remedy: {r['remedy']}"
    )


def _field_bodies(r: dict) -> list[tuple[str, str]]:
    """Build field-chunker (field_name, body) pairs from seed data."""
    vehs = ", ".join(f"{make} {model}" for make, model, _ in r["vehicles"])
    prefix = f"{vehs} | {r['component']} — "
    return [
        ("defect_summary:0", f"{prefix}{r['defect']}"),
        ("consequence_summary:0", f"{prefix}{r['consequence']}"),
        ("corrective_action:0", f"{prefix}{r['remedy']}"),
    ]


@pytest.fixture
def seed_retrieval_data(db_conn: psycopg.Connection):
    """Insert five test campaigns with record and field chunks, embedded
    with FakeEmbedder.  Returns the FakeEmbedder instance."""
    embedder = FakeEmbedder()

    with db_conn.cursor() as cur:
        for r in SEED_RECALLS:
            cur.execute(
                "INSERT INTO recalls "
                "(campaign_id, manufacturer, component, recall_type) "
                "VALUES (%(cid)s, %(mfg)s, %(comp)s, 'V')",
                {
                    "cid": r["campaign_id"],
                    "mfg": r["manufacturer"],
                    "comp": r["component"],
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

            # Record chunk
            body = _record_body(r)
            vec = embedder.embed([body])[0]
            cur.execute(
                "INSERT INTO chunks "
                "(campaign_id, chunker, field_name, body, embedding) "
                "VALUES (%(cid)s, 'record', 'all:0', %(body)s, "
                "%(vec)s::vector)",
                {"cid": r["campaign_id"], "body": body, "vec": str(vec)},
            )

            # Field chunks
            for field_name, body in _field_bodies(r):
                vec = embedder.embed([body])[0]
                cur.execute(
                    "INSERT INTO chunks "
                    "(campaign_id, chunker, field_name, body, embedding) "
                    "VALUES (%(cid)s, 'field', %(fn)s, %(body)s, "
                    "%(vec)s::vector)",
                    {
                        "cid": r["campaign_id"],
                        "fn": field_name,
                        "body": body,
                        "vec": str(vec),
                    },
                )

    db_conn.commit()
    # ANALYZE so the planner has accurate stats for the freshly-inserted
    # rows.  DELETE-based cleanup between tests does not reset
    # pg_class.reltuples, and stale stats can change query plans.
    with db_conn.cursor() as cur:
        cur.execute("ANALYZE chunks")
        cur.execute("ANALYZE recall_vehicles")
        cur.execute("ANALYZE recalls")
    return embedder
