"""Socratic hint generation (§3.3).

Produces a *candidate* hint. It is not cleared for display until the leak-check
stage passes it — §3.3 makes that guardrail blocking, and this stage returning a
string is not permission to show it to anyone.

The generator phrases a retrieved, teacher-approved strategy for this child and
this problem. It does not choose the strategy: §7 forbids stating remediation
that was not retrieved, because unconstrained explanation is where curriculum
drift starts.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from packages.domain.enums import HintSource, PipelineStage
from packages.fallbacks import TemplateError
from packages.fallbacks import render as render_template
from packages.llm import PromptContext
from packages.llm.errors import TransportError
from services.orchestrator.state import PipelineDeps, Problem, StageOutcome

PROMPT_VERSION = "v1"


@dataclass(frozen=True)
class HintCandidate:
    text: str
    level: int
    source: HintSource


def _template(
    deps: PipelineDeps, *, tag: str, problem: Problem, level: int, attempt: int
) -> HintCandidate | None:
    """The pre-approved path. Returns None when even the template would leak."""
    try:
        rendered = render_template(
            tag=tag,
            grade_band=problem.grade_band,
            level=level,
            values=problem.operands,
            correct_answer=problem.correct_answer,
            attempt=attempt,
        )
    except TemplateError as exc:
        deps.recorder.stage_failed(PipelineStage.GENERATE_HINT, reason=str(exc))
        return None
    return HintCandidate(text=rendered.text, level=level, source=HintSource.TEMPLATE_FALLBACK)


def run(
    deps: PipelineDeps,
    *,
    session_id: UUID,
    problem: Problem,
    tag: str,
    strategy: str,
    student_answer: str,
    level: int = 1,
    attempt: int = 1,
    force_template: bool = False,
    template_reason: str = "template forced after leak-check failures",
) -> StageOutcome[HintCandidate | None]:
    """Generate a hint candidate.

    `force_template` is how the orchestrator implements §3.3's "after 2 failed
    regenerations, fall back to a pre-approved template" — never a third free
    generation. `template_reason` is required alongside it because the *why*
    ends up in the audit record: a shadow-mode template and a template forced by
    two leak-check failures look identical in the timeline unless they are named
    differently, and reading one as the other would badly misstate what happened
    to a child's session.
    """
    deps.recorder.stage_started(PipelineStage.GENERATE_HINT, level=level)

    if deps.llm is None or force_template:
        reason = template_reason if force_template else "no model"
        candidate = _template(deps, tag=tag, problem=problem, level=level, attempt=attempt)
        deps.recorder.fallback_used(PipelineStage.GENERATE_HINT, reason=reason)
        return StageOutcome(value=candidate, used_fallback=True, reason=reason)

    prompt = deps.prompts.render(
        stage=PipelineStage.GENERATE_HINT,
        band=problem.grade_band.value,
        version=PROMPT_VERSION,
        values={
            "problem": problem.prompt,
            "strategy": strategy,
            "hint_level": str(level),
            "student_answer": student_answer,
        },
    )
    try:
        result = deps.llm.complete(
            stage=PipelineStage.GENERATE_HINT,
            context=PromptContext(
                session_id=session_id,
                grade_band=problem.grade_band,
                problem_prompt=problem.prompt,
                student_answer=student_answer,
                attempt_number=attempt,
            ),
            prompt=prompt,
        )
    except TransportError as exc:
        candidate = _template(deps, tag=tag, problem=problem, level=level, attempt=attempt)
        deps.recorder.fallback_used(PipelineStage.GENERATE_HINT, reason=str(exc))
        return StageOutcome(value=candidate, used_fallback=True, reason=str(exc))

    deps.recorder.stage_completed(
        PipelineStage.GENERATE_HINT, llm_call_id=result.llm_call_id, level=level
    )
    return StageOutcome(
        value=HintCandidate(text=result.text.strip(), level=level, source=HintSource.GENERATED),
        llm_call_id=result.llm_call_id,
    )
