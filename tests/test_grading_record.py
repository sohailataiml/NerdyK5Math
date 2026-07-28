"""Grading leaves a durable, inspectable record (§3.5, §5, §12).

Two gaps of the same kind, both invisible to every test that existed:

- The grade stage recorded `stage_started` and never an ending, because
  `recorder.graded` carries no `stage`. The per-stage trace read it as
  `unterminated`, its duration was unmeasurable, and a grade that completed was
  indistinguishable from one that died mid-call.
- `GradeResult` had an entity, an append-only table, and a migration, and the
  only code that ever wrote one was a demo script. Every real session recorded a
  verdict as an event and nothing else.

Neither broke anything a user could see, which is exactly why they need tests
that assert on the *record* rather than on the response.
"""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session as DbSession

from packages.curriculum.seed import seed
from packages.domain.enums import EventType, GradeBand, GradeMethod, PipelineStage
from packages.prompts import PromptRegistry
from packages.telemetry import EventRecorder, InMemoryEventSink, trace_from
from packages.telemetry.replay import ReplayStep, SessionReplay
from services.orchestrator import graph
from services.orchestrator.grades import InMemoryGradeSink
from services.orchestrator.state import PipelineDeps, Problem
from services.orchestrator.symbolic_client import InProcessSymbolicChecker

PROBLEM = Problem(prompt="What is 7 + 5?", correct_answer="12", grade_band=GradeBand.K_1)


def _deps(sink: InMemoryEventSink, grades: InMemoryGradeSink | None = None) -> PipelineDeps:
    return PipelineDeps(
        recorder=EventRecorder(sink, uuid.uuid4()),
        prompts=PromptRegistry(),
        symbolic=InProcessSymbolicChecker(),
        grade_sink=grades,
    )


def _trace(sink: InMemoryEventSink, session_id: uuid.UUID):  # type: ignore[no-untyped-def]
    return trace_from(
        SessionReplay(
            session_id=session_id,
            steps=tuple(ReplayStep(event=e) for e in sink.events),
        )
    )


class TestTheStageClosesItself:
    def test_grade_is_not_left_unterminated(self, session: DbSession) -> None:
        """Every other stage records an ending. This one silently did not, so
        the inspector showed `grade — unterminated, nothing recorded` on every
        run ever traced."""
        seed(session)
        sink = InMemoryEventSink()
        deps = _deps(sink)

        graph.check_answer(
            deps, session_id=deps.recorder.session_id, problem=PROBLEM, student_answer="12"
        )

        runs = [
            r
            for r in _trace(sink, deps.recorder.session_id).stages
            if r.stage is PipelineStage.GRADE
        ]
        assert len(runs) == 1
        assert runs[0].outcome == "completed"
        assert runs[0].ended_at is not None

    def test_the_verdict_is_visible_on_the_stage_not_only_the_timeline(
        self, session: DbSession
    ) -> None:
        """§8 segments cost and latency per stage. A score reachable only from
        the session timeline cannot be joined to the stage that produced it."""
        seed(session)
        sink = InMemoryEventSink()
        deps = _deps(sink)

        graph.check_answer(
            deps, session_id=deps.recorder.session_id, problem=PROBLEM, student_answer="2"
        )

        run = next(
            r
            for r in _trace(sink, deps.recorder.session_id).stages
            if r.stage is PipelineStage.GRADE
        )
        assert run.inputs["answer"] == "2"
        assert run.outputs["score"] == 0.0
        assert run.outputs["method"] == "symbolic"
        assert run.duration_ms is not None

    def test_a_missing_checker_reads_as_degradation(self, session: DbSession) -> None:
        """Grading with no checker is the provider-down path for §3.5, and §8's
        dashboards segment on exactly that."""
        seed(session)
        sink = InMemoryEventSink()
        deps = PipelineDeps(
            recorder=EventRecorder(sink, uuid.uuid4()), prompts=PromptRegistry(), symbolic=None
        )

        graph.check_answer(
            deps, session_id=deps.recorder.session_id, problem=PROBLEM, student_answer="12"
        )

        result = _trace(sink, deps.recorder.session_id)
        assert PipelineStage.GRADE in result.degraded_stages


class TestTheVerdictIsDurable:
    def test_a_grade_result_row_is_written_for_the_attempt(self, session: DbSession) -> None:
        """`grade_result` was a table nothing outside a demo script ever wrote
        to — the same shape of gap as the review queue nobody filled."""
        seed(session)
        grades = InMemoryGradeSink()
        deps = _deps(InMemoryEventSink(), grades)
        attempt_id = uuid.uuid4()

        graph.check_answer(
            deps,
            session_id=deps.recorder.session_id,
            problem=PROBLEM,
            student_answer="12",
            attempt_id=attempt_id,
        )

        assert len(grades.results) == 1
        recorded = grades.results[0]
        assert recorded.attempt_id == attempt_id
        assert recorded.score == 1.0
        assert recorded.method is GradeMethod.SYMBOLIC
        # §3.5's division of labour, carried rather than summarised away.
        assert recorded.symbolic_agreed is True

    def test_a_wrong_answer_is_recorded_too(self, session: DbSession) -> None:
        """A record that only keeps the grades a child passed is not a record."""
        seed(session)
        grades = InMemoryGradeSink()
        deps = _deps(InMemoryEventSink(), grades)

        graph.check_answer(
            deps,
            session_id=deps.recorder.session_id,
            problem=PROBLEM,
            student_answer="2",
            attempt_id=uuid.uuid4(),
        )

        assert grades.results[0].score == 0.0

    def test_without_an_attempt_id_nothing_is_written(self, session: DbSession) -> None:
        """`GradeResult` is keyed by attempt. A caller with no attempt — a
        script, a test — gets the event log and no orphan row."""
        seed(session)
        grades = InMemoryGradeSink()
        deps = _deps(InMemoryEventSink(), grades)

        graph.check_answer(
            deps, session_id=deps.recorder.session_id, problem=PROBLEM, student_answer="12"
        )

        assert grades.results == []

    def test_the_event_and_the_row_agree(self, session: DbSession) -> None:
        """Two records of one verdict must not be able to disagree; if they can,
        an audit has to pick one and cannot say why."""
        seed(session)
        sink = InMemoryEventSink()
        grades = InMemoryGradeSink()
        deps = _deps(sink, grades)

        graph.check_answer(
            deps,
            session_id=deps.recorder.session_id,
            problem=PROBLEM,
            student_answer="12",
            attempt_id=uuid.uuid4(),
        )

        graded_event = next(e for e in sink.events if e.event_type is EventType.GRADED)
        assert graded_event.detail["score"] == grades.results[0].score
        assert graded_event.detail["method"] == grades.results[0].method.value
