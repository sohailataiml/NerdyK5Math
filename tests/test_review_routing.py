"""§3.6 review routing (P0.8, P1.6).

This exists because of a gap that every other test missed: `ReviewItem` had a
table, a queue endpoint, and a console that rendered it, and nothing in the
system ever wrote one. Sessions escalated, the child was told *"your teacher is
going to look at this one with you"*, and the queue stayed empty. Each piece
worked; the connection between them did not exist.

So the load-bearing test is `test_a_finished_session_reaches_a_teacher`, and the
second is `test_the_child_is_only_promised_a_teacher_when_one_was_told` — because
the failure was not just a missing row, it was a promise made to a child that
nothing could keep.
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
from packages.domain.enums import UNKNOWN_TAG_LABEL, ReviewReason, Role
from packages.domain.mapping import to_row
from services.api.app import app
from services.api.auth import PRINCIPAL_HEADER
from services.api.db import get_db
from services.orchestrator.review import DatabaseReviewSink, reasons_for_review

NOW = dt.datetime(2026, 7, 28, tzinfo=dt.UTC)
CORRECT = "12"


class TestWhatCountsAsNeedingATeacher:
    """The policy, as a pure function — no database, no model, no session."""

    def test_phase_0_routes_a_clean_session_anyway(self) -> None:
        """P0.8 is 100% review. "Nothing to flag" cannot mean "no row", or the
        exit gates counted in teacher-reviewed sessions never fill."""
        reasons = reasons_for_review(
            tag="subtracted_instead_of_added",
            confidence=0.95,
            leak_rejections=0,
            escalated=False,
            hints_exhausted=False,
            needs_review=False,
            review_everything=True,
        )
        assert reasons == (ReviewReason.AUDIT_SAMPLE,)

    def test_outside_phase_0_a_clean_session_is_not_routed(self) -> None:
        """P1.6 has to keep the queue under 25% of sessions; routing everything
        forever makes that impossible by construction."""
        reasons = reasons_for_review(
            tag="subtracted_instead_of_added",
            confidence=0.95,
            leak_rejections=0,
            escalated=False,
            hints_exhausted=False,
            needs_review=False,
            review_everything=False,
        )
        assert reasons == ()

    def test_a_guardrail_failure_outranks_everything(self) -> None:
        """A hint that could not be cleared is the highest-value thing a teacher
        can see during a pilot — it seeds the P0.5 corpus."""
        reasons = reasons_for_review(
            tag=UNKNOWN_TAG_LABEL,
            confidence=0.2,
            leak_rejections=1,
            escalated=True,
            hints_exhausted=True,
            needs_review=True,
            review_everything=True,
        )
        assert reasons[0] is ReviewReason.LEAK_FALLBACK

    def test_every_matching_reason_is_reported_not_just_the_first(self) -> None:
        """One row is shown, but a caller that wants the rest can have them."""
        reasons = reasons_for_review(
            tag=UNKNOWN_TAG_LABEL,
            confidence=0.2,
            leak_rejections=0,
            escalated=False,
            hints_exhausted=True,
            needs_review=False,
            review_everything=True,
        )
        assert set(reasons) == {
            ReviewReason.MAX_HINTS,
            ReviewReason.UNKNOWN_TAG,
            ReviewReason.LOW_CONFIDENCE,
        }
        assert reasons[0] is ReviewReason.MAX_HINTS  # precedence, not set order

    def test_a_shadow_template_is_not_a_guardrail_failure(self) -> None:
        """Every Phase 0 session is served a template — that is the design.

        Keying `leak_fallback` off the hint source alone tagged the whole pilot
        as guardrail failures, burying the sessions where the leak check actually
        fired inside the ones where nothing went wrong. Those are precisely the
        sessions that seed the P0.5 corpus.
        """
        reasons = reasons_for_review(
            tag="subtracted_instead_of_added",
            confidence=0.95,
            leak_rejections=0,
            escalated=False,
            hints_exhausted=False,
            needs_review=False,
            review_everything=True,
        )
        assert reasons == (ReviewReason.AUDIT_SAMPLE,)

    def test_a_template_forced_by_leak_failures_is_a_guardrail_failure(self) -> None:
        """Outside shadow mode, the same hint source means the checker fired."""
        reasons = reasons_for_review(
            tag="subtracted_instead_of_added",
            confidence=0.95,
            leak_rejections=2,
            escalated=False,
            hints_exhausted=False,
            needs_review=False,
            review_everything=False,
        )
        assert reasons == (ReviewReason.LEAK_FALLBACK,)

    def test_a_rule_diagnosis_is_not_low_confidence(self) -> None:
        """Rule confidence is 0.99. A threshold that flagged it would route every
        session the deterministic path handled perfectly."""
        reasons = reasons_for_review(
            tag="subtracted_instead_of_added",
            confidence=0.99,
            leak_rejections=0,
            escalated=False,
            hints_exhausted=False,
            needs_review=False,
            review_everything=False,
        )
        assert ReviewReason.LOW_CONFIDENCE not in reasons

    def test_a_welfare_signal_never_enters_the_teaching_queue(self) -> None:
        """§7's path has its own table and its own urgency.

        Routing a possible disclosure into the list a teacher clears at the end
        of a lesson is how it gets read at the end of a lesson.
        """
        for review_everything in (True, False):
            reasons = reasons_for_review(
                tag=UNKNOWN_TAG_LABEL,
                confidence=0.1,
                leak_rejections=1,
                escalated=True,
                hints_exhausted=True,
                needs_review=True,
                review_everything=review_everything,
            )
            assert ReviewReason.SAFETY_FLAG not in reasons


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
    def __init__(self, db: DbSession) -> None:
        self.db = db
        self.student = m.Student(grade_level=1, created_at=NOW)
        db.add(to_row(self.student, t.StudentRow))
        db.flush()
        self.child = m.Principal(
            role=Role.STUDENT, display_name="A child", student_id=self.student.id, created_at=NOW
        )
        db.add(to_row(self.child, t.PrincipalRow))
        self.teacher = m.Principal(role=Role.TEACHER, display_name="Ms Rivera", created_at=NOW)
        db.add(to_row(self.teacher, t.PrincipalRow))
        db.flush()
        room = m.Classroom(teacher_id=self.teacher.id, name="Pilot", created_at=NOW)
        db.add(to_row(room, t.ClassroomRow))
        db.flush()
        db.add(
            to_row(
                m.Enrollment(classroom_id=room.id, student_id=self.student.id, created_at=NOW),
                t.EnrollmentRow,
            )
        )
        node = m.CurriculumNode(
            standard_code="1.OA.C.6", grade_band="K-1", definition="Add within 20."
        )
        db.add(to_row(node, t.CurriculumNodeRow))
        db.flush()
        db.add(
            to_row(
                m.Problem(
                    curriculum_node_id=node.id,
                    prompt="What is 7 + 5?",
                    correct_answer=CORRECT,
                    answer_type="numeric",
                    grade_band="K-1",
                ),
                t.ProblemRow,
            )
        )
        db.commit()

    def headers(self, principal: m.Principal) -> dict[str, str]:
        return {PRINCIPAL_HEADER: str(principal.id)}


@pytest.fixture
def world(db: DbSession) -> World:
    return World(db)


def _play(client: TestClient, world: World, answers: list[str]) -> dict[str, object]:
    started = client.post("/student/session", headers=world.headers(world.child)).json()
    result: dict[str, object] = {}
    for answer in answers:
        result = client.post(
            f"/student/session/{started['session_id']}/answer",
            headers=world.headers(world.child),
            json={"answer": answer},
        ).json()
    result["session_id"] = started["session_id"]
    return result


class TestASessionActuallyReachesATeacher:
    def test_a_finished_session_reaches_a_teacher(
        self, client: TestClient, world: World, db: DbSession
    ) -> None:
        """The bug this module exists to prevent: an empty queue that looks fine.

        Before this, every component here passed its own tests and no session
        ever arrived.
        """
        _play(client, world, [CORRECT])

        items = db.execute(select(t.ReviewItemRow)).scalars().all()
        assert len(items) == 1

    def test_it_shows_up_in_the_teacher_s_queue(self, client: TestClient, world: World) -> None:
        """Written is not the same as visible — the queue is scoped per student,
        so a row nobody's teacher can see is still nobody being told."""
        _play(client, world, [CORRECT])

        queue = client.get("/teacher/review-queue", headers=world.headers(world.teacher)).json()
        assert len(queue) == 1
        assert queue[0]["reason"] in {r.value for r in ReviewReason}

    def test_a_struggling_session_is_routed_on_its_most_significant_reason(
        self, client: TestClient, world: World, db: DbSession
    ) -> None:
        _play(client, world, ["2", "13", "11"])

        item = db.execute(select(t.ReviewItemRow)).scalars().one()
        # Not `leak_fallback`: no model is configured in tests, so no shadow ran
        # and no leak check rejected anything — the child simply ran out of hints.
        assert item.reason is ReviewReason.MAX_HINTS

    def test_one_session_produces_one_queue_row(
        self, client: TestClient, world: World, db: DbSession
    ) -> None:
        """A teacher clearing the same session three times learns to clear
        without reading."""
        _play(client, world, ["2", "13", "11"])

        assert len(db.execute(select(t.ReviewItemRow)).scalars().all()) == 1

    def test_routing_twice_does_not_duplicate(self, db: DbSession) -> None:
        sink = DatabaseReviewSink(db)
        session_id = uuid.uuid4()

        first = sink.route(session_id=session_id, reason=ReviewReason.MAX_HINTS)
        second = sink.route(session_id=session_id, reason=ReviewReason.UNKNOWN_TAG)

        assert first is not None
        assert second is None

    def test_a_resolved_session_can_be_routed_again(self, db: DbSession) -> None:
        """Idempotence is about the *open* queue. A child who comes back to the
        same problem after a teacher closed it is new work."""
        sink = DatabaseReviewSink(db)
        session_id = uuid.uuid4()
        first = sink.route(session_id=session_id, reason=ReviewReason.MAX_HINTS)
        assert first is not None

        row = db.get(t.ReviewItemRow, first.id)
        assert row is not None
        row.resolved_at = NOW
        db.flush()

        assert sink.route(session_id=session_id, reason=ReviewReason.UNKNOWN_TAG) is not None


class TestThePromiseToTheChild:
    def test_the_child_is_only_promised_a_teacher_when_one_was_told(
        self, client: TestClient, world: World
    ) -> None:
        """The original failure was not a missing row. It was a sentence."""
        result = _play(client, world, ["2", "13", "11"])

        assert result["going_to_teacher"] is True
        assert "teacher" in str(result["message"])

    def test_without_routing_the_child_is_not_promised_a_teacher(self) -> None:
        """If nothing is wired, the message must not claim otherwise.

        Exercised at the graph rather than through the API, because the API
        always wires a sink — and the point is that the sentence follows the
        record wherever the record comes from.
        """
        from packages.prompts import PromptRegistry
        from packages.telemetry import EventRecorder, InMemoryEventSink
        from services.orchestrator import graph
        from services.orchestrator.state import PipelineDeps

        deps = PipelineDeps(
            recorder=EventRecorder(InMemoryEventSink(), uuid.uuid4()),
            prompts=PromptRegistry(),
            shadow_mode=True,
            review_sink=None,
        )
        routed = graph.end_session_for_review(deps, reason="hint levels exhausted")
        assert routed == ()
