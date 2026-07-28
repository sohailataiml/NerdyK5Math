"""Reconstruct a session from the record (§4, M0.8).

The operator-facing form of "given a session ID, reconstruct exactly which
misconception was diagnosed, which curriculum node was retrieved, and why a hint
was shown". This is what a teacher's grade explanation or an incident
investigation starts from.

Usage::

    .venv/Scripts/python -m scripts.show_replay              # most recent session
    .venv/Scripts/python -m scripts.show_replay <session_id>
"""

from __future__ import annotations

import os
import sys
import uuid

from dotenv import load_dotenv
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from packages.domain import tables as t
from packages.telemetry import replay

DEFAULT_URL = "postgresql+psycopg://tutor:tutor@localhost:5433/tutor"


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    load_dotenv(".env", override=True)
    engine = create_engine(os.environ.get("DATABASE_URL", DEFAULT_URL))

    with Session(engine) as db:
        if args:
            try:
                session_id = uuid.UUID(args[0])
            except ValueError:
                print(f"not a session id: {args[0]!r}")
                return 1
        else:
            latest = db.execute(
                select(t.SessionRow).order_by(t.SessionRow.started_at.desc()).limit(1)
            ).scalar_one_or_none()
            if latest is None:
                print("no sessions recorded yet")
                return 1
            session_id = latest.id

        result = replay(db, session_id)
        if result.is_empty:
            print(f"session {session_id} has no recorded events")
            return 1
        print()
        print(result.render())
        print()

    engine.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(main())
