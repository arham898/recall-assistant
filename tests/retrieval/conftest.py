"""Fixtures for retrieval tests.

RecordingEmbedder — wraps FakeEmbedder and records every call so tests
can assert that embed_query applies the BGE prefix.

LookupEmbedder — maps specific texts to chosen vectors so tests can
control which cards rank where in dense search.

FakeReranker — returns caller-supplied scores so rerank tests are
deterministic.

Seed data — five campaigns with known text for keyword matching and
vehicle filtering.
"""

from __future__ import annotations

import hashlib
import math
import random

import psycopg
import pytest

# ---------------------------------------------------------------------------
# Fake embedders
# ---------------------------------------------------------------------------

EMBEDDING_DIM = 384


class FakeEmbedder:
    """Deterministic fake: SHA256-seeded Gaussian, L2-normalised."""

    def __init__(self, dim: int = EMBEDDING_DIM) -> None:
        self.dim = dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._embed_one(t) for t in texts]

    def _embed_one(self, text: str) -> list[float]:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        rng = random.Random(digest)
        raw = [rng.gauss(0, 1) for _ in range(self.dim)]
        norm = math.sqrt(sum(x * x for x in raw))
        return [x / norm for x in raw]


class RecordingEmbedder:
    """Wraps FakeEmbedder and records every embed() call."""

    def __init__(self) -> None:
        self._inner = FakeEmbedder()
        self.calls: list[list[str]] = []

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return self._inner.embed(texts)


class LookupEmbedder:
    """Maps specific texts to chosen vectors; falls back to FakeEmbedder."""

    def __init__(self, mapping: dict[str, list[float]]) -> None:
        self._mapping = mapping
        self._fallback = FakeEmbedder()

    def embed(self, texts: list[str]) -> list[list[float]]:
        result = []
        for t in texts:
            if t in self._mapping:
                result.append(self._mapping[t])
            else:
                result.append(self._fallback.embed([t])[0])
        return result


# ---------------------------------------------------------------------------
# Fake reranker
# ---------------------------------------------------------------------------


class FakeReranker:
    """Returns caller-supplied scores so tests are deterministic."""

    def __init__(self, scores: list[float]) -> None:
        self._scores = scores

    def score(self, query: str, passages: list[str]) -> list[float]:
        return self._scores[: len(passages)]


# ---------------------------------------------------------------------------
# Seed data
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
