"""Answer-leak checking (§3.3) — the blocking guardrail.

§12 names this the defining risk of the architecture and Implementation-Plan.md
§1 calls it the highest-severity component in the system. Both layers run, in
order, and the stage fails closed at every branch:

1. **Deterministic** (`packages.fallbacks.answer_leak`) — the answer written out
   in any numeric form. Always runs; needs no model.
2. **Classifier** — the implicit case regex cannot see: "it's the number right
   after eleven". Runs only when a model is available.

The ordering matters for cost and for safety. The cheap exact check catches the
blatant cases for free, and the classifier is spent only on hints that already
look clean.

**Failing closed at each branch is deliberate.** A hint wrongly held back is
regenerated for a fraction of a cent; a leak reaches a child and cannot be taken
back. So an ambiguous classifier reply counts as a leak, and a classifier that
cannot be reached leaves the deterministic verdict standing while recording that
the second layer never ran — rather than reporting a check that did not happen.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from packages.domain.enums import PipelineStage
from packages.fallbacks import CHECKER_VERSION, check_hint
from packages.llm import PromptContext
from packages.llm.errors import TransportError
from services.orchestrator.state import PipelineDeps, Problem, StageOutcome

PROMPT_VERSION = "v1"


@dataclass(frozen=True)
class LeakCheck:
    passed: bool
    reason: str | None
    checker_version: str
    classifier_ran: bool


def run(
    deps: PipelineDeps, *, session_id: UUID, problem: Problem, hint_text: str
) -> StageOutcome[LeakCheck]:
    deps.recorder.stage_started(PipelineStage.LEAK_CHECK)

    # Layer 1 — free, exact, and sufficient on its own to reject.
    deterministic = check_hint(hint_text, problem.correct_answer)
    if deterministic.leaked:
        deps.recorder.stage_failed(
            PipelineStage.LEAK_CHECK, reason=deterministic.reason or "deterministic match"
        )
        return StageOutcome(
            value=LeakCheck(
                passed=False,
                reason=deterministic.reason,
                checker_version=CHECKER_VERSION,
                classifier_ran=False,
            )
        )

    # Layer 2 — the implicit case. Without a model this layer does not run; the
    # deterministic pass stands, and the record says so rather than implying a
    # check that never happened.
    if deps.llm is None:
        deps.recorder.fallback_used(
            PipelineStage.LEAK_CHECK, reason="classifier unavailable; deterministic layer only"
        )
        return StageOutcome(
            value=LeakCheck(
                passed=True,
                reason="deterministic layer only",
                checker_version=CHECKER_VERSION,
                classifier_ran=False,
            ),
            used_fallback=True,
            reason="classifier unavailable",
        )

    prompt = deps.prompts.render(
        stage=PipelineStage.LEAK_CHECK,
        band="shared",
        version=PROMPT_VERSION,
        values={
            "problem": problem.prompt,
            "correct_answer": problem.correct_answer,
            "hint_text": hint_text,
        },
    )
    try:
        result = deps.llm.complete(
            stage=PipelineStage.LEAK_CHECK,
            context=PromptContext(
                session_id=session_id,
                grade_band=problem.grade_band,
                problem_prompt=problem.prompt,
                correct_answer=problem.correct_answer,
            ),
            prompt=prompt,
        )
    except TransportError as exc:
        deps.recorder.fallback_used(PipelineStage.LEAK_CHECK, reason=str(exc))
        return StageOutcome(
            value=LeakCheck(
                passed=True,
                reason="deterministic layer only (classifier unreachable)",
                checker_version=CHECKER_VERSION,
                classifier_ran=False,
            ),
            used_fallback=True,
            reason=str(exc),
        )

    verdict = result.text.strip().upper()
    if verdict.startswith("SAFE"):
        deps.recorder.stage_completed(
            PipelineStage.LEAK_CHECK, llm_call_id=result.llm_call_id, verdict="safe"
        )
        return StageOutcome(
            value=LeakCheck(
                passed=True,
                reason=None,
                checker_version=f"{CHECKER_VERSION}+classifier/{PROMPT_VERSION}",
                classifier_ran=True,
            ),
            llm_call_id=result.llm_call_id,
        )

    # LEAK, or anything that is not clearly SAFE. An ambiguous reply about
    # whether a child is about to be handed the answer is treated as a leak.
    reason = (
        "classifier flagged a leak"
        if verdict.startswith("LEAK")
        else f"classifier reply was not a verdict: {result.text.strip()[:60]!r}"
    )
    deps.recorder.stage_failed(PipelineStage.LEAK_CHECK, reason=reason)
    return StageOutcome(
        value=LeakCheck(
            passed=False,
            reason=reason,
            checker_version=f"{CHECKER_VERSION}+classifier/{PROMPT_VERSION}",
            classifier_ran=True,
        ),
        llm_call_id=result.llm_call_id,
    )
