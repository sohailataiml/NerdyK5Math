"""Per-stage model and timeout configuration (M0.4).

Stage → tier assignments come straight from Architecture.md §9. Timeouts come
from §8's latency budget: the diagnose → retrieve → hint path has a sub-2s p95
target, so those stages get tight ceilings and fail over to their deterministic
fallbacks rather than blocking a child at the keyboard. Grading tolerates more.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from packages.domain.enums import PipelineStage
from packages.llm.models import ModelTier


class StageConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    tier: ModelTier
    timeout_s: float = Field(gt=0)
    max_tokens: int = Field(gt=0)
    max_retries: int = Field(ge=0)


STAGE_CONFIG: dict[PipelineStage, StageConfig] = {
    # Sub-2s path (§8). Tight timeouts: a slow diagnosis should degrade to the
    # rule pre-check, not stall the hint.
    PipelineStage.DIAGNOSE: StageConfig(
        tier=ModelTier.FAST, timeout_s=5.0, max_tokens=1024, max_retries=1
    ),
    PipelineStage.RERANK: StageConfig(
        tier=ModelTier.FAST, timeout_s=3.0, max_tokens=512, max_retries=1
    ),
    PipelineStage.GENERATE_HINT: StageConfig(
        tier=ModelTier.BALANCED, timeout_s=12.0, max_tokens=2048, max_retries=1
    ),
    # The leak-checker is on the critical path for every hint and must never be
    # the reason a hint is slow — a timeout here fails closed to a template.
    PipelineStage.LEAK_CHECK: StageConfig(
        tier=ModelTier.FAST, timeout_s=4.0, max_tokens=256, max_retries=1
    ),
    # Grading is off the real-time path (§8) and gets room for two-pass rubric
    # work plus per-criterion evidence spans.
    #
    # The DEEP tier budgets are large for a reason: on the current Opus model
    # thinking is ON when the request doesn't say otherwise, and `max_tokens`
    # caps thinking *plus* response text. A budget sized for the answer alone
    # truncates mid-response once thinking consumes part of it. Rubric grading
    # is exactly where reasoning earns its cost, so the budget goes up rather
    # than the thinking going off.
    PipelineStage.GRADE: StageConfig(
        tier=ModelTier.DEEP, timeout_s=60.0, max_tokens=16000, max_retries=2
    ),
    PipelineStage.SAFETY_SCREEN: StageConfig(
        tier=ModelTier.FAST, timeout_s=5.0, max_tokens=512, max_retries=2
    ),
    PipelineStage.TEACHER_SUMMARY: StageConfig(
        tier=ModelTier.DEEP, timeout_s=45.0, max_tokens=8000, max_retries=2
    ),
}


def config_for(stage: PipelineStage) -> StageConfig:
    return STAGE_CONFIG[stage]
