"""Recording the verdict on an attempt (§5's `GradeResult`).

The gap this closes is the same shape as the one `review.py` describes, and it
went unnoticed for the same reason: `GradeResult` had an entity with validation,
an append-only table in the schema, and a migration — and the only code that
ever inserted one was `scripts/demo_session.py`. The production pipeline
recorded a `graded` *event* and nothing else, so the table sat empty in every
real session while every component passed its own tests.

Why the event is not enough. It answers "what happened in this session", in
order, and it is the right shape for a replay. It is the wrong shape for the
question §12 says this architecture exists to answer: *this grade, on this
attempt — what was the verdict, by what method, and did the checker agree?* That
is a lookup keyed by attempt, and recovering it by scanning a timeline is a
weaker guarantee than storing it.

Append-only, like every other log-bearing table (§5). A regrade appends a second
row; the first is what the child was told at the time, and an audit that cannot
see it is not an audit.
"""

from __future__ import annotations

from sqlalchemy.orm import Session as DbSession

from packages.domain.mapping import to_row
from packages.domain.models import GradeResult
from packages.domain.tables import GradeResultRow


class DatabaseGradeSink:
    def __init__(self, db: DbSession) -> None:
        self._db = db

    def record(self, result: GradeResult) -> GradeResult:
        self._db.add(to_row(result, GradeResultRow))
        self._db.flush()
        return result


class InMemoryGradeSink:
    """For stage tests: assert on the verdict without a database."""

    def __init__(self) -> None:
        self.results: list[GradeResult] = []

    def record(self, result: GradeResult) -> GradeResult:
        self.results.append(result)
        return result
