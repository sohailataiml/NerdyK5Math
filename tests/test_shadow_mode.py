"""Phase 0 shadow mode (P0.4).

The defining property, and the one every test here circles: **generated text
never reaches the child.** The full model pipeline runs, its output is recorded
for teacher rating, and the hint shown comes from the template library.

Implementation-Plan.md's argument for this is worth restating, because the
temptation is to skip it and launch behind a confidence threshold: a threshold
picked before there is labelled data is a guess, and the first population it
would be tested on is children. Shadow mode costs one phase and buys a calibrated
gate, a real leak corpus, and a documented answer to "how do you know it works".
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

from sqlalchemy.orm import Session as DbSession

from packages.curriculum.seed import seed
from packages.domain.enums import GradeBand, HintSource
from packages.llm import LLMClient
from packages.llm.errors import TransportError
from packages.llm.fake import FakeTransport
from packages.llm.ledger import InMemoryLedger
from packages.prompts import PromptRegistry
from packages.telemetry import EventRecorder, InMemoryEventSink
from services.orchestrator import graph
from services.orchestrator.shadow import InMemoryShadowSink
from services.orchestrator.state import PipelineDeps, Problem

PROBLEM = Problem(
    prompt="What is 7 + 5?",
    correct_answer="12",
    grade_band=GradeBand.K_1,
    operands={"a": "7", "b": "5"},
)


DEFAULT_HINT = "Fill your ten-frame with 7 counters. How many more to make ten?"


def _pipeline_responder(hint: str) -> Callable[[str, str], str]:
    """Answer each stage in its own contract.

    The leak-check classifier is genuinely asked for a verdict, so it is given
    one — and the deterministic layer still runs first, which is what makes the
    leaking-hint test meaningful rather than fixture-driven.
    """

    def respond(system: str, _user: str) -> str:
        if "gives away the answer" in system:
            return "SAFE"
        return hint

    return respond


def _shadow_deps(
    db: DbSession, *, reply: str = DEFAULT_HINT
) -> tuple[PipelineDeps, InMemoryShadowSink, InMemoryLedger]:
    sink = InMemoryEventSink()
    ledger = InMemoryLedger()
    shadow = InMemoryShadowSink()
    deps = PipelineDeps(
        recorder=EventRecorder(sink, uuid.uuid4()),
        prompts=PromptRegistry(),
        llm=LLMClient(FakeTransport(responder=_pipeline_responder(reply)), ledger),
        db=db,
        shadow_mode=True,
        shadow_sink=shadow,
    )
    return deps, shadow, ledger


class TestGeneratedTextNeverReachesTheChild:
    def test_shown_hint_is_always_a_template(self, session: DbSession) -> None:
        seed(session)
        generated = "This is the model's hint and it must not be displayed."
        deps, shadow, _ = _shadow_deps(session, reply=generated)

        result = graph.run_attempt(
            deps, session_id=uuid.uuid4(), problem=PROBLEM, student_answer="2"
        )

        assert result.shadow_ran is True
        assert result.hint_source is HintSource.TEMPLATE_FALLBACK
        assert result.hint_text != generated
        # And the model's output was captured rather than discarded.
        assert len(shadow.candidates) == 1
        assert shadow.candidates[0].generated_text == generated

    def test_a_leaking_generation_still_never_reaches_the_child(self, session: DbSession) -> None:
        """Shadow mode is not a reason to relax the guardrail — but it is also
        not the guardrail. The child is safe because a template is served, and
        the leak is recorded as evidence for P0.5."""
        seed(session)
        deps, shadow, _ = _shadow_deps(session, reply="The answer is 12.")

        result = graph.run_attempt(
            deps, session_id=uuid.uuid4(), problem=PROBLEM, student_answer="2"
        )

        assert result.hint_text is not None
        assert "12" not in result.hint_text
        candidate = shadow.candidates[0]
        assert candidate.leak_check_passed is False
        assert candidate.leak_reason

    def test_the_record_says_why_a_template_was_served(self, session: DbSession) -> None:
        """A shadow-mode template and one forced by two leak-check failures look
        identical in the timeline unless the reason distinguishes them — and
        reading one as the other badly misstates what happened to a child."""
        seed(session)
        sink = InMemoryEventSink()
        shadow = InMemoryShadowSink()
        deps = PipelineDeps(
            recorder=EventRecorder(sink, uuid.uuid4()),
            prompts=PromptRegistry(),
            llm=LLMClient(
                FakeTransport(responder=_pipeline_responder(DEFAULT_HINT)), InMemoryLedger()
            ),
            db=session,
            shadow_mode=True,
            shadow_sink=shadow,
        )

        graph.run_attempt(deps, session_id=uuid.uuid4(), problem=PROBLEM, student_answer="2")

        fallbacks = [e for e in sink.events if e.event_type.value == "fallback_used"]
        reasons = [str(e.detail.get("reason", "")) for e in fallbacks]
        assert any("shadow mode" in r for r in reasons)
        assert not any("leak-check failures" in r for r in reasons)

    def test_shadow_mode_is_not_reported_as_degradation(self, session: DbSession) -> None:
        """Every Phase 0 session serves a template by design. Counting that as
        degradation would put the whole pilot in the same bucket as a provider
        outage, and hide the real outages inside the noise."""
        seed(session)
        deps, _, _ = _shadow_deps(session)

        result = graph.run_attempt(
            deps, session_id=uuid.uuid4(), problem=PROBLEM, student_answer="2"
        )

        assert result.shadow_ran is True
        assert "generate" not in result.degraded_stages


class TestEvidenceIsCaptured:
    """The phase's output is the record, so what gets recorded is the deliverable."""

    def test_candidate_carries_everything_a_rating_needs(self, session: DbSession) -> None:
        seed(session)
        deps, shadow, _ = _shadow_deps(session)

        result = graph.run_attempt(
            deps, session_id=uuid.uuid4(), problem=PROBLEM, student_answer="2"
        )

        candidate = shadow.candidates[0]
        assert candidate.misconception_tag == "subtracted_instead_of_added"
        assert candidate.prompt_version  # §8 segments quality by this
        assert candidate.llm_call_id is not None  # joins to the M0.4 ledger
        assert candidate.leak_checker_version  # which checker cleared it
        # The comparison the rating is actually about: without the shown hint a
        # teacher is judging the generated one in a vacuum.
        assert candidate.shown_text == result.hint_text
        assert candidate.shown_text != candidate.generated_text

    def test_the_leak_checker_is_exercised_on_real_generated_output(
        self, session: DbSession
    ) -> None:
        """P0.5's gate is the checker holding against real model output.

        Hand-written attacks cannot tell you that; only running it on what the
        model actually produces can, which is why shadow mode leak-checks the
        candidate it will never show.
        """
        seed(session)
        deps, shadow, ledger = _shadow_deps(session)

        graph.run_attempt(deps, session_id=uuid.uuid4(), problem=PROBLEM, student_answer="2")

        assert shadow.candidates[0].leak_check_passed is True
        # Generation and the leak-check classifier both ran.
        assert len(ledger.calls) >= 2


class TestDegradation:
    def test_provider_failure_in_shadow_still_serves_a_hint(self, session: DbSession) -> None:
        """Shadow mode must not make an outage worse: no candidate to rate, but
        the child's experience is unchanged because it was a template anyway."""
        seed(session)
        sink = InMemoryEventSink()
        shadow = InMemoryShadowSink()
        deps = PipelineDeps(
            recorder=EventRecorder(sink, uuid.uuid4()),
            prompts=PromptRegistry(),
            llm=LLMClient(FakeTransport(raises=TransportError("provider down")), InMemoryLedger()),
            db=session,
            shadow_mode=True,
            shadow_sink=shadow,
        )

        result = graph.run_attempt(
            deps, session_id=uuid.uuid4(), problem=PROBLEM, student_answer="2"
        )

        assert result.hint_text
        assert result.hint_source is HintSource.TEMPLATE_FALLBACK
        assert shadow.candidates == []  # nothing generated, so nothing to rate

    def test_shadow_mode_off_restores_generated_hints(self, session: DbSession) -> None:
        """The switch that ends Phase 0 — one deployment setting, not a per-call
        parameter something could flip by accident."""
        seed(session)
        deps, shadow, _ = _shadow_deps(session, reply="A safe generated hint about counters.")
        deps.shadow_mode = False

        result = graph.run_attempt(
            deps, session_id=uuid.uuid4(), problem=PROBLEM, student_answer="2"
        )

        assert result.shadow_ran is False
        assert result.hint_source is HintSource.GENERATED
        assert shadow.candidates == []
