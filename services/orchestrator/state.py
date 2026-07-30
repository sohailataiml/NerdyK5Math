"""Types the pipeline stages exchange (§4).

Stages never import one another — Implementation-Plan.md §1 rule 1, enforced by
an import contract — so everything they share lives here. That is what keeps
composition in one place: `graph.py` is the only module that knows the order.

`StageOutcome` carries `used_fallback` on every result rather than only on the
failure path. §4 requires the pipeline to keep serving when the provider is down,
and a run that quietly degraded is indistinguishable from a healthy one unless
each stage says which path it took (§8 segments quality by exactly this).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol
from uuid import UUID

from packages.domain.enums import DiagnosisSource, GradeBand, ReviewReason, SafetyCategory
from packages.domain.models import GradeResult, HintLog, ShadowCandidate
from packages.llm import LLMClient
from packages.prompts import PromptRegistry
from packages.telemetry import EventRecorder
from services.orchestrator.rollout import RolloutSource


class UnroutedSafetyScreenError(RuntimeError):
    """Distress screening is enabled with no destination for its alerts (P1.8)."""


@dataclass(frozen=True)
class StageOutcome[T]:
    """A stage's result plus how it got there."""

    value: T
    used_fallback: bool = False
    reason: str | None = None
    llm_call_id: UUID | None = None


class SymbolicChecker(Protocol):
    """The §3.5 equivalence check.

    A Protocol rather than a direct import of `services.symbolic`: that service
    is deliberately isolated because it evaluates untrusted input (M0.7), and
    calling it in-process would hand that risk straight back to the orchestrator.
    """

    def equivalent(self, *, expected: str, actual: str) -> bool: ...


@dataclass(frozen=True)
class Problem:
    """The problem being worked. Deliberately not the §5 row — a stage needs the
    values, not a database identity."""

    prompt: str
    correct_answer: str
    grade_band: GradeBand
    operands: dict[str, str] = field(default_factory=dict)


class ShadowSink(Protocol):
    """Where shadow candidates are recorded (P0.4)."""

    def record(self, candidate: ShadowCandidate) -> None: ...


class HintSink(Protocol):
    """Where the hint a child actually read is recorded (§5's `HintLog`).

    The event log says a hint was shown, at which level, from which source — not
    what it said. Without the text, a teacher reviewing the session cannot see
    what the child read, and §3.6's premise is that a judgment is defensible
    because the evidence is there.
    """

    def record(self, hint: HintLog) -> object: ...


class DiagnosisSink(Protocol):
    """Where §5's `DiagnosisLog` rows go.

    Takes the diagnosis's parts rather than a built entity, unlike `GradeSink`.
    The reason is the taxonomy: `DiagnosisLog.misconception_tag_id` is a foreign
    key and the stage produces a *label*, so something has to resolve one to the
    other against the authored tag table. Doing that behind this Protocol keeps
    the orchestrator from needing the taxonomy — and keeps the rule that an
    unrecognised label does not become a new tag in one place.
    """

    def record(
        self,
        *,
        attempt_id: UUID,
        tag: str,
        confidence: float,
        evidence: str,
        source: DiagnosisSource,
        llm_call_id: UUID | None = None,
    ) -> object: ...


class GradeSink(Protocol):
    """Where §5's `GradeResult` rows go.

    The `graded` event records that a verdict was reached; this records the
    verdict as a queryable row joined to the attempt it judged. Both are needed
    and they are not redundant: an event log answers "what happened in this
    session", and a teacher defending a grade months later needs "what was the
    verdict on this attempt, by what method, and did the checker agree" without
    replaying a timeline to find out.
    """

    def record(self, result: GradeResult) -> object: ...


class ReviewSink(Protocol):
    """Where a session goes when it needs a teacher (§3.6).

    Separate from `SafetyAlertSink` on purpose: a teaching queue and a welfare
    alert have different readers and different consequences for being missed.
    """

    def route(self, *, session_id: UUID, reason: ReviewReason) -> object: ...


class SafetyAlertSink(Protocol):
    """Where a §7 welfare alert goes (P1.8).

    A Protocol so the orchestrator never learns the destination. The real one is
    site-specific — a counsellor's pager here, a safeguarding inbox there — and a
    pipeline that knows which would be a pipeline someone has to edit to deploy
    somewhere else.
    """

    def raise_alert(
        self,
        *,
        session_id: UUID,
        student_id: UUID,
        attempt_number: int,
        category: SafetyCategory,
        excerpt: str,
        screener_version: str,
        classifier_ran: bool,
    ) -> object: ...


@dataclass
class PipelineDeps:
    """Everything the pipeline needs, injected.

    `llm` is optional on purpose. `None` is not an error state — it is the
    provider-down path, which must produce a hint for a child rather than an
    exception.
    """

    recorder: EventRecorder
    prompts: PromptRegistry
    llm: LLMClient | None = None
    symbolic: SymbolicChecker | None = None
    db: object | None = None  # SQLAlchemy Session, when curriculum lookup is wired

    shadow_mode: bool = False
    """Phase 0 (P0.4). The full model pipeline runs, the child is served the
    template hint, and the generated candidate is recorded for teacher rating.

    This is a property of the *deployment*, not of a request. Phase 0's whole
    argument is that a confidence threshold picked before there is labelled data
    is a guess, and the first population it would be tested on is children — so
    the switch that lets generated text reach a child is one deliberate
    configuration change, made once the exit gates are met, not a per-call
    parameter something could set by accident.
    """

    shadow_sink: ShadowSink | None = None

    rollout: RolloutSource | None = None
    """Phase 1's staged rollout and kill switch (P1.1), read once per attempt.

    Distinct from `shadow_mode`, and both gates are real. Shadow mode is the
    *phase*: no generated text reaches any child, and it is one deployment
    decision made when Phase 0's exit gates are met. The rollout is what happens
    after that — 5%, then 25%, then 100% — plus the switch that takes it back to
    zero without a deploy. Collapsing them into one setting would mean killing a
    bad rollout re-enters shadow mode and starts recording candidates for a
    rating queue nobody asked to refill.

    `None` means no staged rollout is configured and `shadow_mode` is the only
    gate. That is right for a script and for the tests; a served deployment wires
    `DatabaseRolloutSource`, whose unconfigured state is generation *off*.
    """

    screen_for_distress: bool = False
    """§7 distress screening on student text (P1.8).

    Off by default, and that default is a statement rather than a convenience:
    turning it on is a commitment to a configured on-call destination, and a
    deployment that cannot name one should be honest that it has no screen rather
    than run one that alerts nobody.

    Implementation-Plan.md puts P1.8 in Phase 1, after the pilot. That ordering
    looks wrong from here. Shadow mode is not a dry run — real children type real
    text into it from day one — so screening protects the pilot rather than
    depending on it, and belongs on before the first student session.
    """

    safety_sink: SafetyAlertSink | None = None

    hint_sink: HintSink | None = None
    """Where §5's `HintLog` rows go. `None` means the text of what a child read
    is not kept, which is a valid choice for a script and not for a pilot."""

    diagnosis_sink: DiagnosisSink | None = None
    """Where §5's `DiagnosisLog` rows go.

    `None` means diagnoses exist only as events in the timeline, which is what
    the pipeline did until now — leaving `diagnosis_log` a table nothing wrote
    to. §8 makes diagnoser accuracy and calibration against teacher-confirmed
    tags the core quality metric of the whole system, and that measurement is a
    join this table is one side of. Fine for a script; not for a phase whose exit
    gate is calibration.
    """

    grade_sink: GradeSink | None = None
    """Where §5's `GradeResult` rows go.

    `None` means grades exist only as events in the timeline — which is what the
    pipeline did until now, leaving `grade_result` a table in the schema that
    nothing outside a demo script ever wrote to. §12's argument for this whole
    architecture is that a grade can be defended later; a verdict recoverable
    only by replaying an event stream is a weak form of that.
    """

    review_sink: ReviewSink | None = None
    """Where a finished session goes for a teacher to see (§3.6).

    `None` means sessions are not routed, which is a real configuration for a
    script or a test but not for anything a child uses: the student page tells
    them a teacher will look, and that sentence is only true if this is wired.
    """

    def __post_init__(self) -> None:
        """Refuse to compose a screen that alerts nobody.

        The failure this prevents: screening enabled, no destination, alerts
        accumulating in a table while a dashboard reports coverage. That is worse
        than no screening, because no screening is a gap someone owns and this is
        a gap nobody can see. Raising here means it is found at startup rather
        than by a child's disclosure.
        """
        if self.screen_for_distress and self.safety_sink is None:
            raise UnroutedSafetyScreenError(
                "screen_for_distress is on but no safety_sink is configured. An "
                "alert that routes nowhere is worse than no screening: it produces "
                "the appearance of coverage. Configure a destination, or leave "
                "screening off and record that gap explicitly."
            )
