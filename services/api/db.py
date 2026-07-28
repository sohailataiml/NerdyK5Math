"""Database session per request."""

from __future__ import annotations

import os
from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from packages.dburl import normalize

DEFAULT_URL = "postgresql+psycopg://tutor:tutor@localhost:5433/tutor"

# `normalize` is what lets a hosted provider's `postgres://` work unedited; see
# packages/dburl.py for why both bare schemes fail confusingly without it.
_engine = create_engine(normalize(os.environ.get("DATABASE_URL", DEFAULT_URL)), pool_pre_ping=True)
SessionFactory = sessionmaker(bind=_engine, expire_on_commit=False)


def get_db() -> Iterator[Session]:
    db = SessionFactory()
    try:
        yield db
    finally:
        db.close()
