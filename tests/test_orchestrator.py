"""M0 exit criterion — the pipeline end to end.

The plan's M0 exit is: "the pipeline runs end-to-end on a 3-node fixture KB with
real model calls at every stage, every call ledgered, every stage falling back
cleanly when the provider is stubbed to fail." These tests are that sentence,
executed with a fake transport standing in for the provider.

The rules the graph enforces that no individual stage can are the ones worth
reading: a hint reaching a child only after the leak-check passes, and never a
third free generation.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.orm import Session as DbSession

from packages.curriculum.seed import seed
from packages.domain.enums import DiagnosisSource, GradeBand, HintSource, PipelineStage
from packages.llm import LLMClient, TokenUsage
from packages.llm.errors import TransportError
from packages.llm.fake import FakeTransport
from packages.llm.ledger import InMemoryLedger
from packages.prompts import PromptRegistry
from packages.telemetry import EventRecorder, InMemoryEventSink
from services.orchestrator import graph
from services.orchestrator.state import PipelineDeps, Problem, SymbolicChecker
from services.orchestrator.symbolic_client import InProcessSymbolicChecker

PROBLEM = Problem(
    prompt="What is 7 + 5?",
    correct_answer="12",
    grade_band=GradeBand.K_1,
    operands={"a": "7", "b": "5"},
)


def _deps(
    db: DbSession | None = None,
    *,
    transport: FakeTransport | None = None,
    symbolic: SymbolicChecker | None = None,
) -> tuple[PipelineDeps, InMemoryEventSink, InMemoryLedger]:
    sink = InMemoryEventSink()
    ledger = InMemoryLedger()
    session_id = uuid.uuid4()
    llm = LLMClient(transport, ledger) if transport is not None else None
    deps = PipelineDeps(
        recorder=EventRecorder(sink, session_id),
        prompts=PromptRegistry(),
        llm=llm,
        symbolic=symbolic,
        db=db,
    )
    return deps, sink, ledger


class TestHappyPath:
    def test_rule_diagnosis_skips_the_model_entirely(self, session: DbSession) -> None:
        """§3.1's cost lever: an exact rule match needs no model call."""
        seed(session)
        transport = FakeTransport(reply="SAFE")
        deps, _, ledger = _deps(session, transport=transport)

        result = graph.run_attempt(
            deps, session_id=uuid.uuid4(), problem=PROBLEM, student_answer="2"
        )

        assert result.diagnosis.tag == "subtracted_instead_of_added"
        assert result.diagnosis.source is DiagnosisSource.RULE
        # Only the hint generation and leak check reached the model — not diagnosis.
        assert {c.stage for c in ledger.calls} == {
            PipelineStage.GENERATE_HINT,
            PipelineStage.LEAK_CHECK,
        }

    def test_a_cleared_hint_is_returned_with_its_checker_version(self, session: DbSession) -> None:
        seed(session)
        deps, _, _ = _deps(session, transport=FakeTransport(reply="SAFE"))

        result = graph.run_attempt(
            deps, session_id=uuid.uuid4(), problem=PROBLEM, student_answer="2"
        )

        assert result.hint_text
        assert result.leak_check is not None
        assert result.leak_check.passed is True
        assert result.escalated is False


class TestLeakGuardrail:
    """The rules only the graph can enforce."""

    def test_a_leaking_hint_never_reaches_the_child(self, session: DbSession) -> None:
        """The generator returns the answer; the graph must refuse it."""
        seed(session)
        # The model always emits the answer, and the classifier always says LEAK.
        deps, sink, _ = _deps(session, transport=FakeTransport(reply="The answer is 12."))

        result = graph.run_attempt(
            deps, session_id=uuid.uuid4(), problem=PROBLEM, student_answer="2"
        )

        # A template was substituted, and whatever is returned is leak-free.
        if result.hint_text is not None:
            assert "12" not in result.hint_text
            assert result.hint_source is HintSource.TEMPLATE_FALLBACK
        else:
            assert result.escalated is True

        failures = [e for e in sink.events if e.event_type.value == "stage_failed"]
        assert any(e.stage is PipelineStage.LEAK_CHECK for e in failures)

    def test_never_a_third_free_generation(self, session: DbSession) -> None:
        """§3.3: two attempts, then a pre-approved template."""
        seed(session)
        deps, _, ledger = _deps(session, transport=FakeTransport(reply="The answer is 12."))

        graph.run_attempt(deps, session_id=uuid.uuid4(), problem=PROBLEM, student_answer="2")

        generations = [c for c in ledger.calls if c.stage is PipelineStage.GENERATE_HINT]
        assert len(generations) <= graph.MAX_GENERATION_ATTEMPTS

    def test_escalates_when_no_hint_can_be_cleared(self, session: DbSession) -> None:
        """A problem whose answer equals an operand: every template would leak."""
        seed(session)
        impossible = Problem(
            prompt="What is 7 + 0?",
            correct_answer="7",
            grade_band=GradeBand.K_1,
            operands={"a": "7", "b": "0"},
        )
        deps, sink, _ = _deps(session, transport=FakeTransport(reply="The answer is 7."))

        result = graph.run_attempt(
            deps, session_id=uuid.uuid4(), problem=impossible, student_answer="0"
        )

        assert result.hint_text is None
        assert result.escalated is True
        assert any(e.event_type.value == "escalated" for e in sink.events)


class TestProviderDown:
    """M0's exit criterion: every stage falls back cleanly."""

    def test_whole_attempt_completes_with_the_provider_failing(self, session: DbSession) -> None:
        seed(session)
        deps, sink, ledger = _deps(
            session, transport=FakeTransport(raises=TransportError("provider unreachable"))
        )

        result = graph.run_attempt(
            deps, session_id=uuid.uuid4(), problem=PROBLEM, student_answer="2"
        )

        # A child still gets a hint.
        assert result.hint_text
        assert result.hint_source is HintSource.TEMPLATE_FALLBACK
        assert result.ran_degraded is True
        assert "generate" in result.degraded_stages

        # And the failed calls are still ledgered (M0.4).
        assert ledger.calls
        assert all(c.output_payload.get("error") for c in ledger.calls)

        # The record says plainly that it ran degraded (§8).
        assert any(e.event_type.value == "fallback_used" for e in sink.events)

    def test_no_model_configured_is_not_an_error(self, session: DbSession) -> None:
        """Shadow mode: no provider at all, and the pipeline still serves."""
        seed(session)
        deps, _, _ = _deps(session, transport=None)

        result = graph.run_attempt(
            deps, session_id=uuid.uuid4(), problem=PROBLEM, student_answer="2"
        )

        assert result.hint_text
        assert result.hint_source is HintSource.TEMPLATE_FALLBACK
        assert result.leak_check is not None
        # The classifier layer did not run, and the record does not pretend it did.
        assert result.leak_check.classifier_ran is False


class TestGrading:
    def test_correct_answer_grades_one(self, session: DbSession) -> None:
        seed(session)
        deps, _, _ = _deps(session, symbolic=InProcessSymbolicChecker())

        result = graph.grade_answer(
            deps, session_id=uuid.uuid4(), problem=PROBLEM, student_answer="12"
        )
        assert result.score == 1.0

    def test_equivalent_form_grades_correct(self, session: DbSession) -> None:
        """§3.5's worked example, through the whole stack."""
        seed(session)
        deps, _, _ = _deps(session, symbolic=InProcessSymbolicChecker())
        halves = Problem(
            prompt="What is half of 1?",
            correct_answer="1/2",
            grade_band=GradeBand.K_1,
            operands={"a": "1", "b": "2"},
        )
        assert (
            graph.grade_answer(
                deps, session_id=uuid.uuid4(), problem=halves, student_answer="4/8"
            ).score
            == 1.0
        )

    def test_correct_after_a_diagnosed_gap_routes_to_review(self, session: DbSession) -> None:
        """§3.5's consistency check."""
        from services.orchestrator.stages.diagnose import Diagnosis

        seed(session)
        deps, _, _ = _deps(session, symbolic=InProcessSymbolicChecker())
        result = graph.grade_answer(
            deps,
            session_id=uuid.uuid4(),
            problem=PROBLEM,
            student_answer="12",
            prior_diagnosis=Diagnosis(
                tag="subtracted_instead_of_added",
                confidence=0.99,
                evidence="",
                source=DiagnosisSource.RULE,
            ),
        )
        assert result.score == 1.0
        assert result.needs_review is True

    def test_no_checker_escalates_rather_than_guessing(self, session: DbSession) -> None:
        """Guessing at a child's grade is not an acceptable degradation."""
        seed(session)
        deps, _, _ = _deps(session, symbolic=None)

        result = graph.grade_answer(
            deps, session_id=uuid.uuid4(), problem=PROBLEM, student_answer="12"
        )
        assert result.needs_review is True


class TestDiagnosisThreshold:
    def test_low_confidence_becomes_unknown(self, session: DbSession) -> None:
        """§3.1: a confident wrong tag is worse than no tag."""
        seed(session)
        transport = FakeTransport(
            reply='{"tag": "subtracted_instead_of_added", "confidence": 0.2}',
            usage=TokenUsage(input_tokens=10, output_tokens=5),
        )
        deps, _, _ = _deps(session, transport=transport)

        # An answer no rule recognises, so the model path runs.
        result = graph.run_attempt(
            deps, session_id=uuid.uuid4(), problem=PROBLEM, student_answer="75"
        )
        assert result.diagnosis.tag == "unknown"

    @pytest.mark.parametrize("reply", ["not json at all", "", "{}"])
    def test_unparseable_reply_becomes_unknown(self, session: DbSession, reply: str) -> None:
        seed(session)
        deps, _, _ = _deps(session, transport=FakeTransport(reply=reply))

        result = graph.run_attempt(
            deps, session_id=uuid.uuid4(), problem=PROBLEM, student_answer="75"
        )
        assert result.diagnosis.tag == "unknown"
