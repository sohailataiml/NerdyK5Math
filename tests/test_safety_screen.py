"""The §7 distress screen (P1.8).

Two properties here are load-bearing, and they pull in opposite directions:

- `test_screening_without_a_destination_refuses_to_compose` — a screen that
  alerts nobody must be impossible to build, not merely discouraged.
- `test_a_classifier_outage_does_not_flag_everything` — and the fix for the
  first must not become a system that cries wolf during every provider outage,
  because a responder who dismisses a thousand alerts will dismiss the real one.

The rest guard the smaller decisions: that a flag never changes what the child
sees, that screening does not wait for a wrong answer, and that ordinary
frustration with maths is not treated as a welfare concern.
"""

from __future__ import annotations

import uuid

import pytest

from packages.domain.enums import EventType, GradeBand, PipelineStage, SafetyCategory
from packages.fallbacks.distress import screen
from packages.llm import InMemoryLedger, LLMClient
from packages.llm.errors import TransportError
from packages.llm.fake import FakeTransport
from packages.prompts import PromptRegistry
from packages.telemetry import EventRecorder, InMemoryEventSink
from services.orchestrator import graph
from services.orchestrator.stages import safety
from services.orchestrator.state import PipelineDeps, Problem, UnroutedSafetyScreenError

PROBLEM = Problem(prompt="What is 7 + 5?", correct_answer="12", grade_band=GradeBand.K_1)


class RecordingSink:
    """A responder that always succeeds, and remembers."""

    def __init__(self) -> None:
        self.alerts: list[dict[str, object]] = []

    def raise_alert(self, **kwargs: object) -> object:
        self.alerts.append(kwargs)
        return None


class FailingSink:
    def raise_alert(self, **kwargs: object) -> object:
        raise RuntimeError("pager unreachable")


def _deps(
    *,
    sink: object | None = None,
    responder: str | None = None,
    transport_error: bool = False,
) -> tuple[PipelineDeps, InMemoryEventSink]:
    events = InMemoryEventSink()
    llm: LLMClient | None = None
    if responder is not None or transport_error:
        transport = FakeTransport(
            responder=lambda stage, _prompt: _boom() if transport_error else (responder or "CLEAR")
        )
        llm = LLMClient(transport, InMemoryLedger())
    deps = PipelineDeps(
        recorder=EventRecorder(events, uuid.uuid4()),
        prompts=PromptRegistry(),
        llm=llm,
        screen_for_distress=sink is not None,
        safety_sink=sink,  # type: ignore[arg-type]
    )
    return deps, events


def _boom() -> str:
    raise TransportError("provider unavailable")


class TestUnroutedScreeningIsUnbuildable:
    def test_screening_without_a_destination_refuses_to_compose(self) -> None:
        """The central rule: no screen is better than a screen alerting nobody.

        No screen is a gap somebody owns. A screen wired to nothing produces
        green dashboards and the belief children are watched over, while every
        alert lands in a table nobody reads.
        """
        with pytest.raises(UnroutedSafetyScreenError) as caught:
            PipelineDeps(
                recorder=EventRecorder(InMemoryEventSink(), uuid.uuid4()),
                prompts=PromptRegistry(),
                screen_for_distress=True,
            )
        assert "appearance of coverage" in str(caught.value)

    def test_screening_off_is_allowed_and_silent(self) -> None:
        """An honest gap is a valid configuration; a pretend screen is not."""
        deps, events = _deps()
        outcome = safety.run(
            deps,
            session_id=uuid.uuid4(),
            student_id=uuid.uuid4(),
            problem=PROBLEM,
            student_answer="12",
        )
        assert outcome.value.flagged is False
        assert events.events == []


class TestDeterministicLayer:
    @pytest.mark.parametrize(
        ("text", "category"),
        [
            ("i want to die", SafetyCategory.SELF_HARM),
            ("12 and i want to hurt myself", SafetyCategory.SELF_HARM),
            ("nobody would miss me", SafetyCategory.SELF_HARM),
            ("my brother hits me", SafetyCategory.HARM_FROM_OTHERS),
            ("im scared to go home", SafetyCategory.HARM_FROM_OTHERS),
            ("i hate myself", SafetyCategory.HOPELESSNESS),
        ],
    )
    def test_explicit_statements_are_caught(self, text: str, category: SafetyCategory) -> None:
        verdict = screen(text)
        assert verdict.flagged is True
        assert verdict.category is category

    @pytest.mark.parametrize(
        "text",
        [
            "12",
            "i hate math",
            "this is killing me",
            "im so dumb at this",
            "this problem is impossible",
            "",
        ],
    )
    def test_ordinary_frustration_is_not_a_welfare_concern(self, text: str) -> None:
        """Over-flagging is not the safe direction. A responder sent fifty false
        alarms stops reading the fifty-first, which is the one that matters."""
        assert screen(text).flagged is False

    def test_adult_idiom_still_flags_for_this_age_group(self) -> None:
        """ "I want to die on this hill" is an adult phrase, and a seven-year-old
        is not using it. Adding a lookahead to exclude idioms would trade a
        vanishingly rare false positive for the ability to miss a real one, in
        the one place where a miss is not recoverable — so it flags, and a person
        spends ten seconds deciding it was nothing."""
        assert screen("i want to die on this hill lol").flagged is True

    def test_the_most_urgent_signal_wins_not_the_first(self) -> None:
        """Urgency must not depend on the order the child wrote their sentences."""
        verdict = screen("i hate myself and i want to die")
        assert verdict.category is SafetyCategory.SELF_HARM


class TestAlerting:
    def test_a_flag_raises_an_alert_carrying_the_child_s_identity(self) -> None:
        """The model judged text without knowing who wrote it; the adult who has
        to act needs to know exactly which child."""
        sink = RecordingSink()
        deps, _ = _deps(sink=sink)
        student_id = uuid.uuid4()

        safety.run(
            deps,
            session_id=uuid.uuid4(),
            student_id=student_id,
            problem=PROBLEM,
            student_answer="i want to die",
        )

        assert len(sink.alerts) == 1
        assert sink.alerts[0]["student_id"] == student_id
        assert sink.alerts[0]["excerpt"] == "i want to die"

    def test_the_event_log_does_not_carry_the_child_s_words(self) -> None:
        """A disclosure belongs in `safety_alert`, which has its own reader — not
        on every surface that renders a session timeline."""
        deps, events = _deps(sink=RecordingSink())
        safety.run(
            deps,
            session_id=uuid.uuid4(),
            student_id=uuid.uuid4(),
            problem=PROBLEM,
            student_answer="i want to die",
        )

        flags = [e for e in events.events if e.event_type is EventType.SAFETY_FLAG]
        assert len(flags) == 1
        assert "i want to die" not in str(flags[0].detail)
        assert flags[0].detail["category"] == "self_harm"

    def test_a_delivery_failure_is_recorded_not_swallowed(self) -> None:
        """ "The alert was raised" and "an adult was told" are different facts."""
        deps, events = _deps(sink=FailingSink())
        outcome = safety.run(
            deps,
            session_id=uuid.uuid4(),
            student_id=uuid.uuid4(),
            problem=PROBLEM,
            student_answer="i want to die",
        )

        assert outcome.value.flagged is True
        assert outcome.value.delivery_failed is True
        assert outcome.used_fallback is True
        fallbacks = [e for e in events.events if e.event_type is EventType.FALLBACK_USED]
        assert any("delivery" in str(e.detail) for e in fallbacks)


class TestClassifierLayer:
    def test_it_catches_what_the_patterns_cannot(self) -> None:
        sink = RecordingSink()
        deps, _ = _deps(sink=sink, responder="FLAG SELF_HARM")

        outcome = safety.run(
            deps,
            session_id=uuid.uuid4(),
            student_id=uuid.uuid4(),
            problem=PROBLEM,
            student_answer="i wanna dye and not come bak",
        )

        assert outcome.value.flagged is True
        assert outcome.value.classifier_ran is True
        assert len(sink.alerts) == 1

    def test_a_classifier_outage_does_not_flag_everything(self) -> None:
        """The counterweight to failing loudly.

        Flagging every attempt during an outage would bury the real alert under
        thousands of false ones — that is not failing safe, it is disabling the
        responder. The gap is recorded as degradation instead.
        """
        sink = RecordingSink()
        deps, events = _deps(sink=sink, transport_error=True)

        outcome = safety.run(
            deps,
            session_id=uuid.uuid4(),
            student_id=uuid.uuid4(),
            problem=PROBLEM,
            student_answer="12",
        )

        assert outcome.value.flagged is False
        assert sink.alerts == []
        assert outcome.used_fallback is True  # §8 must see the weaker protection
        assert any(e.event_type is EventType.FALLBACK_USED for e in events.events)

    def test_an_outage_does_not_disable_the_pattern_layer(self) -> None:
        """Degraded screening is still screening. The explicit cases must survive
        a provider outage, or an outage becomes an unscreened window."""
        sink = RecordingSink()
        deps, _ = _deps(sink=sink, transport_error=True)

        outcome = safety.run(
            deps,
            session_id=uuid.uuid4(),
            student_id=uuid.uuid4(),
            problem=PROBLEM,
            student_answer="i want to die",
        )

        assert outcome.value.flagged is True
        assert len(sink.alerts) == 1

    def test_an_unparseable_reply_is_not_turned_into_a_category(self) -> None:
        """Guessing would write a fabricated claim about a child into a welfare
        record — worse than recording that the layer failed."""
        sink = RecordingSink()
        deps, _ = _deps(sink=sink, responder="I'm not sure what you mean")

        outcome = safety.run(
            deps,
            session_id=uuid.uuid4(),
            student_id=uuid.uuid4(),
            problem=PROBLEM,
            student_answer="12",
        )

        assert outcome.value.flagged is False
        assert outcome.used_fallback is True
        assert sink.alerts == []


class TestTheChildSeesNoDifference:
    def test_a_flagged_session_still_gets_its_hint(self) -> None:
        """A machine that visibly stops after a child types something
        frightening teaches them not to type it again."""
        sink = RecordingSink()
        deps, _ = _deps(sink=sink)

        result = graph.run_attempt(
            deps,
            session_id=uuid.uuid4(),
            problem=PROBLEM,
            student_answer="2 i want to die",
            student_id=uuid.uuid4(),
        )

        assert result.hint_text  # the lesson continues, unchanged
        assert result.escalated is False
        assert result.safety_screen is not None
        assert result.safety_screen.flagged is True
        assert len(sink.alerts) == 1

    def test_screening_runs_before_the_answer_is_judged(self) -> None:
        """A child can type the right answer and something that matters in the
        same box; a screen gated on a wrong answer misses those children."""
        sink = RecordingSink()
        deps, events = _deps(sink=sink)

        graph.run_attempt(
            deps,
            session_id=uuid.uuid4(),
            problem=PROBLEM,
            student_answer="12 nobody would miss me",
            student_id=uuid.uuid4(),
        )

        assert len(sink.alerts) == 1
        ordered = [e.event_type for e in events.events]
        started = [
            index
            for index, event in enumerate(events.events)
            if event.event_type is EventType.STAGE_STARTED
            and event.stage is PipelineStage.SAFETY_SCREEN
        ]
        diagnose_at = [
            index
            for index, event in enumerate(events.events)
            if event.stage is PipelineStage.DIAGNOSE
        ]
        assert started and diagnose_at
        assert started[0] < diagnose_at[0], ordered
