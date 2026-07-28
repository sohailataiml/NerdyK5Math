"""Recording the hint a child actually read (§5's `HintLog`).

The event log already says a hint was shown — at which level, from which source,
cleared by which checker version. What it does not say is *what the hint said*,
and that is the one thing a teacher reviewing the session needs: §3.6's premise
is that a judgment is defensible because the evidence is reachable, and "a level
2 template was shown" is not evidence of anything a teacher can act on.

`HintLog` refuses to be constructed for a hint the leak check did not clear
(`_only_cleared_hints_exist`), so this sink can only ever hold text that actually
reached a student. That invariant is why the write lives inside the graph's
`check.passed` branch rather than beside the generation call.
"""

from __future__ import annotations

from sqlalchemy.orm import Session as DbSession

from packages.domain.mapping import to_row
from packages.domain.models import HintLog
from packages.domain.tables import HintLogRow


class DatabaseHintSink:
    def __init__(self, db: DbSession) -> None:
        self._db = db

    def record(self, hint: HintLog) -> HintLog:
        self._db.add(to_row(hint, HintLogRow))
        self._db.flush()
        return hint


class InMemoryHintSink:
    """For stage tests: assert on what a child was shown without a database."""

    def __init__(self) -> None:
        self.hints: list[HintLog] = []

    def record(self, hint: HintLog) -> HintLog:
        self.hints.append(hint)
        return hint
