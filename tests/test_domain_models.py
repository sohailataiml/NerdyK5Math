"""M0.2 — every §5 entity survives a write/read round trip, and the validation
that protects downstream stages actually fires.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from pydantic import BaseModel, ValidationError
from sqlalchemy.orm import Session as DbSession

from packages.domain import models as m
from packages.domain import tables as t
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
from packages.domain.mapping import from_row, to_row

NOW = dt.datetime(2026, 7, 27, 12, 0, 0, tzinfo=dt.UTC)
ID = uuid.uuid4


def _cases() -> dict[str, tuple[BaseModel, type[t.Base], type[BaseModel]]]:
    """One populated instance of every §5 entity, with optional fields filled in.

    Optionals are populated on purpose — a round trip that only exercises
    defaults would pass while a nullable column was mismapped.
    """
    return {
        "student": (
            m.Student(grade_level=3, iep_flags=["extended_time"], created_at=NOW),
            t.StudentRow,
            m.Student,
        ),
        "principal": (
            m.Principal(
                role=Role.TEACHER,
                display_name="Ms Rivera",
                student_id=None,
                created_at=NOW,
            ),
            t.PrincipalRow,
            m.Principal,
        ),
        "classroom": (
            m.Classroom(teacher_id=ID(), name="Room 1", created_at=NOW),
            t.ClassroomRow,
            m.Classroom,
        ),
        "enrollment": (
            m.Enrollment(classroom_id=ID(), student_id=ID(), created_at=NOW),
            t.EnrollmentRow,
            m.Enrollment,
        ),
        "misconception_tag": (
            m.MisconceptionTag(
                label="subtracted_instead_of_added",
                operation_type=Operation.ADDITION,
                description="Student applies subtraction to an addition problem.",
                example_pattern="wrong_answer == abs(a - b)",
            ),
            t.MisconceptionTagRow,
            m.MisconceptionTag,
        ),
        "curriculum_node": (
            m.CurriculumNode(
                standard_code="3.OA.A.1",
                grade_band=GradeBand.G2_3,
                definition="Interpret products of whole numbers.",
                remediation_strategies=["array model", "equal groups"],
                prerequisite_ids=[ID(), ID()],
                embedding_version=2,
            ),
            t.CurriculumNodeRow,
            m.CurriculumNode,
        ),
        "problem": (
            m.Problem(
                curriculum_node_id=ID(),
                prompt="What is 7 + 5?",
                correct_answer="12",
                answer_type=AnswerType.NUMERIC,
                grade_band=GradeBand.K_1,
            ),
            t.ProblemRow,
            m.Problem,
        ),
        "session": (
            m.Session(
                student_id=ID(),
                problem_id=ID(),
                started_at=NOW,
                state=SessionState.GENERATING_HINT,
                attempt_count=2,
            ),
            t.SessionRow,
            m.Session,
        ),
        "attempt": (
            m.Attempt(session_id=ID(), student_answer="2", timestamp=NOW, hint_level_shown=1),
            t.AttemptRow,
            m.Attempt,
        ),
        "llm_call": (
            m.LLMCall(
                session_id=ID(),
                stage=PipelineStage.DIAGNOSE,
                model_id="claude-haiku-4-5-20251001",
                prompt_version="diagnose/k-1/v3",
                input_payload={"problem": "7 + 5", "student_answer": "2"},
                output_payload={"misconception_tag": "subtracted_instead_of_added"},
                tokens_in=412,
                tokens_out=38,
                latency_ms=630,
                cost_usd=0.00021,
                created_at=NOW,
            ),
            t.LLMCallRow,
            m.LLMCall,
        ),
        "diagnosis_log": (
            m.DiagnosisLog(
                attempt_id=ID(),
                misconception_tag_id=ID(),
                confidence=0.86,
                evidence="student answer (2) equals a-b; correct op is addition",
                alternatives=[
                    m.DiagnosisAlternative(
                        tag_label="counted_back_from_wrong_start", confidence=0.31
                    )
                ],
                source=DiagnosisSource.LLM,
                llm_call_id=ID(),
            ),
            t.DiagnosisLogRow,
            m.DiagnosisLog,
        ),
        "hint_log": (
            m.HintLog(
                session_id=ID(),
                attempt_number=1,
                misconception_tag_id=ID(),
                curriculum_node_id=ID(),
                hint_text="You have 7 counters and you're putting 5 more with them...",
                hint_level=1,
                source=HintSource.GENERATED,
                leak_check_passed=True,
                leak_checker_version="leak/v1.2",
                llm_call_id=ID(),
            ),
            t.HintLogRow,
            m.HintLog,
        ),
        "grade_result": (
            m.GradeResult(
                attempt_id=ID(),
                score=1.0,
                confidence=0.94,
                method=GradeMethod.HYBRID,
                rubric_breakdown=[
                    m.RubricCriterion(
                        criterion="Explains regrouping",
                        met=True,
                        evidence_span="I made a ten from 7 and 3",
                    )
                ],
                symbolic_agreed=True,
                llm_call_id=ID(),
            ),
            t.GradeResultRow,
            m.GradeResult,
        ),
        "review_item": (
            m.ReviewItem(
                session_id=ID(),
                reason=ReviewReason.LOW_CONFIDENCE,
                created_at=NOW,
                resolved_at=NOW + dt.timedelta(hours=3),
            ),
            t.ReviewItemRow,
            m.ReviewItem,
        ),
        "shadow_candidate": (
            m.ShadowCandidate(
                session_id=ID(),
                attempt_number=1,
                hint_level=1,
                generated_text="Fill your ten-frame with 7. How many more to make ten?",
                shown_text="You have 7 counters and you're getting 5 more...",
                misconception_tag="subtracted_instead_of_added",
                prompt_version="generate_hint/K-1/v1",
                leak_check_passed=True,
                leak_checker_version="deterministic/v1+classifier/v1",
                leak_reason=None,
                llm_call_id=ID(),
                created_at=NOW,
            ),
            t.ShadowCandidateRow,
            m.ShadowCandidate,
        ),
        "shadow_rating": (
            m.ShadowRating(
                shadow_candidate_id=ID(),
                teacher_id=ID(),
                better_than_shown=True,
                would_leak=False,
                notes="Clearer than the template for this child.",
                created_at=NOW,
            ),
            t.ShadowRatingRow,
            m.ShadowRating,
        ),
        "safety_alert": (
            m.SafetyAlert(
                session_id=ID(),
                student_id=ID(),
                attempt_number=1,
                category=SafetyCategory.SELF_HARM,
                excerpt="12 i want to die",
                screener_version="distress/deterministic/v1",
                classifier_ran=False,
                detected_at=NOW,
            ),
            t.SafetyAlertRow,
            m.SafetyAlert,
        ),
        "safety_acknowledgement": (
            m.SafetyAcknowledgement(
                safety_alert_id=ID(),
                principal_id=ID(),
                action_taken="Spoke with the child; referred to the school counsellor.",
                created_at=NOW,
            ),
            t.SafetyAcknowledgementRow,
            m.SafetyAcknowledgement,
        ),
        "pipeline_event": (
            m.PipelineEvent(
                session_id=ID(),
                sequence=4,
                event_type=EventType.STAGE_COMPLETED,
                stage=PipelineStage.DIAGNOSE,
                from_state=SessionState.DIAGNOSING,
                to_state=SessionState.GENERATING_HINT,
                detail={"tag": "subtracted_instead_of_added", "confidence": 0.86},
                llm_call_id=ID(),
                occurred_at=NOW,
            ),
            t.PipelineEventRow,
            m.PipelineEvent,
        ),
        "rollout_change": (
            m.RolloutChange(
                generation_enabled=True,
                percentage=5,
                changed_by=ID(),
                reason="Phase 0 exit gates met; opening generation to 5% of sessions.",
                created_at=NOW,
            ),
            t.RolloutChangeRow,
            m.RolloutChange,
        ),
        "review_verdict": (
            m.ReviewVerdict(
                review_item_id=ID(),
                teacher_id=ID(),
                verdict=TeacherVerdict.OVERRIDDEN,
                notes="Answer is right, phrasing threw the grader.",
                created_at=NOW,
            ),
            t.ReviewVerdictRow,
            m.ReviewVerdict,
        ),
    }


@pytest.mark.parametrize("name", sorted(_cases()))
def test_entity_survives_round_trip(name: str, session: DbSession) -> None:
    entity, row_cls, entity_cls = _cases()[name]

    session.add(to_row(entity, row_cls))
    session.commit()
    session.expunge_all()

    stored = session.query(row_cls).one()
    assert from_row(stored, entity_cls) == entity


def test_every_section_5_entity_is_covered() -> None:
    """Guard against an entity being added to the model without a round-trip case."""
    declared = {
        name
        for name, obj in vars(m).items()
        if isinstance(obj, type)
        and issubclass(obj, BaseModel)
        and obj is not BaseModel
        and not name.startswith("_")
    }
    # Value objects that live inside a parent entity's JSON column.
    nested = {"DiagnosisAlternative", "RubricCriterion"}
    covered = {type(entity).__name__ for entity, _, _ in _cases().values()}
    assert declared - nested - covered == set()


class TestValidation:
    """The checks that stop a bad value reaching a threshold comparison."""

    def test_confidence_above_one_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            m.DiagnosisLog(
                attempt_id=ID(),
                misconception_tag_id=ID(),
                confidence=1.4,
                evidence="",
                source=DiagnosisSource.RULE,
            )

    def test_llm_diagnosis_without_call_id_is_rejected(self) -> None:
        """M0.4: an LLM decision that points at no ledger row is un-auditable."""
        with pytest.raises(ValidationError, match="llm_call_id"):
            m.DiagnosisLog(
                attempt_id=ID(),
                misconception_tag_id=ID(),
                confidence=0.9,
                evidence="",
                source=DiagnosisSource.LLM,
            )

    def test_rule_diagnosis_needs_no_call_id(self) -> None:
        """The rule pre-check makes no model call, which is the point of it (§3.1)."""
        diagnosis = m.DiagnosisLog(
            attempt_id=ID(),
            misconception_tag_id=ID(),
            confidence=1.0,
            evidence="wrong_answer == a - b",
            source=DiagnosisSource.RULE,
        )
        assert diagnosis.llm_call_id is None

    def test_failed_leak_check_cannot_be_logged_as_shown(self) -> None:
        """§3.3: a hint that failed the checker never reaches a student, so it
        never reaches the log of hints that were shown."""
        with pytest.raises(ValidationError, match="leak-check"):
            m.HintLog(
                session_id=ID(),
                attempt_number=1,
                misconception_tag_id=None,
                curriculum_node_id=None,
                hint_text="The answer is 12.",
                hint_level=1,
                source=HintSource.GENERATED,
                leak_check_passed=False,
                leak_checker_version="leak/v1.2",
                llm_call_id=ID(),
            )

    def test_hint_level_beyond_max_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            m.HintLog(
                session_id=ID(),
                attempt_number=1,
                misconception_tag_id=None,
                curriculum_node_id=None,
                hint_text="...",
                hint_level=m.MAX_HINT_LEVEL + 1,
                source=HintSource.TEMPLATE_FALLBACK,
                leak_check_passed=True,
                leak_checker_version="leak/v1.2",
            )

    def test_entities_are_immutable(self) -> None:
        student = m.Student(grade_level=3, created_at=NOW)
        with pytest.raises(ValidationError):
            student.grade_level = 4  # type: ignore[misc]

    def test_unknown_field_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            m.Student(grade_level=3, created_at=NOW, nickname="Sam")  # type: ignore[call-arg]


def test_mapper_rejects_entity_field_with_no_column() -> None:
    """A field added to an entity but not to its table fails loudly here rather
    than being silently dropped on write."""

    class Drifted(BaseModel):
        id: uuid.UUID
        grade_level: int
        iep_flags: list[str]
        created_at: dt.datetime
        favourite_colour: str

    drifted = Drifted(id=ID(), grade_level=3, iep_flags=[], created_at=NOW, favourite_colour="blue")
    with pytest.raises(ValueError, match="favourite_colour"):
        to_row(drifted, t.StudentRow)
