"""Recording shadow candidates (Phase 0, P0.4).

Phase 0 runs the full model pipeline on every session but shows the child a
template hint. What the model produced is written here, beside what was actually
shown, so the phase's exit gates can be measured on real student work:

- Is generation beating the deterministic path? (>=70% of teacher ratings)
- Does the leak-checker hold on real generated output, not just on hand-written
  attacks? (P0.5 seeds its corpus from anything a teacher flags)

A shadow run that is not recorded is a session's worth of evidence discarded, so
the sink is separated from the graph and given the same in-memory/database pair
as the ledger — a stage test can assert on what was captured without a database.
"""

from __future__ import annotations

from sqlalchemy.orm import Session as DbSession

from packages.domain.mapping import to_row
from packages.domain.models import ShadowCandidate
from packages.domain.tables import ShadowCandidateRow


class DatabaseShadowSink:
    def __init__(self, db: DbSession) -> None:
        self._db = db

    def record(self, candidate: ShadowCandidate) -> None:
        self._db.add(to_row(candidate, ShadowCandidateRow))
        self._db.commit()


class InMemoryShadowSink:
    def __init__(self) -> None:
        self.candidates: list[ShadowCandidate] = []

    def record(self, candidate: ShadowCandidate) -> None:
        self.candidates.append(candidate)
