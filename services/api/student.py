"""The student session API (P0.11).

The surface a child actually touches. Everything else in this repo produces
evidence *about* sessions; nothing until now let a session happen without an
engineer running a script, and Phase 0's exit gates all need sessions that real
children had.

Three rules hold here that hold nowhere else in the codebase.

**No response ever contains the correct answer.** Not on a hint, not on a wrong
answer, not on completion. §12 makes leakage the defining risk, and the whole
leak-check apparatus guards the *hint text* — a JSON field carrying
`correct_answer` would walk straight past it. A child with the browser's network
tab open is still a child in the pilot, so the answer stays server-side and the
client is told only whether what they typed was right. `_SafeProblem` exists to
make that structural rather than remembered.

**Grading is never client-side.** The client sends what the child typed and gets
back a verdict. It is not given a target to compare against, which is the same
rule stated from the other end.

**Escalation is not a loss state (§11.5).** When no hint can be cleared, or the
child has used every level, the response says the work is going to their teacher
— not that they failed. A child who reads "escalated" as punishment stops asking
for hints, and hint-seeking is the behaviour this system exists to reward.
"""

from __future__ import annotations

import datetime as dt
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession

from packages.auth import Scope, can_read_session
from packages.domain.enums import GradeBand, Operation, Role, SessionState
from packages.domain.mapping import to_row
from packages.domain.models import MAX_HINT_LEVEL, Attempt, Session
from packages.domain.tables import AttemptRow, ProblemRow, SessionRow
from packages.fallbacks.rules import parse_problem
from services.api.auth import current_scope
from services.api.db import get_db
from services.api.pipeline import build_deps
from services.orchestrator import graph
from services.orchestrator.state import Problem

router = APIRouter(prefix="/student", tags=["student"])


class Visual(BaseModel):
    """A §11.2 representation, as data for the client to draw.

    Sent as operands rather than rendered markup so the same session can be a
    ten-frame on screen and a spoken description for a screen reader (§11.5's
    text-only equivalent) without the server deciding which.

    Note what is absent: the total. The ten-frame shows what the child is
    *starting from*, never where they end up.
    """

    model_config = ConfigDict(frozen=True)

    kind: str
    left: int
    right: int
    operation: str


class _SafeProblem(BaseModel):
    """A problem as the client may see it.

    The type exists to make the omission of `correct_answer` structural. A dict
    built by hand would carry it the first time someone reached for the row
    directly, and that mistake is invisible in review because the field name
    looks like it belongs.
    """

    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    prompt: str
    grade_band: str
    visual: Visual | None


class SessionStarted(BaseModel):
    model_config = ConfigDict(frozen=True)

    session_id: uuid.UUID
    problem: _SafeProblem
    attempt: int
    max_hint_level: int = MAX_HINT_LEVEL


class AnswerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answer: str = Field(min_length=1, max_length=500)
    """Bounded because it lands in a prompt. 500 characters is far past any real
    K-5 answer and far short of a payload worth paying to process."""


class AnswerResponse(BaseModel):
    """What the child gets back. Deliberately small."""

    model_config = ConfigDict(frozen=True)

    correct: bool
    hint: str | None = None
    hint_level: int | None = None
    attempt: int
    done: bool = False
    going_to_teacher: bool = False
    """Not "escalated" and not "failed" (§11.5). The child is told an adult will
    look at their work, which is what actually happens and is not a punishment."""

    message: str | None = None


TEN_FRAME_CAPACITY = 10


def _visual_for(prompt: str) -> Visual | None:
    """Pick the §11.2 representation that fits the problem.

    A ten-frame holds ten. Drawing `13 - 8` in one produces a picture that
    contradicts the question in front of the child, which is worse than no
    picture — the whole point of a concrete representation is that it can be
    trusted. Above ten, and for taking away, a number line is the model that
    actually shows the operation.
    """
    parsed = parse_problem(prompt)
    if parsed is None:
        return None

    fits_a_ten_frame = (
        parsed.operation is Operation.ADDITION
        and parsed.left <= TEN_FRAME_CAPACITY
        and parsed.right <= TEN_FRAME_CAPACITY
    )
    return Visual(
        kind="ten_frame" if fits_a_ten_frame else "number_line",
        left=parsed.left,
        right=parsed.right,
        operation=parsed.operation.value,
    )


def _safe(row: ProblemRow) -> _SafeProblem:
    return _SafeProblem(
        id=row.id,
        prompt=row.prompt,
        grade_band=row.grade_band.value,
        visual=_visual_for(row.prompt),
    )


def _require_student(scope: Scope) -> uuid.UUID:
    """The student surface is for students.

    A teacher browsing here would produce sessions and attempts attributed to a
    child who never sat down, which quietly corrupts every Phase 0 measurement
    built on those rows.
    """
    if scope.role is not Role.STUDENT or scope.principal.student_id is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="this surface is for students"
        )
    return scope.principal.student_id


@router.post("/session", response_model=SessionStarted, status_code=status.HTTP_201_CREATED)
def start_session(
    scope: Scope = Depends(current_scope),
    db: DbSession = Depends(get_db),
) -> SessionStarted:
    """Begin work on a problem."""
    student_id = _require_student(scope)

    problem_row = db.execute(
        select(ProblemRow).order_by(func.random()).limit(1)
    ).scalar_one_or_none()
    if problem_row is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="no problems are loaded"
        )

    session = Session(
        student_id=student_id,
        problem_id=problem_row.id,
        started_at=dt.datetime.now(dt.UTC),
        state=SessionState.AWAITING_ANSWER,
        attempt_count=0,
    )
    db.add(to_row(session, SessionRow))
    db.commit()

    return SessionStarted(session_id=session.id, problem=_safe(problem_row), attempt=1)


@router.post("/session/{session_id}/answer", response_model=AnswerResponse)
def submit_answer(
    session_id: uuid.UUID,
    request: AnswerRequest,
    scope: Scope = Depends(current_scope),
    db: DbSession = Depends(get_db),
) -> AnswerResponse:
    """Submit an answer and get a hint, or confirmation that it was right."""
    student_id = _require_student(scope)

    session_row = db.get(SessionRow, session_id)
    # A session that does not exist and a session belonging to another child get
    # the identical response. Distinguishing them turns this endpoint into an
    # oracle for which session ids are real.
    if session_row is None or not can_read_session(scope, student_id=session_row.student_id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="not your session")

    if session_row.state in {SessionState.COMPLETE, SessionState.ESCALATED}:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="this session is finished")

    problem_row = db.get(ProblemRow, session_row.problem_id)
    if problem_row is None:  # pragma: no cover - foreign key guarantees it
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="no problem")

    attempt_number = session_row.attempt_count + 1
    parsed = parse_problem(problem_row.prompt)
    problem = Problem(
        prompt=problem_row.prompt,
        correct_answer=problem_row.correct_answer,
        grade_band=GradeBand(problem_row.grade_band.value),
        operands=({"a": str(parsed.left), "b": str(parsed.right)} if parsed is not None else {}),
    )

    # Held rather than discarded: §5's `GradeResult` is keyed by attempt, so the
    # verdict below can only be made durable if this id survives to the grading
    # call.
    attempt = Attempt(
        session_id=session_id,
        student_answer=request.answer,
        timestamp=dt.datetime.now(dt.UTC),
        hint_level_shown=session_row.attempt_count,
    )
    db.add(to_row(attempt, AttemptRow))
    db.flush()

    deps = build_deps(db, session_id)
    # Before grading: the child submitted, then it was judged, and the record has
    # to say so in that order.
    deps.recorder.answer_submitted(attempt_number=attempt_number, answer=request.answer)
    graded = graph.check_answer(
        deps,
        session_id=session_id,
        problem=problem,
        student_answer=request.answer,
        attempt_id=attempt.id,
    )
    if graded.score >= 1.0:
        graph.complete_session(deps, graded)
        session_row.state = SessionState.ESCALATED if graded.needs_review else SessionState.COMPLETE
        session_row.attempt_count = attempt_number
        db.commit()
        # `needs_review` here is §3.5's consistency check — correct immediately
        # after a diagnosed gap. The child is not told; from their side they got
        # it right, which they did. The review happens where reviews happen.
        return AnswerResponse(
            correct=True,
            attempt=attempt_number,
            done=True,
            message="You got it.",
        )

    # Wrong. Diagnose, retrieve, and produce the next hint level.
    hint_level = min(attempt_number, MAX_HINT_LEVEL)
    result = graph.run_attempt(
        deps,
        session_id=session_id,
        problem=problem,
        student_answer=request.answer,
        hint_level=hint_level,
        attempt=attempt_number,
        student_id=student_id,
        record_submission=False,  # already logged above, in the right order
    )

    session_row.attempt_count = attempt_number
    out_of_hints = attempt_number >= MAX_HINT_LEVEL

    if result.hint_text is None or out_of_hints:
        # §11.5: not a loss state. The child is told what actually happens next.
        routed = graph.end_session_for_review(
            deps,
            # `run_attempt` already escalated when no hint cleared; only the
            # out-of-hints case is news to the record.
            reason="hint levels exhausted" if result.hint_text is not None else None,
            frm=SessionState.AWAITING_STUDENT_RETRY,
            attempt=result,
            hints_exhausted=out_of_hints,
        )
        session_row.state = SessionState.ESCALATED
        db.commit()
        # The message follows the record rather than decorating it. Telling a
        # child their teacher will look at this is a promise, and it is keepable
        # only if a queue row exists — which is precisely what was missing while
        # this said it unconditionally and nothing was ever routed.
        return AnswerResponse(
            correct=False,
            hint=result.hint_text,
            hint_level=result.hint_level if result.hint_text else None,
            attempt=attempt_number,
            done=True,
            going_to_teacher=bool(routed),
            message=(
                "Nice work sticking with it. Your teacher is going to look at this one with you."
                if routed
                else "Nice work sticking with it. Let's come back to this one."
            ),
        )

    session_row.state = SessionState.AWAITING_STUDENT_RETRY
    db.commit()
    return AnswerResponse(
        correct=False,
        hint=result.hint_text,
        hint_level=result.hint_level,
        attempt=attempt_number,
    )
