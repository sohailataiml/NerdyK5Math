"""P0.8 — the teacher console.

Weighted toward authorization, for the same reason M0.9's suite was: this is the
first surface where one adult can reach another adult's students, and the thing
being protected is children's schoolwork. A suite that only proves the happy path
passes just as happily against an endpoint that returns everyone.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session as DbSession
from sqlalchemy.pool import StaticPool

from packages.domain import models as m
from packages.domain import tables as t
from packages.domain.enums import ReviewReason, Role
from packages.domain.mapping import to_row
from services.api.app import app
from services.api.auth import PRINCIPAL_HEADER
from services.api.db import get_db

NOW = dt.datetime(2026, 7, 27, tzinfo=dt.UTC)


@pytest.fixture
def db() -> Iterator[DbSession]:
    """In-memory SQLite shared across threads.

    `TestClient` runs the app in its own thread, and SQLite refuses to reuse a
    connection across threads by default — so the test and the request handler
    would otherwise see different databases (or crash). `StaticPool` plus
    `check_same_thread=False` gives both sides the one connection holding the
    in-memory schema.
    """
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
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


class World:
    """Two teachers, one student each, one shadow candidate each."""

    def __init__(self, db: DbSession) -> None:
        self.db = db
        self.teacher_a = self._teacher("Ms Rivera")
        self.teacher_b = self._teacher("Mr Osei")
        self.student_a = self._student()
        self.student_b = self._student()
        self._enrol(self.teacher_a, self.student_a)
        self._enrol(self.teacher_b, self.student_b)
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
        db.flush()
        self.session_a = self._session(self.student_a)
        self.session_b = self._session(self.student_b)
        self.candidate_a = self._candidate(self.session_a)
        self.candidate_b = self._candidate(self.session_b)
        db.commit()

    def _teacher(self, name: str) -> m.Principal:
        principal = m.Principal(role=Role.TEACHER, display_name=name, created_at=NOW)
        self.db.add(to_row(principal, t.PrincipalRow))
        self.db.flush()
        return principal

    def _student(self) -> uuid.UUID:
        student = m.Student(grade_level=1, created_at=NOW)
        self.db.add(to_row(student, t.StudentRow))
        self.db.flush()
        return student.id

    def _enrol(self, teacher: m.Principal, student_id: uuid.UUID) -> None:
        classroom = m.Classroom(teacher_id=teacher.id, name="Room", created_at=NOW)
        self.db.add(to_row(classroom, t.ClassroomRow))
        self.db.flush()
        self.db.add(
            to_row(
                m.Enrollment(classroom_id=classroom.id, student_id=student_id, created_at=NOW),
                t.EnrollmentRow,
            )
        )
        self.db.flush()

    def _session(self, student_id: uuid.UUID) -> uuid.UUID:
        session = m.Session(
            student_id=student_id, problem_id=self.problem.id, started_at=NOW, attempt_count=1
        )
        self.db.add(to_row(session, t.SessionRow))
        self.db.flush()
        self.db.add(
            to_row(
                m.Attempt(
                    session_id=session.id, student_answer="2", timestamp=NOW, hint_level_shown=1
                ),
                t.AttemptRow,
            )
        )
        self.db.flush()
        return session.id

    def _candidate(self, session_id: uuid.UUID) -> uuid.UUID:
        candidate = m.ShadowCandidate(
            session_id=session_id,
            attempt_number=1,
            hint_level=1,
            generated_text="Fill your ten-frame with 7. How many more to make ten?",
            shown_text="You have 7 counters and are getting 5 more. More, or fewer?",
            misconception_tag="subtracted_instead_of_added",
            prompt_version="generate_hint/K-1/v1",
            leak_check_passed=True,
            leak_checker_version="deterministic/v1",
            created_at=NOW,
        )
        self.db.add(to_row(candidate, t.ShadowCandidateRow))
        self.db.flush()
        return candidate.id


@pytest.fixture
def world(db: DbSession) -> World:
    return World(db)


def _as(principal: m.Principal) -> dict[str, str]:
    return {PRINCIPAL_HEADER: str(principal.id)}


class TestAuthentication:
    def test_missing_header_is_rejected(self, client: TestClient) -> None:
        assert client.get("/teacher/shadow-queue").status_code == 401

    def test_unknown_principal_is_rejected(self, client: TestClient, world: World) -> None:
        response = client.get(
            "/teacher/shadow-queue", headers={PRINCIPAL_HEADER: str(uuid.uuid4())}
        )
        assert response.status_code == 401

    def test_malformed_and_unknown_ids_are_indistinguishable(
        self, client: TestClient, world: World
    ) -> None:
        """A different message would let a caller enumerate real principal ids."""
        bad = client.get("/teacher/shadow-queue", headers={PRINCIPAL_HEADER: "not-a-uuid"})
        missing = client.get("/teacher/shadow-queue", headers={PRINCIPAL_HEADER: str(uuid.uuid4())})
        assert bad.status_code == missing.status_code == 401
        assert bad.json() == missing.json()


class TestTenancy:
    """The reason this suite exists."""

    def test_teacher_sees_only_their_own_students_candidates(
        self, client: TestClient, world: World
    ) -> None:
        response = client.get("/teacher/shadow-queue", headers=_as(world.teacher_a))
        assert response.status_code == 200
        ids = {item["id"] for item in response.json()}
        assert str(world.candidate_a) in ids
        assert str(world.candidate_b) not in ids

    def test_teacher_cannot_rate_another_teachers_candidate(
        self, client: TestClient, world: World
    ) -> None:
        response = client.post(
            f"/teacher/shadow/{world.candidate_b}/rating",
            headers=_as(world.teacher_a),
            json={"better_than_shown": True},
        )
        assert response.status_code == 403

    def test_a_student_cannot_open_the_queue(self, client: TestClient, world: World) -> None:
        """§11.1 keeps review invisible to the child."""
        student_principal = m.Principal(
            role=Role.STUDENT,
            display_name="sam",
            student_id=world.student_a,
            created_at=NOW,
        )
        world.db.add(to_row(student_principal, t.PrincipalRow))
        world.db.commit()

        response = client.get("/teacher/shadow-queue", headers=_as(student_principal))
        assert response.status_code == 403


class TestRating:
    def test_rating_is_recorded_and_leaves_the_queue(
        self, client: TestClient, world: World
    ) -> None:
        created = client.post(
            f"/teacher/shadow/{world.candidate_a}/rating",
            headers=_as(world.teacher_a),
            json={"better_than_shown": True, "notes": "Clearer for this child."},
        )
        assert created.status_code == 201

        remaining = client.get("/teacher/shadow-queue", headers=_as(world.teacher_a)).json()
        assert str(world.candidate_a) not in {i["id"] for i in remaining}

    def test_a_corrected_tag_is_captured(self, client: TestClient, world: World) -> None:
        """The label P0.9's calibration measurement is built from. Losing it would
        cost the phase its most valuable signal."""
        client.post(
            f"/teacher/shadow/{world.candidate_a}/rating",
            headers=_as(world.teacher_a),
            json={"better_than_shown": False, "corrected_tag": "counted_on_from_wrong_start"},
        )
        rating = world.db.query(t.ShadowRatingRow).one()
        assert "counted_on_from_wrong_start" in (rating.notes or "")

    def test_a_leak_flag_is_recorded(self, client: TestClient, world: World) -> None:
        """P0.5 seeds its corpus from anything a teacher flags as leaky."""
        client.post(
            f"/teacher/shadow/{world.candidate_a}/rating",
            headers=_as(world.teacher_a),
            json={"better_than_shown": False, "would_leak": True},
        )
        assert world.db.query(t.ShadowRatingRow).one().would_leak is True

    def test_ratings_accumulate_rather_than_overwrite(
        self, client: TestClient, world: World
    ) -> None:
        """A rating is evidence; a second opinion is a second row."""
        for better in (True, False):
            client.post(
                f"/teacher/shadow/{world.candidate_a}/rating",
                headers=_as(world.teacher_a),
                json={"better_than_shown": better},
            )
        assert world.db.query(t.ShadowRatingRow).count() == 2

    def test_unknown_candidate_is_404(self, client: TestClient, world: World) -> None:
        response = client.post(
            f"/teacher/shadow/{uuid.uuid4()}/rating",
            headers=_as(world.teacher_a),
            json={"better_than_shown": True},
        )
        assert response.status_code == 404

    def test_unknown_field_is_rejected(self, client: TestClient, world: World) -> None:
        response = client.post(
            f"/teacher/shadow/{world.candidate_a}/rating",
            headers=_as(world.teacher_a),
            json={"better_than_shown": True, "sneaky": 1},
        )
        assert response.status_code == 422


class TestReviewQueue:
    def _item(self, world: World, session_id: uuid.UUID) -> uuid.UUID:
        item = m.ReviewItem(
            session_id=session_id, reason=ReviewReason.LOW_CONFIDENCE, created_at=NOW
        )
        world.db.add(to_row(item, t.ReviewItemRow))
        world.db.commit()
        return item.id

    def test_queue_is_scoped(self, client: TestClient, world: World) -> None:
        mine = self._item(world, world.session_a)
        theirs = self._item(world, world.session_b)

        ids = {
            i["id"]
            for i in client.get("/teacher/review-queue", headers=_as(world.teacher_a)).json()
        }
        assert str(mine) in ids
        assert str(theirs) not in ids

    def test_verdict_is_appended_and_item_resolves(self, client: TestClient, world: World) -> None:
        item_id = self._item(world, world.session_a)
        response = client.post(
            f"/teacher/review/{item_id}",
            headers=_as(world.teacher_a),
            json={"verdict": "confirmed", "notes": "Looks right."},
        )
        assert response.status_code == 201
        assert world.db.query(t.ReviewVerdictRow).count() == 1
        item = world.db.get(t.ReviewItemRow, item_id)
        assert item is not None
        assert item.resolved_at is not None

    def test_cannot_resolve_another_teachers_item(self, client: TestClient, world: World) -> None:
        item_id = self._item(world, world.session_b)
        response = client.post(
            f"/teacher/review/{item_id}",
            headers=_as(world.teacher_a),
            json={"verdict": "confirmed"},
        )
        assert response.status_code == 403

    def test_invalid_verdict_is_rejected(self, client: TestClient, world: World) -> None:
        item_id = self._item(world, world.session_a)
        response = client.post(
            f"/teacher/review/{item_id}",
            headers=_as(world.teacher_a),
            json={"verdict": "looks_fine_to_me"},
        )
        assert response.status_code == 422


class TestPages:
    def test_health(self, client: TestClient) -> None:
        assert client.get("/health").json() == {"status": "ok"}

    def test_rating_page_renders(self, client: TestClient) -> None:
        response = client.get("/teacher/rate")
        assert response.status_code == 200
        # The comparison is the point of the page.
        assert "Shown to the child" in response.text
        assert "Generated, not shown" in response.text
