"""Controlled vocabularies for the pipeline.

Every one of these is a closed set on purpose. The diagnoser's output is
constrained to `MisconceptionTag` rows rather than free text (Architecture.md
§3.1), grades carry a `GradeMethod` so an audit can tell what decided them
(§5), and `ReviewReason` is the full list of ways a session reaches a human
(§3.6). Adding a member here is a deliberate act, not a passing string.
"""

from __future__ import annotations

from enum import StrEnum


class GradeBand(StrEnum):
    """Grade bands are threaded through every prompt, filter, and rubric (§1.5)."""

    K_1 = "K-1"
    G2_3 = "2-3"
    G4_5 = "4-5"
    G6_8 = "6-8"
    G9_12 = "9-12"


class Operation(StrEnum):
    ADDITION = "addition"
    SUBTRACTION = "subtraction"
    MULTIPLICATION = "multiplication"
    DIVISION = "division"
    FRACTIONS = "fractions"
    ALGEBRA = "algebra"


class AnswerType(StrEnum):
    """Drives which grading path a response takes (§3.5)."""

    NUMERIC = "numeric"
    FRACTION = "fraction"
    EXPRESSION = "expression"
    MULTIPLE_CHOICE = "multiple_choice"
    SHORT_RESPONSE = "short_response"
    FREE_TEXT = "free_text"


class SessionState(StrEnum):
    """The §4 state machine. Persisted, so transitions are observable and resumable."""

    AWAITING_ANSWER = "awaiting_answer"
    DIAGNOSING = "diagnosing"
    RETRIEVING_CURRICULUM = "retrieving_curriculum"
    GENERATING_HINT = "generating_hint"
    LEAK_CHECKING = "leak_checking"
    AWAITING_STUDENT_RETRY = "awaiting_student_retry"
    GRADING = "grading"
    COMPLETE = "complete"
    ESCALATED = "escalated"


class PipelineStage(StrEnum):
    """Stage attribution on every `LLMCall`, so cost and latency are per-stage (§8)."""

    DIAGNOSE = "diagnose"
    RERANK = "rerank"
    GENERATE_HINT = "generate_hint"
    LEAK_CHECK = "leak_check"
    GRADE = "grade"
    SAFETY_SCREEN = "safety_screen"
    TEACHER_SUMMARY = "teacher_summary"


class Role(StrEnum):
    """Who is asking (M0.9).

    Deliberately three, not a permission matrix. §6 has one teacher-facing
    surface, one admin surface, and the student client; inventing finer roles
    before there is a surface that needs them produces a policy nobody can
    reason about — and this policy governs access to children's work.
    """

    STUDENT = "student"
    TEACHER = "teacher"
    ADMIN = "admin"


class EventType(StrEnum):
    """What happened in a session (§2, §4).

    Architecture.md §2 says "each arrow is a logged, versioned event" and §4
    requires every transition to be observable and resumable. These are those
    arrows. The set is deliberately small: an event type earns its place by being
    something a teacher, an auditor, or a debugger would ask about later.
    """

    SESSION_STARTED = "session_started"
    STATE_CHANGED = "state_changed"
    STAGE_STARTED = "stage_started"
    STAGE_COMPLETED = "stage_completed"
    STAGE_FAILED = "stage_failed"
    # §4 requires the pipeline to keep serving when the provider is down. A
    # degradation is a first-class event because "the hint was a template today"
    # is exactly the question asked when quality metrics dip.
    FALLBACK_USED = "fallback_used"
    HINT_SHOWN = "hint_shown"
    ANSWER_SUBMITTED = "answer_submitted"
    # A §7 welfare signal fired (P1.8). The child's words are deliberately not in
    # the event — a disclosure belongs in `safety_alert`, which has its own
    # reader, and not on every surface that renders a session timeline.
    SAFETY_FLAG = "safety_flag"
    GRADED = "graded"
    ESCALATED = "escalated"
    SESSION_COMPLETED = "session_completed"


class DiagnosisSource(StrEnum):
    """`RULE` means the cheap deterministic pre-check fired and no model was called (§3.1)."""

    RULE = "rule"
    LLM = "llm"


class HintSource(StrEnum):
    """`TEMPLATE_FALLBACK` records a leak-check failure or a provider outage (§3.3, §4)."""

    GENERATED = "generated"
    CACHED = "cached"
    TEMPLATE_FALLBACK = "template_fallback"


class GradeMethod(StrEnum):
    SYMBOLIC = "symbolic"
    RUBRIC = "rubric"
    HYBRID = "hybrid"
    TEACHER = "teacher"


class ReviewReason(StrEnum):
    """Every route to a human (§3.6). `LEAK_FALLBACK` is here because a hint that
    failed generation twice is a quality signal worth a teacher's eyes."""

    LOW_CONFIDENCE = "low_confidence"
    RUBRIC_DISAGREEMENT = "rubric_disagreement"
    SYMBOLIC_DISAGREEMENT = "symbolic_disagreement"
    UNKNOWN_TAG = "unknown_tag"
    MAX_HINTS = "max_hints"
    SAFETY_FLAG = "safety_flag"
    LEAK_FALLBACK = "leak_fallback"
    AUDIT_SAMPLE = "audit_sample"


class SafetyCategory(StrEnum):
    """What kind of adult a §7 safety signal needs in front of it (P1.8).

    Deliberately coarse. A finer taxonomy would imply the screen can tell these
    apart reliably, and it cannot — these exist to route an alert, never to
    characterise a child. `ReviewReason.SAFETY_FLAG` is a different thing and
    stays separate: that is a teaching-queue row, and this is a welfare alert
    with its own destination and its own urgency.
    """

    SELF_HARM = "self_harm"
    """Statements about hurting or killing oneself. Highest urgency."""

    HARM_FROM_OTHERS = "harm_from_others"
    """Abuse, violence, or fear of home — often a mandated-reporting matter,
    which is a legal obligation attaching to a person, not to software."""

    HOPELESSNESS = "hopelessness"
    """Not an emergency alone and easy to over-read; a lower-urgency path."""


URGENT_SAFETY_CATEGORIES = frozenset({SafetyCategory.SELF_HARM, SafetyCategory.HARM_FROM_OTHERS})
"""Needs an adult now, not at end of day."""


class TeacherVerdict(StrEnum):
    CONFIRMED = "confirmed"
    OVERRIDDEN = "overridden"
    NEEDS_FOLLOWUP = "needs_followup"


UNKNOWN_TAG_LABEL = "unknown"
"""Reserved tag for "no rule fired and the model wasn't confident enough" (§3.1).

A confident wrong diagnosis is worse than no diagnosis, so this is a first-class
outcome that routes to a generic Socratic hint — not an error state.
"""
