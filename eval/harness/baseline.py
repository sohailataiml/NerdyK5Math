"""Baselines and the regression gate (M0.5).

Implementation-Plan.md §3 states the merge rule this implements: *a prompt or
model change merges only after an eval run showing no regression.* §12 names
silent quality drift as a standing risk, and this is the only thing that catches
it — a prompt edit can degrade hints in ways no unit test sees and no student
reports.

The gate has one refusal that matters more than any threshold: **it will not
compare across dataset versions.** A score measured on a different example set
is not a comparison, and a gate that quietly performs one converts a dataset
edit into a phantom improvement. That case is an error, not a pass.
"""

from __future__ import annotations

import datetime as dt
import json
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class Direction(StrEnum):
    HIGHER_IS_BETTER = "higher_is_better"
    LOWER_IS_BETTER = "lower_is_better"


class MetricSpec(BaseModel):
    """How to judge one metric.

    `tolerance` absorbs the run-to-run noise inherent in sampling a model, so
    the gate fires on drift rather than on jitter. `minimum`/`maximum` are
    absolute floors that hold regardless of the baseline — a metric can be
    "no worse than last time" and still be unacceptable.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    direction: Direction
    tolerance: float = Field(default=0.02, ge=0.0)
    minimum: float | None = None
    maximum: float | None = None


class Verdict(StrEnum):
    PASS = "pass"
    REGRESSED = "regressed"
    BELOW_FLOOR = "below_floor"
    ABOVE_CEILING = "above_ceiling"
    NEW = "new"


class MetricVerdict(BaseModel):
    model_config = ConfigDict(frozen=True)

    metric: str
    verdict: Verdict
    current: float
    baseline: float | None
    detail: str

    @property
    def ok(self) -> bool:
        return self.verdict in (Verdict.PASS, Verdict.NEW)


class Baseline(BaseModel):
    """The recorded reference point for one suite."""

    model_config = ConfigDict(frozen=True)

    suite: str
    dataset_name: str
    dataset_version: str
    dataset_hash: str
    prompt_version: str
    model_id: str
    metrics: dict[str, float]
    recorded_at: dt.datetime

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.model_dump_json(indent=2) + "\n", encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> Baseline | None:
        if not path.exists():
            return None
        return cls.model_validate_json(path.read_text(encoding="utf-8"))


class DatasetMismatchError(RuntimeError):
    """The current run and the baseline were measured on different data."""


class GateReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    suite: str
    verdicts: tuple[MetricVerdict, ...]

    @property
    def passed(self) -> bool:
        return all(v.ok for v in self.verdicts)

    def render(self) -> str:
        width = max((len(v.metric) for v in self.verdicts), default=10)
        lines = []
        for v in self.verdicts:
            mark = "PASS" if v.ok else "FAIL"
            lines.append(f"  [{mark}] {v.metric:<{width}}  {v.detail}")
        return "\n".join(lines)


def check(
    *,
    suite: str,
    current: dict[str, float],
    specs: tuple[MetricSpec, ...],
    dataset_hash: str,
    baseline: Baseline | None,
) -> GateReport:
    """Compare a run against its baseline.

    Raises `DatasetMismatchError` rather than returning a failing report when the
    dataset moved — a mismatch is not a quality signal in either direction, and
    reporting it as a failure would train people to override it.
    """
    if baseline is not None and baseline.dataset_hash != dataset_hash:
        raise DatasetMismatchError(
            f"suite {suite!r} baseline was measured on dataset {baseline.dataset_hash} "
            f"but this run used {dataset_hash}. Re-record the baseline on the current "
            f"dataset instead of comparing across versions."
        )

    verdicts: list[MetricVerdict] = []
    for spec in specs:
        if spec.name not in current:
            raise KeyError(f"suite {suite!r} did not report metric {spec.name!r}")
        value = current[spec.name]

        if spec.minimum is not None and value < spec.minimum:
            verdicts.append(
                MetricVerdict(
                    metric=spec.name,
                    verdict=Verdict.BELOW_FLOOR,
                    current=value,
                    baseline=baseline.metrics.get(spec.name) if baseline else None,
                    detail=f"{value:.4f} is below the required floor of {spec.minimum:.4f}",
                )
            )
            continue

        if spec.maximum is not None and value > spec.maximum:
            verdicts.append(
                MetricVerdict(
                    metric=spec.name,
                    verdict=Verdict.ABOVE_CEILING,
                    current=value,
                    baseline=baseline.metrics.get(spec.name) if baseline else None,
                    detail=f"{value:.4f} exceeds the allowed ceiling of {spec.maximum:.4f}",
                )
            )
            continue

        prior = baseline.metrics.get(spec.name) if baseline else None
        if prior is None:
            verdicts.append(
                MetricVerdict(
                    metric=spec.name,
                    verdict=Verdict.NEW,
                    current=value,
                    baseline=None,
                    detail=f"{value:.4f} (no baseline yet — recording)",
                )
            )
            continue

        drop = prior - value if spec.direction is Direction.HIGHER_IS_BETTER else value - prior
        if drop > spec.tolerance:
            verdicts.append(
                MetricVerdict(
                    metric=spec.name,
                    verdict=Verdict.REGRESSED,
                    current=value,
                    baseline=prior,
                    detail=(
                        f"{value:.4f} vs baseline {prior:.4f} "
                        f"(moved {drop:.4f}, tolerance {spec.tolerance:.4f})"
                    ),
                )
            )
        else:
            verdicts.append(
                MetricVerdict(
                    metric=spec.name,
                    verdict=Verdict.PASS,
                    current=value,
                    baseline=prior,
                    detail=f"{value:.4f} vs baseline {prior:.4f}",
                )
            )

    return GateReport(suite=suite, verdicts=tuple(verdicts))


def write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
