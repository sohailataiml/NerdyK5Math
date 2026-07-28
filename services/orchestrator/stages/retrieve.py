"""Curriculum retrieval (§3.2).

**Currently the keyed lookup only.** §3.2's design is a structured filter, then
embedding similarity, then an LLM rerank that reads the diagnosis evidence and
attempt history. None of that is built: curriculum embeddings are `NULL` because
the embedding model has not been chosen (Implementation-Plan.md §7 open
question), and P1.3 is where the RAG path lands.

So this stage is honest about being the degradation path doing double duty. It
returns teacher-approved strategies via exact-match join, and reports
`used_fallback=True` when a tag has no mapping — which means the §8 dashboards
show current coverage rather than implying a retrieval quality that does not
exist yet.

§3.2's absolute rule holds regardless of which path runs: generation never
proceeds on empty retrieval, because that is the condition under which models
invent curriculum. A miss returns the general-purpose strategy.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.orm import Session as DbSession

from packages.domain.enums import PipelineStage
from packages.fallbacks import GENERAL_STRATEGY, lookup
from services.orchestrator.state import PipelineDeps, Problem, StageOutcome


@dataclass(frozen=True)
class Retrieval:
    node_id: UUID | None
    strategies: tuple[str, ...]

    @property
    def primary(self) -> str:
        return self.strategies[0] if self.strategies else GENERAL_STRATEGY


def run(deps: PipelineDeps, *, tag: str, problem: Problem) -> StageOutcome[Retrieval]:
    deps.recorder.stage_started(PipelineStage.RERANK, tag=tag)

    if not isinstance(deps.db, DbSession):
        # No database — the general strategy is still a real teaching move, so a
        # child gets a usable hint rather than an error.
        deps.recorder.fallback_used(PipelineStage.RERANK, reason="no curriculum database")
        return StageOutcome(
            value=Retrieval(node_id=None, strategies=(GENERAL_STRATEGY,)),
            used_fallback=True,
            reason="no curriculum database",
        )

    result = lookup(deps.db, tag_label=tag, grade_band=problem.grade_band)
    retrieval = Retrieval(node_id=result.node_id, strategies=result.strategies)

    if result.used_fallback:
        deps.recorder.fallback_used(
            PipelineStage.RERANK, reason=result.reason or "no mapped curriculum node"
        )
        return StageOutcome(value=retrieval, used_fallback=True, reason=result.reason)

    deps.recorder.stage_completed(
        PipelineStage.RERANK, node_id=str(result.node_id), strategies=len(result.strategies)
    )
    return StageOutcome(value=retrieval)
