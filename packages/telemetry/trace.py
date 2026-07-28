"""Per-stage input/output for one run (M0.8, §8).

`replay.py` answers "what happened, in order". This answers the narrower and
more operational question: **what went into each stage, and what came out**.

Built entirely from the append-only record, like the replay, so it says the same
thing months later as it did on the day. Nothing here consults live state and
nothing re-runs a stage.

Two things this is careful to distinguish, because collapsing them is how a
degraded run gets read as a healthy one:

- **A stage that called a model** has an `LLMCall`, so its input and output are
  the exact payloads that were sent and received.
- **A stage that took its deterministic path** has none — the rule pre-check
  fired, the keyed lookup hit, a template was rendered, or the provider was
  down. Its input and output are what the stage recorded in the event detail.

Both are real stage runs and both belong in the picture. A dashboard that shows
only model calls would render a full provider outage as an empty screen.

**This carries children's answers and full prompt payloads, including
`correct_answer` on the diagnose and leak-check inputs.** It is an audit surface
for adults and nothing here may reach a student client.
"""

from __future__ import annotations

import datetime as dt
from uuid import UUID

from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session as DbSession

from packages.domain.enums import EventType, PipelineStage
from packages.domain.models import LLMCall, PipelineEvent
from packages.telemetry.replay import ReplayStep, SessionReplay, replay

TERMINAL_EVENTS = frozenset(
    {EventType.STAGE_COMPLETED, EventType.STAGE_FAILED, EventType.FALLBACK_USED}
)


class StageRun(BaseModel):
    """One invocation of one stage, with what went in and what came out."""

    model_config = ConfigDict(frozen=True)

    stage: PipelineStage
    ordinal: int
    """Which invocation of this stage within the session, from 1.

    Stages repeat — three hint levels means three passes through generate and
    leak-check — and a view that shows only the last one hides the two that
    explain it.
    """

    first_sequence: int
    last_sequence: int | None
    started_at: dt.datetime
    ended_at: dt.datetime | None

    outcome: str
    """`completed`, `failed`, `fallback`, or `unterminated`.

    `unterminated` is reported rather than hidden: a stage that started and never
    recorded an ending means the process died mid-run, and a trace that quietly
    dropped it would show a session that looks complete.
    """

    started_detail: dict[str, object]
    ended_detail: dict[str, object]
    llm_call: LLMCall | None = None

    @property
    def used_model(self) -> bool:
        return self.llm_call is not None

    @property
    def duration_ms(self) -> int | None:
        if self.ended_at is None:
            return None
        return int((self.ended_at - self.started_at).total_seconds() * 1000)

    @property
    def inputs(self) -> dict[str, object]:
        """What the stage was given.

        The model payload when there was one, otherwise whatever the stage
        recorded on the way in. Merged rather than either/or so a model-backed
        stage still shows the orchestrator's arguments alongside the prompt.
        """
        merged: dict[str, object] = dict(self.started_detail)
        if self.llm_call is not None:
            merged["llm_input"] = self.llm_call.input_payload
        return merged

    @property
    def outputs(self) -> dict[str, object]:
        merged: dict[str, object] = dict(self.ended_detail)
        if self.llm_call is not None:
            merged["llm_output"] = self.llm_call.output_payload
        return merged

    @property
    def cost_usd(self) -> float:
        return self.llm_call.cost_usd if self.llm_call else 0.0


class SessionTrace(BaseModel):
    model_config = ConfigDict(frozen=True)

    session_id: UUID
    stages: tuple[StageRun, ...]
    timeline: tuple[PipelineEvent, ...]
    """Session-level events that belong to no stage — answer submitted, hint
    shown, graded, escalated. They are the context a stage list cannot supply."""

    gaps: tuple[int, ...]
    """Missing sequence numbers, carried through from the replay. A trace drawn
    from an incomplete record is a plausible story rather than what happened, and
    a caller that audits needs to know before trusting it."""

    @property
    def is_empty(self) -> bool:
        return not self.stages and not self.timeline

    @property
    def total_cost_usd(self) -> float:
        return sum(run.cost_usd for run in self.stages)

    @property
    def model_calls(self) -> int:
        return sum(1 for run in self.stages if run.used_model)

    @property
    def deterministic_stages(self) -> int:
        return sum(1 for run in self.stages if not run.used_model)

    @property
    def degraded_stages(self) -> tuple[PipelineStage, ...]:
        """Stages that fell back. §8 segments quality by exactly this."""
        seen: list[PipelineStage] = []
        for run in self.stages:
            if run.outcome == "fallback" and run.stage not in seen:
                seen.append(run.stage)
        return tuple(seen)


_OUTCOME = {
    EventType.STAGE_COMPLETED: "completed",
    EventType.STAGE_FAILED: "failed",
    EventType.FALLBACK_USED: "fallback",
}


def _llm_call_of(steps: list[ReplayStep]) -> LLMCall | None:
    for step in steps:
        if step.llm_call is not None:
            return step.llm_call
    return None


def trace_from(session_replay: SessionReplay) -> SessionTrace:
    """Group a replay into per-stage runs. Pure — no database."""
    open_runs: dict[PipelineStage, list[ReplayStep]] = {}
    counts: dict[PipelineStage, int] = {}
    finished: list[StageRun] = []
    timeline: list[PipelineEvent] = []

    def close(stage: PipelineStage, collected: list[ReplayStep]) -> None:
        first = collected[0]
        last = collected[-1]
        terminated = last.event.event_type in TERMINAL_EVENTS
        counts[stage] = counts.get(stage, 0) + 1
        finished.append(
            StageRun(
                stage=stage,
                ordinal=counts[stage],
                first_sequence=first.event.sequence,
                last_sequence=last.event.sequence if terminated else None,
                started_at=first.event.occurred_at,
                ended_at=last.event.occurred_at if terminated else None,
                outcome=_OUTCOME.get(last.event.event_type, "unterminated")
                if terminated
                else "unterminated",
                started_detail=dict(first.event.detail),
                ended_detail=dict(last.event.detail) if terminated else {},
                llm_call=_llm_call_of(collected),
            )
        )

    for step in session_replay.steps:
        stage = step.event.stage
        if stage is None:
            timeline.append(step.event)
            continue

        if step.event.event_type is EventType.STAGE_STARTED:
            # A second start with the first still open means the first never
            # recorded an ending. Close it as unterminated rather than dropping
            # it — losing it would hide exactly the run that went wrong.
            if stage in open_runs:
                close(stage, open_runs.pop(stage))
            open_runs[stage] = [step]
            continue

        if stage in open_runs:
            open_runs[stage].append(step)
            if step.event.event_type in TERMINAL_EVENTS:
                close(stage, open_runs.pop(stage))
        else:
            # A terminal event with no start. Still a real thing that happened,
            # so it is reported as its own single-event run.
            close(stage, [step])

    for stage, collected in list(open_runs.items()):
        close(stage, collected)

    finished.sort(key=lambda run: run.first_sequence)
    return SessionTrace(
        session_id=session_replay.session_id,
        stages=tuple(finished),
        timeline=tuple(timeline),
        gaps=session_replay.gaps(),
    )


def trace(db: DbSession, session_id: UUID) -> SessionTrace:
    """Rebuild one session's per-stage inputs and outputs from the record."""
    return trace_from(replay(db, session_id))
