"""Misconception diagnosis (§3.1).

Rule pre-check first, LLM on fallthrough. §3.1 is explicit that this ordering is
not about capability — "it's free, instant, and perfectly precise on the ~40% of
cases it covers" — so the rule engine skipping the model on common errors is the
single largest cost lever in the system, not a fallback.

Three outcomes, and the third matters most: a confident tag, an `unknown` when
nothing is certain, and an `unknown` when the provider was unreachable. §12 makes
confident misdiagnosis a standing risk — a wrong tag sends a child a hint aimed
at a misconception they do not have — so every path that is not certain lands on
`unknown` rather than guessing.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from uuid import UUID

from packages.domain.enums import UNKNOWN_TAG_LABEL, DiagnosisSource, PipelineStage
from packages.fallbacks import diagnose as rule_diagnose
from packages.fallbacks.rules import applies_to
from packages.llm import PromptContext
from packages.llm.errors import TransportError
from services.orchestrator.state import PipelineDeps, Problem, StageOutcome

CONFIDENCE_THRESHOLD = 0.55
"""Below this, tag as `unknown` (§3.1).

A starting value, not a finding. Implementation-Plan.md P1.3 calibrates it
against the held-out set — a threshold chosen before there is labelled data is a
guess, and the population it is first tested on is children.
"""

PROMPT_VERSION = "v2"
_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.MULTILINE)


@dataclass(frozen=True)
class Diagnosis:
    tag: str
    confidence: float
    evidence: str
    source: DiagnosisSource

    @property
    def is_known(self) -> bool:
        return self.tag != UNKNOWN_TAG_LABEL


def _parse(text: str) -> tuple[str, float, str]:
    """Read the §3.1 JSON contract. An unparseable reply is `unknown`, not a raise."""
    try:
        payload = json.loads(_FENCE.sub("", text).strip())
        tag = str(payload["tag"])
        confidence = float(payload.get("confidence", 0.0))
        evidence = str(payload.get("evidence", ""))
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return UNKNOWN_TAG_LABEL, 0.0, "model reply was not valid JSON"
    return tag, max(0.0, min(1.0, confidence)), evidence


def run(
    deps: PipelineDeps, *, session_id: UUID, problem: Problem, student_answer: str
) -> StageOutcome[Diagnosis]:
    deps.recorder.stage_started(PipelineStage.DIAGNOSE)

    # 1. The free path. A rule match is an exact arithmetic identity, so when it
    #    fires there is nothing a model could add.
    ruled = rule_diagnose(problem.prompt, student_answer)
    if ruled.tag != UNKNOWN_TAG_LABEL:
        diagnosis = Diagnosis(
            tag=ruled.tag,
            confidence=ruled.confidence,
            evidence=ruled.evidence,
            source=DiagnosisSource.RULE,
        )
        deps.recorder.stage_completed(PipelineStage.DIAGNOSE, tag=diagnosis.tag, source="rule")
        return StageOutcome(value=diagnosis)

    unknown = Diagnosis(
        tag=UNKNOWN_TAG_LABEL,
        confidence=0.0,
        evidence="no rule matched and no model was available",
        source=DiagnosisSource.RULE,
    )

    # 2. No model configured — shadow mode, or the provider is down.
    if deps.llm is None:
        deps.recorder.fallback_used(PipelineStage.DIAGNOSE, reason="no model configured")
        return StageOutcome(value=unknown, used_fallback=True, reason="no model configured")

    prompt = deps.prompts.render(
        stage=PipelineStage.DIAGNOSE,
        band=problem.grade_band.value,
        version=PROMPT_VERSION,
        values={
            "problem": problem.prompt,
            "correct_answer": problem.correct_answer,
            "student_answer": student_answer,
        },
    )
    try:
        result = deps.llm.complete(
            stage=PipelineStage.DIAGNOSE,
            context=PromptContext(
                session_id=session_id,
                grade_band=problem.grade_band,
                problem_prompt=problem.prompt,
                correct_answer=problem.correct_answer,
                student_answer=student_answer,
            ),
            prompt=prompt,
        )
    except TransportError as exc:
        deps.recorder.fallback_used(PipelineStage.DIAGNOSE, reason=str(exc))
        return StageOutcome(value=unknown, used_fallback=True, reason=str(exc))

    tag, confidence, evidence = _parse(result.text)
    if confidence < CONFIDENCE_THRESHOLD:
        # §3.1: a wrong diagnosis is worse than no diagnosis.
        deps.recorder.stage_completed(
            PipelineStage.DIAGNOSE,
            llm_call_id=result.llm_call_id,
            tag=UNKNOWN_TAG_LABEL,
            below_threshold=confidence,
        )
        return StageOutcome(
            value=Diagnosis(
                tag=UNKNOWN_TAG_LABEL,
                confidence=confidence,
                evidence=f"below threshold ({confidence:.2f} < {CONFIDENCE_THRESHOLD})",
                source=DiagnosisSource.LLM,
            ),
            llm_call_id=result.llm_call_id,
        )

    if not applies_to(tag, problem.prompt):
        # A tag from the taxonomy that cannot occur on this problem — the model
        # naming an addition misconception for a subtraction question. Confining
        # output to the vocabulary is necessary and not sufficient: the hint that
        # follows `counted_on_from_wrong_start` on `13 - 8` tells a child to hop
        # *forward* eight times from thirteen. §3.1 would rather have no
        # diagnosis than a confident wrong one, so this falls back to the same
        # generic Socratic path as low confidence.
        deps.recorder.stage_completed(
            PipelineStage.DIAGNOSE,
            llm_call_id=result.llm_call_id,
            tag=UNKNOWN_TAG_LABEL,
            rejected_tag=tag,
            reason="tag does not apply to this operation",
        )
        return StageOutcome(
            value=Diagnosis(
                tag=UNKNOWN_TAG_LABEL,
                confidence=confidence,
                evidence=f"{tag!r} cannot occur on this problem",
                source=DiagnosisSource.LLM,
            ),
            llm_call_id=result.llm_call_id,
        )

    deps.recorder.stage_completed(
        PipelineStage.DIAGNOSE, llm_call_id=result.llm_call_id, tag=tag, source="llm"
    )
    return StageOutcome(
        value=Diagnosis(
            tag=tag, confidence=confidence, evidence=evidence, source=DiagnosisSource.LLM
        ),
        llm_call_id=result.llm_call_id,
    )
