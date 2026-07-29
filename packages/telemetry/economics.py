"""What the pipeline costs and how long it takes (§8, P1.10).

Read entirely from the `LLMCall` ledger, which has carried cost, latency, tokens,
model id, and prompt version on every call since M0.4. Nothing here needed new
instrumentation; the ledger was built for exactly this question and this is the
first thing to ask it.

**Segmented by prompt version, not only by stage.** §8 asks for that specifically
and §12 says why: a prompt edit can silently double spend or halve latency, and a
number averaged across versions hides the change that caused it. Stage totals
answer "where does the money go"; version rows answer "what did we just do to
it", which is the one an operator needs after a deploy.

Two honesty rules, both borrowed from the Phase 0 gate report:

- **A percentile with too few samples is not reported.** A p95 over nine calls is
  the second-slowest call wearing a statistic's name. Below `MIN_FOR_P95` the
  field is `None` and the sample size is published beside it, so a reader can see
  the difference between "fast" and "not yet known".
- **Deterministic stages are absent, not free.** The rule pre-check, the keyed
  lookup and the template library make no model call, so they have no ledger row
  and cannot appear here. That is worth stating because their absence is what
  makes the totals look small — most of what this system does costs nothing.

Aggregation happens in Python rather than SQL because percentile functions are
not portable across SQLite and Postgres, and the pilot's volumes are trivial. At
a scale where that stops being true, this becomes a `GROUP BY` and a
`percentile_cont`; the shape of the answer does not change.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from packages.domain.enums import PipelineStage
from packages.domain.tables import LLMCallRow

MIN_FOR_P95 = 20
"""Below this, a 95th percentile describes the sample rather than the system."""


def _percentile(values: list[int], fraction: float) -> int:
    ordered = sorted(values)
    index = min(int(len(ordered) * fraction), len(ordered) - 1)
    return ordered[index]


@dataclass
class _Bucket:
    """An internal running tally. Mutable on purpose — everything this module
    returns is frozen; this is the adding-up, not the answer."""

    costs: list[float]
    latencies: list[int]
    tokens_in: int
    tokens_out: int
    models: set[str]


class Segment(BaseModel):
    """One slice of spend — a stage, or a stage at a given prompt version."""

    model_config = ConfigDict(frozen=True)

    label: str
    stage: str
    prompt_version: str | None
    """`None` on a stage row, which sums every version of that stage."""

    model_id: str
    calls: int
    cost_usd: float
    share_of_cost: float
    tokens_in: int
    tokens_out: int
    latency_p50_ms: int
    latency_p95_ms: int | None
    """`None` until there are `MIN_FOR_P95` calls. Reported as unknown rather
    than as a number that would be read as a measurement."""


class Economics(BaseModel):
    model_config = ConfigDict(frozen=True)

    total_cost_usd: float
    calls: int
    sessions: int

    cost_per_session_p50: float
    cost_per_session_max: float

    tokens_in: int
    tokens_out: int

    by_stage: list[Segment]
    by_prompt_version: list[Segment]
    """The §8 segmentation. Two rows for one stage means two prompt versions are
    live at once, which during a staged rollout is expected and worth seeing."""

    min_calls_for_p95: int = MIN_FOR_P95


def _segment(label: str, stage: str, version: str | None, bucket: _Bucket, total: float) -> Segment:
    cost = sum(bucket.costs)
    return Segment(
        label=label,
        stage=stage,
        prompt_version=version,
        model_id=sorted(bucket.models)[0] if bucket.models else "",
        calls=len(bucket.costs),
        cost_usd=cost,
        share_of_cost=(cost / total) if total else 0.0,
        tokens_in=bucket.tokens_in,
        tokens_out=bucket.tokens_out,
        latency_p50_ms=_percentile(bucket.latencies, 0.5) if bucket.latencies else 0,
        latency_p95_ms=(
            _percentile(bucket.latencies, 0.95) if len(bucket.latencies) >= MIN_FOR_P95 else None
        ),
    )


def economics(db: DbSession, *, limit: int = 5000) -> Economics:
    """Aggregate the ledger. `limit` caps the scan so a large table cannot
    turn an operator page into an outage."""
    rows = (
        db.execute(select(LLMCallRow).order_by(LLMCallRow.created_at.desc()).limit(limit))
        .scalars()
        .all()
    )

    by_stage: dict[str, _Bucket] = {}
    by_version: dict[tuple[str, str], _Bucket] = {}
    per_session: dict[str, float] = defaultdict(float)

    def bucket(store: dict[object, _Bucket], key: object) -> _Bucket:
        if key not in store:
            store[key] = _Bucket(costs=[], latencies=[], tokens_in=0, tokens_out=0, models=set())
        return store[key]

    total = 0.0
    for row in rows:
        stage = row.stage.value if isinstance(row.stage, PipelineStage) else str(row.stage)
        total += row.cost_usd
        per_session[str(row.session_id)] += row.cost_usd
        for store, key in ((by_stage, stage), (by_version, (stage, row.prompt_version))):
            slot = bucket(store, key)  # type: ignore[arg-type]
            slot.costs.append(row.cost_usd)
            slot.latencies.append(row.latency_ms)
            slot.models.add(row.model_id)
            slot.tokens_in += row.tokens_in
            slot.tokens_out += row.tokens_out

    session_costs = sorted(per_session.values())
    stages = [_segment(s, s, None, b, total) for s, b in by_stage.items()]
    versions = [_segment(f"{s} · {v}", s, v, b, total) for (s, v), b in by_version.items()]

    return Economics(
        total_cost_usd=total,
        calls=len(rows),
        sessions=len(per_session),
        cost_per_session_p50=(session_costs[len(session_costs) // 2] if session_costs else 0.0),
        cost_per_session_max=max(session_costs) if session_costs else 0.0,
        tokens_in=sum(b.tokens_in for b in by_stage.values()),
        tokens_out=sum(b.tokens_out for b in by_stage.values()),
        by_stage=sorted(stages, key=lambda s: -s.cost_usd),
        by_prompt_version=sorted(versions, key=lambda s: -s.cost_usd),
    )
