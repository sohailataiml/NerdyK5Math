"""The §4 state machine.

    AwaitingAnswer -> Diagnosing -> RetrievingCurriculum -> GeneratingHint
    -> LeakChecking -> AwaitingStudentRetry -> Grading
    -> (Complete | Escalated | Diagnosing[retry])

This is the only module that knows the order. Stages never import one another
(Implementation-Plan.md §1 rule 1), so composition, retry, and degradation all
live here — which is the point §4 makes about keeping the pipeline an explicit
machine rather than one long chain: every transition is observable, and a stage
can be tested without the ones around it.

Two rules the graph enforces that no single stage can:

- **A hint reaches a child only after the leak-check passes.** §3.3 makes that
  guardrail blocking, and it is the graph that refuses to return an uncleared
  candidate.
- **Never a third free generation.** Two failures, then a pre-approved template.
  §3.3 is explicit, and without a counter here a leak-check that keeps failing
  would loop on the model's dime and the child's time.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from uuid import UUID

from packages.domain.enums import (
    UNKNOWN_TAG_LABEL,
    HintSource,
    PipelineStage,
    ReviewReason,
    SessionState,
)
from packages.domain.models import HintLog, ShadowCandidate
from services.orchestrator.review import reasons_for_review
from services.orchestrator.rollout import RolloutDecision, decide
from services.orchestrator.stages import diagnose, generate, grade, leakcheck, retrieve, safety
from services.orchestrator.state import PipelineDeps, Problem

MAX_GENERATION_ATTEMPTS = 2
"""§3.3: regenerate once with a stricter prompt, then fall back to a template."""


@dataclass(frozen=True)
class _ShadowOutcome:
    """What the model produced in shadow, held until the shown hint is known.

    `ShadowCandidate` is append-only, so the row cannot be written when the model
    replies and patched once the template renders — and a rating without both
    hints is a teacher guessing. So the outcome waits here.
    """

    generated_text: str
    leak_passed: bool
    leak_checker_version: str
    leak_reason: str | None
    llm_call_id: UUID | None


@dataclass(frozen=True)
class AttemptResult:
    """What the child gets, and everything needed to explain why."""

    hint_text: str | None
    hint_level: int
    hint_source: HintSource | None
    diagnosis: diagnose.Diagnosis
    retrieval: retrieve.Retrieval
    leak_check: leakcheck.LeakCheck | None
    safety_screen: safety.SafetyScreen | None = None
    leak_rejections: int = 0
    """How many candidates the guardrail actually turned down.

    Counted rather than inferred from `hint_source`, because a template hint has
    three quite different causes — shadow mode by design, a provider outage, and
    the leak check firing — and only the third is a guardrail event. §3.6's
    routing needs to tell them apart, and a proxy cannot.
    """

    escalated: bool = False
    escalation_reason: str | None = None
    degraded_stages: tuple[str, ...] = ()
    shadow_ran: bool = False

    rollout: RolloutDecision | None = None
    """The P1.1 cohort decision for this session, when a rollout is configured.

    Reported rather than inferred from `hint_source`, for the same reason
    `leak_rejections` is counted: a template hint now has four causes — shadow
    mode, a provider outage, the leak check firing, and this session sitting
    outside the rollout — and only a recorded decision tells them apart. During a
    staged rollout the fourth is the *majority* of sessions, so a dashboard that
    cannot see it reads a working 5% rollout as a 95% failure rate.
    """

    @property
    def ran_degraded(self) -> bool:
        return bool(self.degraded_stages)


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
) -> AttemptResult:
    """Diagnose -> retrieve -> generate -> leak-check, producing one hint.

    `record_submission=False` is for a caller that has already logged the
    submission. A served UI has to grade before it knows whether to hint, so it
    records the answer first; leaving this on would put `answer_submitted` after
    `graded` in the log, and a replay that says a child's answer was graded
    before they submitted it is not a faithful account of their session.
    """
    degraded: list[str] = []

    if record_submission:
        deps.recorder.answer_submitted(attempt_number=attempt, answer=student_answer)

    # §7 welfare screening, first and unconditionally (P1.8). Before diagnosis,
    # because whether the maths was right has no bearing on whether a child needs
    # an adult — and a screen that runs after the pipeline decides the answer is
    # correct would miss exactly the children who are keeping up.
    #
    # Nothing below branches on the result. A flag routes sideways to a person;
    # the session the child is sitting in continues unchanged, because a machine
    # that visibly stops after they type something frightening teaches them not
    # to type it again.
    screened = safety.run(
        deps,
        session_id=session_id,
        student_id=student_id,
        problem=problem,
        student_answer=student_answer,
        attempt=attempt,
    )
    if screened.used_fallback:
        degraded.append("safety_screen")

    deps.recorder.state_changed(frm=SessionState.AWAITING_ANSWER, to=SessionState.DIAGNOSING)

    diagnosed = diagnose.run(
        deps, session_id=session_id, problem=problem, student_answer=student_answer
    )
    if diagnosed.used_fallback:
        degraded.append("diagnose")

    deps.recorder.state_changed(frm=SessionState.DIAGNOSING, to=SessionState.RETRIEVING_CURRICULUM)
    retrieved = retrieve.run(deps, tag=diagnosed.value.tag, problem=problem)
    if retrieved.used_fallback:
        degraded.append("retrieve")

    deps.recorder.state_changed(
        frm=SessionState.RETRIEVING_CURRICULUM, to=SessionState.GENERATING_HINT
    )

    # P1.1: is this session in the rollout cohort? Read once, here, so every
    # hint level in this attempt — and every attempt in this session, since the
    # bucket comes from the session id — resolves the same way.
    rollout = decide(deps.rollout.current(), session_id=session_id) if deps.rollout else None
    withheld_by_rollout = rollout is not None and not rollout.serve_generated

    # Phase 0 (P0.4): run the model, measure it, and show the child a template.
    shadow_ran, shadow_outcome = _run_shadow(
        deps,
        session_id=session_id,
        problem=problem,
        tag=diagnosed.value.tag,
        strategy=retrieved.value.primary,
        student_answer=student_answer,
        level=hint_level,
        attempt=attempt,
    )

    # Generate, check, and on a leak try once more before falling back. The loop
    # is bounded by construction rather than by a break condition someone could
    # later relax.
    candidate: generate.HintCandidate | None = None
    check: leakcheck.LeakCheck | None = None
    leak_rejections = 0
    for generation in range(1, MAX_GENERATION_ATTEMPTS + 2):
        # In shadow mode the served hint is always the template — the model has
        # already run above, and its output is evidence, not something a child
        # sees. Outside the rollout cohort it is also always the template, and
        # here no model runs at all: a session that cannot be shown generated
        # text should not be billed for generating it (P1.10).
        force_template = shadow_ran or withheld_by_rollout or generation > MAX_GENERATION_ATTEMPTS
        produced = generate.run(
            deps,
            session_id=session_id,
            problem=problem,
            tag=diagnosed.value.tag,
            strategy=retrieved.value.primary,
            student_answer=student_answer,
            level=hint_level,
            attempt=attempt,
            force_template=force_template,
            template_reason=_template_reason(
                shadow_ran=shadow_ran, rollout=rollout if withheld_by_rollout else None
            ),
        )
        # Shadow mode and the rollout serving a template are the design, not
        # degradation. Marking either degraded would put every Phase 0 session,
        # and every out-of-cohort Phase 1 session, in the same bucket as a
        # provider outage — and §8's dashboards would then show a working system
        # as running degraded, hiding the real outages inside the noise.
        by_design = shadow_ran or withheld_by_rollout
        if produced.used_fallback and not by_design and "generate" not in degraded:
            degraded.append("generate")

        candidate = produced.value
        if candidate is None:
            # Even the template would leak for this problem. There is no safe
            # hint to show, which is an escalation, not a retry.
            break

        deps.recorder.state_changed(frm=SessionState.GENERATING_HINT, to=SessionState.LEAK_CHECKING)
        checked = leakcheck.run(
            deps, session_id=session_id, problem=problem, hint_text=candidate.text
        )
        if checked.used_fallback and "leakcheck" not in degraded:
            degraded.append("leakcheck")
        check = checked.value
        if not check.passed:
            leak_rejections += 1

        if not check.passed and candidate.source is HintSource.TEMPLATE_FALLBACK:
            # Retrying is for a *generated* hint: the second pass uses a stricter
            # prompt and can produce different text. A template is deterministic,
            # so regenerating it yields the identical string and the identical
            # rejection — three leak checks billed for one foregone conclusion,
            # and a replay that reads as three separate failures instead of one
            # template that cannot be shown for this problem.
            candidate = None
            break

        if check.passed:
            deps.recorder.hint_shown(
                hint_level=candidate.level,
                source=candidate.source.value,
                leak_checker_version=check.checker_version,
            )
            # §5's HintLog: the *text* the child read. The event above records
            # that a hint was shown, at what level and from what source — not
            # what it said. A teacher reviewing this session needs the words, and
            # `HintLog` refuses to persist anything the leak check did not clear,
            # so writing it here (inside the passed branch) is the only place it
            # can go.
            _record_hint(
                deps,
                session_id=session_id,
                attempt=attempt,
                candidate=candidate,
                check=check,
                llm_call_id=produced.llm_call_id,
            )
            deps.recorder.state_changed(
                frm=SessionState.LEAK_CHECKING, to=SessionState.AWAITING_STUDENT_RETRY
            )
            _record_shadow(
                deps,
                shadow_outcome,
                session_id=session_id,
                tag=diagnosed.value.tag,
                level=candidate.level,
                attempt=attempt,
                shown_text=candidate.text,
            )
            return AttemptResult(
                hint_text=candidate.text,
                hint_level=candidate.level,
                hint_source=candidate.source,
                diagnosis=diagnosed.value,
                retrieval=retrieved.value,
                leak_check=check,
                safety_screen=screened.value,
                leak_rejections=leak_rejections,
                degraded_stages=tuple(degraded),
                shadow_ran=shadow_ran,
                rollout=rollout,
            )
        candidate = None  # rejected; do not carry it forward

    # Nothing cleared the guardrail. Refusing to show a hint is the correct
    # outcome — §12: a leak reaches a child and cannot be taken back.
    reason = (check.reason if check else None) or "no hint could be cleared for display"
    deps.recorder.escalated(reason=reason)
    deps.recorder.state_changed(frm=SessionState.LEAK_CHECKING, to=SessionState.ESCALATED)
    # No hint was shown, so `shown_text` is genuinely None — and the candidate is
    # still worth rating: a session where nothing could be cleared is exactly the
    # case a teacher should see.
    _record_shadow(
        deps,
        shadow_outcome,
        session_id=session_id,
        tag=diagnosed.value.tag,
        level=hint_level,
        attempt=attempt,
        shown_text=None,
    )
    return AttemptResult(
        hint_text=None,
        hint_level=hint_level,
        hint_source=None,
        diagnosis=diagnosed.value,
        retrieval=retrieved.value,
        leak_check=check,
        safety_screen=screened.value,
        leak_rejections=leak_rejections,
        escalated=True,
        escalation_reason=reason,
        degraded_stages=tuple(degraded),
        shadow_ran=shadow_ran,
        rollout=rollout,
    )


def _template_reason(*, shadow_ran: bool, rollout: RolloutDecision | None) -> str:
    """Name *why* a template was served, for the audit record.

    Four causes produce the same hint text and must not read the same in a
    timeline: shadow mode, the rollout withholding, leak-check failures, and a
    provider outage (which `generate` names for itself). Reading one as another
    misstates what happened in a child's session — and during a staged rollout it
    would misstate the majority of them.
    """
    if shadow_ran:
        # Shadow wins the label: the model did run, and its output was recorded
        # for rating. That is a materially different session from one where
        # nothing was generated at all.
        return "shadow mode: generated hints are recorded, not shown"
    if rollout is not None:
        return f"rollout: {rollout.reason}"
    return "template forced after leak-check failures"


def _run_shadow(
    deps: PipelineDeps,
    *,
    session_id: UUID,
    problem: Problem,
    tag: str,
    strategy: str,
    student_answer: str,
    level: int,
    attempt: int,
) -> tuple[bool, _ShadowOutcome | None]:
    """Run the model without showing it (P0.4).

    Returns `(shadow_ran, outcome)`. The outcome is deliberately *not* recorded
    here. The rating a teacher gives is "is the generated hint better than what
    the child actually saw", and that comparison is unanswerable without both —
    but `ShadowCandidate` is append-only, so the row cannot be written now and
    patched once the template renders. It is written once, later, with both.

    The leak check does run here, on the *generated* text. That is the point:
    P0.5's exit gate is the checker holding against real model output, and
    hand-written attacks alone cannot tell you whether it does.
    """
    if not deps.shadow_mode or deps.llm is None:
        return False, None

    produced = generate.run(
        deps,
        session_id=session_id,
        problem=problem,
        tag=tag,
        strategy=strategy,
        student_answer=student_answer,
        level=level,
        attempt=attempt,
    )
    shadow = produced.value
    if shadow is None or shadow.source is not HintSource.GENERATED:
        # The provider failed. Nothing was generated, so there is nothing to rate
        # — and the template path is what would have run anyway.
        return True, None

    checked = leakcheck.run(deps, session_id=session_id, problem=problem, hint_text=shadow.text)
    deps.recorder.stage_completed(
        PipelineStage.GENERATE_HINT,
        llm_call_id=produced.llm_call_id,
        shadow=True,
        leak_passed=checked.value.passed,
    )
    return True, _ShadowOutcome(
        generated_text=shadow.text,
        leak_passed=checked.value.passed,
        leak_checker_version=checked.value.checker_version,
        leak_reason=checked.value.reason,
        llm_call_id=produced.llm_call_id,
    )


def _record_hint(
    deps: PipelineDeps,
    *,
    session_id: UUID,
    attempt: int,
    candidate: generate.HintCandidate,
    check: leakcheck.LeakCheck,
    llm_call_id: UUID | None,
) -> None:
    """Persist what the child actually read (§5)."""
    if deps.hint_sink is None:
        return
    deps.hint_sink.record(
        HintLog(
            session_id=session_id,
            attempt_number=attempt,
            misconception_tag_id=None,
            curriculum_node_id=None,
            hint_text=candidate.text,
            hint_level=candidate.level,
            source=candidate.source,
            leak_check_passed=check.passed,
            leak_checker_version=check.checker_version,
            llm_call_id=llm_call_id,
        )
    )


def _record_shadow(
    deps: PipelineDeps,
    outcome: _ShadowOutcome | None,
    *,
    session_id: UUID,
    tag: str,
    level: int,
    attempt: int,
    shown_text: str | None,
) -> None:
    """Write the candidate now that both hints are known (P0.8).

    A teacher rating a hint they cannot compare against what the child saw is
    guessing, and the phase's exit gate is built on those ratings.
    """
    if outcome is None or deps.shadow_sink is None:
        return
    deps.shadow_sink.record(
        ShadowCandidate(
            session_id=session_id,
            attempt_number=attempt,
            hint_level=level,
            generated_text=outcome.generated_text,
            shown_text=shown_text,
            misconception_tag=tag,
            prompt_version=generate.PROMPT_VERSION,
            leak_check_passed=outcome.leak_passed,
            leak_checker_version=outcome.leak_checker_version,
            leak_reason=outcome.leak_reason,
            llm_call_id=outcome.llm_call_id,
            created_at=dt.datetime.now(dt.UTC),
        )
    )


def check_answer(
    deps: PipelineDeps,
    *,
    session_id: UUID,
    problem: Problem,
    student_answer: str,
    prior_diagnosis: diagnose.Diagnosis | None = None,
) -> grade.Grade:
    """Grade one submission (§3.5) **without ending the session**.

    Split from `grade_answer` because a served UI has to grade every submission
    to know whether to hint, and most of those submissions are wrong and the
    session continues. Emitting `session_completed` on each of them would put a
    terminal event in the middle of a live session — and `show_replay` would then
    tell a teacher the child finished three times.
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
    return result.value


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
