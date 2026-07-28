"""M0.8 — the event log, replay, and tracing.

The milestone's done-criterion is "any session reconstructable end-to-end from
the record alone" (§4). `TestSessionIsReconstructableFromTheRecord` is that
criterion written as a test: it builds a full session, throws away every
in-memory reference to what happened, and rebuilds the account from Postgres-
shaped tables alone.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy.orm import Session as DbSession

from packages.domain import models as m
from packages.domain import tables as t
from packages.domain.append_only import AppendOnlyError
from packages.domain.enums import (
    EventType,
    HintSource,
    PipelineStage,
    SessionState,
)
from packages.domain.mapping import to_row
from packages.telemetry.events import DatabaseEventSink, EventRecorder, InMemoryEventSink
from packages.telemetry.replay import replay
from packages.telemetry.tracing import SESSION_ID_ATTRIBUTE, stage_span

NOW = dt.datetime(2026, 7, 27, 12, 0, tzinfo=dt.UTC)


def _seed_session(db: DbSession) -> uuid.UUID:
    """Minimum rows for a session's foreign keys to resolve."""
    node = m.CurriculumNode(standard_code="1.OA.C.6", grade_band="K-1", definition="Add within 20.")
    problem = m.Problem(
        curriculum_node_id=node.id,
        prompt="What is 7 + 5?",
        correct_answer="12",
        answer_type="numeric",
        grade_band="K-1",
    )
    student = m.Student(grade_level=1, created_at=NOW)
    session = m.Session(
        student_id=student.id, problem_id=problem.id, started_at=NOW, attempt_count=0
    )
    db.add(to_row(node, t.CurriculumNodeRow))
    db.add(to_row(student, t.StudentRow))
    db.flush()
    db.add(to_row(problem, t.ProblemRow))
    db.flush()
    db.add(to_row(session, t.SessionRow))
    db.commit()
    return session.id


class TestOrdering:
    def test_sequence_increments_per_session(self) -> None:
        sink = InMemoryEventSink()
        session_id = uuid.uuid4()
        recorder = EventRecorder(sink, session_id)

        recorder.session_started()
        recorder.state_changed(frm=None, to=SessionState.DIAGNOSING)
        recorder.stage_started(PipelineStage.DIAGNOSE)

        assert [e.sequence for e in sink.events] == [0, 1, 2]

    def test_sessions_do_not_share_a_sequence(self) -> None:
        sink = InMemoryEventSink()
        a, b = uuid.uuid4(), uuid.uuid4()
        EventRecorder(sink, a).session_started()
        EventRecorder(sink, b).session_started()
        EventRecorder(sink, a).session_started()

        assert [e.sequence for e in sink.events if e.session_id == a] == [0, 1]
        assert [e.sequence for e in sink.events if e.session_id == b] == [0]

    def test_state_change_without_destination_is_rejected(self) -> None:
        """A transition with no destination cannot be replayed."""
        with pytest.raises(ValueError, match="to_state"):
            m.PipelineEvent(
                session_id=uuid.uuid4(),
                sequence=0,
                event_type=EventType.STATE_CHANGED,
                occurred_at=NOW,
            )


class TestAppendOnly:
    def test_events_cannot_be_rewritten(self, session: DbSession) -> None:
        """A rewritable timeline defends nothing — same reasoning as the ledger."""
        session_id = _seed_session(session)
        EventRecorder(DatabaseEventSink(session), session_id).session_started()

        row = session.query(t.PipelineEventRow).one()
        row.detail = {"tampered": True}
        with pytest.raises(AppendOnlyError):
            session.commit()


class TestSessionIsReconstructableFromTheRecord:
    """M0.8's done-criterion (§4)."""

    def test_full_session_replays_from_the_database_alone(self, session: DbSession) -> None:
        session_id = _seed_session(session)
        recorder = EventRecorder(DatabaseEventSink(session), session_id)

        # A model call, ledgered as M0.4 requires.
        call = m.LLMCall(
            session_id=session_id,
            stage=PipelineStage.DIAGNOSE,
            model_id="claude-haiku-4-5",
            prompt_version="diagnose/K-1/v2",
            input_payload={"student_answer": "2"},
            output_payload={"tag": "subtracted_instead_of_added"},
            tokens_in=391,
            tokens_out=97,
            latency_ms=1757,
            cost_usd=0.000876,
            created_at=NOW,
        )
        session.add(to_row(call, t.LLMCallRow))
        session.commit()

        # A session the way it actually unfolds.
        recorder.session_started(problem="What is 7 + 5?")
        recorder.answer_submitted(attempt_number=1, answer="2")
        recorder.state_changed(frm=SessionState.AWAITING_ANSWER, to=SessionState.DIAGNOSING)
        recorder.stage_started(PipelineStage.DIAGNOSE)
        recorder.stage_completed(
            PipelineStage.DIAGNOSE, llm_call_id=call.id, tag="subtracted_instead_of_added"
        )
        recorder.state_changed(frm=SessionState.DIAGNOSING, to=SessionState.GENERATING_HINT)
        recorder.stage_failed(PipelineStage.LEAK_CHECK, reason="hint stated the answer")
        recorder.fallback_used(PipelineStage.GENERATE_HINT, reason="leak check failed twice")
        recorder.hint_shown(hint_level=1, source=HintSource.TEMPLATE_FALLBACK.value)
        recorder.graded(score=1.0, method="symbolic")
        recorder.session_completed(outcome="correct")

        # Everything above is now discarded; only the database is consulted.
        result = replay(session, session_id)

        assert len(result.steps) == 11
        assert result.gaps() == ()
        assert result.model_calls == 1
        assert result.total_cost_usd == pytest.approx(0.000876)

        # §4: which diagnosis, by which model and prompt version.
        diagnosis = result.events_of(EventType.STAGE_COMPLETED)[0]
        assert diagnosis.event.detail["tag"] == "subtracted_instead_of_added"
        assert diagnosis.llm_call is not None
        assert diagnosis.llm_call.prompt_version == "diagnose/K-1/v2"

        # §4: *why* this hint was shown — the leak-check failure and the
        # fallback are both in the record, in order.
        rendered = result.render()
        assert "leak_check" in rendered
        assert "hint stated the answer" in rendered
        assert "template_fallback" in rendered
        assert rendered.index("stage_failed") < rendered.index("fallback_used")

    def test_replay_of_an_unknown_session_is_empty_not_an_error(self, session: DbSession) -> None:
        assert replay(session, uuid.uuid4()).is_empty

    def test_a_gap_in_the_record_is_reported(self, session: DbSession) -> None:
        """An incomplete record must not render as a confident narrative."""
        session_id = _seed_session(session)
        for sequence in (0, 1, 3):
            session.add(
                to_row(
                    m.PipelineEvent(
                        session_id=session_id,
                        sequence=sequence,
                        event_type=EventType.STAGE_STARTED,
                        stage=PipelineStage.DIAGNOSE,
                        occurred_at=NOW,
                    ),
                    t.PipelineEventRow,
                )
            )
        session.commit()

        result = replay(session, session_id)
        assert result.gaps() == (2,)
        assert "INCOMPLETE" in result.render()

    def test_fallback_is_distinguishable_from_a_normal_run(self, session: DbSession) -> None:
        """§4's degradation path. Without this event, a day when the provider was
        down looks in the data like a day the model got worse."""
        session_id = _seed_session(session)
        recorder = EventRecorder(DatabaseEventSink(session), session_id)
        recorder.session_started()
        recorder.fallback_used(PipelineStage.DIAGNOSE, reason="provider timeout")

        result = replay(session, session_id)
        fallbacks = result.events_of(EventType.FALLBACK_USED)
        assert len(fallbacks) == 1
        assert fallbacks[0].event.detail["reason"] == "provider timeout"


class TestTracing:
    def test_span_carries_the_session_id(self) -> None:
        """The join key between a trace and a replay."""
        session_id = uuid.uuid4()
        with stage_span(PipelineStage.DIAGNOSE, session_id) as span:
            attributes = getattr(span, "attributes", None)

        # Unconfigured OTel yields a non-recording span; the contract is that the
        # call is safe either way, and carries the id when tracing is on.
        if attributes:
            assert attributes[SESSION_ID_ATTRIBUTE] == str(session_id)

    def test_tracing_is_safe_without_configuration(self) -> None:
        """Observability must never be the reason a stage cannot run."""
        with stage_span(PipelineStage.GRADE, uuid.uuid4()):
            pass

    def test_exception_propagates_through_the_span(self) -> None:
        with (
            pytest.raises(RuntimeError, match="boom"),
            stage_span(PipelineStage.DIAGNOSE, uuid.uuid4()),
        ):
            raise RuntimeError("boom")
