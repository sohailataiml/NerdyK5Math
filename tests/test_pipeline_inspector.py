"""Per-stage inputs and outputs for a run (M0.8, §8).

Two concerns, and the second is the one that would actually hurt.

**The trace has to be faithful.** It is built from the append-only record, so it
is only as good as its grouping: a stage that ran three times must show three
runs, a stage that never finished must say so rather than vanish, and a
deterministic stage must be distinguishable from a model-backed one. Getting
that wrong produces a confident, wrong account of a child's session — which
§12 says is worse than no account.

**It must not be readable by the wrong person.** This surface carries children's
answers and full prompt payloads including `correct_answer`. The authorization
negatives below are the point of the file.
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

from packages.curriculum.seed import seed
from packages.domain import models as m
from packages.domain import tables as t
from packages.domain.enums import (
    EventType,
    GradeBand,
    PipelineStage,
    Role,
    SessionState,
)
from packages.domain.mapping import to_row
from packages.llm import LLMClient
from packages.llm.fake import FakeTransport
from packages.llm.ledger import InMemoryLedger
from packages.prompts import PromptRegistry
from packages.telemetry import EventRecorder, InMemoryEventSink, trace_from
from packages.telemetry.replay import ReplayStep, SessionReplay
from services.api.app import app
from services.api.auth import PRINCIPAL_HEADER
from services.api.db import get_db
from services.orchestrator import graph
from services.orchestrator.state import PipelineDeps, Problem

NOW = dt.datetime(2026, 7, 28, 12, 0, tzinfo=dt.UTC)

PROBLEM = Problem(
    prompt="What is 7 + 5?",
    correct_answer="12",
    grade_band=GradeBand.K_1,
    operands={"a": "7", "b": "5"},
)


def _event(
    session_id: uuid.UUID,
    sequence: int,
    event_type: EventType,
    *,
    stage: PipelineStage | None = None,
    detail: dict[str, object] | None = None,
    llm_call_id: uuid.UUID | None = None,
) -> m.PipelineEvent:
    return m.PipelineEvent(
        session_id=session_id,
        sequence=sequence,
        event_type=event_type,
        stage=stage,
        to_state=SessionState.DIAGNOSING if event_type is EventType.STATE_CHANGED else None,
        detail=detail or {},
        llm_call_id=llm_call_id,
        occurred_at=NOW + dt.timedelta(milliseconds=sequence * 100),
    )


def _replay(
    *events: m.PipelineEvent, calls: dict[uuid.UUID, m.LLMCall] | None = None
) -> SessionReplay:
    lookup = calls or {}
    session_id = events[0].session_id
    return SessionReplay(
        session_id=session_id,
        steps=tuple(
            ReplayStep(
                event=e,
                llm_call=lookup.get(e.llm_call_id) if e.llm_call_id else None,
            )
            for e in events
        ),
    )


def _call(session_id: uuid.UUID, stage: PipelineStage) -> m.LLMCall:
    return m.LLMCall(
        session_id=session_id,
        stage=stage,
        model_id="claude-haiku-4-5-20251001",
        prompt_version="diagnose/K-1/v1",
        input_payload={"problem_prompt": "What is 7 + 5?", "student_answer": "2"},
        output_payload={"text": "subtracted_instead_of_added"},
        tokens_in=100,
        tokens_out=20,
        latency_ms=640,
        cost_usd=0.0005,
        created_at=NOW,
    )


class TestGrouping:
    def test_a_stage_that_ran_twice_shows_twice(self) -> None:
        """Three hint levels means three passes through generate. A view showing
        only the last hides the two that explain it."""
        sid = uuid.uuid4()
        result = trace_from(
            _replay(
                _event(sid, 0, EventType.STAGE_STARTED, stage=PipelineStage.GENERATE_HINT),
                _event(sid, 1, EventType.STAGE_COMPLETED, stage=PipelineStage.GENERATE_HINT),
                _event(sid, 2, EventType.STAGE_STARTED, stage=PipelineStage.GENERATE_HINT),
                _event(sid, 3, EventType.STAGE_COMPLETED, stage=PipelineStage.GENERATE_HINT),
            )
        )

        assert [r.ordinal for r in result.stages] == [1, 2]
        assert all(r.stage is PipelineStage.GENERATE_HINT for r in result.stages)

    def test_inputs_and_outputs_come_from_the_right_ends(self) -> None:
        sid = uuid.uuid4()
        result = trace_from(
            _replay(
                _event(
                    sid,
                    0,
                    EventType.STAGE_STARTED,
                    stage=PipelineStage.RERANK,
                    detail={"tag": "subtracted_instead_of_added"},
                ),
                _event(
                    sid,
                    1,
                    EventType.STAGE_COMPLETED,
                    stage=PipelineStage.RERANK,
                    detail={"node_id": "abc", "strategies": 2},
                ),
            )
        )

        run = result.stages[0]
        assert run.inputs == {"tag": "subtracted_instead_of_added"}
        assert run.outputs == {"node_id": "abc", "strategies": 2}
        assert run.outcome == "completed"

    def test_a_model_backed_stage_carries_the_payloads(self) -> None:
        sid = uuid.uuid4()
        call = _call(sid, PipelineStage.DIAGNOSE)
        result = trace_from(
            _replay(
                _event(sid, 0, EventType.STAGE_STARTED, stage=PipelineStage.DIAGNOSE),
                _event(
                    sid,
                    1,
                    EventType.STAGE_COMPLETED,
                    stage=PipelineStage.DIAGNOSE,
                    llm_call_id=call.id,
                ),
                calls={call.id: call},
            )
        )

        run = result.stages[0]
        assert run.used_model is True
        assert run.inputs["llm_input"] == call.input_payload
        assert run.outputs["llm_output"] == call.output_payload
        assert run.cost_usd == pytest.approx(0.0005)

    def test_a_deterministic_stage_is_not_a_missing_stage(self) -> None:
        """The rule pre-check firing is the cheap path working, not an absence.
        A dashboard that showed only model calls would render a full provider
        outage as a blank screen."""
        sid = uuid.uuid4()
        result = trace_from(
            _replay(
                _event(sid, 0, EventType.STAGE_STARTED, stage=PipelineStage.DIAGNOSE),
                _event(
                    sid,
                    1,
                    EventType.STAGE_COMPLETED,
                    stage=PipelineStage.DIAGNOSE,
                    detail={"source": "rule"},
                ),
            )
        )

        run = result.stages[0]
        assert run.used_model is False
        assert run.cost_usd == 0.0
        assert result.deterministic_stages == 1
        assert result.model_calls == 0

    def test_a_fallback_is_reported_as_degradation(self) -> None:
        sid = uuid.uuid4()
        result = trace_from(
            _replay(
                _event(sid, 0, EventType.STAGE_STARTED, stage=PipelineStage.GENERATE_HINT),
                _event(
                    sid,
                    1,
                    EventType.FALLBACK_USED,
                    stage=PipelineStage.GENERATE_HINT,
                    detail={"reason": "no model"},
                ),
            )
        )

        assert result.stages[0].outcome == "fallback"
        assert result.degraded_stages == (PipelineStage.GENERATE_HINT,)

    def test_a_stage_that_never_finished_is_reported_not_dropped(self) -> None:
        """A stage that started and recorded no ending means the process died
        mid-run. Dropping it would show a session that looks complete."""
        sid = uuid.uuid4()
        result = trace_from(
            _replay(_event(sid, 0, EventType.STAGE_STARTED, stage=PipelineStage.LEAK_CHECK))
        )

        run = result.stages[0]
        assert run.outcome == "unterminated"
        assert run.ended_at is None
        assert run.last_sequence is None
        assert run.duration_ms is None

    def test_session_level_events_go_to_the_timeline(self) -> None:
        sid = uuid.uuid4()
        result = trace_from(
            _replay(
                _event(sid, 0, EventType.ANSWER_SUBMITTED, detail={"attempt_number": 1}),
                _event(sid, 1, EventType.STAGE_STARTED, stage=PipelineStage.DIAGNOSE),
                _event(sid, 2, EventType.STAGE_COMPLETED, stage=PipelineStage.DIAGNOSE),
                _event(sid, 3, EventType.GRADED, detail={"score": 1.0, "method": "symbolic"}),
            )
        )

        assert [e.event_type for e in result.timeline] == [
            EventType.ANSWER_SUBMITTED,
            EventType.GRADED,
        ]
        assert len(result.stages) == 1

    def test_a_gap_in_the_record_is_surfaced(self) -> None:
        """A trace from an incomplete record is a plausible story, not what
        happened. The caller has to be able to tell."""
        sid = uuid.uuid4()
        result = trace_from(
            _replay(
                _event(sid, 0, EventType.STAGE_STARTED, stage=PipelineStage.DIAGNOSE),
                _event(sid, 3, EventType.STAGE_COMPLETED, stage=PipelineStage.DIAGNOSE),
            )
        )

        assert result.gaps == (1, 2)


class TestAgainstARealRun:
    def test_a_real_attempt_traces_every_stage_it_ran(self, session: DbSession) -> None:
        seed(session)
        sink = InMemoryEventSink()
        session_id = uuid.uuid4()

        def respond(system: str, _user: str) -> str:
            return "SAFE" if "gives away the answer" in system else "Try the ten-frame."

        deps = PipelineDeps(
            recorder=EventRecorder(sink, session_id),
            prompts=PromptRegistry(),
            llm=LLMClient(FakeTransport(responder=respond), InMemoryLedger()),
            db=session,
            shadow_mode=False,
        )
        graph.run_attempt(deps, session_id=session_id, problem=PROBLEM, student_answer="2")

        result = trace_from(
            SessionReplay(
                session_id=session_id,
                steps=tuple(ReplayStep(event=e) for e in sink.events),
            )
        )

        stages = {run.stage for run in result.stages}
        assert PipelineStage.DIAGNOSE in stages
        assert PipelineStage.RERANK in stages
        assert PipelineStage.GENERATE_HINT in stages
        assert PipelineStage.LEAK_CHECK in stages
        assert result.gaps == ()
        # Every run is accounted for: nothing left open by the real orchestrator.
        assert all(run.outcome != "unterminated" for run in result.stages)


# ---------------------------------------------------------------------------
# The endpoint. Weighted toward who may not read it.
# ---------------------------------------------------------------------------


@pytest.fixture
def api_db() -> Iterator[DbSession]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    t.Base.metadata.create_all(engine)
    with DbSession(engine) as db:
        yield db
    engine.dispose()


@pytest.fixture
def client(api_db: DbSession) -> Iterator[TestClient]:
    app.dependency_overrides[get_db] = lambda: api_db
    yield TestClient(app)
    app.dependency_overrides.clear()


class World:
    """Two teachers with a student each, and a session belonging to student A."""

    def __init__(self, db: DbSession) -> None:
        self.admin = self._principal(db, Role.ADMIN)
        self.student_a = self._student(db)
        self.student_b = self._student(db)
        self.teacher_a = self._principal(db, Role.TEACHER)
        self.teacher_b = self._principal(db, Role.TEACHER)
        self._enrol(db, self.teacher_a, self.student_a)
        self._enrol(db, self.teacher_b, self.student_b)

        node = m.CurriculumNode(standard_code="1.OA.C.6", grade_band="K-1", definition="Add.")
        db.add(to_row(node, t.CurriculumNodeRow))
        db.flush()
        problem = m.Problem(
            curriculum_node_id=node.id,
            prompt="What is 7 + 5?",
            correct_answer="12",
            answer_type="numeric",
            grade_band="K-1",
        )
        db.add(to_row(problem, t.ProblemRow))
        db.flush()
        sess = m.Session(
            student_id=self.student_a,
            problem_id=problem.id,
            started_at=NOW,
            state=SessionState.COMPLETE,
            attempt_count=1,
        )
        db.add(to_row(sess, t.SessionRow))
        db.flush()
        self.session_id = sess.id

        db.add(
            to_row(
                _event(
                    self.session_id,
                    0,
                    EventType.STAGE_STARTED,
                    stage=PipelineStage.DIAGNOSE,
                    detail={"answer": "2"},
                ),
                t.PipelineEventRow,
            )
        )
        db.add(
            to_row(
                _event(
                    self.session_id,
                    1,
                    EventType.STAGE_COMPLETED,
                    stage=PipelineStage.DIAGNOSE,
                    detail={"source": "rule"},
                ),
                t.PipelineEventRow,
            )
        )
        db.commit()

    @staticmethod
    def _principal(db: DbSession, role: Role) -> uuid.UUID:
        p = m.Principal(role=role, display_name=f"{role.value}", created_at=NOW)
        db.add(to_row(p, t.PrincipalRow))
        db.flush()
        return p.id

    @staticmethod
    def _student(db: DbSession) -> uuid.UUID:
        s = m.Student(grade_level=1, created_at=NOW)
        db.add(to_row(s, t.StudentRow))
        db.flush()
        return s.id

    @staticmethod
    def _enrol(db: DbSession, teacher: uuid.UUID, student: uuid.UUID) -> None:
        room = m.Classroom(teacher_id=teacher, name="room", created_at=NOW)
        db.add(to_row(room, t.ClassroomRow))
        db.flush()
        db.add(
            to_row(
                m.Enrollment(classroom_id=room.id, student_id=student, created_at=NOW),
                t.EnrollmentRow,
            )
        )
        db.flush()


class TestAuthorization:
    def test_an_admin_sees_the_stage_trace(self, client: TestClient, api_db: DbSession) -> None:
        world = World(api_db)

        response = client.get(
            f"/admin/runs/{world.session_id}",
            headers={PRINCIPAL_HEADER: str(world.admin)},
        )

        assert response.status_code == 200
        body = response.json()
        assert body["stages"][0]["stage"] == "diagnose"
        assert body["stages"][0]["used_model"] is False
        assert body["stages"][0]["inputs"] == {"answer": "2"}

    def test_a_teacher_sees_their_own_students_run(
        self, client: TestClient, api_db: DbSession
    ) -> None:
        """That is the point of the trail: a teacher defends a grade with it."""
        world = World(api_db)

        response = client.get(
            f"/admin/runs/{world.session_id}",
            headers={PRINCIPAL_HEADER: str(world.teacher_a)},
        )

        assert response.status_code == 200

    def test_a_teacher_cannot_read_another_teachers_student(
        self, client: TestClient, api_db: DbSession
    ) -> None:
        """The payloads here contain a child's actual answers."""
        world = World(api_db)

        response = client.get(
            f"/admin/runs/{world.session_id}",
            headers={PRINCIPAL_HEADER: str(world.teacher_b)},
        )

        assert response.status_code == 403

    def test_a_student_cannot_read_any_run(self, client: TestClient, api_db: DbSession) -> None:
        world = World(api_db)
        child = m.Principal(
            role=Role.STUDENT,
            display_name="child",
            student_id=world.student_a,
            created_at=NOW,
        )
        api_db.add(to_row(child, t.PrincipalRow))
        api_db.commit()

        response = client.get(
            f"/admin/runs/{world.session_id}", headers={PRINCIPAL_HEADER: str(child.id)}
        )

        # Even their own session: the inputs carry `correct_answer`, and a child
        # with the network tab open is still a child in the pilot.
        assert response.status_code == 403

    def test_a_missing_session_answers_like_a_forbidden_one(
        self, client: TestClient, api_db: DbSession
    ) -> None:
        """Otherwise this endpoint is an oracle for which session ids are real."""
        world = World(api_db)

        response = client.get(
            f"/admin/runs/{uuid.uuid4()}", headers={PRINCIPAL_HEADER: str(world.admin)}
        )

        assert response.status_code == 403

    def test_only_an_admin_may_list_every_run(self, client: TestClient, api_db: DbSession) -> None:
        """The list spans every child in the deployment. A teacher's reach is
        derived from their own classrooms, never granted by their role."""
        world = World(api_db)

        assert (
            client.get("/admin/runs", headers={PRINCIPAL_HEADER: str(world.admin)}).status_code
            == 200
        )
        assert (
            client.get("/admin/runs", headers={PRINCIPAL_HEADER: str(world.teacher_a)}).status_code
            == 403
        )

    def test_the_page_itself_needs_no_data_to_render(self, client: TestClient) -> None:
        """Markup only — it fetches nothing until it has a principal, so an
        unauthenticated GET leaks no session ids."""
        response = client.get("/admin/pipeline")

        assert response.status_code == 200
        assert "Pipeline inspector" in response.text
