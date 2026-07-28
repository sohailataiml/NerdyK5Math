"""M0.9 — authorization, weighted toward the negatives.

The milestone's done-criterion is stated as a negative: "teacher A cannot read
teacher B's queue, students, or audit trail." That is the right emphasis. A test
suite that only proves the allowed paths work will pass just as happily against a
policy that allows everything, and the thing being protected here is children's
schoolwork.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy.orm import Session as DbSession

from packages.auth import (
    AuthorizationError,
    Scope,
    can_author_curriculum,
    can_publish_prompts,
    can_read_audit_trail,
    can_read_review_queue,
    can_read_session,
    can_read_student,
    can_resolve_review_item,
    require,
    resolve_scope,
)
from packages.domain import models as m
from packages.domain import tables as t
from packages.domain.enums import Role
from packages.domain.mapping import to_row

NOW = dt.datetime(2026, 7, 27, tzinfo=dt.UTC)


def _principal(role: Role, student_id: uuid.UUID | None = None) -> m.Principal:
    return m.Principal(
        role=role, display_name=f"{role.value}-1", student_id=student_id, created_at=NOW
    )


def _scope(role: Role, students: set[uuid.UUID] | None = None) -> Scope:
    student_id = next(iter(students)) if role is Role.STUDENT and students else None
    return Scope(
        principal=_principal(role, student_id),
        student_ids=frozenset(students or set()),
    )


class TestPrincipalShape:
    def test_student_principal_must_reference_a_student(self) -> None:
        """A student login with no student record reads as "scoped to nothing" or
        "scoped to everything" depending on how a caller writes the check."""
        with pytest.raises(ValueError, match="must reference a student_id"):
            m.Principal(role=Role.STUDENT, display_name="sam", created_at=NOW)

    def test_teacher_principal_must_not_reference_a_student(self) -> None:
        with pytest.raises(ValueError, match="must not reference"):
            m.Principal(
                role=Role.TEACHER,
                display_name="ms-rivera",
                student_id=uuid.uuid4(),
                created_at=NOW,
            )


class TestTeacherIsolation:
    """M0.9's headline requirement: teacher A cannot reach teacher B's class."""

    def setup_method(self) -> None:
        self.a_student = uuid.uuid4()
        self.b_student = uuid.uuid4()
        self.teacher_a = _scope(Role.TEACHER, {self.a_student})
        self.teacher_b = _scope(Role.TEACHER, {self.b_student})

    def test_teacher_cannot_read_another_teachers_student(self) -> None:
        assert can_read_student(self.teacher_a, self.b_student) is False
        assert can_read_student(self.teacher_b, self.a_student) is False

    def test_teacher_cannot_read_another_teachers_session(self) -> None:
        assert can_read_session(self.teacher_a, student_id=self.b_student) is False

    def test_teacher_cannot_read_another_teachers_audit_trail(self) -> None:
        """The audit trail holds the child's actual answers."""
        assert can_read_audit_trail(self.teacher_a, student_id=self.b_student) is False

    def test_teacher_cannot_resolve_another_teachers_review_item(self) -> None:
        assert can_resolve_review_item(self.teacher_a, student_id=self.b_student) is False

    def test_teacher_can_reach_their_own(self) -> None:
        assert can_read_student(self.teacher_a, self.a_student) is True
        assert can_read_session(self.teacher_a, student_id=self.a_student) is True
        assert can_read_audit_trail(self.teacher_a, student_id=self.a_student) is True
        assert can_resolve_review_item(self.teacher_a, student_id=self.a_student) is True

    def test_having_the_teacher_role_grants_nothing_by_itself(self) -> None:
        """The check that a `if role is TEACHER` implementation would fail.

        Scope is data, not a role — a teacher with an empty roster reaches no
        student at all.
        """
        rosterless = _scope(Role.TEACHER, set())
        assert can_read_student(rosterless, self.a_student) is False
        assert can_read_audit_trail(rosterless, student_id=self.a_student) is False


class TestStudentIsolation:
    def setup_method(self) -> None:
        self.me = uuid.uuid4()
        self.classmate = uuid.uuid4()
        self.student = _scope(Role.STUDENT, {self.me})

    def test_student_cannot_read_a_classmate(self) -> None:
        assert can_read_student(self.student, self.classmate) is False
        assert can_read_session(self.student, student_id=self.classmate) is False

    def test_student_cannot_open_the_review_queue(self) -> None:
        """§11.1 keeps review invisible to the student — discovering the queue is
        exactly the stigma the design works to avoid."""
        assert can_read_review_queue(self.student) is False

    def test_student_cannot_read_their_own_audit_trail(self) -> None:
        """§6 marks it teacher/admin. It contains diagnosis tags the student is
        never shown (§11.1: never surfaced as a label)."""
        assert can_read_audit_trail(self.student, student_id=self.me) is False

    def test_student_cannot_author_curriculum_or_prompts(self) -> None:
        assert can_author_curriculum(self.student) is False
        assert can_publish_prompts(self.student) is False

    def test_student_can_read_their_own_session(self) -> None:
        assert can_read_session(self.student, student_id=self.me) is True


class TestAdmin:
    def test_admin_is_unscoped(self) -> None:
        admin = _scope(Role.ADMIN)
        assert can_read_student(admin, uuid.uuid4()) is True
        assert can_read_audit_trail(admin, student_id=uuid.uuid4()) is True
        assert can_publish_prompts(admin) is True

    def test_teacher_cannot_publish_prompts(self) -> None:
        """A published prompt governs what every child in the system is told —
        an operator decision, not a classroom one."""
        assert can_publish_prompts(_scope(Role.TEACHER, {uuid.uuid4()})) is False


class TestEnforcement:
    def test_require_raises_on_denial(self) -> None:
        with pytest.raises(AuthorizationError, match="not permitted"):
            require(False, "read session")

    def test_denial_message_reveals_nothing_about_what_exists(self) -> None:
        """A message distinguishing "no such session" from "not yours" tells an
        attacker which session IDs are real."""
        missing = uuid.uuid4()
        existing = uuid.uuid4()
        scope = _scope(Role.TEACHER, {uuid.uuid4()})

        errors = []
        for target in (missing, existing):
            try:
                require(can_read_session(scope, student_id=target), "read session")
            except AuthorizationError as exc:
                errors.append(str(exc))

        assert len(errors) == 2
        assert errors[0] == errors[1]


class TestScopeResolution:
    """The one place that decides which students a principal reaches."""

    def _classroom(self, db: DbSession, teacher: m.Principal, students: list[uuid.UUID]) -> None:
        classroom = m.Classroom(teacher_id=teacher.id, name="Room 1", created_at=NOW)
        db.add(to_row(classroom, t.ClassroomRow))
        db.flush()
        for student_id in students:
            db.add(
                to_row(
                    m.Enrollment(classroom_id=classroom.id, student_id=student_id, created_at=NOW),
                    t.EnrollmentRow,
                )
            )
        db.commit()

    def _student(self, db: DbSession) -> uuid.UUID:
        student = m.Student(grade_level=1, created_at=NOW)
        db.add(to_row(student, t.StudentRow))
        db.flush()
        return student.id

    def test_teacher_scope_is_their_enrolled_students(self, session: DbSession) -> None:
        teacher_a = _principal(Role.TEACHER)
        teacher_b = _principal(Role.TEACHER)
        session.add(to_row(teacher_a, t.PrincipalRow))
        session.add(to_row(teacher_b, t.PrincipalRow))
        session.flush()

        a_students = [self._student(session), self._student(session)]
        b_students = [self._student(session)]
        self._classroom(session, teacher_a, a_students)
        self._classroom(session, teacher_b, b_students)

        scope_a = resolve_scope(session, teacher_a)
        scope_b = resolve_scope(session, teacher_b)

        assert scope_a.student_ids == set(a_students)
        assert scope_b.student_ids == set(b_students)
        # The isolation, end to end through the database.
        assert can_read_student(scope_a, b_students[0]) is False
        assert can_read_student(scope_b, a_students[0]) is False

    def test_teacher_with_no_classroom_reaches_nobody(self, session: DbSession) -> None:
        teacher = _principal(Role.TEACHER)
        session.add(to_row(teacher, t.PrincipalRow))
        session.commit()

        assert resolve_scope(session, teacher).student_ids == frozenset()

    def test_student_scope_is_only_themselves(self, session: DbSession) -> None:
        student_id = self._student(session)
        principal = _principal(Role.STUDENT, student_id)
        session.add(to_row(principal, t.PrincipalRow))
        session.commit()

        assert resolve_scope(session, principal).student_ids == {student_id}

    def test_enrolling_a_student_widens_scope_immediately(self, session: DbSession) -> None:
        """Scope is derived, not cached — a roster change takes effect at once."""
        teacher = _principal(Role.TEACHER)
        session.add(to_row(teacher, t.PrincipalRow))
        session.flush()
        student_id = self._student(session)
        assert resolve_scope(session, teacher).student_ids == frozenset()

        self._classroom(session, teacher, [student_id])
        assert resolve_scope(session, teacher).student_ids == {student_id}
