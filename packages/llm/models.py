"""Model tiers and pricing (M0.4).

Architecture.md §9 assigns a *tier* per stage rather than a model ID, because
tiering is the dominant cost lever and the concrete model behind each tier
changes over time. Stages name a tier; this module resolves it.

Pricing is here so `LLMCall.cost_usd` is computed from one table rather than
estimated at call sites. §8 alerts on cost-per-session regression, and an
alert built on a stale rate is worse than no alert.
"""

from __future__ import annotations

import datetime as dt
import re
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class ModelTier(StrEnum):
    """Per-stage tiers from Architecture.md §9.

    FAST covers diagnosis, retrieval reranking, leak-checking, and safety
    screening — high call volume, low per-call difficulty. BALANCED generates
    hints. DEEP is reserved for rubric grading and teacher summaries.
    """

    FAST = "fast"
    BALANCED = "balanced"
    DEEP = "deep"


TIER_MODELS: dict[ModelTier, str] = {
    ModelTier.FAST: "claude-haiku-4-5",
    ModelTier.BALANCED: "claude-sonnet-5",
    ModelTier.DEEP: "claude-opus-5",
}


class Price(BaseModel):
    """USD per million tokens, with optional promotional rates.

    `intro_*` applies through `intro_until` inclusive. Modelling the promotion
    rather than hardcoding one number keeps historical `LLMCall.cost_usd` values
    correct: a session costed during the promotion should not be re-derived at
    list price afterwards.
    """

    model_config = ConfigDict(frozen=True)

    input_per_mtok: Decimal
    output_per_mtok: Decimal
    intro_input_per_mtok: Decimal | None = None
    intro_output_per_mtok: Decimal | None = None
    intro_until: dt.date | None = None

    def rates(self, on: dt.date) -> tuple[Decimal, Decimal]:
        promo_active = (
            self.intro_until is not None
            and on <= self.intro_until
            and self.intro_input_per_mtok is not None
            and self.intro_output_per_mtok is not None
        )
        if promo_active:
            assert self.intro_input_per_mtok is not None  # narrowed by promo_active
            assert self.intro_output_per_mtok is not None
            return self.intro_input_per_mtok, self.intro_output_per_mtok
        return self.input_per_mtok, self.output_per_mtok


PRICING: dict[str, Price] = {
    "claude-haiku-4-5": Price(input_per_mtok=Decimal("1.00"), output_per_mtok=Decimal("5.00")),
    "claude-sonnet-5": Price(
        input_per_mtok=Decimal("3.00"),
        output_per_mtok=Decimal("15.00"),
        intro_input_per_mtok=Decimal("2.00"),
        intro_output_per_mtok=Decimal("10.00"),
        intro_until=dt.date(2026, 8, 31),
    ),
    "claude-opus-5": Price(input_per_mtok=Decimal("5.00"), output_per_mtok=Decimal("25.00")),
}

CACHE_WRITE_MULTIPLIER = Decimal("1.25")
"""Cache writes bill at 1.25x input rate (5-minute TTL)."""

CACHE_READ_MULTIPLIER = Decimal("0.10")
"""Cache reads bill at ~0.1x input rate — the reason hint caching (P1.5) pays off."""

_PER_MILLION = Decimal(1_000_000)


class TokenUsage(BaseModel):
    """Token counts as reported by the API.

    `input_tokens` is the *uncached remainder* — total prompt size is the sum of
    all three input fields. Costing only `input_tokens` silently under-reports
    every cached request.
    """

    model_config = ConfigDict(frozen=True)

    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cache_read_tokens: int = Field(default=0, ge=0)
    cache_write_tokens: int = Field(default=0, ge=0)


class UnknownModelError(KeyError):
    """Raised when a model has no pricing entry.

    Deliberately fatal rather than defaulting to zero: a silently-free model is
    a cost dashboard that reads correct while under-reporting spend.
    """


_SNAPSHOT_SUFFIX = re.compile(r"-\d{8}$")


def pricing_key(model_id: str) -> str:
    """Resolve a model ID to its pricing key.

    Requests name an alias (`claude-haiku-4-5`) but responses report the dated
    snapshot that actually served them (`claude-haiku-4-5-20251001`). Costing
    the *served* model is the correct behaviour — it's what the bill reflects —
    so the lookup tolerates the suffix instead of the ledger rejecting every
    real response.

    Only a trailing 8-digit date is stripped, so a genuinely unknown model still
    raises rather than being silently coerced onto a neighbour's rates.
    """
    if model_id in PRICING:
        return model_id
    stripped = _SNAPSHOT_SUFFIX.sub("", model_id)
    if stripped in PRICING:
        return stripped
    raise UnknownModelError(
        f"no pricing entry for {model_id!r}; add one to PRICING before using this model"
    )


def cost_usd(model_id: str, usage: TokenUsage, on: dt.date | None = None) -> Decimal:
    """Compute the billed cost of one call."""
    price = PRICING[pricing_key(model_id)]

    input_rate, output_rate = price.rates(on or dt.datetime.now(dt.UTC).date())
    return (
        Decimal(usage.input_tokens) * input_rate
        + Decimal(usage.cache_write_tokens) * input_rate * CACHE_WRITE_MULTIPLIER
        + Decimal(usage.cache_read_tokens) * input_rate * CACHE_READ_MULTIPLIER
        + Decimal(usage.output_tokens) * output_rate
    ) / _PER_MILLION
