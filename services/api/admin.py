"""Operator controls for the Phase 1 rollout (P1.1).

Three endpoints, and the third exists because of how the second reads at 2am.

`POST /admin/rollout` sets both the switch and the percentage — the deliberate,
evidence-backed step from 5% to 25% to 100%. `POST /admin/rollout/kill` sets only
the switch, and asks for nothing but a reason. Whoever reaches for it is reacting
to something that just went wrong, and requiring them to also restate the
percentage invites a fat-fingered `100` in the field next to the one they meant.
It also preserves the configured cohort, so turning generation back on afterwards
restores the rollout that was already reasoned about.

Admin-only, per M0.9's policy: there is one setting for the whole deployment, so
this is not a classroom control. Every change is attributed and appended, because
Phase 1's exit criteria are argued from what happened during each step, and a
percentage nobody signed for makes that argument unreconstructable.
"""

from __future__ import annotations

import datetime as dt
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from packages.auth import (
    AuthorizationError,
    Scope,
    can_control_rollout,
    can_read_audit_trail,
    require,
)
from packages.domain.enums import PipelineStage
from packages.domain.models import LLMCall
from packages.domain.tables import SessionRow
from packages.llm.config import STAGE_CONFIG
from packages.telemetry import Economics
from packages.telemetry import economics as build_economics
from packages.telemetry import trace as build_trace
from services.api.auth import current_scope
from services.api.db import get_db
from services.orchestrator import rollout as rollout_policy
from services.orchestrator import swarm

router = APIRouter(prefix="/admin", tags=["admin"])

HISTORY_LIMIT = 50
RUNS_LIMIT = 40


class RolloutView(BaseModel):
    """The live setting, as an operator needs to read it."""

    model_config = ConfigDict(frozen=True)

    generation_enabled: bool
    percentage: int
    reason: str | None
    configured: bool
    """False when no change has ever been recorded. Reported rather than left to
    be inferred from `percentage == 0`, because "nobody has set this up" and
    "someone deliberately set it to zero" call for different next actions."""


class RolloutChangeView(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    generation_enabled: bool
    percentage: int
    changed_by: uuid.UUID
    reason: str
    created_at: dt.datetime


class _ReasonedRequest(BaseModel):
    """Anything that changes the rollout has to say why, in words.

    `min_length` alone does not get there: it counts whitespace, so a spacebar
    satisfies it. That matters because the reason is the whole audit value of the
    record — a history of blank strings is a history of nobody having to justify
    anything, which is the state this table exists to prevent.
    """

    model_config = ConfigDict(frozen=True)

    reason: str = Field(min_length=1, max_length=500)

    @field_validator("reason")
    @classmethod
    def _must_say_something(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("a rollout change must record why it was made")
        return stripped


class SetRolloutRequest(_ReasonedRequest):
    generation_enabled: bool
    percentage: int = Field(ge=0, le=100)


class KillRequest(_ReasonedRequest):
    pass


def _authorize(scope: Scope) -> None:
    try:
        require(can_control_rollout(scope), "control the generation rollout")
    except AuthorizationError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from None


@router.get("/rollout", response_model=RolloutView)
def read_rollout(
    scope: Scope = Depends(current_scope),
    db: DbSession = Depends(get_db),
) -> RolloutView:
    """What the pipeline will do for the next child."""
    _authorize(scope)
    state = rollout_policy.DatabaseRolloutSource(db).current()
    unconfigured = rollout_policy.RolloutState.unconfigured()
    return RolloutView(
        generation_enabled=state.generation_enabled,
        percentage=state.percentage,
        reason=state.reason,
        configured=state != unconfigured,
    )


@router.get("/rollout/history", response_model=list[RolloutChangeView])
def read_rollout_history(
    scope: Scope = Depends(current_scope),
    db: DbSession = Depends(get_db),
) -> list[RolloutChangeView]:
    """Every change, newest first — who moved it, when, and why."""
    _authorize(scope)
    return [
        RolloutChangeView(**change.model_dump())
        for change in rollout_policy.history(db, limit=HISTORY_LIMIT)
    ]


@router.post("/rollout", response_model=RolloutView, status_code=status.HTTP_201_CREATED)
def set_rollout(
    body: SetRolloutRequest,
    scope: Scope = Depends(current_scope),
    db: DbSession = Depends(get_db),
) -> RolloutView:
    """Advance (or retreat) the staged rollout. Takes effect on the next attempt."""
    _authorize(scope)
    rollout_policy.record_change(
        db,
        generation_enabled=body.generation_enabled,
        percentage=body.percentage,
        changed_by=scope.principal.id,
        reason=body.reason,
    )
    db.commit()
    return read_rollout(scope=scope, db=db)


@router.post("/rollout/kill", response_model=RolloutView, status_code=status.HTTP_201_CREATED)
def kill_generation(
    body: KillRequest,
    scope: Scope = Depends(current_scope),
    db: DbSession = Depends(get_db),
) -> RolloutView:
    """Stop showing generated hints. One field, and the cohort is preserved.

    Deliberately not idempotent-by-skip: killing an already-killed rollout appends
    a second row. Two people both reaching for this within a minute is a fact
    about the incident, and collapsing it to one record loses it.
    """
    _authorize(scope)
    current = rollout_policy.DatabaseRolloutSource(db).current()
    rollout_policy.record_change(
        db,
        generation_enabled=False,
        # Keep the percentage. It is the cohort someone reasoned their way to;
        # zeroing it here would mean the restart after the incident begins with a
        # number typed from memory.
        percentage=current.percentage,
        changed_by=scope.principal.id,
        reason=body.reason,
    )
    db.commit()
    return read_rollout(scope=scope, db=db)


# ---------------------------------------------------------------------------
# Pipeline inspector — per-stage inputs and outputs for a run (M0.8, §8)
# ---------------------------------------------------------------------------
#
# **This surface carries children's answers and full prompt payloads, including
# `correct_answer` on the diagnose and leak-check inputs.** It is governed by
# M0.9's `can_read_audit_trail`, which is the policy §6 already applies to
# `/admin/llm-calls`: an admin, or a teacher for a session belonging to one of
# their own students. It must never be reachable from the student client.


class PromptView(BaseModel):
    """The prompt as sent, replayed from the ledger.

    Not re-rendered. Half the stages substitute values the ledger's
    `PromptContext` does not carry — `generate_hint` a strategy and hint level,
    `leak_check` a hint — so re-rendering those would produce a prompt that
    reads plausibly and was never sent. This is the recorded text or nothing.
    """

    model_config = ConfigDict(frozen=True)

    system: str
    user: str


class StageRunView(BaseModel):
    model_config = ConfigDict(frozen=True)

    stage: str
    ordinal: int
    outcome: str
    used_model: bool
    """False means the stage took its deterministic path — the rule pre-check
    fired, the keyed lookup hit, a template rendered, or the provider was down.
    A view that showed only model calls would render a full outage as a blank
    screen."""

    model_id: str | None
    prompt_version: str | None
    prompt: PromptView | None
    """The text this call actually sent. `None` on a deterministic stage, which
    sent nothing, and on a model call made before prompts were recorded — said
    plainly rather than reconstructed, because a prompt shown to explain a grade
    has to be the one that produced it."""

    tokens_in: int | None
    tokens_out: int | None
    latency_ms: int | None
    duration_ms: int | None
    cost_usd: float
    inputs: dict[str, object]
    outputs: dict[str, object]
    first_sequence: int
    started_at: dt.datetime


class TimelineEventView(BaseModel):
    model_config = ConfigDict(frozen=True)

    sequence: int
    event_type: str
    detail: dict[str, object]
    occurred_at: dt.datetime


class RunTraceView(BaseModel):
    model_config = ConfigDict(frozen=True)

    session_id: uuid.UUID
    stages: list[StageRunView]
    timeline: list[TimelineEventView]
    total_cost_usd: float
    model_calls: int
    deterministic_stages: int
    degraded_stages: list[str]
    gaps: list[int]
    """Missing sequence numbers. Surfaced rather than smoothed over: a trace
    drawn from an incomplete record is a plausible story, not what happened."""


class RunSummaryView(BaseModel):
    model_config = ConfigDict(frozen=True)

    session_id: uuid.UUID
    student_id: uuid.UUID
    state: str
    attempt_count: int
    started_at: dt.datetime


class SwarmNodeView(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    stage: str | None
    """`None` for the two bookkeeping nodes at the graph's terminal edges. It is
    what tells a viewer not to hunt for a stage run that never existed."""

    entry: bool
    handoffs: list[str]
    purpose: str
    """One sentence on what this node is for, from `swarm.NODE_PURPOSE`. The
    canvas is otherwise a picture of names, and a name is not an explanation."""

    tier: str | None
    """Which model tier this node's stage is configured for, or `None` where the
    stage has no model path. Read from `packages.llm.config` rather than
    restated, so the picture cannot claim a tier the client would not use."""


@router.get("/topology", response_model=list[SwarmNodeView])
def read_topology(scope: Scope = Depends(current_scope)) -> list[SwarmNodeView]:
    """The swarm's shape, derived from the code that runs it.

    Nodes and edges come from each agent's `Command[Literal[...]]` return
    annotation — the same annotation LangGraph validates the graph against — so
    a drawing of this pipeline cannot disagree with the pipeline. A hand-drawn
    diagram is accurate on the day it is drawn; this one is accurate or the
    graph is broken.
    """
    _authorize(scope)
    return [
        SwarmNodeView(
            id=node.id,
            stage=node.stage.value if node.stage else None,
            entry=node.entry,
            handoffs=list(node.handoffs),
            purpose=node.purpose,
            tier=_tier_for(node.stage),
        )
        for node in swarm.topology()
    ]


def _prompt_of(call: LLMCall | None) -> PromptView | None:
    """Read the recorded prompt off a ledger row, or report that there isn't one.

    Deliberately all-or-nothing. A row written before prompts were captured has
    no `rendered_prompt` key, and one written by a future payload change might
    have a different shape; in both cases the honest answer is that the text is
    not on record. Filling either half from somewhere else would produce a
    prompt that looks like evidence — and this surface exists precisely so a
    grade can be defended by what happened rather than by what usually happens.
    """
    if call is None:
        return None
    recorded = call.input_payload.get("rendered_prompt")
    if not isinstance(recorded, dict):
        return None
    system, user = recorded.get("system"), recorded.get("user")
    if not isinstance(system, str) or not isinstance(user, str):
        return None
    return PromptView(system=system, user=user)


def _tier_for(stage: PipelineStage | None) -> str | None:
    if stage is None:
        return None
    config = STAGE_CONFIG.get(stage)
    return config.tier.value if config else None


@router.get("/economics", response_model=Economics)
def read_economics(
    scope: Scope = Depends(current_scope),
    db: DbSession = Depends(get_db),
) -> Economics:
    """What the pipeline costs and how long it takes (§8, P1.10).

    Admin-only, like the run list: it spans every child in the deployment.

    Nothing was instrumented for this. The `LLMCall` ledger has carried cost,
    latency, tokens, model, and prompt version on every call since M0.4 — the
    ledger was built to make a grade defensible, and answering "what does this
    cost" turns out to be the same data asked a different question.
    """
    _authorize(scope)
    return build_economics(db)


@router.get("/runs", response_model=list[RunSummaryView])
def list_runs(
    scope: Scope = Depends(current_scope),
    db: DbSession = Depends(get_db),
) -> list[RunSummaryView]:
    """Recent sessions, newest first.

    Admin-only, unlike the per-run view below: this spans every child in the
    deployment, and M0.9's whole design is that a teacher's reach is derived from
    their own classrooms rather than granted by their role.
    """
    _authorize(scope)
    rows = (
        db.execute(select(SessionRow).order_by(SessionRow.started_at.desc()).limit(RUNS_LIMIT))
        .scalars()
        .all()
    )
    return [
        RunSummaryView(
            session_id=row.id,
            student_id=row.student_id,
            state=row.state.value,
            attempt_count=row.attempt_count,
            started_at=row.started_at,
        )
        for row in rows
    ]


@router.get("/runs/{session_id}", response_model=RunTraceView)
def read_run(
    session_id: uuid.UUID,
    scope: Scope = Depends(current_scope),
    db: DbSession = Depends(get_db),
) -> RunTraceView:
    """Every stage of one run, with what went in and what came out."""
    session_row = db.get(SessionRow, session_id)
    # Same response for "no such session" and "not your session", so this cannot
    # be used to enumerate which session ids are real.
    if session_row is None or not can_read_audit_trail(scope, student_id=session_row.student_id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="not your session")

    result = build_trace(db, session_id)
    return RunTraceView(
        session_id=session_id,
        stages=[
            StageRunView(
                stage=run.stage.value,
                ordinal=run.ordinal,
                outcome=run.outcome,
                used_model=run.used_model,
                model_id=run.llm_call.model_id if run.llm_call else None,
                prompt_version=run.llm_call.prompt_version if run.llm_call else None,
                prompt=_prompt_of(run.llm_call),
                tokens_in=run.llm_call.tokens_in if run.llm_call else None,
                tokens_out=run.llm_call.tokens_out if run.llm_call else None,
                latency_ms=run.llm_call.latency_ms if run.llm_call else None,
                duration_ms=run.duration_ms,
                cost_usd=run.cost_usd,
                inputs=run.inputs,
                outputs=run.outputs,
                first_sequence=run.first_sequence,
                started_at=run.started_at,
            )
            for run in result.stages
        ],
        timeline=[
            TimelineEventView(
                sequence=event.sequence,
                event_type=event.event_type.value,
                detail=event.detail,
                occurred_at=event.occurred_at,
            )
            for event in result.timeline
        ],
        total_cost_usd=result.total_cost_usd,
        model_calls=result.model_calls,
        deterministic_stages=result.deterministic_stages,
        degraded_stages=[s.value for s in result.degraded_stages],
        gaps=list(result.gaps),
    )
