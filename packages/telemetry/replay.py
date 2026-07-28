"""Reconstructing a session from the record (M0.8).

This is the deliverable §4 actually promises: "given a session ID, you can
reconstruct exactly which misconception was diagnosed, which curriculum node was
retrieved, and why a hint was shown." Not a log dump — §12 is explicit that a log
dump is not an explanation — but an ordered account joining the event timeline to
the model calls behind it.

It reads only append-only tables. Nothing here consults live session state, which
is what makes a replay months later say the same thing it said on the day.
"""

from __future__ import annotations

import datetime as dt
from uuid import UUID

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from packages.domain.enums import EventType
from packages.domain.mapping import from_row
from packages.domain.models import LLMCall, PipelineEvent
from packages.domain.tables import LLMCallRow, PipelineEventRow


class ReplayStep(BaseModel):
    """One event, with the model call it caused attached."""

    model_config = ConfigDict(frozen=True)

    event: PipelineEvent
    llm_call: LLMCall | None = None

    def describe(self) -> str:
        """A line a human can read.

        Deliberately plain prose rather than a serialized event: the audience for
        a replay is a teacher defending a grade or an engineer explaining a hint,
        and neither is helped by JSON.
        """
        e = self.event
        parts: list[str] = [f"{e.sequence:>3}. {e.event_type.value}"]
        if e.stage is not None:
            parts.append(f"[{e.stage.value}]")
        if e.event_type is EventType.STATE_CHANGED:
            frm = e.from_state.value if e.from_state else "-"
            parts.append(f"{frm} -> {e.to_state.value if e.to_state else '?'}")
        if e.detail:
            rendered = ", ".join(f"{k}={v}" for k, v in sorted(e.detail.items()))
            parts.append(rendered)
        if self.llm_call is not None:
            call = self.llm_call
            parts.append(
                f"({call.model_id} @ {call.prompt_version}, "
                f"{call.latency_ms}ms, ${call.cost_usd:.6f})"
            )
        return "  ".join(parts)


class SessionReplay(BaseModel):
    model_config = ConfigDict(frozen=True)

    session_id: UUID
    steps: tuple[ReplayStep, ...]

    @property
    def is_empty(self) -> bool:
        return not self.steps

    @property
    def started_at(self) -> dt.datetime | None:
        return self.steps[0].event.occurred_at if self.steps else None

    @property
    def total_cost_usd(self) -> float:
        return sum(s.llm_call.cost_usd for s in self.steps if s.llm_call is not None)

    @property
    def model_calls(self) -> int:
        return sum(1 for s in self.steps if s.llm_call is not None)

    def events_of(self, event_type: EventType) -> tuple[ReplayStep, ...]:
        return tuple(s for s in self.steps if s.event.event_type is event_type)

    def gaps(self) -> tuple[int, ...]:
        """Missing sequence numbers.

        A gap means the record is incomplete, and a replay drawn from an
        incomplete record is a plausible story rather than what happened. Callers
        that audit should check this before trusting the narrative.
        """
        present = {s.event.sequence for s in self.steps}
        if not present:
            return ()
        return tuple(n for n in range(max(present) + 1) if n not in present)

    def render(self) -> str:
        lines = [f"session {self.session_id}", ""]
        lines.extend(f"  {step.describe()}" for step in self.steps)
        lines.append("")
        lines.append(
            f"  {len(self.steps)} event(s), {self.model_calls} model call(s), "
            f"${self.total_cost_usd:.6f}"
        )
        missing = self.gaps()
        if missing:
            lines.append(f"  INCOMPLETE — missing sequence number(s): {list(missing)}")
        return "\n".join(lines)


def replay(db: DbSession, session_id: UUID) -> SessionReplay:
    """Rebuild a session's timeline from the append-only record."""
    event_rows = (
        db.execute(
            select(PipelineEventRow)
            .where(PipelineEventRow.session_id == session_id)
            .order_by(PipelineEventRow.sequence)
        )
        .scalars()
        .all()
    )
    events = [from_row(row, PipelineEvent) for row in event_rows]

    wanted = {e.llm_call_id for e in events if e.llm_call_id is not None}
    calls: dict[UUID, LLMCall] = {}
    if wanted:
        call_rows = db.execute(select(LLMCallRow).where(LLMCallRow.id.in_(wanted))).scalars().all()
        calls = {row.id: from_row(row, LLMCall) for row in call_rows}

    steps = tuple(
        ReplayStep(
            event=event,
            llm_call=calls.get(event.llm_call_id) if event.llm_call_id else None,
        )
        for event in events
    )
    return SessionReplay(session_id=session_id, steps=steps)
