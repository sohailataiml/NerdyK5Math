"""Distress screening on student text — the §7 safety screen (P1.8).

Two layers, same shape as leak-check:

1. **Deterministic** (`packages.fallbacks.distress`) — explicit phrases. Always
   runs, needs no model, costs nothing.
2. **Classifier** — the case a regex cannot see: a child who spells badly, says
   it sideways, or buries it inside an answer about subtraction. Runs only when
   layer 1 found nothing and a model is available.

Three decisions here that are not obvious, and are the whole point of the stage.

**A flag never changes what the child sees.** The session continues exactly as
it would have: same hint, same grading, same pacing. A child who has just typed
something frightening must not have the machine visibly stop, blank out, or
switch to a script — that reads as being caught, and the next thing they type is
nothing at all. The alert goes sideways to an adult, out of band.

**It is not gated on the answer being wrong.** Screening runs on every
submission, before the pipeline cares whether the maths was right. A child can
type "12" and something that matters in the same box, and a screen that only
looks at wrong answers would miss exactly the children who are keeping up.

**A classifier outage leaves the deterministic verdict standing, loudly.** This
is the opposite of `leakcheck`, which fails closed, and the asymmetry is
deliberate. Failing closed there blocks a hint — cheap, invisible to the child,
regenerated for a fraction of a cent. Failing "closed" here would mean flagging
every attempt while the provider is down, which during one outage means thousands
of alerts to a counsellor who has to read them all. That does not fail safe; it
destroys the responder's ability to see the real one, which is the only outcome
that matters. So the gap is recorded as degradation rather than papered over with
a verdict the second layer never gave, and an operator has to know that a pilot
running with layer 2 down is a pilot screening on patterns alone.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from packages.domain.enums import PipelineStage, SafetyCategory
from packages.fallbacks.distress import SCREENER_VERSION, DistressVerdict, screen
from packages.llm import PromptContext
from packages.llm.errors import RefusalError, TransportError
from services.orchestrator.state import PipelineDeps, Problem, StageOutcome

PROMPT_VERSION = "v1"
CLASSIFIER_VERSION = f"{SCREENER_VERSION}+classifier/{PROMPT_VERSION}"

_CATEGORIES = {category.value.upper(): category for category in SafetyCategory}


@dataclass(frozen=True)
class SafetyScreen:
    """What the screen saw. Never a conclusion about the child."""

    flagged: bool
    category: SafetyCategory | None
    description: str | None
    excerpt: str | None
    screener_version: str
    classifier_ran: bool
    alert_raised: bool = False
    delivery_failed: bool = False


def _clear(version: str, *, classifier_ran: bool) -> SafetyScreen:
    return SafetyScreen(
        flagged=False,
        category=None,
        description=None,
        excerpt=None,
        screener_version=version,
        classifier_ran=classifier_ran,
    )


def run(
    deps: PipelineDeps,
    *,
    session_id: UUID,
    student_id: UUID | None,
    problem: Problem,
    student_answer: str,
    attempt: int = 1,
) -> StageOutcome[SafetyScreen]:
    """Screen one submission. Returns without ever altering the child's path."""
    if not deps.screen_for_distress:
        return StageOutcome(value=_clear(SCREENER_VERSION, classifier_ran=False))

    deps.recorder.stage_started(PipelineStage.SAFETY_SCREEN)

    deterministic = screen(student_answer)
    if deterministic.flagged:
        return _flag(
            deps,
            session_id=session_id,
            student_id=student_id,
            attempt=attempt,
            category=_require_category(deterministic),
            description=deterministic.description,
            excerpt=student_answer,
            version=SCREENER_VERSION,
            classifier_ran=False,
        )

    if deps.llm is None:
        return _layer_two_missing(deps, reason="classifier unavailable; patterns only")

    prompt = deps.prompts.render(
        stage=PipelineStage.SAFETY_SCREEN,
        band="shared",
        version=PROMPT_VERSION,
        values={"student_text": student_answer},
    )
    try:
        result = deps.llm.complete(
            stage=PipelineStage.SAFETY_SCREEN,
            context=PromptContext(
                session_id=session_id,
                grade_band=problem.grade_band,
                problem_prompt=problem.prompt,
                student_answer=student_answer,
            ),
            prompt=prompt,
        )
    except (TransportError, RefusalError) as exc:
        # A refusal counts here too. The screening prompt quotes a child's words,
        # and a model declining to classify them is not evidence about the child
        # either way — it is the layer not running.
        return _layer_two_missing(deps, reason=str(exc))

    verdict = result.text.strip().upper()
    if verdict.startswith("CLEAR"):
        deps.recorder.stage_completed(
            PipelineStage.SAFETY_SCREEN, llm_call_id=result.llm_call_id, verdict="clear"
        )
        return StageOutcome(
            value=_clear(CLASSIFIER_VERSION, classifier_ran=True),
            llm_call_id=result.llm_call_id,
        )

    category = _parse_category(verdict)
    if category is None:
        # A reply that is neither CLEAR nor a known category is not a verdict.
        # Inventing one would write a fabricated claim about a child into a
        # welfare record, which is worse than recording that the layer failed.
        return _layer_two_missing(
            deps, reason=f"classifier reply was not a verdict: {result.text.strip()[:60]!r}"
        )

    return _flag(
        deps,
        session_id=session_id,
        student_id=student_id,
        attempt=attempt,
        category=category,
        description="classifier flagged the child's text",
        excerpt=student_answer,
        version=CLASSIFIER_VERSION,
        classifier_ran=True,
        llm_call_id=result.llm_call_id,
    )


def _require_category(verdict: DistressVerdict) -> SafetyCategory:
    if verdict.category is None:  # pragma: no cover - flagged implies a category
        raise ValueError("a flagged distress verdict must carry a category")
    return verdict.category


def _parse_category(verdict: str) -> SafetyCategory | None:
    for name, category in _CATEGORIES.items():
        if name in verdict:
            return category
    return None


def _layer_two_missing(deps: PipelineDeps, *, reason: str) -> StageOutcome[SafetyScreen]:
    """Record that the classifier did not produce a verdict.

    `used_fallback` is what puts this in §8's degradation reporting. "Screening
    ran" and "screening ran with only its pattern layer" are different levels of
    protection for a child, and a dashboard that cannot tell them apart will
    report the weaker one as the stronger.
    """
    deps.recorder.fallback_used(PipelineStage.SAFETY_SCREEN, reason=reason)
    return StageOutcome(
        value=_clear(SCREENER_VERSION, classifier_ran=False),
        used_fallback=True,
        reason=reason,
    )


def _flag(
    deps: PipelineDeps,
    *,
    session_id: UUID,
    student_id: UUID | None,
    attempt: int,
    category: SafetyCategory,
    description: str | None,
    excerpt: str,
    version: str,
    classifier_ran: bool,
    llm_call_id: UUID | None = None,
) -> StageOutcome[SafetyScreen]:
    """Raise the alert, and record whether an adult was actually reached."""
    deps.recorder.safety_flagged(
        category=category.value,
        screener_version=version,
        classifier_ran=classifier_ran,
        llm_call_id=llm_call_id,
    )

    if deps.safety_sink is None or student_id is None:
        # Composition should have made this impossible (`require_responder`), so
        # reaching it means the guard was bypassed. Say so in the record rather
        # than returning a flag that looks routed.
        deps.recorder.fallback_used(
            PipelineStage.SAFETY_SCREEN,
            reason="no alert destination configured; signal recorded but nobody was told",
        )
        return StageOutcome(
            value=SafetyScreen(
                flagged=True,
                category=category,
                description=description,
                excerpt=excerpt,
                screener_version=version,
                classifier_ran=classifier_ran,
                alert_raised=False,
                delivery_failed=True,
            ),
            used_fallback=True,
            reason="no alert destination configured",
            llm_call_id=llm_call_id,
        )

    delivery_failed = False
    try:
        deps.safety_sink.raise_alert(
            session_id=session_id,
            student_id=student_id,
            attempt_number=attempt,
            category=category,
            excerpt=excerpt,
            screener_version=version,
            classifier_ran=classifier_ran,
        )
    except Exception as exc:
        # The alert row is written before notification, so the signal survives.
        # What is lost is the part that helps, so it is recorded as its own
        # failure rather than folded into a generic error.
        delivery_failed = True
        deps.recorder.fallback_used(
            PipelineStage.SAFETY_SCREEN,
            reason=f"alert raised but delivery to the responder failed: {exc}",
        )

    return StageOutcome(
        value=SafetyScreen(
            flagged=True,
            category=category,
            description=description,
            excerpt=excerpt,
            screener_version=version,
            classifier_ran=classifier_ran,
            alert_raised=True,
            delivery_failed=delivery_failed,
        ),
        used_fallback=delivery_failed,
        reason="delivery failed" if delivery_failed else None,
        llm_call_id=llm_call_id,
    )
