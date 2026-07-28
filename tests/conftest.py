"""Shared fixtures.

Tests run against in-memory SQLite so the suite stays fast and needs no
container. The schema is deliberately portable (VARCHAR-backed enums, JSON
columns) so this is the same schema Postgres gets — the Postgres-specific parts
(pgvector) live in migrations and are covered by ``-m integration`` tests.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from packages.domain.tables import Base


@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as sess:
        yield sess
    engine.dispose()


@pytest.fixture
def now() -> dt.datetime:
    return dt.datetime(2026, 7, 27, 12, 0, 0, tzinfo=dt.UTC)
