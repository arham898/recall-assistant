"""Shared test fixtures.

Integration tests use a separate database (recall_assistant_test) so
they never touch the real one.  The database is created once per
session, migrations are applied, and each test gets a connection that
rolls back all changes on teardown.
"""

import psycopg
import pytest

from recall_assistant.config import Settings

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
