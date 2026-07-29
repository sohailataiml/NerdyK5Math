"""The §4 state machine's entry points.

    AwaitingAnswer -> Diagnosing -> RetrievingCurriculum -> GeneratingHint
    -> LeakChecking -> AwaitingStudentRetry -> Grading
    -> (Complete | Escalated | Diagnosing[retry])

`run_attempt` covers everything up to `AwaitingStudentRetry` / `Escalated`; it
delegates to the LangGraph swarm in `services.orchestrator.swarm` — see that
module's docstring for how the handoffs between diagnose/retrieve/generate/
leak-check are wired and why they stay deterministic even though the engine
is now a swarm of agents rather than a hand-written loop. Grading happens on
the student's *next* submission, a separate HTTP request, so `check_answer` /
`grade_answer` stay outside that graph and run here directly.

Stages never import one another (Implementation-Plan.md §1 rule 1); this
module (together with `swarm.py`) is the only place that knows the order.
"""

from __future__ import annotations

from uuid import UUID

from packages.domain.enums import UNKNOWN_TAG_LABEL, ReviewReason, SessionState
from packages.domain.models import GradeResult
from services.orchestrator import swarm
from services.orchestrator.review import reasons_for_review
from services.orchestrator.stages import diagnose, grade, safety
from services.orchestrator.state import PipelineDeps, Problem, StageOutcome

MAX_GENERATION_ATTEMPTS = swarm.MAX_GENERATION_ATTEMPTS
"""§3.3: regenerate once with a stricter prompt, then fall back to a template."""

AttemptResult = swarm.AttemptResult
"""What the child gets, and everything needed to explain why (defined in
`swarm.py`, alongside the agents that build it)."""


def screen_submission(
    deps: PipelineDeps,
    *,
    session_id: UUID,
    problem: Problem,
    student_answer: str,
    student_id: UUID | None = None,
    attempt: int = 1,
) -> StageOutcome[safety.SafetyScreen]:
    """Run the §7 welfare screen on one submission, before anything judges it.

    The screen is also the swarm's entry node, and for a caller that reaches the
    swarm on every submission that is the whole story. A served UI is not such a
    caller: it grades first and only enters the swarm when the answer was wrong,
    so a screen that lived only inside the swarm never saw a child who typed the
    right answer and something that matters in the same box — exactly the
    children `stages/safety.py` says a screen must not miss.

    So the screen is hoisted to the one place every submission passes through,
    and the result is handed to `run_attempt` rather than re-derived there. Two
    screens on one submission would mean two alerts for one disclosure and two
    billed classifier calls, and a responder who is paged twice for the same
    child learns to trust the count less.
    """
    return safety.run(
        deps,
        session_id=session_id,
        student_id=student_id,
        problem=problem,
        student_answer=student_answer,
        attempt=attempt,
    )


def run_attempt(
    deps: PipelineDeps,
    *,
    session_id: UUID,
    problem: Problem,
    student_answer: str,
    hint_level: int = 1,
    attempt: int = 1,
    student_id: UUID | None = None,
    record_submission: bool = True,
    screened: StageOutcome[safety.SafetyScreen] | None = None,
) -> AttemptResult:
    """Diagnose -> retrieve -> generate -> leak-check, producing one hint.

    `record_submission=False` is for a caller that has already logged the
    submission. A served UI has to grade before it knows whether to hint, so it
    records the answer first; leaving this on would put `answer_submitted` after
    `graded` in the log, and a replay that says a child's answer was graded
    before they submitted it is not a faithful account of their session.

    `screened` is the same idea for the welfare screen: a caller that already ran
    `screen_submission` passes the outcome in, and the swarm carries it rather
    than screening the child twice. Left `None`, the swarm screens — which is
    right for a caller whose only entry point this is.
    """
    if record_submission:
        deps.recorder.answer_submitted(attempt_number=attempt, answer=student_answer)

    return swarm.run(
        deps,
        session_id=session_id,
        problem=problem,
        student_answer=student_answer,
        hint_level=hint_level,
        attempt=attempt,
        student_id=student_id,
        screened=screened,
    )


def check_answer(
    deps: PipelineDeps,
    *,
    session_id: UUID,
    problem: Problem,
    student_answer: str,
    prior_diagnosis: diagnose.Diagnosis | None = None,
    attempt_id: UUID | None = None,
) -> grade.Grade:
    """Grade one submission (§3.5) **without ending the session**.

    Split from `grade_answer` because a served UI has to grade every submission
    to know whether to hint, and most of those submissions are wrong and the
    session continues. Emitting `session_completed` on each of them would put a
    terminal event in the middle of a live session — and `show_replay` would then
    tell a teacher the child finished three times.

    `attempt_id` is what makes the verdict durable. §5's `GradeResult` is keyed
    by attempt, so a caller that does not supply one gets the event log and
    nothing else — fine for a script, not for a session a teacher may have to
    defend.
    """
    deps.recorder.state_changed(frm=SessionState.AWAITING_STUDENT_RETRY, to=SessionState.GRADING)
    result = grade.run(
        deps,
        session_id=session_id,
        problem=problem,
        student_answer=student_answer,
        # A tag that is not `unknown` means a real misconception was identified,
        # which is what makes an immediate "correct" worth a second look.
        diagnosed_gap=bool(prior_diagnosis and prior_diagnosis.tag != UNKNOWN_TAG_LABEL),
    )
    _record_grade(deps, result.value, attempt_id=attempt_id)
    return result.value


def _record_grade(deps: PipelineDeps, graded: grade.Grade, *, attempt_id: UUID | None) -> None:
    """Persist §5's `GradeResult`, when there is an attempt to attach it to."""
    if deps.grade_sink is None or attempt_id is None:
        return
    deps.grade_sink.record(
        GradeResult(
            attempt_id=attempt_id,
            score=graded.score,
            confidence=graded.confidence,
            method=graded.method,
            symbolic_agreed=graded.symbolic_agreed,
        )
    )


def complete_session(
    deps: PipelineDeps,
    graded: grade.Grade,
    *,
    attempt: AttemptResult | None = None,
    hints_exhausted: bool = False,
) -> tuple[ReviewReason, ...]:
    """Record the terminal transition and route the session to a teacher (§3.6).

    Routing lives here, at the state machine's terminal edge, rather than in
    whichever caller happens to end a session. That is the whole reason
    `ReviewItem` was never written: the table, the queue endpoint, and the
    console all existed, and the one place that knew a session had finished did
    not tell anyone. A caller that forgets is a child told a teacher will look
    when nobody was told.

    Returns the reasons routed, so a caller can say something true to the child.
    """
    final = SessionState.ESCALATED if graded.needs_review else SessionState.COMPLETE
    deps.recorder.state_changed(frm=SessionState.GRADING, to=final)
    deps.recorder.session_completed(
        outcome="review" if graded.needs_review else ("correct" if graded.score else "incorrect")
    )
    return _route_for_review(
        deps, graded=graded, attempt=attempt, hints_exhausted=hints_exhausted, escalated=False
    )


def _route_for_review(
    deps: PipelineDeps,
    *,
    graded: grade.Grade | None,
    attempt: AttemptResult | None,
    hints_exhausted: bool,
    escalated: bool,
) -> tuple[ReviewReason, ...]:
    """Decide, record, and hand off. Returns what was routed, or ()."""
    reasons = reasons_for_review(
        tag=attempt.diagnosis.tag if attempt else None,
        confidence=attempt.diagnosis.confidence if attempt else None,
        leak_rejections=attempt.leak_rejections if attempt else 0,
        escalated=escalated or bool(attempt and attempt.escalated),
        hints_exhausted=hints_exhausted,
        needs_review=bool(graded and graded.needs_review),
        # Phase 0 is shadow mode and Phase 0 is 100% review (P0.8). When shadow
        # mode goes off, P1.6's filtered rules take over and the audit sample
        # replaces the catch-all — one switch, not two that must agree.
        review_everything=deps.shadow_mode,
    )
    if not reasons or deps.review_sink is None:
        return ()

    deps.review_sink.route(session_id=deps.recorder.session_id, reason=reasons[0])
    deps.recorder.routed_for_review(reason=reasons[0].value, also=[r.value for r in reasons[1:]])
    return reasons


def end_session_for_review(
    deps: PipelineDeps,
    *,
    reason: str | None = None,
    frm: SessionState | None = None,
    attempt: AttemptResult | None = None,
    hints_exhausted: bool = False,
) -> tuple[ReviewReason, ...]:
    """Close a session that is going to a teacher rather than completing.

    `reason=None` means the escalation was already recorded — `run_attempt`
    escalates itself when no hint can be cleared, and emitting a second
    `escalated` event would make the replay read as two separate failures. Pass a
    reason only for an escalation the graph has not already logged, which today
    means running out of hint levels.

    Returns the review reasons routed. An empty tuple means no teacher was told,
    which is the caller's cue not to promise a child that one was.
    """
    if reason is not None:
        deps.recorder.escalated(reason=reason)
        deps.recorder.state_changed(frm=frm, to=SessionState.ESCALATED)
    deps.recorder.session_completed(outcome="review")
    return _route_for_review(
        deps, graded=None, attempt=attempt, hints_exhausted=hints_exhausted, escalated=True
    )


def grade_answer(
    deps: PipelineDeps,
    *,
    session_id: UUID,
    problem: Problem,
    student_answer: str,
    prior_diagnosis: diagnose.Diagnosis | None = None,
) -> grade.Grade:
    """Grade a follow-up answer and end the session (§3.5)."""
    graded = check_answer(
        deps,
        session_id=session_id,
        problem=problem,
        student_answer=student_answer,
        prior_diagnosis=prior_diagnosis,
    )
    complete_session(deps, graded)
    return graded
