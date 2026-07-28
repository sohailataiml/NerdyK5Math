"""Persistence mapping for the §5 entities.

Declarative table definitions only — no queries, no sessions. The
``AppendOnly`` mixin on the log-bearing tables is load-bearing: it is what
``append_only.py`` keys off to refuse updates and deletes.

Enums are stored as ``VARCHAR`` + ``CHECK`` (``native_enum=False``) rather than
Postgres enum types, so the same schema runs under SQLite for fast tests and
Postgres in production, and so adding a member doesn't require a type migration.
"""

from __future__ import annotations

import datetime as dt
import uuid

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from packages.domain.append_only import AppendOnly
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

EMBEDDING_DIM = 1024
"""Dimensionality of curriculum strategy embeddings (§3.2).

Provisional: the embedding model has not been chosen yet (open question in
Implementation-Plan.md §7). Changing this is a migration plus a full re-index,
so it is a decision to make before Phase 1 retrieval work — not after.
"""


class Base(DeclarativeBase):
    pass


def _pk() -> Mapped[uuid.UUID]:
    return mapped_column(Uuid, primary_key=True, default=uuid.uuid4)


def _enum(python_enum: type, name: str) -> Enum:
    return Enum(python_enum, name=name, native_enum=False, validate_strings=True)


# ---------------------------------------------------------------------------
# Mutable reference data
# ---------------------------------------------------------------------------


class StudentRow(Base):
    __tablename__ = "student"

    id: Mapped[uuid.UUID] = _pk()
    grade_level: Mapped[int] = mapped_column(Integer)
    iep_flags: Mapped[list[str]] = mapped_column(JSON, default=list)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (CheckConstraint("grade_level >= 0 AND grade_level <= 12", name="ck_grade"),)


class PrincipalRow(Base):
    __tablename__ = "principal"

    id: Mapped[uuid.UUID] = _pk()
    role: Mapped[Role] = mapped_column(_enum(Role, "role"))
    display_name: Mapped[str] = mapped_column(String(128))
    student_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("student.id"), nullable=True, unique=True
    )
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))


class ClassroomRow(Base):
    __tablename__ = "classroom"

    id: Mapped[uuid.UUID] = _pk()
    teacher_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("principal.id"), index=True)
    name: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))


class EnrollmentRow(Base):
    __tablename__ = "enrollment"

    id: Mapped[uuid.UUID] = _pk()
    classroom_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("classroom.id"), index=True)
    student_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("student.id"), index=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (UniqueConstraint("classroom_id", "student_id", name="uq_enrollment"),)


class MisconceptionTagRow(Base):
    __tablename__ = "misconception_tag"

    id: Mapped[uuid.UUID] = _pk()
    label: Mapped[str] = mapped_column(String(128), unique=True)
    operation_type: Mapped[Operation] = mapped_column(_enum(Operation, "operation"))
    description: Mapped[str] = mapped_column(Text)
    example_pattern: Mapped[str | None] = mapped_column(Text, nullable=True)


class CurriculumNodeRow(Base):
    __tablename__ = "curriculum_node"

    id: Mapped[uuid.UUID] = _pk()
    standard_code: Mapped[str] = mapped_column(String(64), index=True)
    grade_band: Mapped[GradeBand] = mapped_column(_enum(GradeBand, "grade_band"))
    definition: Mapped[str] = mapped_column(Text)
    remediation_strategies: Mapped[list[str]] = mapped_column(JSON, default=list)
    prerequisite_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    # Bumped when strategy text changes, so a stale embedding is detectable
    # rather than silently unretrievable (Implementation-Plan.md §2).
    embedding_version: Mapped[int] = mapped_column(Integer, default=0)
    # Vector is the base type and JSON is the SQLite variant, deliberately in
    # that order: SQLAlchemy takes the comparator from the base type, so
    # `Vector` first is what keeps `.cosine_distance()` and friends available on
    # this column. Declared JSON-first, the DDL is identical but every retrieval
    # query (§3.2) would have to drop to raw SQL.
    # SQLite still gets a JSON column, so the unit suite needs no container —
    # it exercises the schema, not similarity search.
    embedding: Mapped[list[float] | None] = mapped_column(
        Vector(EMBEDDING_DIM).with_variant(JSON(), "sqlite"), nullable=True
    )


class ProblemRow(Base):
    __tablename__ = "problem"

    id: Mapped[uuid.UUID] = _pk()
    curriculum_node_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("curriculum_node.id"))
    prompt: Mapped[str] = mapped_column(Text)
    correct_answer: Mapped[str] = mapped_column(Text)
    answer_type: Mapped[AnswerType] = mapped_column(_enum(AnswerType, "answer_type"))
    grade_band: Mapped[GradeBand] = mapped_column(_enum(GradeBand, "grade_band"))


class SessionRow(Base):
    """Workflow state (§4). Mutable by design — this *is* the state machine's cursor."""

    __tablename__ = "session"

    id: Mapped[uuid.UUID] = _pk()
    student_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("student.id"), index=True)
    problem_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("problem.id"))
    started_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    state: Mapped[SessionState] = mapped_column(_enum(SessionState, "session_state"))
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)


class ReviewItemRow(Base):
    """Queue state, not a log — open/closed flips as teachers work the queue.

    The immutable audit trail lives in ``ReviewVerdictRow``; see the note on
    ``models.ReviewVerdict`` for why §5's shape had to be split.
    """

    __tablename__ = "review_item"

    id: Mapped[uuid.UUID] = _pk()
    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("session.id"), index=True)
    reason: Mapped[ReviewReason] = mapped_column(_enum(ReviewReason, "review_reason"))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (Index("ix_review_open", "resolved_at", "created_at"),)


# ---------------------------------------------------------------------------
# Append-only log tables (§5)
# ---------------------------------------------------------------------------


class AttemptRow(Base, AppendOnly):
    __tablename__ = "attempt"

    id: Mapped[uuid.UUID] = _pk()
    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("session.id"), index=True)
    student_answer: Mapped[str] = mapped_column(Text)
    timestamp: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    hint_level_shown: Mapped[int] = mapped_column(Integer, default=0)


class LLMCallRow(Base, AppendOnly):
    """The audit ledger (M0.4). Every model call in the system lands here."""

    __tablename__ = "llm_call"

    id: Mapped[uuid.UUID] = _pk()
    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("session.id"), index=True)
    stage: Mapped[PipelineStage] = mapped_column(_enum(PipelineStage, "pipeline_stage"))
    model_id: Mapped[str] = mapped_column(String(128))
    prompt_version: Mapped[str] = mapped_column(String(64), index=True)
    input_payload: Mapped[dict[str, object]] = mapped_column(JSON)
    output_payload: Mapped[dict[str, object]] = mapped_column(JSON)
    tokens_in: Mapped[int] = mapped_column(Integer)
    tokens_out: Mapped[int] = mapped_column(Integer)
    latency_ms: Mapped[int] = mapped_column(Integer)
    cost_usd: Mapped[float] = mapped_column(Float)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))

    # §8 wants quality and cost segmented by prompt version and model.
    __table_args__ = (Index("ix_llm_call_stage_version", "stage", "prompt_version", "created_at"),)


class DiagnosisLogRow(Base, AppendOnly):
    __tablename__ = "diagnosis_log"

    id: Mapped[uuid.UUID] = _pk()
    attempt_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("attempt.id"), index=True)
    misconception_tag_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("misconception_tag.id"), nullable=True
    )
    confidence: Mapped[float] = mapped_column(Float)
    evidence: Mapped[str] = mapped_column(Text)
    alternatives: Mapped[list[dict[str, object]]] = mapped_column(JSON, default=list)
    source: Mapped[DiagnosisSource] = mapped_column(_enum(DiagnosisSource, "diagnosis_source"))
    llm_call_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("llm_call.id"), nullable=True)


class HintLogRow(Base, AppendOnly):
    __tablename__ = "hint_log"

    id: Mapped[uuid.UUID] = _pk()
    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("session.id"), index=True)
    attempt_number: Mapped[int] = mapped_column(Integer)
    misconception_tag_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("misconception_tag.id"), nullable=True
    )
    curriculum_node_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("curriculum_node.id"), nullable=True
    )
    hint_text: Mapped[str] = mapped_column(Text)
    hint_level: Mapped[int] = mapped_column(Integer)
    source: Mapped[HintSource] = mapped_column(_enum(HintSource, "hint_source"))
    leak_check_passed: Mapped[bool] = mapped_column(Boolean)
    leak_checker_version: Mapped[str] = mapped_column(String(64))
    llm_call_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("llm_call.id"), nullable=True)


class GradeResultRow(Base, AppendOnly):
    __tablename__ = "grade_result"

    id: Mapped[uuid.UUID] = _pk()
    attempt_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("attempt.id"), index=True)
    score: Mapped[float] = mapped_column(Float)
    confidence: Mapped[float] = mapped_column(Float)
    method: Mapped[GradeMethod] = mapped_column(_enum(GradeMethod, "grade_method"))
    rubric_breakdown: Mapped[list[dict[str, object]]] = mapped_column(JSON, default=list)
    symbolic_agreed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    llm_call_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("llm_call.id"), nullable=True)


class ShadowCandidateRow(Base, AppendOnly):
    """Phase 0's primary output (P0.4)."""

    __tablename__ = "shadow_candidate"

    id: Mapped[uuid.UUID] = _pk()
    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("session.id"), index=True)
    attempt_number: Mapped[int] = mapped_column(Integer)
    hint_level: Mapped[int] = mapped_column(Integer)
    generated_text: Mapped[str] = mapped_column(Text)
    shown_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    misconception_tag: Mapped[str] = mapped_column(String(128))
    prompt_version: Mapped[str] = mapped_column(String(64), index=True)
    leak_check_passed: Mapped[bool] = mapped_column(Boolean)
    leak_checker_version: Mapped[str] = mapped_column(String(64))
    leak_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    llm_call_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("llm_call.id"), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))

    # The rating queue is "unrated candidates, oldest first".
    __table_args__ = (Index("ix_shadow_unrated", "created_at", "prompt_version"),)


class ShadowRatingRow(Base, AppendOnly):
    __tablename__ = "shadow_rating"

    id: Mapped[uuid.UUID] = _pk()
    shadow_candidate_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("shadow_candidate.id"), index=True
    )
    teacher_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("principal.id"))
    better_than_shown: Mapped[bool] = mapped_column(Boolean)
    would_leak: Mapped[bool] = mapped_column(Boolean, default=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))


class SafetyAlertRow(Base, AppendOnly):
    """A §7 welfare alert (P1.8). Separate table, separate reader, separate queue
    — deliberately not a `review_item` row."""

    __tablename__ = "safety_alert"

    id: Mapped[uuid.UUID] = _pk()
    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("session.id"), index=True)
    student_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("student.id"), index=True)
    attempt_number: Mapped[int] = mapped_column(Integer)
    category: Mapped[SafetyCategory] = mapped_column(_enum(SafetyCategory, "safety_category"))
    excerpt: Mapped[str] = mapped_column(Text)
    screener_version: Mapped[str] = mapped_column(String(64))
    classifier_ran: Mapped[bool] = mapped_column(Boolean)
    detected_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), index=True)


class SafetyAcknowledgementRow(Base, AppendOnly):
    """Proof a named adult read an alert. Without it, "actioned" and "never
    opened" are the same row."""

    __tablename__ = "safety_acknowledgement"

    id: Mapped[uuid.UUID] = _pk()
    safety_alert_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("safety_alert.id"), index=True)
    principal_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("principal.id"))
    action_taken: Mapped[str] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))


class RolloutChangeRow(Base, AppendOnly):
    """The Phase 1 rollout control (P1.1). Current setting = the latest row."""

    __tablename__ = "rollout_change"

    id: Mapped[uuid.UUID] = _pk()
    generation_enabled: Mapped[bool] = mapped_column(Boolean)
    percentage: Mapped[int] = mapped_column(Integer)
    changed_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("principal.id"))
    reason: Mapped[str] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), index=True)

    __table_args__ = (
        # The pipeline reads "latest row" on every attempt, so this index is on
        # the hot path rather than a reporting convenience.
        Index("ix_rollout_current", "created_at"),
        CheckConstraint("percentage >= 0 AND percentage <= 100", name="ck_rollout_percentage"),
    )


class PipelineEventRow(Base, AppendOnly):
    """The §4 replay log (M0.8)."""

    __tablename__ = "pipeline_event"

    id: Mapped[uuid.UUID] = _pk()
    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("session.id"), index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    event_type: Mapped[EventType] = mapped_column(_enum(EventType, "event_type"))
    stage: Mapped[PipelineStage | None] = mapped_column(
        _enum(PipelineStage, "pipeline_stage"), nullable=True
    )
    from_state: Mapped[SessionState | None] = mapped_column(
        _enum(SessionState, "session_state"), nullable=True
    )
    to_state: Mapped[SessionState | None] = mapped_column(
        _enum(SessionState, "session_state"), nullable=True
    )
    detail: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    llm_call_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("llm_call.id"), nullable=True)
    occurred_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        # Ordering is a correctness property here, not a nicety: a gap or a
        # duplicate means the replay is not the session that happened.
        UniqueConstraint("session_id", "sequence", name="uq_event_sequence"),
        Index("ix_event_replay", "session_id", "sequence"),
    )


class ReviewVerdictRow(Base, AppendOnly):
    """Append-only resolution trail. A teacher revising a decision appends a
    second verdict; the first is never overwritten."""

    __tablename__ = "review_verdict"

    id: Mapped[uuid.UUID] = _pk()
    review_item_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("review_item.id"), index=True)
    teacher_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    verdict: Mapped[TeacherVerdict] = mapped_column(_enum(TeacherVerdict, "teacher_verdict"))
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
