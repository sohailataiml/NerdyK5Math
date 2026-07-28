"""Grading (§3.5).

Closed-form answers only for now. §3.5's full design is LLM-normalizes /
symbolic-decides plus a rubric grader for open responses; the rubric path is P2.2
and is not built. What is here is the part that matters first and is exact.

The division of labour is §3.5's: the checker decides mathematical truth, and
where a model claim and the checker disagree, **the checker wins and the
disagreement is logged**. That is the whole reason a CAS is in this architecture.

The checker is injected as a Protocol rather than imported. `services.symbolic`
is deliberately isolated because it evaluates untrusted input (M0.7); calling it
in-process would hand that risk to the orchestrator and undo the isolation.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from packages.domain.enums import GradeMethod, PipelineStage
from services.orchestrator.state import PipelineDeps, Problem, StageOutcome


@dataclass(frozen=True)
class Grade:
    score: float
    method: GradeMethod
    confidence: float
    symbolic_agreed: bool | None = None
    needs_review: bool = False
    reason: str | None = None


def run(
    deps: PipelineDeps,
    *,
    session_id: UUID,
    problem: Problem,
    student_answer: str,
    diagnosed_gap: bool = False,
) -> StageOutcome[Grade]:
    """Grade one answer.

    `diagnosed_gap` carries §3.5's consistency check: a "correct" arriving
    immediately after a diagnosed fundamental gap is suspicious enough to route
    to a teacher rather than be shown to the child as final.
    """
    deps.recorder.stage_started(PipelineStage.GRADE)

    if deps.symbolic is None:
        # No checker: grading cannot be done exactly, and guessing at a child's
        # grade is not an acceptable degradation. Escalate.
        deps.recorder.escalated(reason="no symbolic checker available")
        return StageOutcome(
            value=Grade(
                score=0.0,
                method=GradeMethod.TEACHER,
                confidence=0.0,
                needs_review=True,
                reason="no symbolic checker available",
            ),
            used_fallback=True,
            reason="no symbolic checker available",
        )

    equivalent = deps.symbolic.equivalent(expected=problem.correct_answer, actual=student_answer)
    score = 1.0 if equivalent else 0.0

    if equivalent and diagnosed_gap:
        # §3.5's contradiction check.
        deps.recorder.escalated(reason="correct immediately after a diagnosed fundamental gap")
        deps.recorder.graded(score=score, method=GradeMethod.SYMBOLIC.value, review=True)
        return StageOutcome(
            value=Grade(
                score=score,
                method=GradeMethod.SYMBOLIC,
                confidence=1.0,
                symbolic_agreed=True,
                needs_review=True,
                reason="graded correct straight after a diagnosed gap",
            )
        )

    deps.recorder.graded(score=score, method=GradeMethod.SYMBOLIC.value)
    return StageOutcome(
        value=Grade(
            score=score,
            method=GradeMethod.SYMBOLIC,
            # Symbolic equivalence is exact — there is nothing to be uncertain
            # about, so reporting anything below 1.0 would be false modesty that
            # downstream confidence gates would then act on.
            confidence=1.0,
            symbolic_agreed=True,
        )
    )
