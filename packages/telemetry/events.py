"""Recording pipeline events (M0.8).

Architecture.md §4: "given a session ID, reconstruct which diagnosis was made, by
which model and prompt version, which node was retrieved, and why a hint was
shown." M0.4's ledger covers the model calls; this covers everything between
them — state transitions, stage outcomes, fallbacks, escalations — so the record
is a whole session rather than a list of API calls.

Sequence numbers are assigned per session and enforced unique in the schema. That
is deliberate: timestamps are too coarse to order events inside a fast stage, and
a replay that shows the template fallback *before* the leak-check failure tells
the wrong story about why a child saw what they saw.
"""

from __future__ import annotations

import datetime as dt
from typing import Protocol
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession

from packages.domain.enums import EventType, PipelineStage, SessionState
from packages.domain.mapping import to_row
from packages.domain.models import PipelineEvent
from packages.domain.tables import PipelineEventRow


class EventSink(Protocol):
    def record(self, event: PipelineEvent) -> None: ...


class DatabaseEventSink:
    def __init__(self, db: DbSession) -> None:
        self._db = db

    def record(self, event: PipelineEvent) -> None:
        self._db.add(to_row(event, PipelineEventRow))
        self._db.commit()

    def next_sequence(self, session_id: UUID) -> int:
        highest = self._db.execute(
            select(func.max(PipelineEventRow.sequence)).where(
                PipelineEventRow.session_id == session_id
            )
        ).scalar_one_or_none()
        return 0 if highest is None else highest + 1


class InMemoryEventSink:
    def __init__(self) -> None:
        self.events: list[PipelineEvent] = []

    def record(self, event: PipelineEvent) -> None:
        self.events.append(event)

    def next_sequence(self, session_id: UUID) -> int:
        return sum(1 for e in self.events if e.session_id == session_id)


class EventRecorder:
    """Writes the session timeline.

    One recorder per session. Sequence allocation reads the current maximum,
    which is safe because a session has a single writer — the orchestrator
    advancing its own state machine. The unique constraint is the backstop if
    that assumption is ever violated: a duplicate sequence fails loudly rather
    than producing a plausible-looking but wrong replay.
    """

    def __init__(self, sink: DatabaseEventSink | InMemoryEventSink, session_id: UUID) -> None:
        self._sink = sink
        self._session_id = session_id

    @property
    def session_id(self) -> UUID:
        """The session being recorded. Read-only — a recorder is bound to one."""
        return self._session_id

    def _emit(
        self,
        event_type: EventType,
        *,
        stage: PipelineStage | None = None,
        from_state: SessionState | None = None,
        to_state: SessionState | None = None,
        detail: dict[str, object] | None = None,
        llm_call_id: UUID | None = None,
    ) -> PipelineEvent:
        event = PipelineEvent(
            session_id=self._session_id,
            sequence=self._sink.next_sequence(self._session_id),
            event_type=event_type,
            stage=stage,
            from_state=from_state,
            to_state=to_state,
            detail=detail or {},
            llm_call_id=llm_call_id,
            occurred_at=dt.datetime.now(dt.UTC),
        )
        self._sink.record(event)
        return event

    def session_started(self, **detail: object) -> PipelineEvent:
        return self._emit(EventType.SESSION_STARTED, detail=dict(detail))

    def state_changed(self, *, frm: SessionState | None, to: SessionState) -> PipelineEvent:
        return self._emit(EventType.STATE_CHANGED, from_state=frm, to_state=to)

    def stage_started(self, stage: PipelineStage, **detail: object) -> PipelineEvent:
        return self._emit(EventType.STAGE_STARTED, stage=stage, detail=dict(detail))

    def stage_completed(
        self, stage: PipelineStage, *, llm_call_id: UUID | None = None, **detail: object
    ) -> PipelineEvent:
        return self._emit(
            EventType.STAGE_COMPLETED, stage=stage, llm_call_id=llm_call_id, detail=dict(detail)
        )

    def stage_failed(self, stage: PipelineStage, *, reason: str, **detail: object) -> PipelineEvent:
        return self._emit(EventType.STAGE_FAILED, stage=stage, detail={"reason": reason, **detail})

    def fallback_used(
        self, stage: PipelineStage, *, reason: str, **detail: object
    ) -> PipelineEvent:
        """§4's degradation path, recorded.

        Without this, a run where the provider was down and every hint came from
        a template is indistinguishable in the data from a normal one — and the
        quality dip that follows looks like a model regression.
        """
        return self._emit(EventType.FALLBACK_USED, stage=stage, detail={"reason": reason, **detail})

    def answer_submitted(self, *, attempt_number: int, **detail: object) -> PipelineEvent:
        return self._emit(
            EventType.ANSWER_SUBMITTED, detail={"attempt_number": attempt_number, **detail}
        )

    def hint_shown(self, *, hint_level: int, source: str, **detail: object) -> PipelineEvent:
        return self._emit(
            EventType.HINT_SHOWN, detail={"hint_level": hint_level, "source": source, **detail}
        )

    def graded(self, *, score: float, method: str, **detail: object) -> PipelineEvent:
        return self._emit(EventType.GRADED, detail={"score": score, "method": method, **detail})

    def safety_flagged(
        self,
        *,
        category: str,
        screener_version: str,
        llm_call_id: UUID | None = None,
        **detail: object,
    ) -> PipelineEvent:
        """A §7 welfare signal was raised (P1.8).

        The child's words are **not** in this event. The event log is read for
        replay and debugging by anyone with database access; a disclosure belongs
        in `safety_alert`, which has its own reader and its own access path. What
        goes here is that a signal fired and which screener fired it — enough to
        reconstruct the session and to measure the screen, without spreading the
        text across every surface that renders a timeline.
        """
        return self._emit(
            EventType.SAFETY_FLAG,
            stage=PipelineStage.SAFETY_SCREEN,
            llm_call_id=llm_call_id,
            detail={"category": category, "screener_version": screener_version, **detail},
        )

    def escalated(self, *, reason: str, **detail: object) -> PipelineEvent:
        return self._emit(EventType.ESCALATED, detail={"reason": reason, **detail})

    def routed_for_review(self, *, reason: str, **detail: object) -> PipelineEvent:
        """A teacher was told about this session (§3.6).

        Logged as an escalation event rather than inventing a type: from the
        replay's point of view "this went to a human" is the same kind of fact,
        and the detail says which queue and why. What matters is that the record
        distinguishes a session that reached a teacher from one that only said it
        would.
        """
        return self._emit(
            EventType.ESCALATED, detail={"routed_to": "review_queue", "reason": reason, **detail}
        )

    def session_completed(self, **detail: object) -> PipelineEvent:
        return self._emit(EventType.SESSION_COMPLETED, detail=dict(detail))
