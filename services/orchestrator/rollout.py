"""The Phase 1 staged rollout and its kill switch (P1.1).

Implementation-Plan.md P1.1: generated hints reach 5% of sessions, then 25%, then
100%, each step gated on leak rate, teacher rating, and escalation rate holding —
and a "kill switch reverts to templates instantly, tested before launch, not
after."

Both halves of that are operational controls rather than configuration, and the
distinction decides the design. A percentage in an environment variable moves at
the speed of a deploy; so does a boolean read at startup. If the leak rate turns
during a lesson, the interval between deciding to stop and stopping is measured
in children. So the setting lives in an append-only table, the pipeline reads it
per attempt, and turning generation off takes effect on the next hint.

Three properties this module exists to guarantee:

**A child's cohort does not change mid-session.** The bucket is derived from the
session id, so every attempt in one session resolves the same way. A tutor that
generates hint 1 and templates hint 2 reads to a child as the tutor changing its
mind about them, and to a teacher rating the session as noise.

**Advancing the rollout never evicts anyone.** ``bucket < percentage`` is
monotonic, so 5% -> 25% adds sessions and removes none. The alternative — a fresh
random draw per step — would mean the teacher ratings that justified advancing
describe a cohort that no longer exists, which quietly destroys the evidence the
gate was decided on.

**Withholding generation is not degradation.** A template served because this
session is outside the cohort is the rollout working. Logging it as a fallback
alongside provider outages would put the whole pre-rollout population in the same
bucket as a real incident, and §8's dashboards would show a healthy system as
broken — which is how a real outage gets missed inside the noise.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import uuid
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from packages.domain.mapping import from_row, to_row
from packages.domain.models import RolloutChange
from packages.domain.tables import RolloutChangeRow

BUCKETS = 100
"""Resolution of the cohort split. 100 buckets means a percentage is exact rather
than approximate, which matters at 5%: a pilot classroom is small enough that
"about 5%" and "5%" can differ by whether any child at all is in the cohort."""


@dataclass(frozen=True)
class RolloutState:
    """How much generated text may currently reach children."""

    generation_enabled: bool
    percentage: int
    reason: str | None = None
    """Why the current setting was chosen, carried from the change record so an
    operator reading the live state sees the argument for it, not just a number."""

    def __post_init__(self) -> None:
        if not 0 <= self.percentage <= BUCKETS:
            raise ValueError(f"rollout percentage must be within 0-100, got {self.percentage}")

    @classmethod
    def unconfigured(cls) -> RolloutState:
        """The state of a deployment where nobody has set a rollout.

        Off, not on. Phase 1 starts at 5% after Phase 0's exit gates are met; a
        deployment that has never been told otherwise has not met them, and the
        default that costs least when wrong is the one that shows a child a
        teacher-approved template.
        """
        return cls(
            generation_enabled=False,
            percentage=0,
            reason="no rollout has been configured",
        )


@dataclass(frozen=True)
class RolloutDecision:
    """Whether *this* session may be served generated hints, and why."""

    serve_generated: bool
    reason: str
    bucket: int
    percentage: int
    generation_enabled: bool


def bucket_for(session_id: uuid.UUID) -> int:
    """Stably assign a session to one of `BUCKETS` cohorts.

    Deliberately not `hash()`: Python salts string and bytes hashing per process
    (PYTHONHASHSEED), so the same session would land in different cohorts in
    different workers and again after every restart. A child would flip between
    generated and template hints on consecutive requests, the 5% cohort would
    silently be a different 5% on each deploy, and no rating collected against it
    would mean anything. A fixed digest has none of those properties.
    """
    digest = hashlib.blake2b(session_id.bytes, digest_size=8).digest()
    return int.from_bytes(digest, "big") % BUCKETS


def decide(state: RolloutState, *, session_id: uuid.UUID) -> RolloutDecision:
    """Apply the current rollout to one session. Pure — no clock, no database."""
    bucket = bucket_for(session_id)

    if not state.generation_enabled:
        # The kill switch, and it wins over the percentage rather than being
        # combined with it. Reverting is then one field, and turning generation
        # back on restores the cohort that was already configured instead of a
        # number retyped by whoever is awake.
        detail = f": {state.reason}" if state.reason else ""
        serve, why = False, f"generation is switched off{detail}"
    elif bucket < state.percentage:
        serve, why = True, f"session is in the {state.percentage}% rollout cohort"
    else:
        serve, why = False, f"session is outside the {state.percentage}% rollout cohort"

    return RolloutDecision(
        serve_generated=serve,
        reason=why,
        bucket=bucket,
        percentage=state.percentage,
        generation_enabled=state.generation_enabled,
    )


class RolloutSource(Protocol):
    """Where the live setting is read from, consulted once per attempt."""

    def current(self) -> RolloutState: ...


@dataclass(frozen=True)
class StaticRollout:
    """A fixed setting, for tests and one-off scripts.

    Not for a served deployment: it cannot be changed without restarting the
    process, which is exactly the property P1.1 rules out.
    """

    state: RolloutState

    def current(self) -> RolloutState:
        return self.state


def _more_restrictive(a: RolloutChangeRow, b: RolloutChangeRow) -> RolloutChangeRow:
    """Tie-break two changes written at the same instant, toward less generation.

    Timestamps are coarse enough that two writes can share one, and the table has
    no sequence column to fall back on. Picking arbitrarily would mean a kill
    switch racing an unrelated percentage bump could lose — so the tie resolves to
    whichever change shows fewer children generated text. The wrong outcome then
    costs a template hint rather than an unwanted leak.
    """
    if a.generation_enabled != b.generation_enabled:
        return a if not a.generation_enabled else b
    return a if a.percentage <= b.percentage else b


class DatabaseRolloutSource:
    """Reads the latest `rollout_change` row.

    A query per attempt, on a single-row-ish table with an index on `created_at`.
    That cost buys the property P1.1 asks for — the switch is live — and caching
    it would reintroduce exactly the lag the switch exists to remove.
    """

    def __init__(self, db: DbSession) -> None:
        self._db = db

    def current(self) -> RolloutState:
        latest = (
            self._db.execute(
                select(RolloutChangeRow).order_by(RolloutChangeRow.created_at.desc()).limit(2)
            )
            .scalars()
            .all()
        )
        if not latest:
            return RolloutState.unconfigured()

        row = latest[0]
        if len(latest) == 2 and latest[1].created_at == row.created_at:
            row = _more_restrictive(row, latest[1])

        return RolloutState(
            generation_enabled=row.generation_enabled,
            percentage=row.percentage,
            reason=row.reason,
        )


def record_change(
    db: DbSession,
    *,
    generation_enabled: bool,
    percentage: int,
    changed_by: uuid.UUID,
    reason: str,
) -> RolloutChange:
    """Append a new setting. Takes effect on the next attempt."""
    change = RolloutChange(
        generation_enabled=generation_enabled,
        percentage=percentage,
        changed_by=changed_by,
        reason=reason,
        created_at=dt.datetime.now(dt.UTC),
    )
    db.add(to_row(change, RolloutChangeRow))
    db.flush()
    return change


def history(db: DbSession, *, limit: int = 50) -> list[RolloutChange]:
    """Every change, newest first — the audit trail behind the current setting."""
    rows = (
        db.execute(
            select(RolloutChangeRow).order_by(RolloutChangeRow.created_at.desc()).limit(limit)
        )
        .scalars()
        .all()
    )
    return [from_row(row, RolloutChange) for row in rows]
