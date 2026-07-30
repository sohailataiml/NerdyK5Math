"""The class overview a teacher opens before working the queue (§3.6, §8).

The review queue answers "what needs my judgement right now", one session at a
time. It deliberately says nothing about the child across sessions, and that is
the gap this fills: a teacher cannot see from the queue that the same
misconception has now been diagnosed four times for the same child, which is
precisely the thing that should change what they do next.

This is also the first surface that can exist at all. Per-student misconception
history is a query over `diagnosis_log`, and until that table had a producer the
answer was always "no rows" — see `services/orchestrator/diagnoses.py`. The
dashboard is the reason the record mattered, not a decoration on top of it.

**Every rate carries its denominator, and a rate the data cannot support is
withheld rather than rendered.** This is the load-bearing decision in the module
and it is worth being explicit about, because the alternative is worse than
useless. A class with three graded attempts and two correct does not have a "67%
accuracy"; it has three data points. Rendering the percentage invites a teacher to
act on noise — and `eval.harness.cli phase0` already refuses to report its gates
below a threshold, for the same reason and in the same words. A dashboard held to
a lower standard than the harness would be the one number a teacher actually reads.

**Sessions opened and never answered are excluded from per-session rates and
counted separately.** Findings.md recorded this correction against the pilot data
(57 of 97 sessions), and any rate computed over all sessions rather than answered
ones silently reports a child as struggling when they in fact walked away.

**Scoped per row, like every other teacher read.** A teacher sees their own
students and nothing else; M0.9's policy decides, and an admin is unscoped.
Aggregates are computed *after* that filter, so a class average can never be
contaminated by a child in another class.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections import Counter, defaultdict

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from packages.auth import (
    AuthorizationError,
    Scope,
    can_read_review_queue,
    can_read_student,
    require,
)
from packages.domain.enums import UNKNOWN_TAG_LABEL
from packages.domain.tables import (
    AttemptRow,
    DiagnosisLogRow,
    GradeResultRow,
    MisconceptionTagRow,
    PrincipalRow,
    ReviewItemRow,
    SessionRow,
    StudentRow,
)
from services.api.auth import current_scope
from services.api.db import get_db

router = APIRouter(prefix="/teacher", tags=["teacher"])

MIN_FOR_A_RATE = 5
"""Below this many observations, report the count and withhold the percentage.

Not a statistical claim — it is a floor, chosen to be obviously too small to act
on rather than to be defensible as a sample size. The honest version of this number
comes from P1.3's calibration work; until then, refusing to render `1/1 = 100%`
next to a child's name is the whole of the benefit.
"""

RECURRENCE_THRESHOLD = 3
"""How many times one misconception must be diagnosed for a child before it is
called out as recurring.

§3.1's diagnoser abstains often and is not yet calibrated (P1.3), so a single tag
is a hypothesis and not a finding. Three is the point at which "look at this with
them" is better advice than "keep going".
"""


class Measure(BaseModel):
    """A count, its denominator, and a rate only when one is warranted.

    The rate is `None` rather than absent so a client cannot accidentally treat a
    withheld rate as zero, and `withheld_because` carries the reason so the page
    can say *why* it is not showing a number instead of rendering a blank cell
    that reads as a bug.
    """

    model_config = ConfigDict(frozen=True)

    count: int
    of: int
    rate: float | None
    withheld_because: str | None = None

    @classmethod
    def build(cls, count: int, of: int) -> Measure:
        if of < MIN_FOR_A_RATE:
            return cls(
                count=count,
                of=of,
                rate=None,
                withheld_because=f"only {of} observation(s); need {MIN_FOR_A_RATE}",
            )
        return cls(count=count, of=of, rate=round(count / of, 3))


class TagCount(BaseModel):
    model_config = ConfigDict(frozen=True)

    label: str
    count: int
    students_affected: int = 1


class StudentSummary(BaseModel):
    """One child's row.

    `recurring` is the field this surface exists for: it is the one thing a
    teacher cannot get from the queue, because it is a fact about the child across
    sessions rather than about any single session.
    """

    model_config = ConfigDict(frozen=True)

    student_id: uuid.UUID
    name: str
    grade_level: int

    sessions_answered: int
    sessions_abandoned: int
    attempts: int

    correct: Measure
    """Share of *graded* attempts marked correct. Attempts with no `GradeResult`
    are excluded from the denominator rather than counted as wrong — an ungraded
    attempt is a gap in the record, not a child's mistake."""

    diagnosed: tuple[TagCount, ...]
    recurring: tuple[str, ...]
    awaiting_review: int
    last_seen: dt.datetime | None


class ClassSummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    students: tuple[StudentSummary, ...]

    sessions_answered: int
    sessions_abandoned: int
    attempts: int
    awaiting_review: int

    class_misconceptions: tuple[TagCount, ...]
    """Ranked across the class, with how many distinct children each affects.

    The second number is what makes this actionable rather than trivia: one tag on
    six children is a lesson to reteach, and the same count on one child is a
    conversation with that child.
    """

    abstained: Measure
    """How often the diagnoser had no opinion.

    Reported because it is a fact about the *system*, not about the class, and a
    teacher reading misconception counts deserves to know what share of attempts
    produced no diagnosis at all. Without it the tag counts read as a complete
    account of the class's errors when they are a partial one.
    """

    generated_at: dt.datetime


def _visible_students(db: DbSession, scope: Scope) -> list[uuid.UUID]:
    """The students this principal may see, resolved before anything is counted.

    An admin is unscoped, so `scope.student_ids` is empty for them by design and
    filtering on it would produce a confidently empty dashboard. `can_read_student`
    is the same predicate the per-row checks use, applied here once so the
    aggregate below cannot disagree with it.
    """
    all_ids = db.execute(select(StudentRow.id)).scalars().all()
    return [sid for sid in all_ids if can_read_student(scope, sid)]


def _names(db: DbSession, student_ids: list[uuid.UUID]) -> dict[uuid.UUID, str]:
    """Display names come from `Principal`, which is where a person's name lives —
    `Student` is the learner record and deliberately holds no identifying text."""
    rows = db.execute(
        select(PrincipalRow.student_id, PrincipalRow.display_name).where(
            PrincipalRow.student_id.in_(student_ids)
        )
    ).all()
    return {sid: name for sid, name in rows if sid is not None}


def build_class_summary(db: DbSession, scope: Scope) -> ClassSummary:
    """Aggregate one teacher's class from the append-only record.

    Bulk-read then group in Python rather than one aggregate query per student: a
    classroom is tens of children and hundreds of attempts, so the readable version
    costs nothing measurable, and it keeps the scoping filter in one place instead
    of repeated into every `WHERE`.
    """
    visible = _visible_students(db, scope)
    now = dt.datetime.now(dt.UTC)
    if not visible:
        return ClassSummary(
            students=(),
            sessions_answered=0,
            sessions_abandoned=0,
            attempts=0,
            awaiting_review=0,
            class_misconceptions=(),
            abstained=Measure.build(0, 0),
            generated_at=now,
        )

    names = _names(db, visible)
    grades_by_student = {
        row.id: row.grade_level
        for row in db.execute(select(StudentRow).where(StudentRow.id.in_(visible))).scalars().all()
    }

    sessions = (
        db.execute(select(SessionRow).where(SessionRow.student_id.in_(visible))).scalars().all()
    )
    session_ids = [s.id for s in sessions]
    student_of_session = {s.id: s.student_id for s in sessions}

    attempts = (
        db.execute(select(AttemptRow).where(AttemptRow.session_id.in_(session_ids))).scalars().all()
        if session_ids
        else []
    )
    attempt_ids = [a.id for a in attempts]

    scores = (
        {
            row.attempt_id: row.score
            for row in db.execute(
                select(GradeResultRow).where(GradeResultRow.attempt_id.in_(attempt_ids))
            )
            .scalars()
            .all()
        }
        if attempt_ids
        else {}
    )

    # Left join: an abstention has a row and no tag, and it has to stay in the
    # denominator or the abstention rate below is unmeasurable.
    diagnoses = (
        db.execute(
            select(DiagnosisLogRow.attempt_id, MisconceptionTagRow.label)
            .outerjoin(
                MisconceptionTagRow, MisconceptionTagRow.id == DiagnosisLogRow.misconception_tag_id
            )
            .where(DiagnosisLogRow.attempt_id.in_(attempt_ids))
        ).all()
        if attempt_ids
        else []
    )

    open_items = (
        db.execute(
            select(ReviewItemRow.session_id).where(
                ReviewItemRow.session_id.in_(session_ids),
                ReviewItemRow.resolved_at.is_(None),
            )
        )
        .scalars()
        .all()
        if session_ids
        else []
    )

    # --- group everything by student ---------------------------------------
    attempts_by_student: dict[uuid.UUID, list[AttemptRow]] = defaultdict(list)
    for attempt in attempts:
        owner = student_of_session.get(attempt.session_id)
        if owner is not None:
            attempts_by_student[owner].append(attempt)

    answered_sessions = {a.session_id for a in attempts}
    tags_by_student: dict[uuid.UUID, Counter[str]] = defaultdict(Counter)
    student_of_attempt = {a.id: student_of_session.get(a.session_id) for a in attempts}
    abstentions = 0
    for attempt_id, label in diagnoses:
        if label is None or label == UNKNOWN_TAG_LABEL:
            abstentions += 1
            continue
        owner = student_of_attempt.get(attempt_id)
        if owner is not None:
            tags_by_student[owner][label] += 1

    review_by_student: Counter[uuid.UUID] = Counter()
    for sid in open_items:
        owner = student_of_session.get(sid)
        if owner is not None:
            review_by_student[owner] += 1

    summaries: list[StudentSummary] = []
    for student_id in visible:
        mine = attempts_by_student.get(student_id, [])
        my_sessions = [s for s in sessions if s.student_id == student_id]
        graded = [scores[a.id] for a in mine if a.id in scores]
        tags = tags_by_student.get(student_id, Counter())
        summaries.append(
            StudentSummary(
                student_id=student_id,
                name=names.get(student_id, "(unnamed)"),
                grade_level=grades_by_student.get(student_id, 0),
                sessions_answered=sum(1 for s in my_sessions if s.id in answered_sessions),
                sessions_abandoned=sum(1 for s in my_sessions if s.id not in answered_sessions),
                attempts=len(mine),
                correct=Measure.build(sum(1 for s in graded if s >= 1.0), len(graded)),
                diagnosed=tuple(
                    TagCount(label=label, count=count) for label, count in tags.most_common()
                ),
                recurring=tuple(
                    label for label, count in tags.items() if count >= RECURRENCE_THRESHOLD
                ),
                awaiting_review=review_by_student.get(student_id, 0),
                last_seen=max((a.timestamp for a in mine), default=None),
            )
        )

    # Ranked by who most needs looking at, not alphabetically: a queue-shaped
    # surface should put the row that changes a teacher's next action on top.
    summaries.sort(key=lambda s: (-len(s.recurring), -s.awaiting_review, s.name))

    class_tags: Counter[str] = Counter()
    affected: dict[str, set[uuid.UUID]] = defaultdict(set)
    for student_id, counter in tags_by_student.items():
        for label, count in counter.items():
            class_tags[label] += count
            affected[label].add(student_id)

    return ClassSummary(
        students=tuple(summaries),
        sessions_answered=len(answered_sessions),
        sessions_abandoned=len(sessions) - len(answered_sessions),
        attempts=len(attempts),
        awaiting_review=len(open_items),
        class_misconceptions=tuple(
            TagCount(label=label, count=count, students_affected=len(affected[label]))
            for label, count in class_tags.most_common()
        ),
        abstained=Measure.build(abstentions, len(diagnoses)),
        generated_at=now,
    )


@router.get("/class-summary", response_model=ClassSummary)
def class_summary(
    scope: Scope = Depends(current_scope),
    db: DbSession = Depends(get_db),
) -> ClassSummary:
    """The dashboard's data (§3.6, §8).

    Gated on `can_read_review_queue` — the same predicate as the queue, because
    this is the same audience and the same category of content. A student must not
    reach it: the page names other children and what each of them got wrong.
    """
    try:
        require(can_read_review_queue(scope), "read the class summary")
    except AuthorizationError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    return build_class_summary(db, scope)
