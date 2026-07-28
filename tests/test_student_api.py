"""P0.11 — the student surface.

The load-bearing test is `test_no_response_ever_contains_the_correct_answer`.
Every other leak guard in this system inspects *hint text*; a JSON field carrying
`correct_answer` would walk past all of them, and a child with the network tab
open is still a child in the pilot.

The rest weight toward the two things that would quietly corrupt Phase 0: one
child reaching another child's session, and a session's record saying something
untrue about what happened in it.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session as DbSession
from sqlalchemy.pool import StaticPool

from packages.domain import models as m
from packages.domain import tables as t
from packages.domain.enums import EventType, Role, SessionState
from packages.domain.mapping import to_row
from services.api.app import app
from services.api.auth import PRINCIPAL_HEADER
from services.api.db import get_db
from services.api.student import _visual_for

NOW = dt.datetime(2026, 7, 28, tzinfo=dt.UTC)
CORRECT = "12"


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


class World:
    """Two children and a teacher, so tenancy is testable rather than assumed."""

    def __init__(self, db: DbSession) -> None:
        self.db = db
        self.child_a, self.principal_a = self._child()
        self.child_b, self.principal_b = self._child()
        self.teacher = m.Principal(role=Role.TEACHER, display_name="Ms Rivera", created_at=NOW)
        db.add(to_row(self.teacher, t.PrincipalRow))

        node = m.CurriculumNode(
            standard_code="1.OA.C.6", grade_band="K-1", definition="Add within 20."
        )
        db.add(to_row(node, t.CurriculumNodeRow))
        db.flush()
        self.problem = m.Problem(
            curriculum_node_id=node.id,
            prompt="What is 7 + 5?",
            correct_answer=CORRECT,
            answer_type="numeric",
            grade_band="K-1",
        )
        db.add(to_row(self.problem, t.ProblemRow))
        db.commit()

    def _child(self) -> tuple[m.Student, m.Principal]:
        student = m.Student(grade_level=1, created_at=NOW)
        self.db.add(to_row(student, t.StudentRow))
        self.db.flush()
        principal = m.Principal(
            role=Role.STUDENT, display_name="A child", student_id=student.id, created_at=NOW
        )
        self.db.add(to_row(principal, t.PrincipalRow))
        self.db.flush()
        return student, principal

    def headers(self, principal: m.Principal) -> dict[str, str]:
        return {PRINCIPAL_HEADER: str(principal.id)}


@pytest.fixture
def world(db: DbSession) -> World:
    return World(db)


def _start(client: TestClient, world: World, principal: m.Principal) -> dict[str, object]:
    response = client.post("/student/session", headers=world.headers(principal))
    assert response.status_code == 201, response.text
    result: dict[str, object] = response.json()
    return result


def _answer(
    client: TestClient, world: World, principal: m.Principal, session_id: str, answer: str
) -> dict[str, object]:
    response = client.post(
        f"/student/session/{session_id}/answer",
        headers=world.headers(principal),
        json={"answer": answer},
    )
    assert response.status_code == 200, response.text
    result: dict[str, object] = response.json()
    return result


def _content_values(payload: object, key: str = "") -> Iterator[str]:
    """Every value a child could read, with identifiers excluded.

    UUIDs are opaque and contain arbitrary digits — a session id holding "12" is
    a coincidence, not a leak, and a blind substring scan over the whole payload
    passes or fails depending on which uuid4 came up. Identifiers are skipped so
    the check is about content, which is the only place a leak can actually be.
    """
    if key == "id" or key.endswith("_id"):
        return
    if isinstance(payload, dict):
        for name, value in payload.items():
            yield from _content_values(value, str(name))
    elif isinstance(payload, list):
        for item in payload:
            yield from _content_values(item, key)
    elif payload is not None:
        yield str(payload)


class TestTheAnswerNeverLeaves:
    def test_no_response_ever_contains_the_correct_answer(
        self, client: TestClient, world: World
    ) -> None:
        """The one that matters.

        Walks a whole session — start, wrong, wrong, right — and checks every
        payload. The leak-check apparatus guards hint text; nothing else guards
        a field named `correct_answer`, so this does.
        """
        started = _start(client, world, world.principal_a)
        session_id = str(started["session_id"])
        payloads = [started]
        payloads.append(_answer(client, world, world.principal_a, session_id, "2"))
        payloads.append(_answer(client, world, world.principal_a, session_id, "13"))

        for payload in payloads:
            assert "correct_answer" not in str(payload), payload
            # The answer as a bare value would be just as much of a leak as the
            # field name, so every readable value is checked — not just the ones
            # that look like they might hold it.
            for value in _content_values(payload):
                assert CORRECT not in value, (value, payload)

    def test_the_ten_frame_does_not_draw_the_total(self, client: TestClient, world: World) -> None:
        """§11.2's visual must not answer the question it illustrates."""
        started = _start(client, world, world.principal_a)
        visual = started["problem"]["visual"]  # type: ignore[index]
        assert visual["kind"] == "ten_frame"
        assert visual["left"] == 7
        assert visual["right"] == 5
        assert "total" not in visual
        assert 12 not in visual.values()


class TestTheVisualMatchesTheProblem:
    """A representation that contradicts the question is worse than none.

    Found by running the server rather than by a test: a random problem came up
    as `13 - 8` and the page drew thirteen counters in a ten-square frame.
    """

    @pytest.mark.parametrize(
        ("prompt", "kind"),
        [
            ("What is 7 + 5?", "ten_frame"),
            ("What is 3 + 4?", "ten_frame"),
            ("What is 13 - 8?", "number_line"),  # taking away is not a ten-frame
            ("What is 12 + 6?", "number_line"),  # neither part fits in ten
            ("What is 4 + 11?", "number_line"),
        ],
    )
    def test_the_representation_fits_the_numbers(self, prompt: str, kind: str) -> None:
        visual = _visual_for(prompt)
        assert visual is not None
        assert visual.kind == kind

    def test_a_problem_with_no_arithmetic_gets_no_visual(self) -> None:
        """Better nothing than a diagram invented for a word problem."""
        assert _visual_for("Which shape has four equal sides?") is None


class TestTenancy:
    def test_a_child_cannot_answer_another_child_s_session(
        self, client: TestClient, world: World
    ) -> None:
        started = _start(client, world, world.principal_a)
        response = client.post(
            f"/student/session/{started['session_id']}/answer",
            headers=world.headers(world.principal_b),
            json={"answer": "12"},
        )
        assert response.status_code == 403

    def test_a_missing_session_looks_the_same_as_someone_else_s(
        self, client: TestClient, world: World
    ) -> None:
        """Distinguishing them tells a caller which session ids are real."""
        response = client.post(
            f"/student/session/{uuid.uuid4()}/answer",
            headers=world.headers(world.principal_a),
            json={"answer": "12"},
        )
        assert response.status_code == 403

    def test_a_teacher_cannot_produce_student_sessions(
        self, client: TestClient, world: World
    ) -> None:
        """A teacher clicking through the child's page would write sessions and
        attempts attributed to a child who never sat down — and every Phase 0
        measurement is built on those rows."""
        response = client.post("/student/session", headers=world.headers(world.teacher))
        assert response.status_code == 403

    def test_an_unidentified_caller_is_refused(self, client: TestClient) -> None:
        assert client.post("/student/session").status_code == 401


class TestSessionFlow:
    def test_a_correct_answer_completes_the_session(self, client: TestClient, world: World) -> None:
        started = _start(client, world, world.principal_a)
        result = _answer(client, world, world.principal_a, str(started["session_id"]), CORRECT)

        assert result["correct"] is True
        assert result["done"] is True
        assert result["hint"] is None

    def test_a_wrong_answer_returns_a_hint_and_keeps_going(
        self, client: TestClient, world: World
    ) -> None:
        started = _start(client, world, world.principal_a)
        result = _answer(client, world, world.principal_a, str(started["session_id"]), "2")

        assert result["correct"] is False
        assert result["done"] is False
        assert result["hint"]
        assert result["hint_level"] == 1

    def test_running_out_of_hints_is_not_a_loss_state(
        self, client: TestClient, world: World
    ) -> None:
        """§11.5. A child who reads the end of a session as punishment stops
        asking for hints, and hint-seeking is the behaviour being taught."""
        started = _start(client, world, world.principal_a)
        session_id = str(started["session_id"])
        for wrong in ("2", "13", "11"):
            result = _answer(client, world, world.principal_a, session_id, wrong)

        assert result["done"] is True
        assert result["going_to_teacher"] is True
        message = str(result["message"])
        assert "teacher" in message
        for shaming in ("fail", "wrong", "incorrect", "sorry"):
            assert shaming not in message.lower(), message

    def test_a_finished_session_cannot_be_answered_again(
        self, client: TestClient, world: World
    ) -> None:
        started = _start(client, world, world.principal_a)
        session_id = str(started["session_id"])
        _answer(client, world, world.principal_a, session_id, CORRECT)

        response = client.post(
            f"/student/session/{session_id}/answer",
            headers=world.headers(world.principal_a),
            json={"answer": CORRECT},
        )
        assert response.status_code == 409


class TestTheRecordIsTrue:
    def test_a_live_session_is_not_logged_as_completed(
        self, client: TestClient, world: World, db: DbSession
    ) -> None:
        """Every submission is graded, but only the last one ends the session.

        Emitting `session_completed` per attempt would make `show_replay` tell a
        teacher the child finished three times.
        """
        started = _start(client, world, world.principal_a)
        session_id = uuid.UUID(str(started["session_id"]))
        _answer(client, world, world.principal_a, str(session_id), "2")

        completions = db.execute(
            select(t.PipelineEventRow).where(
                t.PipelineEventRow.session_id == session_id,
                t.PipelineEventRow.event_type == EventType.SESSION_COMPLETED,
            )
        ).all()
        assert completions == []

    def test_the_session_ends_exactly_once(
        self, client: TestClient, world: World, db: DbSession
    ) -> None:
        started = _start(client, world, world.principal_a)
        session_id = uuid.UUID(str(started["session_id"]))
        _answer(client, world, world.principal_a, str(session_id), "2")
        _answer(client, world, world.principal_a, str(session_id), CORRECT)

        completions = db.execute(
            select(t.PipelineEventRow).where(
                t.PipelineEventRow.session_id == session_id,
                t.PipelineEventRow.event_type == EventType.SESSION_COMPLETED,
            )
        ).all()
        assert len(completions) == 1

        row = db.get(t.SessionRow, session_id)
        assert row is not None
        assert row.state in {SessionState.COMPLETE, SessionState.ESCALATED}

    def test_the_answer_is_logged_before_it_is_graded(
        self, client: TestClient, world: World, db: DbSession
    ) -> None:
        """A served UI grades before it knows whether to hint.

        Left alone, that put `graded` ahead of `answer_submitted` in the log, and
        a replay claiming a child's answer was judged before they submitted it is
        not the faithful account §4 promises.
        """
        started = _start(client, world, world.principal_a)
        session_id = uuid.UUID(str(started["session_id"]))
        _answer(client, world, world.principal_a, str(session_id), "2")

        order = (
            db.execute(
                select(t.PipelineEventRow.event_type)
                .where(t.PipelineEventRow.session_id == session_id)
                .order_by(t.PipelineEventRow.sequence)
            )
            .scalars()
            .all()
        )
        assert order.index(EventType.ANSWER_SUBMITTED) < order.index(EventType.GRADED)

    def test_a_submission_is_logged_exactly_once(
        self, client: TestClient, world: World, db: DbSession
    ) -> None:
        """Both the API and `run_attempt` could log it; only one may."""
        started = _start(client, world, world.principal_a)
        session_id = uuid.UUID(str(started["session_id"]))
        _answer(client, world, world.principal_a, str(session_id), "2")

        submissions = db.execute(
            select(t.PipelineEventRow).where(
                t.PipelineEventRow.session_id == session_id,
                t.PipelineEventRow.event_type == EventType.ANSWER_SUBMITTED,
            )
        ).all()
        assert len(submissions) == 1

    def test_every_submission_is_recorded_as_an_attempt(
        self, client: TestClient, world: World, db: DbSession
    ) -> None:
        """The teacher console reads `Attempt`; without it a rater sees a hint
        with no idea what the child wrote."""
        started = _start(client, world, world.principal_a)
        session_id = uuid.UUID(str(started["session_id"]))
        _answer(client, world, world.principal_a, str(session_id), "2")
        _answer(client, world, world.principal_a, str(session_id), "13")

        answers = (
            db.execute(
                select(t.AttemptRow.student_answer)
                .where(t.AttemptRow.session_id == session_id)
                .order_by(t.AttemptRow.timestamp)
            )
            .scalars()
            .all()
        )
        assert list(answers) == ["2", "13"]


class TestThePageItself:
    def test_the_page_is_served_at_the_root(self, client: TestClient) -> None:
        """A URL a seven-year-old has to type accurately produces support
        requests instead of sessions."""
        response = client.get("/")
        assert response.status_code == 200
        assert "Maths practice" in response.text

    def test_the_page_carries_no_answer_and_no_grading_logic(self, client: TestClient) -> None:
        """Grading is server-side. A client that could decide correctness would
        need the answer to compare against."""
        body = client.get("/").text
        assert "correct_answer" not in body

    def test_the_page_uses_no_inline_event_handlers(self, client: TestClient) -> None:
        """Found the hard way, by a child-shaped user clicking Check.

        An inline handler resolves names against the element and its form before
        the global scope, so `onsubmit="return submit(event)"` bound to the
        form's own native `submit()`. The form posted for real, the browser
        navigated to a URL without `?as=`, and the next request 401'd — the page
        appeared to lose the child's login the moment they answered a question.
        `preventDefault` never ran because the function never ran.

        Nothing in the API suite can see this; the endpoints were fine
        throughout. So the guard is on the page: no inline handlers, and the
        shadowing has nowhere to happen.
        """
        body = client.get("/").text
        for attribute in ("onsubmit=", "onclick=", "onchange=", "oninput="):
            assert attribute not in body, (
                f"{attribute} is an inline handler — its scope chain includes the "
                f"element and form, so a name like `submit` silently binds to the "
                f"DOM method. Use addEventListener."
            )
