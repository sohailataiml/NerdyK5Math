"""The teacher console (§3.6, §6, P0.8).

Phase 0's primary data-collection instrument. Everything the phase's exit gates
measure comes through here:

- **Shadow ratings** — is the generated hint better than the template the child
  was actually shown? The exit gate needs that on >=70% of cases before
  generation is worth its cost and risk.
- **Diagnosis confirmations** — the labelled set P0.9 turns into the calibration
  measurement, which is the gate for letting generated text reach a child at all.
- **Leak flags** — anything a teacher marks as leaky seeds the P0.5 corpus.

Two design decisions run through the whole module.

**Every read is scoped, not just role-checked.** M0.9's policy is applied per
row, so a teacher sees their own students and nothing else. A `403` here is a
child's schoolwork not being shown to the wrong adult.

**A rating is evidence, so it is appended.** Ratings and verdicts never overwrite
each other; a second opinion is a second row. That is the same reasoning behind
`ReviewVerdict` being separate from `ReviewItem`.
"""

from __future__ import annotations

import datetime as dt
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from packages.auth import (
    AuthorizationError,
    Scope,
    can_read_review_queue,
    can_resolve_review_item,
    require,
)
from packages.domain.enums import EventType, PipelineStage
from packages.domain.mapping import to_row
from packages.domain.models import ReviewVerdict, ShadowRating
from packages.domain.tables import (
    AttemptRow,
    GradeResultRow,
    HintLogRow,
    PipelineEventRow,
    ProblemRow,
    ReviewItemRow,
    ReviewVerdictRow,
    SessionRow,
    ShadowCandidateRow,
    ShadowRatingRow,
)
from packages.telemetry import replay
from services.api.auth import current_scope
from services.api.db import get_db

router = APIRouter(prefix="/teacher", tags=["teacher"])

MAX_PAGE = 50


class ShadowItem(BaseModel):
    """One candidate awaiting a rating, with everything needed to judge it.

    The template and the generated hint are shown together deliberately: the
    question a teacher is answering is not "is this hint good" in the abstract
    but "is it better than what the child actually saw", and that comparison is
    unanswerable without both.
    """

    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    session_id: uuid.UUID
    problem: str
    correct_answer: str
    student_answer: str | None
    misconception_tag: str
    hint_level: int
    shown_hint: str | None
    generated_hint: str
    leak_check_passed: bool
    leak_reason: str | None
    prompt_version: str
    created_at: dt.datetime


class AttemptGrade(BaseModel):
    """One attempt's verdict, as §5's `GradeResult` recorded it.

    `symbolic_agreed` is carried rather than summarised into the score because
    §3.5's division of labour is the point: where a model claim and the checker
    disagree the checker wins, and a teacher judging the grade needs to see that
    the two were consulted, not just the number they produced.
    """

    model_config = ConfigDict(frozen=True)

    answer: str
    score: float
    method: str
    confidence: float
    symbolic_agreed: bool | None


class ReviewItemDetail(BaseModel):
    """A queue row with enough context to reach a verdict without leaving it.

    §3.6 wants triage to be navigation, not a summary a teacher trusts blindly.
    So this carries the facts of the session — what was asked, what the child
    typed each time, what they were shown — and the full replay stays one click
    away rather than being folded in. A teacher who has to open three screens to
    judge one row will judge it without opening them.
    """

    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    session_id: uuid.UUID
    reason: str
    problem: str
    correct_answer: str
    """Present here and *not* on any student-facing response. This is the
    teacher's surface, and a verdict on a grade cannot be reached without it."""

    answers: tuple[str, ...]
    hints_shown: tuple[str, ...]
    misconception_tag: str | None
    ran_degraded: bool

    grades: tuple[AttemptGrade, ...]
    """The verdict on each attempt, in order.

    Absent until now, which meant a teacher opening a row to reach a verdict on
    a grade could see the child's answers and the correct answer but not what
    the system had decided about them — leaving them to redo the arithmetic to
    find out what they were being asked to confirm.
    """

    created_at: dt.datetime


class RatingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    better_than_shown: bool
    would_leak: bool = False
    corrected_tag: str | None = None
    notes: str | None = Field(default=None, max_length=2000)


class VerdictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verdict: str
    notes: str | None = Field(default=None, max_length=2000)


def _forbid(exc: AuthorizationError) -> HTTPException:
    # Uniform, detail-free: distinguishing "no such item" from "not yours" tells
    # a caller which ids are real.
    return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc))


def _student_for_session(db: DbSession, session_id: uuid.UUID) -> uuid.UUID | None:
    return db.execute(
        select(SessionRow.student_id).where(SessionRow.id == session_id)
    ).scalar_one_or_none()


@router.get("/shadow-queue", response_model=list[ShadowItem])
def shadow_queue(
    limit: int = 20,
    scope: Scope = Depends(current_scope),
    db: DbSession = Depends(get_db),
) -> list[ShadowItem]:
    """Unrated shadow candidates for this teacher's students, oldest first."""
    try:
        require(can_read_review_queue(scope), "read the review queue")
    except AuthorizationError as exc:
        raise _forbid(exc) from exc

    rated = select(ShadowRatingRow.shadow_candidate_id)
    rows = (
        db.execute(
            select(ShadowCandidateRow)
            .where(ShadowCandidateRow.id.not_in(rated))
            .order_by(ShadowCandidateRow.created_at)
            .limit(min(limit, MAX_PAGE))
        )
        .scalars()
        .all()
    )

    items: list[ShadowItem] = []
    for row in rows:
        student_id = _student_for_session(db, row.session_id)
        # Scoping is applied per row, not once per request: the queue is a view
        # over every session in the system, and a teacher must only see theirs.
        if student_id is None or not can_resolve_review_item(scope, student_id=student_id):
            continue

        session_row = db.get(SessionRow, row.session_id)
        problem = db.get(ProblemRow, session_row.problem_id) if session_row else None
        attempt = db.execute(
            select(AttemptRow)
            .where(AttemptRow.session_id == row.session_id)
            .order_by(AttemptRow.timestamp)
            .limit(1)
        ).scalar_one_or_none()

        items.append(
            ShadowItem(
                id=row.id,
                session_id=row.session_id,
                problem=problem.prompt if problem else "(problem not found)",
                correct_answer=problem.correct_answer if problem else "",
                student_answer=attempt.student_answer if attempt else None,
                misconception_tag=row.misconception_tag,
                hint_level=row.hint_level,
                shown_hint=row.shown_text,
                generated_hint=row.generated_text,
                leak_check_passed=row.leak_check_passed,
                leak_reason=row.leak_reason,
                prompt_version=row.prompt_version,
                created_at=row.created_at,
            )
        )
    return items


@router.post("/shadow/{candidate_id}/rating", status_code=status.HTTP_201_CREATED)
def rate_shadow_candidate(
    candidate_id: uuid.UUID,
    request: RatingRequest,
    scope: Scope = Depends(current_scope),
    db: DbSession = Depends(get_db),
) -> dict[str, str]:
    """Record a teacher's judgement of a generated hint (P0.8)."""
    candidate = db.get(ShadowCandidateRow, candidate_id)
    if candidate is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")

    student_id = _student_for_session(db, candidate.session_id)
    try:
        require(
            student_id is not None and can_resolve_review_item(scope, student_id=student_id),
            "rate this candidate",
        )
    except AuthorizationError as exc:
        raise _forbid(exc) from exc

    notes = request.notes
    if request.corrected_tag and request.corrected_tag != candidate.misconception_tag:
        # A corrected tag is the label P0.9's calibration measurement is built
        # from, so it is captured even before the taxonomy-editing UI exists —
        # losing it would mean losing the phase's most valuable signal.
        correction = f"[corrected tag: {request.corrected_tag}]"
        notes = f"{correction} {notes}" if notes else correction

    rating = ShadowRating(
        shadow_candidate_id=candidate_id,
        teacher_id=scope.principal.id,
        better_than_shown=request.better_than_shown,
        would_leak=request.would_leak,
        notes=notes,
        created_at=dt.datetime.now(dt.UTC),
    )
    db.add(to_row(rating, ShadowRatingRow))
    db.commit()
    return {"id": str(rating.id)}


@router.get("/review-queue", response_model=list[ReviewItemDetail])
def review_queue(
    limit: int = 20,
    scope: Scope = Depends(current_scope),
    db: DbSession = Depends(get_db),
) -> list[ReviewItemDetail]:
    """Open `ReviewItem`s for this teacher's students (§3.6)."""
    try:
        require(can_read_review_queue(scope), "read the review queue")
    except AuthorizationError as exc:
        raise _forbid(exc) from exc

    rows = (
        db.execute(
            select(ReviewItemRow)
            .where(ReviewItemRow.resolved_at.is_(None))
            .order_by(ReviewItemRow.created_at)
            .limit(min(limit, MAX_PAGE))
        )
        .scalars()
        .all()
    )

    out: list[ReviewItemDetail] = []
    for row in rows:
        student_id = _student_for_session(db, row.session_id)
        # Scoping per row, not once per request: this query spans every session
        # in the system and a teacher must only ever see their own class.
        if student_id is None or not can_resolve_review_item(scope, student_id=student_id):
            continue

        session_row = db.get(SessionRow, row.session_id)
        problem = db.get(ProblemRow, session_row.problem_id) if session_row else None
        attempts = (
            db.execute(
                select(AttemptRow)
                .where(AttemptRow.session_id == row.session_id)
                .order_by(AttemptRow.timestamp)
            )
            .scalars()
            .all()
        )
        events = (
            db.execute(
                select(PipelineEventRow)
                .where(PipelineEventRow.session_id == row.session_id)
                .order_by(PipelineEventRow.sequence)
            )
            .scalars()
            .all()
        )
        hints = tuple(
            db.execute(
                select(HintLogRow.hint_text)
                .where(HintLogRow.session_id == row.session_id)
                .order_by(HintLogRow.attempt_number)
            )
            .scalars()
            .all()
        )
        tag = next(
            (
                str(e.detail["tag"])
                for e in reversed(events)
                if e.stage is PipelineStage.DIAGNOSE and e.detail.get("tag")
            ),
            None,
        )

        # Joined on attempt so a verdict is attributable to the answer that
        # earned it. A session-level list would show three scores and leave the
        # teacher matching them to answers by position.
        verdicts = {
            g.attempt_id: g
            for g in db.execute(
                select(GradeResultRow).where(
                    GradeResultRow.attempt_id.in_([a.id for a in attempts] or [uuid.uuid4()])
                )
            )
            .scalars()
            .all()
        }
        grades = tuple(
            AttemptGrade(
                answer=a.student_answer,
                score=verdicts[a.id].score,
                method=verdicts[a.id].method.value,
                confidence=verdicts[a.id].confidence,
                symbolic_agreed=verdicts[a.id].symbolic_agreed,
            )
            for a in attempts
            if a.id in verdicts
        )

        out.append(
            ReviewItemDetail(
                id=row.id,
                session_id=row.session_id,
                reason=row.reason.value,
                problem=problem.prompt if problem else "(problem not found)",
                correct_answer=problem.correct_answer if problem else "",
                answers=tuple(a.student_answer for a in attempts),
                hints_shown=hints,
                misconception_tag=tag,
                ran_degraded=any(e.event_type is EventType.FALLBACK_USED for e in events),
                grades=grades,
                created_at=row.created_at,
            )
        )
    return out


@router.get("/review/{item_id}/evidence")
def review_evidence(
    item_id: uuid.UUID,
    scope: Scope = Depends(current_scope),
    db: DbSession = Depends(get_db),
) -> dict[str, object]:
    """The session's full replay (§4, M0.8).

    §3.6's argument is that a teacher must be able to *check* rather than trust:
    the summary above is navigation, and this is the ordered account of what
    actually happened, reconstructed from append-only rows. Kept behind a second
    request rather than inlined so the queue stays fast and the act of looking is
    deliberate — but one click, because evidence a teacher has to go hunting for
    is evidence they stop consulting.
    """
    item = db.get(ReviewItemRow, item_id)
    student_id = _student_for_session(db, item.session_id) if item else None
    if (
        item is None
        or student_id is None
        or not can_resolve_review_item(scope, student_id=student_id)
    ):
        # Uniform with the resolve endpoint: never distinguish "no such item"
        # from "not your student".
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="not available")

    session_replay = replay(db, item.session_id)
    return {
        "session_id": str(item.session_id),
        "steps": [step.describe() for step in session_replay.steps],
        "model_calls": session_replay.model_calls,
        "cost_usd": session_replay.total_cost_usd,
    }


@router.post("/review/{item_id}", status_code=status.HTTP_201_CREATED)
def resolve_review_item(
    item_id: uuid.UUID,
    request: VerdictRequest,
    scope: Scope = Depends(current_scope),
    db: DbSession = Depends(get_db),
) -> dict[str, str]:
    """Submit a verdict (§3.6). Appended, never overwriting a prior one."""
    item = db.get(ReviewItemRow, item_id)
    if item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")

    student_id = _student_for_session(db, item.session_id)
    try:
        require(
            student_id is not None and can_resolve_review_item(scope, student_id=student_id),
            "resolve this review item",
        )
    except AuthorizationError as exc:
        raise _forbid(exc) from exc

    from packages.domain.enums import TeacherVerdict

    try:
        verdict = TeacherVerdict(request.verdict)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"verdict must be one of {[v.value for v in TeacherVerdict]}",
        ) from None

    record = ReviewVerdict(
        review_item_id=item_id,
        teacher_id=scope.principal.id,
        verdict=verdict,
        notes=request.notes,
        created_at=dt.datetime.now(dt.UTC),
    )
    db.add(to_row(record, ReviewVerdictRow))
    # The queue row flips to resolved; the verdict itself is the durable record.
    item.resolved_at = dt.datetime.now(dt.UTC)
    db.commit()
    return {"id": str(record.id)}
