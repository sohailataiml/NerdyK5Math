"""The `LLMCall` audit ledger (M0.4).

Architecture.md §4 is explicit that replay on this revision means *reconstructing
the record*, not reproducing the computation — model calls are not deterministic,
so a grade is defensible later only if the exact model, prompt version, and
payloads were stored at the time. This is that store.

`LedgerWriter` is a Protocol so stage tests can assert on ledger contents without
a database, while production writes through SQLAlchemy.
"""

from __future__ import annotations

from typing import Protocol

from sqlalchemy.orm import Session as DbSession

from packages.domain.mapping import to_row
from packages.domain.models import LLMCall
from packages.domain.tables import LLMCallRow


class LedgerWriter(Protocol):
    def record(self, call: LLMCall) -> None: ...


class DatabaseLedger:
    """Production ledger. Writes an append-only `llm_call` row per model call."""

    def __init__(self, db: DbSession) -> None:
        self._db = db

    def record(self, call: LLMCall) -> None:
        self._db.add(to_row(call, LLMCallRow))
        self._db.commit()


class InMemoryLedger:
    """Test ledger. Keeps calls in order so tests can assert on what was written."""

    def __init__(self) -> None:
        self.calls: list[LLMCall] = []

    def record(self, call: LLMCall) -> None:
        self.calls.append(call)
