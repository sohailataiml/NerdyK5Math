"""The §5 core entities as validated values.

These are the contracts stages exchange (Implementation-Plan.md §2). They carry
validation the database cannot express — a confidence outside [0, 1] is a bug in
a calling stage, and catching it here means it never reaches a threshold
comparison that would silently do the wrong thing.

Pure by design: no I/O, no SQLAlchemy, no model SDK. Enforced by the
"Domain layer is pure" import contract in pyproject.toml.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from packages.domain.enums import (
    AnswerType,
    DiagnosisSource,
    EventType,
    GradeBand,
    GradeMethod,
    HintSource,
    Operation,
    PipelineStage,
    ReviewReason,
    Role,
    SafetyCategory,
    SessionState,
    TeacherVerdict,
)

MAX_HINT_LEVEL = 3
"""Level 1 is a question, 2 a partial worked example, 3 fully worked with the
final step blank (§3.3). Past this the system reveals the solution and escalates."""


class _Entity(BaseModel):
    """Frozen by default — these are records, not scratch space.

    Immutability is what makes it safe to pass an entity through five pipeline
    stages without defensive copying, and it means a logged value cannot be
    edited after the fact by a caller holding a reference.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")


# ---------------------------------------------------------------------------
# People and content
# ---------------------------------------------------------------------------


class Student(_Entity):
    id: UUID = Field(default_factory=uuid4)
    grade_level: int = Field(ge=0, le=12)
    iep_flags: list[str] = Field(default_factory=list)
    created_at: datetime


class Principal(_Entity):
    """An authenticated subject (M0.9).

    Extends §5, which has no auth model — M0.9 requires "teacher scoped to their
    class" and there was nothing to scope against. A `Principal` is who is
    asking; `Student` remains who the work belongs to. Keeping them separate
    means a teacher account and the child they teach are different kinds of
    thing, which is what stops a role check from accidentally granting a student
    a teacher's view.
    """

    id: UUID = Field(default_factory=uuid4)
    role: Role
    display_name: str = Field(min_length=1)
    # Set only for STUDENT principals: the student record this login acts as.
    student_id: UUID | None = None
    created_at: datetime

    @model_validator(mode="after")
    def _student_principals_are_linked(self) -> Principal:
        """A student login with no student record could read nothing — or,
        depending on how a caller wrote the check, everything."""
        if self.role is Role.STUDENT and self.student_id is None:
            raise ValueError("STUDENT principals must reference a student_id")
        if self.role is not Role.STUDENT and self.student_id is not None:
            raise ValueError(f"{self.role.value} principals must not reference a student_id")
        return self


class Classroom(_Entity):
    """A teacher's class. The unit of tenancy (M0.9)."""

    id: UUID = Field(default_factory=uuid4)
    teacher_id: UUID
    name: str = Field(min_length=1)
    created_at: datetime


class Enrollment(_Entity):
    """A student's membership in a classroom."""

    id: UUID = Field(default_factory=uuid4)
    classroom_id: UUID
    student_id: UUID
    created_at: datetime


class MisconceptionTag(_Entity):
    """The diagnoser's closed output vocabulary (§3.1).

    `example_pattern` is documentation for the humans auditing the taxonomy, and
    the seed for the rule pre-check where the pattern is mechanically checkable.
    """

    id: UUID = Field(default_factory=uuid4)
    label: str = Field(min_length=1)
    operation_type: Operation
    description: str
    example_pattern: str | None = None


class CurriculumNode(_Entity):
    """Teacher-approved ground truth. The hint generator may phrase these
    strategies but never invent one (§3.2, §7)."""

    id: UUID = Field(default_factory=uuid4)
    standard_code: str = Field(min_length=1)
    grade_band: GradeBand
    definition: str
    remediation_strategies: list[str] = Field(default_factory=list)
    prerequisite_ids: list[UUID] = Field(default_factory=list)
    embedding_version: int = Field(default=0, ge=0)
    # Populated by the embedding pipeline, not by authoring. `None` means the
    # node exists but is not yet retrievable by similarity (§3.2).
    embedding: list[float] | None = None


class Problem(_Entity):
    id: UUID = Field(default_factory=uuid4)
    curriculum_node_id: UUID
    prompt: str
    correct_answer: str
    answer_type: AnswerType
    grade_band: GradeBand


# ---------------------------------------------------------------------------
# Session and attempts
# ---------------------------------------------------------------------------


class Session(_Entity):
    id: UUID = Field(default_factory=uuid4)
    student_id: UUID
    problem_id: UUID
    started_at: datetime
    state: SessionState = SessionState.AWAITING_ANSWER
    attempt_count: int = Field(default=0, ge=0)


class Attempt(_Entity):
    id: UUID = Field(default_factory=uuid4)
    session_id: UUID
    student_answer: str
    timestamp: datetime
    hint_level_shown: int = Field(default=0, ge=0, le=MAX_HINT_LEVEL)


# ---------------------------------------------------------------------------
# Model-call audit (§5, §12)
# ---------------------------------------------------------------------------


class LLMCall(_Entity):
    """The record that replaces determinism with auditability (§4, §12).

    Model calls cannot be re-derived from first principles, so a session is only
    defensible later if the exact model, prompt version, and payloads were stored
    at the time. No stage may call a model without writing one of these (M0.4).
    """

    id: UUID = Field(default_factory=uuid4)
    session_id: UUID
    stage: PipelineStage
    model_id: str = Field(min_length=1)
    prompt_version: str = Field(min_length=1)
    input_payload: dict[str, object]
    output_payload: dict[str, object]
    tokens_in: int = Field(ge=0)
    tokens_out: int = Field(ge=0)
    latency_ms: int = Field(ge=0)
    cost_usd: float = Field(ge=0.0)
    created_at: datetime


# ---------------------------------------------------------------------------
# Pipeline outputs
# ---------------------------------------------------------------------------


class DiagnosisAlternative(_Entity):
    """A runner-up tag. When the top two are close the answer is genuinely
    ambiguous, and §3.1 requires routing to a generic hint instead of guessing."""

    tag_label: str
    confidence: float = Field(ge=0.0, le=1.0)


class DiagnosisLog(_Entity):
    id: UUID = Field(default_factory=uuid4)
    attempt_id: UUID
    misconception_tag_id: UUID | None
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: str
    alternatives: list[DiagnosisAlternative] = Field(default_factory=list)
    source: DiagnosisSource
    llm_call_id: UUID | None = None

    @model_validator(mode="after")
    def _llm_diagnoses_are_attributable(self) -> DiagnosisLog:
        """An LLM-sourced diagnosis without a call ID is an un-auditable decision.

        This is the domain-level half of M0.4's guarantee: the ledger enforces
        that calls get written, and this enforces that outputs point back at them.
        """
        if self.source is DiagnosisSource.LLM and self.llm_call_id is None:
            raise ValueError("LLM-sourced diagnosis must reference its llm_call_id")
        return self


class HintLog(_Entity):
    """`leak_checker_version` is not optional bookkeeping — when the checker is
    revised, it identifies which hints were cleared by the older logic (§3.3)."""

    id: UUID = Field(default_factory=uuid4)
    session_id: UUID
    attempt_number: int = Field(ge=1)
    misconception_tag_id: UUID | None
    curriculum_node_id: UUID | None
    hint_text: str
    hint_level: int = Field(ge=1, le=MAX_HINT_LEVEL)
    source: HintSource
    leak_check_passed: bool
    leak_checker_version: str = Field(min_length=1)
    llm_call_id: UUID | None = None

    @model_validator(mode="after")
    def _only_cleared_hints_exist(self) -> HintLog:
        """A hint that failed leak-check must never be persisted as shown.

        Failures route to regeneration and then to a template fallback (§3.3);
        they do not reach a student, so they do not reach this log.
        """
        if not self.leak_check_passed:
            raise ValueError(
                "hint failed leak-check and must not be logged as shown; "
                "regenerate or fall back to a template hint"
            )
        return self


class RubricCriterion(_Entity):
    """`evidence_span` quotes the student's own words back (§3.5) — the
    difference between a grade a teacher can defend and an opaque one."""

    criterion: str
    met: bool
    evidence_span: str | None = None


class GradeResult(_Entity):
    id: UUID = Field(default_factory=uuid4)
    attempt_id: UUID
    score: float = Field(ge=0.0, le=1.0)
    confidence: float = Field(ge=0.0, le=1.0)
    method: GradeMethod
    rubric_breakdown: list[RubricCriterion] = Field(default_factory=list)
    symbolic_agreed: bool | None = None
    llm_call_id: UUID | None = None


class ReviewItem(_Entity):
    """A queue row. Unlike the log entities this one is *mutable state* — see
    `ReviewVerdict` for why the audit trail lives elsewhere."""

    id: UUID = Field(default_factory=uuid4)
    session_id: UUID
    reason: ReviewReason
    created_at: datetime
    resolved_at: datetime | None = None


class ShadowCandidate(_Entity):
    """A generated hint that was produced but never shown (Phase 0, P0.4).

    Shadow mode is what makes Phase 0 a trial rather than a soft launch: the full
    model pipeline runs on every session, the child is served the template hint,
    and the generated candidate is recorded here beside it for teacher rating.
    The model is on trial; the student experience stays deterministic.

    This row is the phase's primary output. Implementation-Plan.md P0.9 turns
    these ratings into the labelled evaluation set, and P0.5 seeds the adversarial
    leak corpus from any candidate a teacher flags as leaky — so a shadow run that
    is not recorded is a session's worth of evidence thrown away.
    """

    id: UUID = Field(default_factory=uuid4)
    session_id: UUID
    attempt_number: int = Field(ge=1)
    hint_level: int = Field(ge=1, le=MAX_HINT_LEVEL)
    # What the model produced, and what the child actually saw. Both are stored
    # because the Phase 0 exit gate compares them: is generation beating the
    # deterministic path on real student work?
    generated_text: str
    shown_text: str | None
    misconception_tag: str
    prompt_version: str
    leak_check_passed: bool
    leak_checker_version: str = Field(min_length=1)
    leak_reason: str | None = None
    llm_call_id: UUID | None = None
    created_at: datetime


class ShadowRating(_Entity):
    """A teacher's judgement of a shadow candidate (P0.8).

    Appended rather than written onto the candidate, for the same reason
    `ReviewVerdict` is separate from `ReviewItem`: a rating is evidence, and a
    second opinion must not overwrite the first.
    """

    id: UUID = Field(default_factory=uuid4)
    shadow_candidate_id: UUID
    teacher_id: UUID
    # Is the generated hint better than, equal to, or worse than the template
    # the child was actually shown? Phase 0's exit needs generation to beat the
    # template on >=70% of cases before it is worth its cost and risk.
    better_than_shown: bool
    would_leak: bool = False
    notes: str | None = None
    created_at: datetime


class SafetyAlert(_Entity):
    """A §7 welfare signal raised from student text (P1.8).

    Deliberately *not* a `ReviewItem`. A teaching queue and a welfare alert have
    different readers, different urgency, and different consequences for being
    missed; routing a possible disclosure through the same list a teacher works
    through at the end of a lesson is how it gets read at the end of a lesson.

    **This row carries the student's identity, and the prompt that produced it
    does not.** That asymmetry is the point. `PromptContext` has no field that can
    name a child (M0.10), so the classifier judges text alone — while the person
    who has to act needs to know exactly which child, in which class, and what
    they wrote. The model gets the words without the identity; the adult gets the
    identity with the words.

    Append-only: an alert is evidence that a signal was raised, and evidence that
    can be edited afterward is worth less than none. Acknowledgement is a
    separate appended row, so "nobody opened this" stays distinguishable from
    "someone looked and judged it nothing".
    """

    id: UUID = Field(default_factory=uuid4)
    session_id: UUID
    student_id: UUID
    attempt_number: int = Field(ge=1)
    category: SafetyCategory
    excerpt: str = Field(min_length=1)
    """The child's own words. An alert a human cannot evaluate without opening
    three other screens is an alert that gets deferred."""

    screener_version: str = Field(min_length=1)
    classifier_ran: bool
    """False means only the deterministic layer saw this text. It does not mean
    the text was cleared by the classifier — §8 has to be able to tell those
    apart when a false-negative rate is computed."""

    detected_at: datetime


class SafetyAcknowledgement(_Entity):
    """A named adult saw a `SafetyAlert` (P1.8).

    The whole question about a safety path is whether anyone actually read it.
    Without this row an alert that was actioned and one nobody opened are
    identical in the data, and "we have distress screening" becomes unfalsifiable.
    """

    id: UUID = Field(default_factory=uuid4)
    safety_alert_id: UUID
    principal_id: UUID
    action_taken: str = Field(min_length=1)
    """Free text on purpose. The appropriate response to a child's disclosure is
    not enumerable, and a dropdown would quietly teach responders to pick the
    nearest listed option instead of describing what they did."""

    created_at: datetime


class RolloutChange(_Entity):
    """A deliberate change to how much generated text may reach children (P1.1).

    Phase 1 turns generation on in steps — 5%, then 25%, then 100% — each step
    gated on leak rate, teacher rating, and escalation rate holding, with a kill
    switch back to templates. Both halves of that are *operational* controls: they
    have to move without a deploy, or the kill switch is a redeploy and "instantly"
    is a word in a plan rather than a property of the system.

    So the current setting is the most recent row of this table, and changing it
    is an append. That has three consequences worth the table:

    - **The change is instant.** The pipeline reads this per attempt, so the next
      child gets the new setting. Nothing to restart, nothing to redeploy.
    - **Every change is attributable.** `changed_by` and `reason` are required. A
      rollout advanced to 100% is a decision someone made against evidence, and
      the exit criteria in Implementation-Plan.md Phase 1 are argued from data —
      an unattributed percentage change makes that argument unreconstructable.
    - **A rollback cannot erase the rollout it reverses.** Append-only, like every
      other log-bearing table (§5). If generation is killed after a leak, the
      window during which it was on is exactly what an incident review needs.
    """

    id: UUID = Field(default_factory=uuid4)

    generation_enabled: bool
    """The kill switch. `False` serves every child a template, whatever the
    percentage says — so reverting does not require also remembering to zero the
    percentage, and turning generation back on restores the cohort that was
    already configured rather than a number typed under pressure."""

    percentage: int = Field(ge=0, le=100)
    """Share of *sessions* that may be served generated hints."""

    changed_by: UUID
    reason: str = Field(min_length=1)
    """Why, in a human's words. Required — a change to what a child is shown that
    nobody wrote a sentence about is a change nobody can review."""

    created_at: datetime


class PipelineEvent(_Entity):
    """One logged transition in a session (§2, §4, M0.8).

    `sequence` is what makes replay faithful rather than approximately ordered.
    Two events inside the same stage can share a timestamp — clocks are coarse
    and stages are fast — and a replay that reorders "leak-check failed" and
    "template fallback used" tells the wrong story about why a child saw what
    they saw. A per-session counter has no such ambiguity.
    """

    id: UUID = Field(default_factory=uuid4)
    session_id: UUID
    sequence: int = Field(ge=0)
    event_type: EventType
    stage: PipelineStage | None = None
    from_state: SessionState | None = None
    to_state: SessionState | None = None
    detail: dict[str, object] = Field(default_factory=dict)
    llm_call_id: UUID | None = None
    occurred_at: datetime

    @model_validator(mode="after")
    def _state_changes_record_both_ends(self) -> PipelineEvent:
        """A transition without a destination cannot be replayed."""
        if self.event_type is EventType.STATE_CHANGED and self.to_state is None:
            raise ValueError("STATE_CHANGED events must record to_state")
        return self


class ReviewVerdict(_Entity):
    """Appended when a teacher resolves a `ReviewItem`.

    §5 lists `teacher_verdict` and `resolved_at` as fields on `ReviewItem` while
    also calling that table append-only — those cannot both hold, since resolving
    an item would rewrite the row. Splitting the verdict into its own appended
    record keeps the audit guarantee that §5 is actually after: the queue row
    tracks open/closed state, and every verdict ever recorded survives, including
    a teacher revising an earlier decision.
    """

    id: UUID = Field(default_factory=uuid4)
    review_item_id: UUID
    teacher_id: UUID
    verdict: TeacherVerdict
    notes: str | None = None
    created_at: datetime
