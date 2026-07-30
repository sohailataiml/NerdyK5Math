"""The class overview (§3.6, §8, M0.9).

Two kinds of test here, and the first kind is the one that matters most.

**Tenancy.** Every other teacher read is scoped per row, and an aggregate is where
that discipline is easiest to lose: a class average is a single number, so a leak
does not look like someone else's data appearing on screen — it looks like a
slightly different percentage, which nobody can spot by eye. So the load-bearing
test is `test_another_teachers_child_is_not_in_the_aggregate`, and it asserts on
the counts rather than only on the visible names.

**Honesty about small numbers.** The dashboard withholds a rate below
`MIN_FOR_A_RATE` and excludes sessions that were opened and never answered. Both
are deliberate and both are the kind of thing a later change would quietly undo to
make the page look tidier, which is exactly why they are pinned here.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session as DbSession
from sqlalchemy.pool import StaticPool

from packages.domain import models as m
from packages.domain import tables as t
from packages.domain.enums import (
    UNKNOWN_TAG_LABEL,
    DiagnosisSource,
    GradeMethod,
    Operation,
    ReviewReason,
    Role,
)
from packages.domain.mapping import to_row
from services.api.app import app
from services.api.auth import PRINCIPAL_HEADER
from services.api.db import get_db
from services.api.teacher_dashboard import MIN_FOR_A_RATE, RECURRENCE_THRESHOLD

NOW = dt.datetime(2026, 7, 29, 12, 0, 0, tzinfo=dt.UTC)
SUBTRACTED = "subtracted_instead_of_added"
COUNTED_ON = "counted_on_from_wrong_start"


@pytest.fixture
def db() -> Iterator[DbSession]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    t.Base.metadata.create_all(engine)
    with DbSession(engine) as session:
        yield session
    engine.dispose()


@pytest.fixture
def client(db: DbSession) -> Iterator[TestClient]:
    app.dependency_overrides[get_db] = lambda: db
    yield TestClient(app)
    app.dependency_overrides.clear()


class School:
    """Two classes, so tenancy is testable at all.

    A single-class fixture cannot distinguish "scoped correctly" from "returns
    everything", which is the failure this surface is most exposed to.
    """

    def __init__(self, db: DbSession) -> None:
        self.db = db
        self.tags = {
            label: m.MisconceptionTag(
                label=label, operation_type=Operation.ADDITION, description=label
            )
            for label in (SUBTRACTED, COUNTED_ON)
        }
        for tag in self.tags.values():
            db.add(to_row(tag, t.MisconceptionTagRow))

        node = m.CurriculumNode(
            standard_code="1.OA.C.6", grade_band="K-1", definition="Add within 20."
        )
        db.add(to_row(node, t.CurriculumNodeRow))
        db.flush()
        self.problem = m.Problem(
            curriculum_node_id=node.id,
            prompt="What is 7 + 5?",
            correct_answer="12",
            answer_type="numeric",
            grade_band="K-1",
        )
        db.add(to_row(self.problem, t.ProblemRow))

        self.teacher = m.Principal(role=Role.TEACHER, display_name="Ms Rivera", created_at=NOW)
        self.other_teacher = m.Principal(role=Role.TEACHER, display_name="Mr Osei", created_at=NOW)
        self.admin = m.Principal(role=Role.ADMIN, display_name="Operator", created_at=NOW)
        for p in (self.teacher, self.other_teacher, self.admin):
            db.add(to_row(p, t.PrincipalRow))
        db.flush()

        self.room = m.Classroom(teacher_id=self.teacher.id, name="Pilot A", created_at=NOW)
        self.other_room = m.Classroom(
            teacher_id=self.other_teacher.id, name="Pilot B", created_at=NOW
        )
        for room in (self.room, self.other_room):
            db.add(to_row(room, t.ClassroomRow))
        db.flush()
        db.commit()

    def enrol(self, name: str, *, room: m.Classroom | None = None) -> m.Student:
        student = m.Student(grade_level=1, created_at=NOW)
        self.db.add(to_row(student, t.StudentRow))
        self.db.flush()
        self.db.add(
            to_row(
                m.Principal(
                    role=Role.STUDENT,
                    display_name=name,
                    student_id=student.id,
                    created_at=NOW,
                ),
                t.PrincipalRow,
            )
        )
        self.db.add(
            to_row(
                m.Enrollment(
                    classroom_id=(room or self.room).id,
                    student_id=student.id,
                    created_at=NOW,
                ),
                t.EnrollmentRow,
            )
        )
        self.db.commit()
        return student

    def session_for(self, student: m.Student) -> m.Session:
        session = m.Session(
            student_id=student.id, problem_id=self.problem.id, started_at=NOW, attempt_count=0
        )
        self.db.add(to_row(session, t.SessionRow))
        self.db.commit()
        return session

    def attempt(
        self,
        session: m.Session,
        *,
        answer: str = "2",
        score: float | None = None,
        tag: str | None = None,
        at: dt.datetime | None = None,
    ) -> m.Attempt:
        """One attempt, optionally graded and optionally diagnosed.

        `score=None` leaves the attempt ungraded on purpose — the record has such
        rows, and they must not be counted as wrong answers.
        """
        attempt = m.Attempt(
            session_id=session.id,
            student_answer=answer,
            timestamp=at or NOW,
            hint_level_shown=0,
        )
        self.db.add(to_row(attempt, t.AttemptRow))
        self.db.flush()
        if score is not None:
            self.db.add(
                to_row(
                    m.GradeResult(
                        attempt_id=attempt.id,
                        score=score,
                        confidence=1.0,
                        method=GradeMethod.SYMBOLIC,
                        symbolic_agreed=True,
                    ),
                    t.GradeResultRow,
                )
            )
        if tag is not None:
            self.db.add(
                to_row(
                    m.DiagnosisLog(
                        attempt_id=attempt.id,
                        misconception_tag_id=(
                            None if tag == UNKNOWN_TAG_LABEL else self.tags[tag].id
                        ),
                        confidence=0.99,
                        evidence="fixture",
                        source=DiagnosisSource.RULE,
                    ),
                    t.DiagnosisLogRow,
                )
            )
        self.db.commit()
        return attempt

    def queue(self, session: m.Session) -> None:
        self.db.add(
            to_row(
                m.ReviewItem(
                    session_id=session.id, reason=ReviewReason.AUDIT_SAMPLE, created_at=NOW
                ),
                t.ReviewItemRow,
            )
        )
        self.db.commit()

    def headers(self, principal: m.Principal) -> dict[str, str]:
        return {PRINCIPAL_HEADER: str(principal.id)}


@pytest.fixture
def school(db: DbSession) -> School:
    return School(db)


def _summary(client: TestClient, school: School, principal: m.Principal) -> dict:
    res = client.get("/teacher/class-summary", headers=school.headers(principal))
    assert res.status_code == 200, res.text
    return res.json()


class TestOnlyYourOwnClass:
    def test_another_teachers_child_is_not_in_the_aggregate(
        self, client: TestClient, school: School
    ) -> None:
        """The failure this guards against does not look like a leak on screen.

        Someone else's child contaminating a class average shows up as a slightly
        different number, which no one notices by eye — so this asserts on the
        counts, not only on the names.
        """
        mine = school.enrol("Ada")
        theirs = school.enrol("Bruno", room=school.other_room)
        school.attempt(school.session_for(mine), score=1.0, tag=SUBTRACTED)
        for _ in range(4):
            school.attempt(school.session_for(theirs), score=0.0, tag=COUNTED_ON)

        data = _summary(client, school, school.teacher)

        assert [s["name"] for s in data["students"]] == ["Ada"]
        assert data["attempts"] == 1
        assert data["sessions_answered"] == 1
        # The other class's misconception must not appear at all.
        assert [t["label"] for t in data["class_misconceptions"]] == [SUBTRACTED]

    def test_a_student_cannot_read_it(self, client: TestClient, school: School) -> None:
        """The page names other children and what each of them got wrong."""
        student = school.enrol("Ada")
        child = (
            school.db.query(t.PrincipalRow).filter(t.PrincipalRow.student_id == student.id).one()
        )
        res = client.get("/teacher/class-summary", headers={PRINCIPAL_HEADER: str(child.id)})
        assert res.status_code == 403

    def test_an_admin_is_unscoped(self, client: TestClient, school: School) -> None:
        """`Scope.student_ids` is empty for an admin by design, so a dashboard that
        filtered on it would show an operator a confidently empty class."""
        school.enrol("Ada")
        school.enrol("Bruno", room=school.other_room)

        data = _summary(client, school, school.admin)

        assert {s["name"] for s in data["students"]} == {"Ada", "Bruno"}


class TestItRefusesToReportWhatItCannotSupport:
    def test_a_rate_below_the_floor_is_withheld_with_a_reason(
        self, client: TestClient, school: School
    ) -> None:
        """`1 of 1 = 100%` beside a child's name invites acting on noise. The
        reason travels with the withheld rate so the page can say why rather than
        render a blank that reads as a bug."""
        student = school.enrol("Ada")
        session = school.session_for(student)
        school.attempt(session, score=1.0)
        school.attempt(session, score=0.0)

        row = _summary(client, school, school.teacher)["students"][0]

        assert row["correct"]["rate"] is None
        assert row["correct"]["count"] == 1
        assert row["correct"]["of"] == 2
        assert str(MIN_FOR_A_RATE) in row["correct"]["withheld_because"]

    def test_a_rate_at_the_floor_is_reported(self, client: TestClient, school: School) -> None:
        student = school.enrol("Ada")
        session = school.session_for(student)
        for i in range(MIN_FOR_A_RATE):
            school.attempt(session, score=1.0 if i < 4 else 0.0)

        row = _summary(client, school, school.teacher)["students"][0]

        assert row["correct"]["of"] == MIN_FOR_A_RATE
        assert row["correct"]["rate"] == round(4 / MIN_FOR_A_RATE, 3)

    def test_an_ungraded_attempt_is_not_counted_as_wrong(
        self, client: TestClient, school: School
    ) -> None:
        """A missing `GradeResult` is a gap in the record, not a child's mistake."""
        student = school.enrol("Ada")
        session = school.session_for(student)
        for _ in range(MIN_FOR_A_RATE):
            school.attempt(session, score=1.0)
        school.attempt(session, score=None)  # ungraded

        row = _summary(client, school, school.teacher)["students"][0]

        assert row["attempts"] == MIN_FOR_A_RATE + 1
        assert row["correct"]["of"] == MIN_FOR_A_RATE
        assert row["correct"]["rate"] == 1.0

    def test_a_session_never_answered_is_excluded_and_counted_separately(
        self, client: TestClient, school: School
    ) -> None:
        """Findings.md measured this against the pilot: 57 of 97 sessions were
        opened and never answered. Folding them into a per-session rate reports a
        child who walked away as a child who got it wrong."""
        student = school.enrol("Ada")
        answered = school.session_for(student)
        school.attempt(answered, score=1.0)
        school.session_for(student)  # opened, never answered

        data = _summary(client, school, school.teacher)

        assert data["sessions_answered"] == 1
        assert data["sessions_abandoned"] == 1
        assert data["students"][0]["sessions_answered"] == 1
        assert data["students"][0]["sessions_abandoned"] == 1


class TestWhatTheQueueCannotShow:
    def test_a_repeated_misconception_is_flagged_as_recurring(
        self, client: TestClient, school: School
    ) -> None:
        """The reason this surface exists. A teacher working the queue sees one
        session at a time and cannot see the fourth repeat of the same tag."""
        student = school.enrol("Ada")
        for _ in range(RECURRENCE_THRESHOLD):
            school.attempt(school.session_for(student), score=0.0, tag=SUBTRACTED)

        row = _summary(client, school, school.teacher)["students"][0]

        assert row["recurring"] == [SUBTRACTED]
        assert row["diagnosed"][0] == {
            "label": SUBTRACTED,
            "count": RECURRENCE_THRESHOLD,
            "students_affected": 1,
        }

    def test_one_repeat_short_is_not_yet_recurring(
        self, client: TestClient, school: School
    ) -> None:
        """§3.1's diagnoser is uncalibrated until P1.3, so a couple of hits is a
        hypothesis. Flagging it would make the badge meaningless."""
        student = school.enrol("Ada")
        for _ in range(RECURRENCE_THRESHOLD - 1):
            school.attempt(school.session_for(student), score=0.0, tag=SUBTRACTED)

        row = _summary(client, school, school.teacher)["students"][0]

        assert row["recurring"] == []

    def test_a_class_wide_tag_reports_how_many_children_it_affects(
        self, client: TestClient, school: School
    ) -> None:
        """One tag on six children is a lesson to reteach; the same count on one
        child is a conversation with that child. The count alone cannot tell them
        apart."""
        ada = school.enrol("Ada")
        ben = school.enrol("Ben")
        school.attempt(school.session_for(ada), score=0.0, tag=SUBTRACTED)
        school.attempt(school.session_for(ada), score=0.0, tag=SUBTRACTED)
        school.attempt(school.session_for(ben), score=0.0, tag=SUBTRACTED)

        tags = _summary(client, school, school.teacher)["class_misconceptions"]

        assert tags[0]["label"] == SUBTRACTED
        assert tags[0]["count"] == 3
        assert tags[0]["students_affected"] == 2

    def test_the_child_who_needs_looking_at_sorts_first(
        self, client: TestClient, school: School
    ) -> None:
        """A queue-shaped surface should put the row that changes a teacher's next
        action on top, not the alphabetically first name."""
        zoe = school.enrol("Zoe")
        school.enrol("Ada")
        for _ in range(RECURRENCE_THRESHOLD):
            school.attempt(school.session_for(zoe), score=0.0, tag=SUBTRACTED)

        names = [s["name"] for s in _summary(client, school, school.teacher)["students"]]

        assert names[0] == "Zoe"

    def test_open_review_items_are_counted_per_child(
        self, client: TestClient, school: School
    ) -> None:
        student = school.enrol("Ada")
        open_session = school.session_for(student)
        school.attempt(open_session, score=0.0)
        school.queue(open_session)

        data = _summary(client, school, school.teacher)

        assert data["awaiting_review"] == 1
        assert data["students"][0]["awaiting_review"] == 1


class TestTheAbstentionIsNotHidden:
    def test_an_abstention_counts_against_the_diagnosis_total_not_as_a_tag(
        self, client: TestClient, school: School
    ) -> None:
        """Without this the tag counts read as a complete account of the class's
        errors when they are a partial one. §8 wants the `unknown` rate visible,
        and this page is where a teacher would otherwise be misled by its absence.
        """
        student = school.enrol("Ada")
        school.attempt(school.session_for(student), score=0.0, tag=SUBTRACTED)
        for _ in range(3):
            school.attempt(school.session_for(student), score=0.0, tag=UNKNOWN_TAG_LABEL)

        data = _summary(client, school, school.teacher)

        assert data["abstained"]["count"] == 3
        assert data["abstained"]["of"] == 4
        # The abstentions did not become a misconception.
        assert [t["label"] for t in data["class_misconceptions"]] == [SUBTRACTED]
        assert data["students"][0]["diagnosed"] == [
            {"label": SUBTRACTED, "count": 1, "students_affected": 1}
        ]


class TestTheEmptyCases:
    def test_a_class_with_no_students_is_not_an_error(
        self, client: TestClient, school: School
    ) -> None:
        """A teacher opening this before the pilot starts should read zero, not a
        stack trace — and not a division by it either."""
        data = _summary(client, school, school.teacher)

        assert data["students"] == []
        assert data["attempts"] == 0
        assert data["abstained"]["rate"] is None
        assert data["class_misconceptions"] == []

    def test_a_child_who_has_not_started_reads_as_never_answered(
        self, client: TestClient, school: School
    ) -> None:
        school.enrol("Ada")

        row = _summary(client, school, school.teacher)["students"][0]

        assert row["attempts"] == 0
        assert row["last_seen"] is None
        assert row["correct"]["of"] == 0


class TestThePageIsReachable:
    def test_the_dashboard_is_served(self, client: TestClient) -> None:
        res = client.get("/teacher/dashboard")
        assert res.status_code == 200
        assert "Class overview" in res.text

    def test_the_review_queue_links_to_it(self, client: TestClient) -> None:
        """Two surfaces a teacher cannot navigate between are one surface and a
        dead end. The principal travels in the link because re-pasting a uuid is
        how a teacher stops using the second page."""
        res = client.get("/teacher/review")
        assert "/teacher/dashboard" in res.text
        assert res.status_code == 200


def test_the_summary_does_not_query_per_student(school: School, db: DbSession) -> None:
    """An N+1 here is a dashboard that gets slower with every child enrolled.

    Asserted as a bound on statements rather than a benchmark: the aggregation is
    deliberately bulk-read-then-group-in-Python, and a future change that moves a
    query inside the per-student loop should fail this rather than quietly ship.
    """
    from sqlalchemy import event

    from packages.auth import resolve_scope
    from packages.domain.mapping import from_row
    from services.api.teacher_dashboard import build_class_summary

    for name in ("Ada", "Ben", "Cleo", "Dev"):
        student = school.enrol(name)
        school.attempt(school.session_for(student), score=1.0, tag=SUBTRACTED)

    teacher = from_row(
        db.query(t.PrincipalRow).filter(t.PrincipalRow.id == school.teacher.id).one(),
        m.Principal,
    )
    scope = resolve_scope(db, teacher)

    statements: list[str] = []

    @event.listens_for(db.get_bind(), "before_cursor_execute")
    def count(conn, cursor, statement, *args):  # type: ignore[no-untyped-def]
        statements.append(statement)

    build_class_summary(db, scope)

    assert len(statements) <= 10, f"{len(statements)} queries for 4 students"
