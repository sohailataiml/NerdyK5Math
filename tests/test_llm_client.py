"""M0.4 — tiering, cost, and the ledger guarantee.

The load-bearing test here is `TestNoCallEscapesTheLedger`. Architecture.md §12
names nondeterminism as a standing risk and §5's `LLMCall` as the mitigation; a
mitigation with a hole in it on the failure path is worse than none, because the
gap only appears when something has already gone wrong.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

import pytest
from pydantic import ValidationError

from packages.domain.enums import GradeBand, PipelineStage
from packages.llm.client import LLMClient, PromptContext
from packages.llm.config import config_for
from packages.llm.errors import RefusalError, TransportError
from packages.llm.fake import FakeTransport
from packages.llm.ledger import InMemoryLedger
from packages.llm.models import (
    PRICING,
    TIER_MODELS,
    ModelTier,
    TokenUsage,
    UnknownModelError,
    cost_usd,
    pricing_key,
)
from packages.prompts.registry import RenderedPrompt

SESSION = uuid.uuid4()


def _context() -> PromptContext:
    return PromptContext(
        session_id=SESSION,
        grade_band=GradeBand.K_1,
        problem_prompt="What is 7 + 5?",
        correct_answer="12",
        student_answer="2",
        attempt_number=1,
    )


def _client(transport: FakeTransport) -> tuple[LLMClient, InMemoryLedger]:
    ledger = InMemoryLedger()
    return LLMClient(transport, ledger), ledger


def _prompt() -> RenderedPrompt:
    return RenderedPrompt(
        version="diagnose/K-1/v1",
        content_hash="deadbeefdeadbeef",
        system="You are a diagnostic classifier.",
        user="Diagnose the error.",
    )


def _complete(client: LLMClient, stage: PipelineStage = PipelineStage.DIAGNOSE) -> object:
    return client.complete(stage=stage, context=_context(), prompt=_prompt())


class TestNoCallEscapesTheLedger:
    """M0.4's done-criterion: a stage cannot make a call that leaves no row."""

    def test_success_is_ledgered(self) -> None:
        client, ledger = _client(FakeTransport(reply="subtracted_instead_of_added"))
        _complete(client)

        assert len(ledger.calls) == 1
        call = ledger.calls[0]
        assert call.stage is PipelineStage.DIAGNOSE
        assert call.prompt_version == "diagnose/K-1/v1"
        assert call.session_id == SESSION

    def test_ledger_pins_the_exact_prompt_text(self) -> None:
        """M0.6: the version alone is not enough — the hash proves which wording
        produced this call even after the library moves on."""
        client, ledger = _client(FakeTransport())
        _complete(client)

        assert ledger.calls[0].input_payload["prompt_content_hash"] == "deadbeefdeadbeef"

    def test_transport_failure_is_ledgered(self) -> None:
        client, ledger = _client(FakeTransport(raises=TransportError("provider unreachable")))

        with pytest.raises(TransportError):
            _complete(client)

        assert len(ledger.calls) == 1
        assert ledger.calls[0].output_payload["error"] == "TransportError"

    def test_refusal_is_ledgered_with_its_token_spend(self) -> None:
        """A mid-stream refusal is billed, so it must not log as free."""
        refusal = RefusalError(
            "declined",
            usage=TokenUsage(input_tokens=310, output_tokens=12),
            model_id="claude-haiku-4-5",
        )
        client, ledger = _client(FakeTransport(raises=refusal))

        with pytest.raises(RefusalError):
            _complete(client, PipelineStage.SAFETY_SCREEN)

        call = ledger.calls[0]
        assert call.output_payload["refused"] is True
        assert call.tokens_in == 310
        assert call.cost_usd > 0

    def test_unexpected_exception_is_still_ledgered(self) -> None:
        """The `finally` covers anything out of the transport, not just our errors."""
        client, ledger = _client(FakeTransport(raises=RuntimeError("boom")))

        with pytest.raises(RuntimeError):
            _complete(client)

        assert len(ledger.calls) == 1
        assert ledger.calls[0].output_payload["error"] == "UnknownError"

    def test_result_references_its_ledger_row(self) -> None:
        client, ledger = _client(FakeTransport())
        result = _complete(client)

        assert result.llm_call_id == ledger.calls[0].id  # type: ignore[attr-defined]


class TestTiering:
    """§9's per-stage tiering is the dominant cost lever, so it is asserted."""

    @pytest.mark.parametrize(
        ("stage", "expected"),
        [
            (PipelineStage.DIAGNOSE, ModelTier.FAST),
            (PipelineStage.RERANK, ModelTier.FAST),
            (PipelineStage.LEAK_CHECK, ModelTier.FAST),
            (PipelineStage.SAFETY_SCREEN, ModelTier.FAST),
            (PipelineStage.GENERATE_HINT, ModelTier.BALANCED),
            (PipelineStage.GRADE, ModelTier.DEEP),
            (PipelineStage.TEACHER_SUMMARY, ModelTier.DEEP),
        ],
    )
    def test_stage_uses_its_configured_tier(
        self, stage: PipelineStage, expected: ModelTier
    ) -> None:
        transport = FakeTransport()
        client, _ = _client(transport)
        _complete(client, stage)

        assert transport.calls[0].model_id == TIER_MODELS[expected]

    def test_every_stage_has_a_config(self) -> None:
        """A stage without a config would KeyError at request time, in front of a student."""
        for stage in PipelineStage:
            assert config_for(stage) is not None

    def test_every_tier_maps_to_a_priced_model(self) -> None:
        for tier in ModelTier:
            assert TIER_MODELS[tier] in PRICING

    def test_realtime_stages_fit_the_latency_budget(self) -> None:
        """§8 budgets diagnose -> retrieve -> hint at sub-2s p95.

        Timeouts can't prove the budget is met, but they bound the worst case:
        if the ceilings alone exceed it, the budget is unmeetable by construction.
        """
        realtime = (PipelineStage.DIAGNOSE, PipelineStage.RERANK, PipelineStage.GENERATE_HINT)
        assert sum(config_for(s).timeout_s for s in realtime) <= 20.0

    def test_deep_tier_has_room_for_thinking(self) -> None:
        """Thinking is on by default on the deep-tier model and shares `max_tokens`
        with the response, so a budget sized for the answer alone truncates."""
        for stage in (PipelineStage.GRADE, PipelineStage.TEACHER_SUMMARY):
            assert config_for(stage).max_tokens >= 8000


class TestCost:
    def test_cost_uses_the_priced_rates(self) -> None:
        usage = TokenUsage(input_tokens=1_000_000, output_tokens=1_000_000)
        assert cost_usd("claude-haiku-4-5", usage, on=dt.date(2026, 7, 27)) == 6

    def test_cached_tokens_are_priced_differently(self) -> None:
        """Cache reads bill at ~0.1x — the reason hint caching pays off (P1.5)."""
        plain = TokenUsage(input_tokens=1_000_000, output_tokens=0)
        cached = TokenUsage(input_tokens=0, cache_read_tokens=1_000_000, output_tokens=0)

        full = cost_usd("claude-haiku-4-5", plain, on=dt.date(2026, 7, 27))
        discounted = cost_usd("claude-haiku-4-5", cached, on=dt.date(2026, 7, 27))
        assert discounted == full / 10

    def test_cache_writes_carry_a_premium(self) -> None:
        write = TokenUsage(input_tokens=0, cache_write_tokens=1_000_000, output_tokens=0)
        assert cost_usd("claude-haiku-4-5", write, on=dt.date(2026, 7, 27)) == Decimal("1.25")

    def test_promotional_rate_applies_before_expiry(self) -> None:
        usage = TokenUsage(input_tokens=1_000_000, output_tokens=0)
        during = cost_usd("claude-sonnet-5", usage, on=dt.date(2026, 8, 1))
        after = cost_usd("claude-sonnet-5", usage, on=dt.date(2026, 9, 1))

        assert during == 2
        assert after == 3

    def test_unpriced_model_is_fatal(self) -> None:
        """Defaulting to zero would make a cost dashboard under-report silently."""
        with pytest.raises(UnknownModelError):
            cost_usd("claude-not-a-real-model", TokenUsage(input_tokens=1, output_tokens=1))

    def test_dated_snapshot_prices_as_its_alias(self) -> None:
        """Requests name an alias; responses report the dated snapshot that served
        them. Costing the served model is correct, so the lookup must tolerate it."""
        usage = TokenUsage(input_tokens=1_000_000, output_tokens=0)
        alias = cost_usd("claude-haiku-4-5", usage, on=dt.date(2026, 7, 27))
        snapshot = cost_usd("claude-haiku-4-5-20251001", usage, on=dt.date(2026, 7, 27))

        assert snapshot == alias
        assert pricing_key("claude-haiku-4-5-20251001") == "claude-haiku-4-5"

    def test_unknown_model_with_a_date_suffix_is_still_fatal(self) -> None:
        """Stripping the suffix must not coerce a stranger onto a neighbour's rates."""
        with pytest.raises(UnknownModelError):
            pricing_key("claude-imaginary-9-20260101")


class TestPiiBoundary:
    """M0.10 — student identity is not representable in a prompt payload."""

    def test_student_identifiers_are_rejected(self) -> None:
        for field, value in [
            ("student_id", str(uuid.uuid4())),
            ("student_name", "Sam Rivera"),
            ("iep_flags", "extended_time"),
        ]:
            with pytest.raises(ValidationError):
                PromptContext(
                    session_id=SESSION,
                    grade_band=GradeBand.K_1,
                    problem_prompt="What is 7 + 5?",
                    **{field: value},
                )

    def test_ledger_payload_carries_no_student_identity(self) -> None:
        """A closed allowlist, so a future edit to `client.py` that starts
        recording something extra fails here rather than in a compliance review.

        `rendered_prompt` is on the list deliberately. It is the text that was
        actually sent, so recording it cannot expose anything that did not
        already cross the provider boundary — and §12's argument that a grade can
        be defended later needs the wording that produced it. Note what it does
        change: the context half of a payload is structurally safe because
        `PromptContext` forbids identity fields, while this half is safe only
        because no stage passes identity into `render()`. Anything added to this
        set wants that sentence answered for it too.
        """
        client, ledger = _client(FakeTransport())
        _complete(client)

        payload = ledger.calls[0].input_payload
        assert set(payload) <= {
            "session_id",
            "grade_band",
            "problem_prompt",
            "correct_answer",
            "student_answer",
            "attempt_number",
            "extra",
            "prompt_content_hash",
            "rendered_prompt",
            "max_tokens",
            "timeout_s",
        }

    def test_the_recorded_prompt_carries_only_the_text_that_was_sent(self) -> None:
        """No nesting beyond the two strings, so the field cannot become a place
        that other things get tucked into."""
        client, ledger = _client(FakeTransport())
        _complete(client)

        recorded = ledger.calls[0].input_payload["rendered_prompt"]
        assert isinstance(recorded, dict)
        assert set(recorded) == {"system", "user"}
        assert all(isinstance(value, str) for value in recorded.values())
