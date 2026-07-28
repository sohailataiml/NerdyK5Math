"""M0.12 — the deterministic degradation paths.

Two things are being proven:

1. **Every stage has a working non-model path**, and a session completes with the
   provider down (§4). That is the milestone's stated criterion.
2. **The template library cannot leak an answer.** §12 makes leakage the defining
   risk of this architecture, and these templates are what Phase 0 actually shows
   children — so the leak tests here carry the same weight as the ones guarding
   generated hints.
"""

from __future__ import annotations

import uuid
from fractions import Fraction

import pytest
from sqlalchemy.orm import Session as DbSession

from packages.domain.enums import GradeBand, PipelineStage
from packages.fallbacks import (
    GENERAL_STRATEGY,
    LIBRARY,
    TemplateError,
    check_hint,
    check_template_safety,
    diagnose,
    lookup,
    numbers_in,
    parse_number,
    render,
)
from packages.fallbacks.rules import RULES, TAG_OPERATIONS, applies_to
from packages.llm.client import LLMClient, PromptContext
from packages.llm.errors import TransportError
from packages.llm.fake import FakeTransport
from packages.llm.ledger import InMemoryLedger
from packages.prompts.registry import RenderedPrompt
from packages.telemetry import EventRecorder, InMemoryEventSink


class TestAnswerForms:
    """The reason string matching is not enough."""

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("12", Fraction(12)),
            ("12.0", Fraction(12)),
            ("twelve", Fraction(12)),
            ("1/2", Fraction(1, 2)),
            ("0.5", Fraction(1, 2)),
            ("half", Fraction(1, 2)),
            ("2/4", Fraction(1, 2)),
            ("three quarters", Fraction(3, 4)),
        ],
    )
    def test_written_forms_parse_to_the_same_value(self, text: str, expected: Fraction) -> None:
        assert parse_number(text) == expected

    def test_number_words_are_found_in_prose(self) -> None:
        assert Fraction(12) in numbers_in("you should end up with twelve counters")

    def test_multiword_fractions_are_not_split(self) -> None:
        """ "three quarters" is 3/4, not 3 and 4."""
        assert Fraction(3, 4) in numbers_in("that leaves three quarters of the bar")


class TestLeakDetection:
    """§3.3 layer 1. Biased toward false positives on purpose."""

    @pytest.mark.parametrize(
        "hint",
        [
            "The answer is 12.",
            "You end up with twelve.",
            "Count on and you get 12 counters.",
            "It comes to 12.0 altogether.",
        ],
    )
    def test_answer_in_any_form_is_caught(self, hint: str) -> None:
        assert check_hint(hint, "12").leaked is True

    def test_equivalent_fraction_is_caught(self) -> None:
        """A hint saying 2/4 gives away an answer of 1/2."""
        assert check_hint("that shades 2/4 of the bar", "1/2").leaked is True

    @pytest.mark.parametrize(
        "hint",
        [
            "You have 7 counters and you're getting 5 more. More or fewer?",
            "Start at 7 and hop forward 5 times. Where do you land?",
            "Fill a ten-frame with 7, then add the rest.",
        ],
    )
    def test_method_without_the_answer_is_safe(self, hint: str) -> None:
        """Showing the method is the hint doing its job, not a leak."""
        assert check_hint(hint, "12").leaked is False

    def test_non_numeric_answer_fails_closed(self) -> None:
        """This layer cannot verify an algebraic or written answer. Saying "safe"
        would be a guess dressed as a verdict, so it escalates instead."""
        verdict = check_hint("think about what balances the equation", "2x + 3")
        assert verdict.leaked is True
        assert verdict.reason is not None
        assert "classifier" in verdict.reason

    def test_slot_collision_is_reported_specifically(self) -> None:
        """The failure authoring-time review cannot catch: a template that is safe
        as written leaks once *this* problem's values go in."""
        verdict = check_template_safety(
            "Start at 12 and count on.", "12", problem_values={"a": "12", "b": "5"}
        )
        assert verdict.leaked is True
        assert verdict.reason is not None
        assert "equal to the answer" in verdict.reason


class TestTemplateLibrary:
    def test_every_template_renders_without_leaking(self) -> None:
        """The whole library, against every problem in the seed strand.

        This is the check that makes the library shippable: not "the ones I
        thought about", but every template crossed with every problem.
        """
        problems = [
            ("7", "5", "12"),
            ("8", "6", "14"),
            ("9", "4", "13"),
            ("13", "8", "5"),
            ("12", "5", "7"),
            ("15", "6", "9"),
        ]
        for template in LIBRARY:
            for a, b, answer in problems:
                result = render(
                    tag=template.tag,
                    grade_band=template.grade_band,
                    level=template.level,
                    values={"a": a, "b": b},
                    correct_answer=answer,
                )
                assert not check_hint(result.text, answer).leaked, (
                    f"{template.tag}/{template.level} leaked {answer} for {a},{b}: {result.text}"
                )

    def test_variants_rotate_across_attempts(self) -> None:
        """§12 staleness: the same misconception twice must not be word-for-word."""
        first = render(
            tag="subtracted_instead_of_added",
            grade_band=GradeBand.K_1,
            level=1,
            values={"a": "7", "b": "5"},
            correct_answer="12",
            attempt=1,
        )
        second = render(
            tag="subtracted_instead_of_added",
            grade_band=GradeBand.K_1,
            level=1,
            values={"a": "7", "b": "5"},
            correct_answer="12",
            attempt=2,
        )
        assert first.text != second.text

    def test_unknown_tag_falls_back_to_the_unknown_template(self) -> None:
        """§3.1 routes an unrecognised error here rather than guessing."""
        result = render(
            tag="a_tag_nobody_authored",
            grade_band=GradeBand.K_1,
            level=1,
            values={"a": "7", "b": "5"},
            correct_answer="12",
        )
        assert result.tag == "unknown"

    def test_render_refuses_rather_than_leaking(self) -> None:
        """If every phrasing would give the answer away, showing one anyway is
        never the better option — escalate instead."""
        with pytest.raises(TemplateError, match="would leak"):
            render(
                tag="subtracted_instead_of_added",
                grade_band=GradeBand.K_1,
                level=3,
                values={"a": "7", "b": "5"},
                correct_answer="7",  # a slot value equals the answer
            )

    def test_hint_levels_escalate_in_specificity(self) -> None:
        """§3.3: level 1 asks, level 3 works nearly all of it."""
        texts = [
            render(
                tag="subtracted_instead_of_added",
                grade_band=GradeBand.K_1,
                level=level,
                values={"a": "7", "b": "5"},
                correct_answer="12",
            ).text
            for level in (1, 2, 3)
        ]
        assert len(texts[0]) < len(texts[2])


class TestKeyedLookup:
    def test_unmapped_tag_returns_the_general_strategy(self, session: DbSession) -> None:
        """§3.2: never an empty retrieval — that is when models invent curriculum."""
        result = lookup(session, tag_label="not_a_real_tag", grade_band=GradeBand.K_1)
        assert result.used_fallback is True
        assert result.primary_strategy == GENERAL_STRATEGY

    def test_mapped_tag_returns_approved_strategies(self, session: DbSession) -> None:
        from packages.curriculum.seed import seed

        seed(session)
        result = lookup(session, tag_label="subtracted_instead_of_added", grade_band=GradeBand.K_1)
        assert result.used_fallback is False
        assert result.strategies


class TestProviderDown:
    """M0.12's criterion: a session completes with the provider unavailable."""

    def test_diagnosis_falls_back_to_the_rule_precheck(self) -> None:
        """The LLM path is unreachable; the rule engine still answers."""
        client = LLMClient(
            FakeTransport(raises=TransportError("provider unreachable")), InMemoryLedger()
        )
        with pytest.raises(TransportError):
            client.complete(
                stage=PipelineStage.DIAGNOSE,
                context=PromptContext(
                    session_id=uuid.uuid4(),
                    grade_band=GradeBand.K_1,
                    problem_prompt="What is 7 + 5?",
                ),
                prompt=RenderedPrompt(
                    version="diagnose/K-1/v2", content_hash="x", system="s", user="u"
                ),
            )

        result = diagnose("What is 7 + 5?", "2")
        assert result.tag == "subtracted_instead_of_added"

    def test_a_whole_session_completes_with_no_model_available(self, session: DbSession) -> None:
        """Diagnose, retrieve, hint, and leak-check, none of them reaching a model.

        §4 requires the pipeline to stay up when the provider is down — degraded,
        not dark. This is that claim, executed.
        """
        from packages.curriculum.seed import seed

        seed(session)
        sink = InMemoryEventSink()
        session_id = uuid.uuid4()
        recorder = EventRecorder(sink, session_id)

        problem, student_answer, correct = "What is 7 + 5?", "2", "12"
        recorder.session_started(problem=problem)
        recorder.answer_submitted(attempt_number=1, answer=student_answer)

        # 1. Diagnose — rules, no model.
        diagnosis = diagnose(problem, student_answer)
        recorder.fallback_used(PipelineStage.DIAGNOSE, reason="provider unavailable")
        assert diagnosis.tag == "subtracted_instead_of_added"

        # 2. Retrieve — keyed lookup, no embeddings.
        retrieval = lookup(session, tag_label=diagnosis.tag, grade_band=GradeBand.K_1)
        recorder.fallback_used(PipelineStage.RERANK, reason="provider unavailable")
        assert retrieval.primary_strategy

        # 3. Hint — template, no generation.
        hint = render(
            tag=diagnosis.tag,
            grade_band=GradeBand.K_1,
            level=1,
            values={"a": "7", "b": "5"},
            correct_answer=correct,
        )
        recorder.fallback_used(PipelineStage.GENERATE_HINT, reason="provider unavailable")

        # 4. Leak-check — deterministic layer, no classifier.
        verdict = check_hint(hint.text, correct)
        recorder.fallback_used(PipelineStage.LEAK_CHECK, reason="provider unavailable")
        assert verdict.leaked is False

        recorder.hint_shown(hint_level=1, source="template_fallback")
        recorder.session_completed(outcome="hint_shown")

        # The session ran, and the record says plainly that it ran degraded —
        # so a later quality dip is attributable rather than mysterious.
        fallbacks = [e for e in sink.events if e.event_type.value == "fallback_used"]
        assert len(fallbacks) == 4
        assert {e.stage for e in fallbacks} == {
            PipelineStage.DIAGNOSE,
            PipelineStage.RERANK,
            PipelineStage.GENERATE_HINT,
            PipelineStage.LEAK_CHECK,
        }

    def test_ledger_still_records_the_failed_call(self) -> None:
        """Degradation does not mean losing the audit trail (M0.4)."""
        ledger = InMemoryLedger()
        client = LLMClient(FakeTransport(raises=TransportError("down")), ledger)
        with pytest.raises(TransportError):
            client.complete(
                stage=PipelineStage.DIAGNOSE,
                context=PromptContext(
                    session_id=uuid.uuid4(),
                    grade_band=GradeBand.K_1,
                    problem_prompt="What is 7 + 5?",
                ),
                prompt=RenderedPrompt(
                    version="diagnose/K-1/v2", content_hash="x", system="s", user="u"
                ),
            )
        assert len(ledger.calls) == 1


class TestTagApplicability:
    """A tag from the taxonomy can still be nonsense for the problem in hand.

    Found by running a real session: the model diagnosed
    `counted_on_from_wrong_start` — an addition misconception — on `13 - 8`, and
    the level-2 template then told the child to put a finger on 13 and hop
    forward 8 times. Confining the diagnoser to the taxonomy is necessary and not
    sufficient.
    """

    def test_an_addition_tag_does_not_apply_to_subtraction(self) -> None:
        assert applies_to("counted_on_from_wrong_start", "What is 13 - 8?") is False

    def test_an_addition_tag_applies_to_addition(self) -> None:
        assert applies_to("counted_on_from_wrong_start", "What is 7 + 5?") is True

    def test_a_subtraction_tag_does_not_apply_to_addition(self) -> None:
        assert applies_to("added_instead_of_subtracted", "What is 7 + 5?") is False

    def test_an_unmapped_tag_is_unconstrained(self) -> None:
        """The taxonomy is larger than the set of exact arithmetic identities.

        Refusing every tag without a rule would discard the diagnoser's whole
        reason for existing — it is there for the cases rules cannot express.
        """
        assert applies_to("place_value_confusion", "What is 13 - 8?") is True

    def test_an_unparseable_problem_abstains(self) -> None:
        """No operation to contradict, so the check does not block."""
        assert applies_to("counted_on_from_wrong_start", "Which shape has 4 sides?") is True

    def test_the_map_is_derived_from_the_rules(self) -> None:
        """Written by hand, this would drift from RULES within one change."""
        for rule in RULES:
            assert rule.operation in TAG_OPERATIONS[rule.tag]
